"""Starts the phases of a run's pipeline and acts on their results: the
turns of the headless agents (plan, implement, code_review), the checks
phases and their fixes, and the end of the run.

An agent works in turns of `claude -p` (see WorkRunner). A turn ends with a
result: done, a question for a person, or nothing valid. The messages of a
person go to the agent as its next turn."""

import logging
from datetime import datetime, timedelta

from spyhop.application.checkpoint_runner import CheckRunner
from spyhop.application.checkpoint_verdicts import VERDICTS_FILE
from spyhop.application.context_builder import PLAN_FILE, RESULT_FILE, ContextBuilder
from spyhop.application.knowledge import KnowledgeWriter
from spyhop.application.run_alerts import RunAlerts
from spyhop.application.step_runner import log_file
from spyhop.application.turn_progress import read_progress
from spyhop.application.work_runner import TurnState, WorkRunner
from spyhop.config.settings import AgentSettings
from spyhop.domain.clock import Clock, utc_now
from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.exceptions import OrcaUnavailableError, OrcaUncertainError, SpyhopError
from spyhop.domain.models import (
    AutomationRun,
    ChecksPhase,
    CodeReviewPhase,
    ResolvedWorkflow,
    StepRun,
    Worktree,
)
from spyhop.domain.protocols import BoardMirror
from spyhop.infrastructure.persistence.repositories import RunRepository

_log = logging.getLogger(__name__)


class PhaseRunner:
    def __init__(
        self,
        runs: RunRepository,
        context: ContextBuilder,
        agents: AgentSettings,
        alerts: RunAlerts,
        work_runner: WorkRunner,
        check_runner: CheckRunner | None = None,
        knowledge: KnowledgeWriter | None = None,
        board: BoardMirror | None = None,
        plan_review_status: str | None = None,
        now: Clock = utc_now,
    ) -> None:
        self._runs = runs
        self._now = now
        # The time and the log offset of the last progress message of each
        # turn. A restart of Spyhop starts again from the start of the turn.
        self._progress: dict[str, tuple[datetime, int]] = {}
        self._board = board
        self._plan_review_status = plan_review_status
        self._context = context
        self._agents = agents
        self._alerts = alerts
        self._work = work_runner
        self._check_runner = check_runner
        self._knowledge = knowledge

    async def change_plan(self, run: AutomationRun, worktree: Worktree) -> None:
        """Gives the plan session the changes that a person asked for with
        `spyhop replan`, while the plan waits for its review."""

        changes = self._runs.take_plan_changes(run.id) or ""

        if run.waiting_reason is not WaitingReason.PLAN_REVIEW:
            _log.warning("%s: plan changes ignored: no plan waits for a review", run.issue_key)

            return

        session_id = self._work.last_session(run.id, "plan")

        await self.start_turn(
            run,
            worktree,
            "plan",
            "replan",
            self._context.turn_prompt(
                run.issue_key,
                "replan",
                worktree.path,
                changes=changes,
                new_session=session_id is None,
            ),
            session_id,
        )

    async def enter(
        self, run: AutomationRun, worktree: Worktree, workflow: ResolvedWorkflow, index: int
    ) -> None:
        """Starts the phase at ``index``, or completes the run after the last
        phase."""

        if index >= len(workflow.phases):
            self._runs.mark_completed(run.id)

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
            await self.checks(run, worktree, workflow)
        elif isinstance(phase, CodeReviewPhase):
            # A turn of the implementation session, which knows why it made
            # each choice.

            session_id = self._work.last_session(run.id, "implement")

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
        elif phase == "plan":
            await self.start_turn(
                run,
                worktree,
                "plan",
                "launch",
                self._context.turn_prompt(run.issue_key, "plan", worktree.path),
            )
        elif phase == "implement":
            # A new session implements the plan that a person approved. In a
            # workflow without plan_review, it also writes the plan.

            approved = workflow.plan_review or run.waiting_reason is WaitingReason.PLAN_REVIEW

            await self.start_turn(
                run,
                worktree,
                "implement",
                "launch",
                self._context.turn_prompt(
                    run.issue_key, "implement" if approved else "full", worktree.path
                ),
            )

    async def start_turn(
        self,
        run: AutomationRun,
        worktree: Worktree,
        name: str,
        turn: str,
        prompt: str,
        session_id: str | None = None,
    ) -> None:
        """Starts a turn of the agent ``name`` in the current phase."""

        row = await self._work.turn(run, worktree, name, turn, prompt, session_id)

        self._runs.mark_resumed(run.id)

        _log.info("%s: turn %s of %s started (%s)", run.issue_key, turn, name, row.session_id)

        await self._alerts.tell(run, f"turno {turn} de {name} arrancado; log en {log_file(row)}")

    async def work(
        self, run: AutomationRun, worktree: Worktree, workflow: ResolvedWorkflow
    ) -> None:
        """Follows the turn of the current phase and acts on its outcome."""

        state = await self._work.advance(run, worktree)
        phase = workflow.phases[run.phase]
        name = (
            "code_review"
            if isinstance(phase, CodeReviewPhase)
            else "plan"
            if phase == "plan"
            else "implement"
        )
        messages = self._runs.pending_messages(run.id)

        if messages and state.state not in ("running", "blocked"):
            # The messages of a person go before what the end of the turn
            # starts.

            await self._deliver(run, worktree, name, state, messages)

            return

        if state.state == "done" and isinstance(phase, ChecksPhase):
            # A fix or a message for the failed round: the failed and stale
            # checkpoints run again.

            assert state.row is not None and state.row.finished_at is not None

            if run.checks_requested_at is None or run.checks_requested_at < state.row.finished_at:
                self._runs.request_checks(run.id, failed_only=True)

            await self.checks(self._runs.get(run.id) or run, worktree, workflow)

            return

        if state.state == "done" and phase == "plan":
            await self._plan_done(run, worktree, workflow)

            return

        if state.state == "running":
            await self._alerts.resume(run)

            if state.row is not None:
                await self._tell_progress(run, state.row)
        elif state.state == "blocked":
            await self._alerts.wait(run, WaitingReason.UNKNOWN_STATE, state.detail or "")
        elif state.state == "done":
            await self.enter(run, worktree, workflow, run.phase + 1)
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
                    planning=name == "plan",
                ),
                None if new_session else state.row.session_id,
            )
        else:
            await self._alerts.wait(
                run,
                WaitingReason.UNKNOWN_STATE,
                f"the turn of {name} ended {state.state}: {state.detail}",
            )

    async def checks(
        self, run: AutomationRun, worktree: Worktree, workflow: ResolvedWorkflow
    ) -> None:
        if self._check_runner is None:
            await self._alerts.wait(run, WaitingReason.UNKNOWN_STATE, "checkpoints cannot run here")

            return

        try:
            outcome = await self._check_runner.advance(run, worktree, workflow)
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
            await self.enter(run, worktree, workflow, run.phase + 1)
        elif phase.on_fail == "fix" and outcome.round < phase.rounds:
            await self.request_fix(run, worktree, outcome.failed)
        elif self._runs.take_acceptance(run.id):
            _log.info("%s: a person accepted the failed checkpoints", run.issue_key)

            await self._alerts.tell(
                run, "la persona acepta los checkpoints fallidos; empieza la fase siguiente"
            )
            await self.enter(run, worktree, workflow, run.phase + 1)
        else:
            rounds = f" after {outcome.round} rounds" if phase.on_fail == "fix" else ""

            await self._alerts.wait(
                run,
                WaitingReason.CHECKPOINTS_FAILED,
                f"checkpoints {outcome.message}{rounds}; see {worktree.path / VERDICTS_FILE}",
            )

    async def request_fix(
        self, run: AutomationRun, worktree: Worktree, failed: tuple[str, ...]
    ) -> None:
        """A turn of the implementation session, in the checks phase, fixes
        the findings."""

        session_id = self._work.last_session(run.id, "implement")

        await self.start_turn(
            run,
            worktree,
            "implement",
            "fix",
            self._context.turn_prompt(
                run.issue_key,
                "fix",
                worktree.path,
                new_session=session_id is None,
                failed=failed,
            ),
            session_id,
        )

    async def _move_card(self, run: AutomationRun, worktree: Worktree) -> None:
        """Shows on the Orca board that the plan waits for its review. The card
        is only a reflection: an error is a warning."""

        if self._board is None or self._plan_review_status is None:
            return

        try:
            await self._board.set_card_status(worktree.orca_worktree_id, self._plan_review_status)
        except SpyhopError as error:
            _log.warning("%s: the card did not move: %s", run.issue_key, error)

    async def _tell_progress(self, run: AutomationRun, row: StepRun) -> None:
        """Tells the supervisor session what a long turn did since the last
        message, every agents.progress_minutes."""

        interval = timedelta(minutes=self._agents.progress_minutes)

        if not interval or row.unit is None:
            return

        last, offset = self._progress.get(row.id, (row.started_at, 0))
        now = self._now()

        if now - last < interval:
            return

        progress = read_progress(log_file(row), offset)

        self._progress[row.id] = (now, progress.offset)

        minutes = int((now - row.started_at).total_seconds() // 60)
        text = f"turno {row.turn} de {row.name} en marcha desde hace {minutes} min: "

        if progress.calls:
            text += (
                f"{progress.calls} llamadas desde el último aviso; la última: {progress.last_call}"
            )
        else:
            text += "ninguna llamada nueva desde el último aviso"

        if progress.last_text:
            text += f"; el agente dice: «{progress.last_text}»"

        await self._alerts.tell(run, text)

    def has_messages(self, run: AutomationRun) -> bool:
        return bool(self._runs.pending_messages(run.id))

    def turn_after_checks(self, run: AutomationRun) -> bool:
        """Whether a turn for the failed round of the current checks phase
        runs or has to be acted on (see WorkRunner)."""

        return self._work.turn_after_checks(run)

    async def _deliver(
        self,
        run: AutomationRun,
        worktree: Worktree,
        name: str,
        state: TurnState,
        messages: list[tuple[int, str]],
    ) -> None:
        session_id = (
            None
            if state.state == "resume_failed"
            else state.row.session_id
            if state.row is not None
            else self._work.last_session(run.id, name)
        )
        turn = "answer" if run.waiting_reason is WaitingReason.AGENT_QUESTION else "message"

        await self.start_turn(
            run,
            worktree,
            name,
            turn,
            self._context.turn_prompt(
                run.issue_key,
                turn,
                worktree.path,
                changes="\n".join(text for _, text in messages),
                new_session=session_id is None,
                planning=name == "plan",
            ),
            session_id,
        )

        for command_id, _ in messages:
            self._runs.consume_command(command_id)

    async def _plan_done(
        self, run: AutomationRun, worktree: Worktree, workflow: ResolvedWorkflow
    ) -> None:
        """The plan waits for its review. After `spyhop approve`, the phase
        after plan_review starts."""

        approved = (
            run.waiting_reason is WaitingReason.PLAN_REVIEW
            and run.approved_at is not None
            and run.status_changed_at is not None
            and run.approved_at >= run.status_changed_at
        )

        if not approved:
            if run.waiting_reason is not WaitingReason.PLAN_REVIEW:
                await self._move_card(run, worktree)

            await self._alerts.wait(
                run, WaitingReason.PLAN_REVIEW, f"review {worktree.path / PLAN_FILE}"
            )

            return

        self._runs.consume(run.id, "approve")

        phases = workflow.phases
        after = (
            phases.index("plan_review") + 1
            if "plan_review" in phases
            else phases.index("implement")
        )

        await self.enter(run, worktree, workflow, after)
