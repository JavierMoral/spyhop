"""Follows each started run, phase by phase.

A phase of the pipeline of a run is a checks phase or a phase of a headless
agent: plan, implement or code_review. An agent works in turns of
`claude -p` in systemd units (see WorkRunner), and each turn ends with a
result. PhaseRunner starts each phase and acts on the result of each turn.
A checks phase runs without an agent through CheckRunner. When it fails,
the run waits for a person, or, with on_fail: fix, a fix turn of the
implementation session fixes the findings and a new round starts.

The rows of the turns and of the checkpoints keep the state, so the first
cycle after a start of Spyhop continues each run where it was.

Through RunAlerts, the supervisor session of the worktree, when there is
one, gets the progress and each reason why the run waits for a person. A
wait that does not reach the session goes again on the next cycle. When the
user closes the session, a new one starts at once.
"""

import logging
from pathlib import Path

from spyhop.application.checkpoint_runner import CheckRunner
from spyhop.application.context_builder import ContextBuilder
from spyhop.application.knowledge import KnowledgeWriter
from spyhop.application.phase_runner import PhaseRunner
from spyhop.application.run_alerts import RunAlerts
from spyhop.application.supervisor_session import SupervisorSession
from spyhop.application.work_runner import WorkRunner
from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.config.settings import AgentSettings
from spyhop.domain.clock import Clock, utc_now
from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.exceptions import (
    OrcaCommandError,
    OrcaUnavailableError,
    OrcaUncertainError,
)
from spyhop.domain.models import AutomationRun, ChecksPhase, ResolvedWorkflow, Worktree
from spyhop.domain.protocols import BoardMirror
from spyhop.infrastructure.persistence.repositories import RunRepository, WorktreeRepository

_log = logging.getLogger(__name__)

SUPERVISED = (RunStatus.RUNNING, RunStatus.WAITING_FOR_HUMAN)


class AgentSupervisor:
    def __init__(
        self,
        runs: RunRepository,
        worktrees: WorktreeRepository,
        context: ContextBuilder,
        agents: AgentSettings,
        resolver: WorkflowResolver,
        work_runner: WorkRunner,
        check_runner: CheckRunner | None = None,
        supervisor_session: SupervisorSession | None = None,
        knowledge: KnowledgeWriter | None = None,
        agent_states_dir: Path | None = None,
        board: BoardMirror | None = None,
        plan_review_status: str | None = None,
        now: Clock = utc_now,
    ) -> None:
        self._runs = runs
        self._worktrees = worktrees
        self._resolver = resolver
        self._supervisor_session = supervisor_session
        self._agent_states_dir = agent_states_dir
        self._alerts = RunAlerts(runs, worktrees, supervisor_session)
        self._phases = PhaseRunner(
            runs,
            context,
            agents,
            self._alerts,
            work_runner,
            check_runner,
            knowledge,
            board,
            plan_review_status,
            now,
        )

    async def supervise(self) -> None:
        for run in self._runs.list_by_status(*SUPERVISED):
            try:
                await self._supervise(run)
            except (OrcaCommandError, OrcaUnavailableError, OrcaUncertainError) as error:
                # One run's trouble never stops the others from being followed.

                _log.warning("%s: will check again next cycle: %s", run.issue_key, error)

    async def _supervise(self, run: AutomationRun) -> None:
        worktree = self._worktrees.get(run.worktree_id) if run.worktree_id else None

        if worktree is None:
            await self._alerts.wait(
                run, WaitingReason.UNKNOWN_STATE, "no worktree recorded for this run"
            )

            return

        if not worktree.path.exists():
            await self._alerts.wait(
                run, WaitingReason.UNKNOWN_STATE, f"worktree {worktree.path} is gone"
            )

            return

        await self._reopen_session(run, worktree)

        workflow = self._workflow(run)

        if workflow is None:
            await self._alerts.wait(
                run,
                WaitingReason.UNKNOWN_STATE,
                f"workflow {run.workflow_name!r} or its phase {run.phase} is not configured",
            )

            return

        if run.plan_changes is not None:
            await self._phases.change_plan(run, worktree)

            return

        phase = workflow.phases[run.phase]

        # A checks phase follows its checkpoints, except while a turn of the
        # agent works on its failed round, or a message for the agent waits.

        if (
            isinstance(phase, ChecksPhase)
            and not self._phases.turn_after_checks(run)
            and not (run.status is RunStatus.WAITING_FOR_HUMAN and self._phases.has_messages(run))
        ):
            await self._phases.checks(run, worktree, workflow)

            return

        await self._phases.work(run, worktree, workflow)

    async def _reopen_session(self, run: AutomationRun, worktree: Worktree) -> None:
        """A run in progress keeps a supervisor session: when the user closes
        it, a new one starts at once."""

        if (
            self._supervisor_session is None
            or self._agent_states_dir is None
            or not self._supervisor_session.ended(worktree, self._agent_states_dir)
        ):
            return

        waiting = f", espera: {run.waiting_reason}" if run.waiting_reason else ""

        await self._supervisor_session.reopen(
            worktree,
            f"la sesión supervisora anterior se cerró; la run {run.id[:8]} sigue "
            f"({run.status}{waiting}); muestra su estado al usuario",
        )

    def _workflow(self, run: AutomationRun) -> ResolvedWorkflow | None:
        workflow = self._resolver.resolve_for_run(run)

        if workflow is None or run.phase >= len(workflow.phases):
            return None

        if run.pipeline is None:
            # A run from before the column: from now on, a change of the
            # configuration does not move its phase.

            self._runs.set_pipeline(run.id, workflow.phases)

        return workflow
