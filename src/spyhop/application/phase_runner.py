"""Starts the phases of a run's pipeline: the plan and implementation
agents, the checks phases and the fixes, and the end of the run.

The launch of an agent follows the launcher's rules: the operation id is
stored before the call to the terminal host, so a launch whose answer was
lost is replayed and not repeated."""

import logging

from spyhop.application.checkpoint_runner import CheckRunner
from spyhop.application.checkpoint_verdicts import VERDICTS_FILE
from spyhop.application.context_builder import RESULT_FILE, ContextBuilder
from spyhop.application.knowledge import KnowledgeWriter
from spyhop.application.launcher import label_agent, replay_expired
from spyhop.application.run_alerts import RunAlerts
from spyhop.config.settings import AgentSettings
from spyhop.domain.enums import WaitingReason
from spyhop.domain.exceptions import (
    OrcaCommandError,
    OrcaUnavailableError,
    OrcaUncertainError,
    SpyhopError,
)
from spyhop.domain.models import (
    AgentSession,
    AutomationRun,
    ChecksPhase,
    ResolvedWorkflow,
    Worktree,
)
from spyhop.domain.protocols import TerminalHost
from spyhop.infrastructure.orca.client import new_operation_id
from spyhop.infrastructure.persistence.repositories import RunRepository, SessionRepository

_log = logging.getLogger(__name__)


class PhaseRunner:
    def __init__(
        self,
        runs: RunRepository,
        sessions: SessionRepository,
        terminal_host: TerminalHost,
        context: ContextBuilder,
        agents: AgentSettings,
        plan_review_status: str,
        alerts: RunAlerts,
        check_runner: CheckRunner | None = None,
        knowledge: KnowledgeWriter | None = None,
    ) -> None:
        self._runs = runs
        self._sessions = sessions
        self._terminal_host = terminal_host
        self._context = context
        self._agents = agents
        self._plan_review_status = plan_review_status
        self._alerts = alerts
        self._check_runner = check_runner
        self._knowledge = knowledge

    async def change_plan(
        self, run: AutomationRun, worktree: Worktree, session: AgentSession | None
    ) -> None:
        """Starts a new plan agent with the changes that a person asked for
        with `spyhop replan`, while the plan waits for its review."""

        changes = self._runs.take_plan_changes(run.id) or ""

        if run.waiting_reason is not WaitingReason.PLAN_REVIEW:
            _log.warning("%s: plan changes ignored: no plan waits for a review", run.issue_key)

            return

        await self.launch(
            run,
            worktree,
            session,
            self._agents.plan_model,
            self._context.goal(run.issue_key, "replan", changes),
            "plan",
            "plan",
            "use `spyhop replan` again or start the agent yourself",
        )

    async def enter(
        self,
        run: AutomationRun,
        worktree: Worktree,
        workflow: ResolvedWorkflow,
        index: int,
        session: AgentSession | None,
    ) -> None:
        """Starts the phase at ``index``, or completes the run after the last
        phase."""

        if index >= len(workflow.phases):
            self._runs.mark_completed(run.id)

            if session is not None:
                self._sessions.mark_finished(session.id)

            _log.info("%s: completed; CLAUDE-RESULT.md is in %s", run.issue_key, worktree.path)

            if self._knowledge is not None:
                await self._knowledge.record(run)

            await self._alerts.tell(run, f"run completada; el resultado está en {RESULT_FILE}")

            return

        if index != run.phase:
            self._runs.set_phase(run.id, index)

            run = self._runs.get(run.id) or run

        phase = workflow.phases[index]

        if isinstance(phase, ChecksPhase):
            await self.checks(run, worktree, workflow, session)
        elif phase == "plan":
            await self.launch(
                run,
                worktree,
                session,
                self._agents.plan_model,
                self._context.launch_goal(run.issue_key, workflow),
                phase,
                "plan",
                "use `spyhop retry` or start the agent yourself",
            )
        elif phase == "implement":
            # A plan that a person approved is implemented, also in a
            # workflow without plan_review.

            approved = workflow.plan_review or run.waiting_reason is WaitingReason.PLAN_REVIEW
            goal = (
                self._context.goal(run.issue_key, "implement")
                if approved
                else self._context.launch_goal(run.issue_key, workflow)
            )

            await self.launch(
                run,
                worktree,
                session,
                self._agents.model,
                goal,
                phase,
                "implementation",
                f"move the card back to {self._plan_review_status} and approve the plan again"
                if workflow.plan_review
                else "use `spyhop retry` or start the agent yourself",
            )

    async def launch(
        self,
        run: AutomationRun,
        worktree: Worktree,
        previous: AgentSession | None,
        model: str,
        goal: str,
        step: str,
        role: str,
        advice: str,
    ) -> None:
        session = self._sessions.find_launching(run.id)

        if session is None:
            session = self._sessions.create_launching(
                run.id, self._agents.provider, model, new_operation_id(), step
            )
        elif replay_expired(session):
            self._sessions.mark_failed(session.id)
            await self._alerts.wait(
                run,
                WaitingReason.UNKNOWN_STATE,
                f"{role} launch outcome unknown; check Orca",
            )

            return

        assert session.launch_operation_id is not None

        try:
            result = await self._terminal_host.launch_agent(
                worktree.orca_worktree_id,
                goal,
                session.launch_operation_id,
                session.model,
                self._agents.effort,
            )
        except OrcaCommandError as error:
            self._sessions.mark_failed(session.id)
            await self._alerts.wait(
                run,
                WaitingReason.UNKNOWN_STATE,
                f"Orca refused the {role} launch: {error}; {advice}",
            )

            return

        if previous is not None:
            self._sessions.mark_finished(previous.id)

        self._sessions.mark_launched(session.id, result.agent_handle, result.pane_key)
        self._runs.mark_resumed(run.id)

        # A new agent: the cycles without the agent before it do not count,
        # and its own close is news to the person.

        if run.missing_cycles:
            self._runs.clear_missing(run.id)

        if run.plan_agent_closed_told:
            self._runs.set_plan_agent_closed_told(run.id, False)
        await label_agent(self._terminal_host, run.issue_key, result.agent_handle, session)

        _log.info(
            "%s: %s agent %s started (%s)", run.issue_key, role, result.agent_handle, session.model
        )

        await self._alerts.tell(run, f"agente de {role} arrancado ({session.model})")

    async def checks(
        self,
        run: AutomationRun,
        worktree: Worktree,
        workflow: ResolvedWorkflow,
        session: AgentSession | None,
        agent_busy: bool = False,
    ) -> None:
        if self._check_runner is None:
            await self._alerts.wait(run, WaitingReason.UNKNOWN_STATE, "checkpoints cannot run here")

            return

        try:
            outcome = await self._check_runner.advance(run, worktree, workflow, agent_busy)
        except (OrcaUnavailableError, OrcaUncertainError):
            raise
        except SpyhopError as error:
            await self._alerts.wait(
                run, WaitingReason.UNKNOWN_STATE, f"checkpoints cannot run: {error}"
            )

            return

        phase = workflow.phases[run.phase]

        assert isinstance(phase, ChecksPhase)

        if outcome.state == "running":
            await self._alerts.resume(run)
        elif outcome.state == "blocked":
            await self._alerts.wait(run, WaitingReason.UNKNOWN_STATE, outcome.message)
        elif outcome.state == "passed":
            await self.enter(run, worktree, workflow, run.phase + 1, session)
        elif phase.on_fail == "fix" and outcome.round < phase.rounds:
            await self.request_fix(run, session, outcome.failed)
        else:
            rounds = f" after {outcome.round} rounds" if phase.on_fail == "fix" else ""

            await self._alerts.wait(
                run,
                WaitingReason.CHECKPOINTS_FAILED,
                f"checkpoints {outcome.message}{rounds}; see {worktree.path / VERDICTS_FILE}",
            )

    async def request_fix(
        self, run: AutomationRun, session: AgentSession | None, failed: tuple[str, ...]
    ) -> None:
        if session is None or session.agent_handle is None:
            await self._alerts.wait(
                run, WaitingReason.CHECKPOINTS_FAILED, "no agent to fix the findings"
            )

            return

        # Recorded first: a prompt whose outcome is unknown is not sent twice.

        self._runs.mark_fix_requested(run.id)

        try:
            await self._terminal_host.send_prompt(
                session.agent_handle, self._context.fix_prompt(run.issue_key, failed)
            )
        except OrcaUnavailableError:
            # The prompt did not go: the next cycle asks for the fix again.

            self._runs.clear_fix_request(run.id)

            raise
        except (OrcaCommandError, OrcaUncertainError) as error:
            await self._alerts.wait(
                run,
                WaitingReason.UNKNOWN_STATE,
                f"the fix prompt for {', '.join(failed)} may not have reached the agent: {error}",
            )

            return

        _log.info("%s: asked the agent to fix %s", run.issue_key, ", ".join(failed))

        await self._alerts.tell(run, f"el agente corrige los hallazgos de {', '.join(failed)}")
