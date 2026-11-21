"""Tells the supervisor session of a run about its progress and its waits."""

import logging

from spyhop.application.supervisor_session import SupervisorSession
from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.models import AutomationRun, Worktree
from spyhop.infrastructure.persistence.repositories import RunRepository, WorktreeRepository

_log = logging.getLogger(__name__)


class RunAlerts:
    def __init__(
        self,
        runs: RunRepository,
        worktrees: WorktreeRepository,
        supervisor_session: SupervisorSession | None,
    ) -> None:
        self._runs = runs
        self._worktrees = worktrees
        self._supervisor_session = supervisor_session

    async def resume(self, run: AutomationRun) -> None:
        if run.status is RunStatus.RUNNING:
            return

        self._runs.mark_resumed(run.id)

        if run.alert_pending:
            self._runs.set_alert_pending(run.id, False)

        _log.info("%s: agent working again (was waiting: %s)", run.issue_key, run.waiting_reason)

        await self.tell(run, f"la run sigue (esperaba: {run.waiting_reason})")

    async def wait(self, run: AutomationRun, reason: WaitingReason, message: str) -> None:
        if run.status is RunStatus.WAITING_FOR_HUMAN and run.waiting_reason is reason:
            # The same wait goes again only when it did not reach the session.

            if not run.alert_pending:
                return
        else:
            self._runs.mark_waiting(run.id, reason, message)

            _log.info("%s: waiting for a person (%s): %s", run.issue_key, reason, message)

        worktree = self._session_worktree(run)

        if worktree is None:
            return

        assert self._supervisor_session is not None

        delivered = await self._supervisor_session.alert(worktree, f"{reason}: {message}")

        self._runs.set_alert_pending(run.id, not delivered)

    async def tell(self, run: AutomationRun, text: str) -> None:
        worktree = self._session_worktree(run)

        if worktree is not None:
            assert self._supervisor_session is not None

            await self._supervisor_session.tell(worktree, text)

    def _session_worktree(self, run: AutomationRun) -> Worktree | None:
        """The worktree of the run when it has a supervisor session."""

        if self._supervisor_session is None or run.worktree_id is None:
            return None

        return self._worktrees.get(run.worktree_id)
