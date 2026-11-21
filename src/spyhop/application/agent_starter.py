"""Starts the agent of a run in its worktree, for the launcher and for the
phases of the supervisor.

The operation id is stored before the call to the terminal host and is used
again on each attempt, so a launch whose answer was lost is replayed and not
repeated. Each caller handles the errors and the status of the run."""

import logging
import time

from spyhop.config.settings import AgentSettings
from spyhop.domain.exceptions import OrcaCommandError, OrcaError, SpyhopError
from spyhop.domain.models import AgentSession, AutomationRun, Worktree
from spyhop.domain.protocols import TerminalHost
from spyhop.infrastructure.orca.client import new_operation_id
from spyhop.infrastructure.persistence.repositories import SessionRepository

_log = logging.getLogger(__name__)

# A launch whose answer was lost is replayed with its operation id. Past this
# age Orca may have forgotten the id and would start a second agent, so the
# run is handed to a person instead.

REPLAY_WINDOW_SECONDS = 15 * 60

# The prompt bar color of the Claude session of each role. The supervisor
# session has its own color.

ROLE_COLORS = {"plan": "blue", "implement": "orange"}


class LaunchOutcomeUnknown(SpyhopError):
    """The answer to an earlier launch was lost too long ago to replay it."""


class AgentStarter:
    def __init__(
        self, sessions: SessionRepository, terminal_host: TerminalHost, agents: AgentSettings
    ) -> None:
        self._sessions = sessions
        self._terminal_host = terminal_host
        self._agents = agents

    async def start(
        self, run: AutomationRun, worktree: Worktree, model: str, goal: str, role: str
    ) -> AgentSession:
        """Returns the launched session. Raises LaunchOutcomeUnknown, or
        OrcaCommandError when the terminal host refuses the launch; the
        session is then failed. The other errors of the terminal host leave
        the session to be replayed on the next attempt.

        The caller stores the status of the run and only then calls `label`:
        a stop of Spyhop during the labels must not start a second agent."""

        session = self._sessions.find_launching(run.id)

        if session is None:
            session = self._sessions.create_launching(
                run.id, self._agents.provider, model, new_operation_id(), role
            )
        elif replay_expired(session):
            _log.warning(
                "%s: the answer to launch %s was lost too long ago to replay it safely; "
                "check the worktree in Orca",
                run.issue_key,
                session.launch_operation_id,
            )

            self._sessions.mark_failed(session.id)

            raise LaunchOutcomeUnknown(f"launch {session.launch_operation_id} outcome unknown")

        assert session.launch_operation_id is not None

        try:
            result = await self._terminal_host.launch_agent(
                worktree.orca_worktree_id,
                goal,
                session.launch_operation_id,
                session.model,
                self._agents.effort,
            )
        except OrcaCommandError:
            self._sessions.mark_failed(session.id)

            raise

        self._sessions.mark_launched(session.id, result.agent_handle, result.pane_key)

        launched = self._sessions.get(session.id)

        assert launched is not None

        return launched

    async def label(self, run: AutomationRun, session: AgentSession) -> None:
        """Gives the tab and the session of the agent its role and model, for
        example "plan opus", and gives the session the color of its role. An
        error only goes to the log."""

        assert session.agent_handle is not None

        terminal = session.agent_handle
        title = " ".join(value for value in (session.role, session.model) if value)

        try:
            await self._terminal_host.rename_terminal(terminal, title)
            await self._terminal_host.send_prompt(terminal, f"/rename {title}")

            if session.role in ROLE_COLORS:
                await self._terminal_host.send_prompt(
                    terminal, f"/color {ROLE_COLORS[session.role]}"
                )
        except OrcaError as error:
            _log.warning("%s: agent %s not labeled: %s", run.issue_key, terminal, error)


def replay_expired(session: AgentSession, now: float | None = None) -> bool:
    operation_id = session.launch_operation_id or ""

    try:
        issued_at = int(operation_id.split("-", 1)[0]) / 1000
    except ValueError:
        return True

    return (now or time.time()) - issued_at > REPLAY_WINDOW_SECONDS
