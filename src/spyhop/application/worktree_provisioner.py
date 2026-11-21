"""Gets the worktree of a run: the one that the ticket has, the one that
Orca links to the ticket, or a new one. A new one comes from the worktree
script of the repository or from Orca, on the kept branch of a reopened
ticket when Git still has it."""

import asyncio
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from pathlib import Path

from spyhop.domain.enums import WaitingReason, WorktreeStatus
from spyhop.domain.exceptions import OrcaCommandError
from spyhop.domain.models import OrcaWorktree, ResolvedWorkflow, TicketDetails, Worktree
from spyhop.domain.protocols import WorktreeHost
from spyhop.infrastructure.disk import GIB, free_bytes
from spyhop.infrastructure.git import branch_exists
from spyhop.infrastructure.persistence.repositories import WorktreeRepository
from spyhop.infrastructure.worktree_script import (
    WorktreeRequest,
    WorktreeScriptError,
    WorktreeScriptTryAgainError,
    run_worktree_script,
)

_log = logging.getLogger(__name__)

# Characters of the ticket title kept in a worktree's name, cut at a word.

MAX_SLUG_LENGTH = 50


class RunFailed(Exception):
    """The run cannot start. The launcher marks it FAILED."""


class NotYet(Exception):
    """The run cannot advance now but may next cycle."""


class Waiting(Exception):
    """Like NotYet, but the reason is recorded on the run for `status`."""

    def __init__(self, reason: WaitingReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class WorktreeProvisioner:
    def __init__(
        self,
        worktrees: WorktreeRepository,
        worktree_host: WorktreeHost,
        min_free_disk_gb: float = 0,
        branch_exists: Callable[[Path, str], bool] = branch_exists,
        free_bytes: Callable[[Path], int] = free_bytes,
        worktree_script: Callable[
            [Path, WorktreeRequest, float], Awaitable[Path]
        ] = run_worktree_script,
    ) -> None:
        self._worktrees = worktrees
        self._worktree_host = worktree_host
        self._min_free_bytes = int(min_free_disk_gb * GIB)
        self._branch_exists = branch_exists
        self._free_bytes = free_bytes
        self._worktree_script = worktree_script

    async def disk_shortage(self, issue_key: str, workflow: ResolvedWorkflow) -> str | None:
        """Why a new worktree of the ticket does not fit on the disk, or None.
        The disk that would hold the worktree must have min_free_disk_gb
        free. A ticket that has a worktree needs no space."""

        if self._min_free_bytes <= 0 or self._worktrees.find_current(issue_key):
            return None

        try:
            location = await self._worktree_host.worktree_base_path(workflow.repository_path)
        except OrcaCommandError:
            location = None

        location = location or workflow.repository_path
        free = self._free_bytes(location)

        if free >= self._min_free_bytes:
            return None

        return (
            f"waiting for disk space: {free / GIB:.1f} GB free in {location}, "
            f"{self._min_free_bytes / GIB:g} GB required to create a worktree"
        )

    async def worktree(self, ticket: TicketDetails, workflow: ResolvedWorkflow) -> Worktree:
        """The ticket's worktree: SQLite first, then Orca's reference to the
        ticket, and only then a new one."""

        issue_key = ticket.key
        current = self._worktrees.find_current(issue_key)

        if current is not None:
            if current.status is WorktreeStatus.REMOVING:
                raise NotYet(f"its worktree {current.path} is being removed")

            if current.status is WorktreeStatus.CLEANUP_BLOCKED:
                # Reopened before its changes were dealt with: the worktree
                # still holds them, so the work goes on there.

                _log.info(
                    "%s: reopened; reusing %s, which could not be removed", issue_key, current.path
                )

                self._worktrees.mark_active(current.id)

            try:
                await self._worktree_host.show_worktree(current.orca_worktree_id)

                return current
            except OrcaCommandError as error:
                if error.code != "selector_not_found":
                    raise

                _log.warning(
                    "%s: worktree %s is gone from Orca; looking for another one",
                    issue_key,
                    current.path,
                )

                self._worktrees.mark_removed(current.id, "not found in Orca")

        orca_worktree = await self._worktree_host.find_worktree_by_reference(issue_key)
        base_branch = workflow.base_branch

        if orca_worktree is None:
            name = worktree_name(issue_key, ticket.summary)
            branch = name
            previous = await self._reopened(issue_key, workflow)

            if previous is not None:
                branch = previous.branch_name

            # The script checks the kept branch out by its name. It gets the
            # base branch of the workflow, which it can use for more than the
            # start of the branch.

            if workflow.worktree_script is not None:
                orca_worktree = await self._scripted_worktree(
                    WorktreeRequest(issue_key, name, branch, base_branch, workflow.repository_path),
                    workflow.worktree_script,
                    workflow.worktree_script_timeout,
                )
            else:
                if previous is not None:
                    # The same name makes Orca check the kept branch out
                    # again instead of creating a new one, even if the
                    # title changed.

                    name = previous.path.name
                    base_branch = previous.branch_name

                orca_worktree = await self._worktree_host.create_worktree(
                    workflow.repository_path,
                    name,
                    base_branch,
                    issue_key,
                    workflow.setup,
                )

            _log.info(
                "%s: created worktree %s on branch %s from %s",
                issue_key,
                orca_worktree.path,
                orca_worktree.branch,
                base_branch,
            )
        else:
            _log.info("%s: reusing worktree %s linked in Orca", issue_key, orca_worktree.path)

        return self._worktrees.create(
            issue_key, orca_worktree, workflow.repository_path, base_branch
        )

    async def _scripted_worktree(
        self, request: WorktreeRequest, script: Path, time_limit: float
    ) -> OrcaWorktree:
        """Creates the worktree with the repository's script and links it to
        the ticket in Orca. Until Orca has seen the new worktree in Git, the
        run waits; next cycle the script runs again and answers with the
        worktree it already created. A script that asks to try again runs
        again next cycle too."""

        try:
            path = await self._worktree_script(script, request, time_limit)
        except WorktreeScriptTryAgainError as error:
            raise Waiting(WaitingReason.WORKTREE_SCRIPT, str(error)) from error
        except WorktreeScriptError as error:
            raise RunFailed(str(error)) from error

        try:
            return await self._worktree_host.link_worktree(path, request.issue_key)
        except OrcaCommandError as error:
            if error.code != "selector_not_found":
                raise

            raise NotYet(f"Orca does not list {path} yet") from error

    async def _reopened(self, issue_key: str, workflow: ResolvedWorkflow) -> Worktree | None:
        """The removed worktree a reopened ticket continues from, when Git
        still has its branch (Orca keeps unmerged branches)."""

        previous = self._worktrees.find_last_removed(issue_key)

        if (
            previous is None
            or previous.repository_path != workflow.repository_path
            or not await asyncio.to_thread(
                self._branch_exists, previous.repository_path, previous.branch_name
            )
        ):
            return None

        _log.info(
            "%s: reopened; continuing from its previous branch %s", issue_key, previous.branch_name
        )

        return previous


def worktree_name(issue_key: str, summary: str) -> str:
    """The ticket key followed by its title as a slug: LPD-1234-fix-the-import."""

    ascii_summary = unicodedata.normalize("NFKD", summary).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", ascii_summary.lower())
    slug = ""

    for word in words:
        longer = f"{slug}-{word}" if slug else word

        if len(longer) > MAX_SLUG_LENGTH:
            break

        slug = longer

    if not slug and words:
        slug = words[0][:MAX_SLUG_LENGTH]

    return f"{issue_key}-{slug}" if slug else issue_key
