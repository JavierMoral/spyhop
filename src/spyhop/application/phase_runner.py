"""Starts the phases of a run's pipeline: the plan and implementation
agents, the checks phases and the fixes, and the end of the run.

The agents start through AgentStarter, as in the launcher."""

import logging

from spyhop.application.agent_starter import AgentStarter, LaunchOutcomeUnknown
from spyhop.application.checkpoint_runner import CheckRunner
from spyhop.application.checkpoint_verdicts import VERDICTS_FILE
from spyhop.application.context_builder import RESULT_FILE, ContextBuilder
from spyhop.application.knowledge import KnowledgeWriter
from spyhop.application.run_alerts import RunAlerts
from spyhop.application.step_runner import log_file
from spyhop.application.work_runner import WorkRunner
from spyhop.config.settings import AgentSettings
from spyhop.domain.clock import Clock, utc_now
from spyhop.domain.enums import RunStatus, WaitingReason
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
    CodeReviewPhase,
    ResolvedWorkflow,
    Worktree,
)
from spyhop.domain.protocols import TerminalHost
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
        alerts: RunAlerts,
        check_runner: CheckRunner | None = None,
        knowledge: KnowledgeWriter | None = None,
        now: Clock = utc_now,
        work_runner: WorkRunner | None = None,
    ) -> None:
        self._runs = runs
        self._work = work_runner
        self._sessions = sessions
        self._terminal_host = terminal_host
        self._context = context
        self._agents = agents
        self._alerts = alerts
        self._check_runner = check_runner
        self._knowledge = knowledge
        self._starter = AgentStarter(sessions, terminal_host, agents, now)

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
        elif isinstance(phase, CodeReviewPhase):
            if self._headless(run):
                # A turn of the implementation session, which knows why it
                # made each choice.

                session_id = self._work_runner().last_session(run, "implement")

                await self.start_turn(
                    run,
                    worktree,
                    "code_review",
                    "code_review",
                    self._context.turn_prompt(
                        run.issue_key,
                        "code_review",
                        worktree.path,
                        command=phase.command,
                        new_session=session_id is None,
                    ),
                    session_id,
                )
            else:
                # In an Orca run, the implementation agent ran the review itself.

                await self.enter(run, worktree, workflow, index + 1, session)
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

            if self._headless(run):
                await self.start_turn(
                    run,
                    worktree,
                    "implement",
                    "launch",
                    self._context.turn_prompt(
                        run.issue_key, "implement" if approved else "full", worktree.path
                    ),
                    previous=session,
                )

                return

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
                f"run `spyhop declare {run.issue_key} plan` and `spyhop approve {run.issue_key}`"
                if workflow.plan_review
                else "use `spyhop retry` or start the agent yourself",
            )

    async def start_turn(
        self,
        run: AutomationRun,
        worktree: Worktree,
        name: str,
        turn: str,
        prompt: str,
        session_id: str | None = None,
        previous: AgentSession | None = None,
    ) -> None:
        """Starts a headless turn of the agent ``name`` in the current phase.
        ``previous``: the Orca agent before it, for example the plan agent."""

        row = await self._work_runner().turn(run, worktree, name, turn, prompt, session_id)

        if previous is not None:
            self._sessions.mark_finished(previous.id)

        self._runs.mark_resumed(run.id)

        _log.info("%s: turn %s of %s started (%s)", run.issue_key, turn, name, row.session_id)

        await self._alerts.tell(run, f"turno {turn} de {name} arrancado; log en {log_file(row)}")

    async def work(
        self, run: AutomationRun, worktree: Worktree, workflow: ResolvedWorkflow
    ) -> None:
        """Follows the headless turn of the current phase and acts on its
        outcome."""

        state = await self._work_runner().advance(run, worktree)
        phase = workflow.phases[run.phase]
        name = "code_review" if isinstance(phase, CodeReviewPhase) else "implement"

        if state.state == "running":
            await self._alerts.resume(run)
        elif state.state == "blocked":
            await self._alerts.wait(run, WaitingReason.UNKNOWN_STATE, state.detail or "")
        elif state.state == "done":
            await self.enter(run, worktree, workflow, run.phase + 1, None)
        elif state.state == "question":
            await self._alerts.wait(run, WaitingReason.AGENT_QUESTION, state.detail or "")
        elif state.state == "none":
            await self._alerts.wait(
                run, WaitingReason.UNKNOWN_STATE, f"no turn of {name} in this phase"
            )
        elif run.restarts < self._agents.max_restarts and run.status is RunStatus.RUNNING:
            # lost, unfinished or resume_failed: one more turn, in the same
            # session when it still exists.

            assert state.row is not None

            self._runs.count_restart(run.id)

            new_session = state.state == "resume_failed"

            await self.start_turn(
                run,
                worktree,
                name,
                "continue",
                self._context.turn_prompt(
                    run.issue_key,
                    "continue",
                    worktree.path,
                    changes=state.detail or state.state,
                    new_session=new_session,
                ),
                None if new_session else state.row.session_id,
            )
        else:
            await self._alerts.wait(
                run,
                WaitingReason.UNKNOWN_STATE,
                f"the turn of {name} ended {state.state}: {state.detail}",
            )

    def _headless(self, run: AutomationRun) -> bool:
        return run.runner == "headless"

    def _work_runner(self) -> WorkRunner:
        if self._work is None:
            raise SpyhopError("this Spyhop runs no headless turns")

        return self._work

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
        try:
            launched = await self._starter.start(run, worktree, model, goal, step)
        except LaunchOutcomeUnknown:
            await self._alerts.wait(
                run,
                WaitingReason.UNKNOWN_STATE,
                f"{role} launch outcome unknown; check Orca",
            )

            return
        except OrcaCommandError as error:
            await self._alerts.wait(
                run,
                WaitingReason.UNKNOWN_STATE,
                f"Orca refused the {role} launch: {error}; {advice}",
            )

            return

        if previous is not None:
            self._sessions.mark_finished(previous.id)

        self._runs.mark_resumed(run.id)

        # A new agent: the cycles without the agent before it do not count,
        # and its own close is news to the person.

        if run.missing_cycles:
            self._runs.clear_missing(run.id)

        if run.plan_agent_closed_told:
            self._runs.set_plan_agent_closed_told(run.id, False)

        await self._starter.label(run, launched)

        _log.info(
            "%s: %s agent %s started (%s)",
            run.issue_key,
            role,
            launched.agent_handle,
            launched.model,
        )

        await self._alerts.tell(run, f"agente de {role} arrancado ({launched.model})")

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
            await self.request_fix(run, worktree, session, outcome.failed)
        else:
            rounds = f" after {outcome.round} rounds" if phase.on_fail == "fix" else ""

            await self._alerts.wait(
                run,
                WaitingReason.CHECKPOINTS_FAILED,
                f"checkpoints {outcome.message}{rounds}; see {worktree.path / VERDICTS_FILE}",
            )

    async def request_fix(
        self,
        run: AutomationRun,
        worktree: Worktree,
        session: AgentSession | None,
        failed: tuple[str, ...],
    ) -> None:
        if self._headless(run):
            await self._alerts.wait(
                run,
                WaitingReason.CHECKPOINTS_FAILED,
                f"failed: {', '.join(failed)}; a headless run does not fix the findings yet",
            )

            return

        # Recorded first: a prompt whose outcome is unknown is not sent twice.

        self._runs.mark_fix_requested(run.id)

        if session is None or session.agent_handle is None:
            await self._fix_with_new_agent(run, worktree, session, failed)

            return

        try:
            await self._terminal_host.send_prompt(
                session.agent_handle, self._context.fix_prompt(run.issue_key, failed)
            )
        except OrcaUnavailableError:
            # The prompt did not go: the next cycle asks for the fix again.

            self._runs.clear_fix_request(run.id)

            raise
        except OrcaCommandError as error:
            # The terminal of the agent is gone: a new agent fixes the findings.

            _log.info("%s: the agent cannot take the fix prompt: %s", run.issue_key, error)

            await self._fix_with_new_agent(run, worktree, session, failed)

            return
        except OrcaUncertainError as error:
            await self._alerts.wait(
                run,
                WaitingReason.UNKNOWN_STATE,
                f"the fix prompt for {', '.join(failed)} may not have reached the agent: {error}",
            )

            return

        _log.info("%s: asked the agent to fix %s", run.issue_key, ", ".join(failed))

        await self._alerts.tell(run, f"el agente corrige los hallazgos de {', '.join(failed)}")

    async def _fix_with_new_agent(
        self,
        run: AutomationRun,
        worktree: Worktree,
        session: AgentSession | None,
        failed: tuple[str, ...],
    ) -> None:
        await self.launch(
            run,
            worktree,
            session,
            self._agents.model,
            self._context.fix_prompt(run.issue_key, failed, new_agent=True),
            "implement",
            "implementation",
            "use `spyhop retry` or start the agent yourself",
        )
