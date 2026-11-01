"""Removes the worktrees of closed tickets.

Detection uses the ticket's current status, not a transition: removing is
idempotent, and closures that happened while Spyhop was stopped are covered.
The query does not filter by assignee, because closing a ticket often changes
it.

ACTIVE → REMOVING → REMOVED, or CLEANUP_BLOCKED when Orca refuses to remove
the worktree (uncommitted changes, since removal is never forced). A blocked
worktree is retried once it is clean, or on `spyhop cleanup`. One left in
REMOVING (Orca unavailable, Spyhop stopped mid-way) is resumed next cycle.
"""

import logging
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path

from spyhop.application.launcher import PENDING
from spyhop.config.settings import CleanupSettings
from spyhop.domain.enums import WorktreeStatus
from spyhop.domain.exceptions import (
    JiraAuthenticationError,
    JiraError,
    OrcaCommandError,
    OrcaError,
    OrcaUnavailableError,
)
from spyhop.domain.models import Worktree
from spyhop.domain.protocols import JiraGateway, OrcaGateway
from spyhop.infrastructure.git import has_changes
from spyhop.infrastructure.persistence.repositories import RunRepository, WorktreeRepository

_log = logging.getLogger(__name__)

TICKET_CLOSED = "ticket_closed"

# JQL `key in (…)` stays well under JIRA's query length limits.

_KEYS_PER_QUERY = 100


class Cleanup(StrEnum):
    REMOVED = "removed"
    BLOCKED = "blocked"


class WorktreeJanitor:
    def __init__(
        self,
        remover: "WorktreeRemover",
        runs: RunRepository,
        worktrees: WorktreeRepository,
        jira: JiraGateway,
        orca: OrcaGateway,
        settings: CleanupSettings,
        has_changes: Callable[[Path], bool] = has_changes,
    ) -> None:
        self._remover = remover
        self._runs = runs
        self._worktrees = worktrees
        self._jira = jira
        self._orca = orca
        self._settings = settings
        self._has_changes = has_changes

    async def sweep(self) -> list[Worktree]:
        """Cleans up what is due. Returns the worktrees it removed."""

        if not self._settings.enabled:
            return []

        due = self._worktrees.list_by_status(WorktreeStatus.REMOVING)
        candidates = self._worktrees.list_by_status(
            WorktreeStatus.ACTIVE, WorktreeStatus.CLEANUP_BLOCKED
        )
        # Pending runs without a worktree, such as one waiting for disk space,
        # are cancelled when their ticket closes; nothing else would end them.

        unstarted = [
            run
            for run in self._runs.list_by_status(*PENDING)
            if self._worktrees.find_current(run.issue_key) is None
        ]
        keys = {worktree.issue_key for worktree in candidates}
        keys.update(run.issue_key for run in unstarted)

        if keys:
            closed = await self._closed_keys(sorted(keys))

            due += [
                worktree
                for worktree in candidates
                if worktree.issue_key in closed and self._may_remove(worktree)
            ]

            for run in unstarted:
                if run.issue_key in closed:
                    self._runs.mark_cancelled(run.id, TICKET_CLOSED)

                    _log.info(
                        "%s: run %s cancelled before it started (%s)",
                        run.issue_key,
                        run.id,
                        TICKET_CLOSED,
                    )

        if not due:
            return []

        try:
            await self._orca.ensure_ready()
        except OrcaUnavailableError as error:
            _log.info("%d worktree(s) waiting for Orca to be removed: %s", len(due), error)

            return []

        removed = []

        for worktree in due:
            try:
                if await self._remover.clean_up(worktree, TICKET_CLOSED) is Cleanup.REMOVED:
                    removed.append(worktree)
            except OrcaError as error:
                _log.warning("%s: removal will resume next cycle: %s", worktree.issue_key, error)

        return removed

    def _may_remove(self, worktree: Worktree) -> bool:
        """A blocked worktree is retried only once nothing would block it."""

        if worktree.status is not WorktreeStatus.CLEANUP_BLOCKED:
            return True

        return not worktree.path.exists() or not self._has_changes(worktree.path)

    async def _closed_keys(self, keys: Sequence[str]) -> set[str]:
        closed: set[str] = set()

        for start in range(0, len(keys), _KEYS_PER_QUERY):
            batch = keys[start : start + _KEYS_PER_QUERY]

            try:
                issues = await self._jira.search(
                    closed_tickets_jql(batch, self._settings), ["status"], changelog=False
                )
            except JiraAuthenticationError:
                raise
            except JiraError as error:
                _log.warning("Could not check which tickets are closed: %s", error)

                return closed

            closed.update(issue["key"] for issue in issues)

        return closed


class WorktreeRemover:
    """Removes one worktree: used by the janitor and by `spyhop cleanup`."""

    def __init__(
        self, runs: RunRepository, worktrees: WorktreeRepository, orca: OrcaGateway
    ) -> None:
        self._runs = runs
        self._worktrees = worktrees
        self._orca = orca

    async def clean_up(self, worktree: Worktree, reason: str) -> Cleanup:
        """Cancels the ticket's active run, stops its agents and removes the
        worktree. Raises OrcaUnavailableError or OrcaUncertainError when the
        outcome is unknown; the worktree then stays REMOVING."""

        self._worktrees.mark_removing(worktree.id)

        for run in self._runs.list(issue_key=worktree.issue_key):
            self._runs.mark_cancelled(run.id, reason)

            _log.info("%s: run %s cancelled (%s)", run.issue_key, run.id, reason)

        try:
            await self._orca.close_terminals(worktree.orca_worktree_id)

            result = await self._orca.remove_worktree(worktree.orca_worktree_id)
        except OrcaCommandError as error:
            if error.code == "selector_not_found":
                self._worktrees.mark_removed(worktree.id, "already gone from Orca")

                _log.info("%s: worktree %s was already removed", worktree.issue_key, worktree.path)

                return Cleanup.REMOVED

            await self._block(worktree, error.message)

            return Cleanup.BLOCKED

        self._worktrees.mark_removed(worktree.id)

        _log.info(
            "%s: worktree %s removed%s",
            worktree.issue_key,
            worktree.path,
            f"; branch {result.preserved_branch} kept because it is not merged"
            if result.preserved_branch
            else "",
        )

        return Cleanup.REMOVED

    async def _block(self, worktree: Worktree, message: str) -> None:
        self._worktrees.mark_cleanup_blocked(worktree.id, message)

        _log.warning(
            "%s: worktree %s not removed: %s. Commit or discard the changes, "
            "or run `spyhop cleanup %s`",
            worktree.issue_key,
            worktree.path,
            message,
            worktree.issue_key,
        )

        try:
            await self._orca.mark_card(
                worktree.orca_worktree_id,
                f"{worktree.issue_key} cerrado: Spyhop no pudo borrar el worktree "
                "porque tiene cambios sin commitear",
            )
        except OrcaError as error:
            _log.warning("%s: could not mark the Orca card: %s", worktree.issue_key, error)


def closed_tickets_jql(keys: Sequence[str], settings: CleanupSettings) -> str:
    conditions = []

    if settings.close_status_categories:
        conditions.append(f"statusCategory in ({_jql_list(settings.close_status_categories)})")

    if settings.close_statuses:
        conditions.append(f"status in ({_jql_list(settings.close_statuses)})")

    closed = " OR ".join(conditions) if conditions else "statusCategory = done"

    return f"key in ({', '.join(keys)}) AND ({closed})"


def _jql_list(values: Sequence[str]) -> str:
    return ", ".join(
        '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"' for value in values
    )
