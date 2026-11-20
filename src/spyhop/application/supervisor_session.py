"""The supervisor session: one interactive Claude session for each worktree,
in an Orca terminal, that tells the user about the runs of the worktree.

Spyhop sends it the progress of the run and the reasons why the run waits for
a person. The session explains a wait from the reports, and runs a Spyhop
command only when the user asks for it. It changes no file.

The worktree row keeps the terminal and the pane of the session. The
supervisor uses the pane to keep the session out of the agents of the run.
When the terminal is gone, for example because the user closed it, the next
message starts a new session. A message never stops the supervision: an
error of Orca only goes to the log.
"""

import logging
import shlex
from pathlib import Path

from spyhop.application.context_builder import ContextBuilder
from spyhop.domain.exceptions import OrcaCommandError, OrcaError
from spyhop.domain.models import Worktree
from spyhop.domain.protocols import OrcaGateway
from spyhop.infrastructure.persistence.repositories import WorktreeRepository

_log = logging.getLogger(__name__)

TITLE = "spyhop"

PREFIX = "[spyhop]"

# The terminal of a worktree whose session Spyhop cannot use: Orca gave no
# handle or no pane. Spyhop starts no other session there.

UNUSABLE = "unusable"

# The prompt bar color of the session, apart from the colors of the agents.

COLOR = "violet"

# The session only reads. It runs without permission prompts: a prompt
# blocks the messages of Spyhop. The prompt of the session tells it to run a
# Spyhop command only when the user asks.

DISALLOWED_TOOLS = ("Edit", "Write", "NotebookEdit")


class SupervisorSession:
    def __init__(
        self,
        worktrees: WorktreeRepository,
        orca: OrcaGateway,
        context: ContextBuilder,
        spyhop: Path,
        model: str,
    ) -> None:
        self._worktrees = worktrees
        self._orca = orca
        self._context = context
        self._spyhop = spyhop
        self._model = model

    async def tell(self, worktree: Worktree, text: str) -> bool:
        """Sends a progress message to the session of the worktree. Returns
        False when the message did not reach a session that can get it."""

        message = f"{PREFIX} {worktree.issue_key}: {text}"

        # Read again: an earlier message of the same cycle can have started
        # the session.

        current = self._worktrees.get(worktree.id) or worktree

        if current.supervisor_terminal == UNUSABLE:
            return True

        if current.supervisor_terminal is not None:
            try:
                await self._orca.send_prompt(current.supervisor_terminal, message)

                return True
            except OrcaCommandError as error:
                _log.info(
                    "%s: supervisor session gone (%s); starting a new one",
                    worktree.issue_key,
                    error,
                )
            except OrcaError as error:
                _log.warning(
                    "%s: message not sent to the supervisor session: %s", worktree.issue_key, error
                )

                return False

        return await self._start(current, message)

    async def alert(self, worktree: Worktree, text: str) -> bool:
        """Tells the session that the run waits for a person, and marks the
        card of the worktree unread. Returns False when the message did not
        reach the session."""

        delivered = await self.tell(worktree, f"ESPERA: {text}")

        try:
            await self._orca.mark_card(worktree.orca_worktree_id, f"{worktree.issue_key}: {text}")
        except OrcaError as error:
            _log.warning("%s: could not mark the Orca card: %s", worktree.issue_key, error)

        return delivered

    async def _start(self, worktree: Worktree, message: str) -> bool:
        prompt = self._context.supervisor_prompt(
            worktree.issue_key, worktree.path, self._spyhop, message
        )

        # The prompt comes before --disallowed-tools, which takes all the
        # words after it. env keeps the title of the tab for any shell.

        command = shlex.join(
            [
                "env",
                "CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1",
                "claude",
                prompt,
                "--model",
                self._model,
                "--dangerously-skip-permissions",
                "--disallowed-tools",
                *DISALLOWED_TOOLS,
            ]
        )

        try:
            terminal = await self._orca.create_terminal(worktree.orca_worktree_id, TITLE, command)
        except OrcaError as error:
            _log.warning("%s: supervisor session not started: %s", worktree.issue_key, error)

            return False

        if terminal.handle is None or terminal.pane_key is None:
            await self._give_up(worktree, terminal.handle)

            return True

        self._worktrees.set_supervisor(worktree.id, terminal.handle, terminal.pane_key)

        _log.info("%s: supervisor session %s started", worktree.issue_key, terminal.handle)

        try:
            await self._orca.send_prompt(terminal.handle, f"/color {COLOR}")
        except OrcaError as error:
            _log.warning("%s: supervisor session without color: %s", worktree.issue_key, error)

        return True

    async def _give_up(self, worktree: Worktree, handle: str | None) -> None:
        """Without a pane, the supervisor cannot keep the session out of the
        agents of the run, so Spyhop closes it. Without a handle, Spyhop
        cannot send it messages, and a new session for each message would
        open a tab each time."""

        if handle is not None:
            try:
                await self._orca.close_terminal(handle)
            except OrcaError as error:
                _log.warning(
                    "%s: supervisor session %s without a pane not closed: %s",
                    worktree.issue_key,
                    handle,
                    error,
                )

        self._worktrees.set_supervisor(worktree.id, UNUSABLE, None)

        _log.warning(
            "%s: Orca gave no handle or no pane for the supervisor session; "
            "this worktree gets no supervisor session",
            worktree.issue_key,
        )
