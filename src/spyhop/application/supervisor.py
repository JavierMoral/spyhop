"""Follows each launched run until its agent declares the end.

Claude emits no event when it reaches its /goal, so the agent declares its own
progress with `spyhop declare KEY plan` once CLAUDE-PLAN.md is written, or
`spyhop declare KEY done` once CLAUDE-RESULT.md is written. Each cycle reads
the declarations of the run, the agents of every worktree, and those two files,
and applies the signal table in `observe`. The state of an agent comes from the
Claude Code hooks of the worktree (see agent_state), and from Orca when there
is no hook state; Orca still tells whether the agent's pane exists.

Plan approval: a person approves with `spyhop approve`, and Spyhop launches a
new agent on agents.model with the implementation /goal. CLAUDE-PLAN.md is all
that agent gets from the plan. The launch follows the launcher's rules: the
operation id is stored before calling Orca, so a launch whose answer was lost
is replayed rather than repeated. The plan agent stays open and idle; its
session is finished.

A new agent, and not /model in the plan agent's session, because /model asks
for a confirmation and saves the model as the user's default for every session.

Pipelines: the run's phase says which step of the workflow's pipeline runs
now. A checks phase runs without an agent through CheckRunner. When it
passes, the next phase starts: the plan agent, the implementation agent, or
the end of the run. When it fails, the run waits for a person, or, with
on_fail: fix, the implementation agent gets the reports and fixes the
findings. Its new CLAUDE-RESULT.md starts the next round of the checks.

The first cycle after a start also reconciles: runs left RUNNING or
WAITING_FOR_HUMAN are checked against Orca with the same table. Nothing is
ever COMPLETED without the agent's explicit signal, and without the checks
after the implementation, if the pipeline has them.

A lost agent, one that Orca no longer lists while the run is RUNNING in a
plan or implement phase, starts again. A stopped agent, one that is idle
without the plan or the end, gets CONTINUE_PROMPT. Each one counts as a
restart; after agents.max_restarts, the run waits for a person.

The supervisor session of the worktree, when there is one, gets the progress
and each reason why the run waits for a person. A wait that does not reach the
session goes again on the next cycle. Its pane is not an agent of the run.
"""

import dataclasses
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from spyhop.application.checkpoint_runner import CheckRunner
from spyhop.application.checkpoint_verdicts import VERDICTS_FILE
from spyhop.application.context_builder import PLAN_FILE, RESULT_FILE, ContextBuilder
from spyhop.application.knowledge import KnowledgeWriter
from spyhop.application.launcher import label_agent, replay_expired
from spyhop.application.supervisor_session import SupervisorSession
from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.config.settings import AgentSettings
from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.exceptions import (
    OrcaCommandError,
    OrcaError,
    OrcaUnavailableError,
    OrcaUncertainError,
    SpyhopError,
)
from spyhop.domain.models import (
    AgentRow,
    AgentSession,
    AutomationRun,
    ChecksPhase,
    ResolvedWorkflow,
    Worktree,
)
from spyhop.domain.protocols import OrcaGateway
from spyhop.infrastructure.agent_state import read_state
from spyhop.infrastructure.orca.client import new_operation_id
from spyhop.infrastructure.persistence.repositories import (
    RunRepository,
    SessionRepository,
    WorktreeRepository,
)

_log = logging.getLogger(__name__)

SUPERVISED = (RunStatus.RUNNING, RunStatus.WAITING_FOR_HUMAN)

AGENT = "claude"

# Cycles without the run's agent in `worktree ps` before handing the run to a
# person. Orca lists a new agent a few seconds after the launch, and a restart
# of Orca briefly lists none.

MISSING_CYCLES = 3

MAX_QUESTION_LENGTH = 1500

CONTINUE_PROMPT = (
    "Continúa con la tarea de CLAUDE-TASK.md. Si ya has terminado, escribe CLAUDE-PLAN.md o "
    "CLAUDE-RESULT.md y declara el plan o el fin con el comando spyhop declare de tu /goal."
)


class Signal(StrEnum):
    COMPLETED = "completed"
    WORKING = "working"
    QUESTION = "question"
    PLAN_READY = "plan_ready"
    PLAN_APPROVED = "plan_approved"
    STOPPED = "stopped"  # done without declaring anything
    MISSING = "missing"
    NONE = "none"  # nothing new


@dataclass(frozen=True)
class Observation:
    declared: str | None  # "plan" or "done", declared by this run's agent
    approved: bool  # a person approved the plan after it went to review
    agent: AgentRow | None
    plan_written: bool  # CLAUDE-PLAN.md written by this run's agent
    result_written: bool  # CLAUDE-RESULT.md written by this run's agent


def observe(run: AutomationRun, seen: Observation) -> Signal:
    """The signal table. Order matters: the agent's explicit declaration of
    the end wins, then what the agent is doing right now, then the plan."""

    if seen.declared == "done" and seen.result_written:
        return Signal.COMPLETED

    agent = seen.agent
    approved = run.waiting_reason is WaitingReason.PLAN_REVIEW and seen.approved

    # The implementation agent needs only CLAUDE-PLAN.md, not the plan agent:
    # a person can close the plan agent before or after the approval.

    if agent is None:
        if approved:
            return Signal.PLAN_APPROVED

        if run.waiting_reason is WaitingReason.PLAN_REVIEW:
            return Signal.NONE

        return Signal.MISSING

    if agent.state == "working":
        return Signal.WORKING

    if agent.state in ("waiting", "blocked"):
        return Signal.QUESTION

    # An approval wins over the plan that it approves: the declaration of the
    # plan stays after `spyhop approve`.

    if approved:
        return Signal.PLAN_APPROVED

    if seen.declared == "plan" and seen.plan_written and not seen.result_written:
        return Signal.PLAN_READY

    # A state older than the run's last transition was already accounted for:
    # right after a launch or a prompt the agent still shows its previous turn.

    if (
        run.status is RunStatus.RUNNING
        and run.status_changed_at is not None
        and agent.state_started_at is not None
        and agent.state_started_at < run.status_changed_at
    ):
        return Signal.NONE

    return Signal.STOPPED


class AgentSupervisor:
    def __init__(
        self,
        runs: RunRepository,
        worktrees: WorktreeRepository,
        sessions: SessionRepository,
        orca: OrcaGateway,
        context: ContextBuilder,
        agents: AgentSettings,
        plan_review_status: str,
        plan_approved_status: str,
        resolver: WorkflowResolver,
        check_runner: CheckRunner | None = None,
        supervisor_session: SupervisorSession | None = None,
        knowledge: KnowledgeWriter | None = None,
        agent_states_dir: Path | None = None,
    ) -> None:
        self._runs = runs
        self._agent_states_dir = agent_states_dir
        self._knowledge = knowledge
        self._supervisor_session = supervisor_session
        self._resolver = resolver
        self._check_runner = check_runner
        self._worktrees = worktrees
        self._sessions = sessions
        self._orca = orca
        self._context = context
        self._agents = agents
        self._plan_review_status = plan_review_status
        self._plan_approved_status = plan_approved_status
        self._missing: dict[str, int] = {}
        self._plan_agent_closed: set[str] = set()
        # The runs whose wait did not reach the supervisor session. Each
        # cycle sends the wait again until it arrives.
        self._unalerted: set[str] = set()
        self._reconciled = False

    async def supervise(self) -> None:
        runs = self._runs.list_by_status(*SUPERVISED)

        if not runs:
            self._reconciled = True

            return

        try:
            await self._orca.ensure_ready()

            agents = await self._orca.worktree_agents()
        except OrcaUnavailableError as error:
            # Nothing is concluded while Orca cannot be read.

            _log.info("%d run(s) unsupervised until Orca answers: %s", len(runs), error)

            return

        if not self._reconciled:
            _log.info("Reconciling %d run(s) left running with Orca", len(runs))

            self._reconciled = True

        for run in runs:
            try:
                await self._supervise(run, agents)
            except (OrcaCommandError, OrcaUnavailableError, OrcaUncertainError) as error:
                # One run's trouble never stops the others from being followed.

                _log.warning("%s: will check again next cycle: %s", run.issue_key, error)

    async def _supervise(self, run: AutomationRun, agents: dict[str, list[AgentRow]]) -> None:
        worktree = self._worktrees.get(run.worktree_id) if run.worktree_id else None

        if worktree is None:
            await self._wait(run, WaitingReason.UNKNOWN_STATE, "no worktree recorded for this run")

            return

        try:
            card = await self._orca.show_worktree(worktree.orca_worktree_id)
        except OrcaCommandError as error:
            if error.code != "selector_not_found":
                raise

            await self._wait(run, WaitingReason.UNKNOWN_STATE, f"worktree {worktree.path} is gone")

            return

        workflow = self._workflow(run)

        if workflow is None:
            await self._wait(
                run,
                WaitingReason.UNKNOWN_STATE,
                f"workflow {run.workflow_name!r} or its phase {run.phase} is not configured",
            )

            return

        rows = [
            row
            for row in agents.get(worktree.orca_worktree_id, [])
            if worktree.supervisor_pane_key is None or row.pane_key != worktree.supervisor_pane_key
        ]
        session = self._sessions.find_launched(run.id)

        if run.plan_changes is not None:
            await self._change_plan(run, worktree, session)

            return

        if isinstance(workflow.phases[run.phase], ChecksPhase) and run.fix_requested_at is None:
            busy = any(row.agent_type == AGENT and row.state == "working" for row in rows)

            await self._checks(run, worktree, workflow, session, busy)

            return

        # While the agent fixes findings, only a CLAUDE-RESULT.md written
        # after the request counts.

        since = run.fix_requested_at or _since(run, session)

        if run.signals == "card":
            # Fallback for the runs from before `spyhop declare`, whose agent
            # moves its card. Remove it once no such run is active.

            declared = {
                self._plan_review_status: "plan",
                self._plan_approved_status: "done",
            }.get(card.workspace_status or "")
            approved = card.workspace_status == self._plan_approved_status
        else:
            declared = (
                run.declared
                if run.declared_at is not None and (since is None or run.declared_at >= since)
                else None
            )
            approved = (
                run.approved_at is not None
                and run.status_changed_at is not None
                and run.approved_at >= run.status_changed_at
            )

        seen = Observation(
            declared=declared,
            approved=approved,
            agent=self._with_hook_state(_run_agent(rows, session)),
            plan_written=_written_since(worktree.path / PLAN_FILE, since),
            result_written=_written_since(worktree.path / RESULT_FILE, since),
        )
        signal = observe(run, seen)

        if signal is Signal.MISSING:
            await self._on_missing(run, worktree, workflow, session)

            return

        self._missing.pop(run.id, None)

        if signal is Signal.NONE and seen.agent is None:
            await self._on_plan_agent_closed(run, worktree)

            return

        self._plan_agent_closed.discard(run.id)

        if signal is Signal.COMPLETED:
            if run.fix_requested_at is not None:
                # A request, not an argument: it stays when the next round
                # cannot start yet, for example on uncommitted changes.

                self._runs.request_checks(run.id)
                self._runs.clear_fix_request(run.id)

                _log.info("%s: fixes done; checkpoints run again", run.issue_key)

                await self._checks(self._reload(run), worktree, workflow, session)
            else:
                # The declared end is the end of the implementation, also
                # when the plan agent did it.

                await self._enter(
                    run, worktree, workflow, workflow.phases.index("implement") + 1, session
                )
        elif signal is Signal.WORKING:
            await self._resume(run)
        elif signal is Signal.QUESTION:
            await self._wait(run, WaitingReason.AGENT_QUESTION, _question(seen.agent))
        elif signal is Signal.PLAN_READY:
            await self._wait(run, WaitingReason.PLAN_REVIEW, f"review {worktree.path / PLAN_FILE}")
        elif signal is Signal.PLAN_APPROVED:
            # Without plan_review in the pipeline, a person approved a plan
            # that the agent wrote by itself: as before, a new agent
            # implements it.

            phases = workflow.phases
            after = (
                phases.index("plan_review") + 1
                if "plan_review" in phases
                else phases.index("implement")
            )

            await self._enter(run, worktree, workflow, after, session)
        elif signal is Signal.STOPPED and not await self._prompt_stopped(run, session):
            await self._wait(
                run,
                WaitingReason.UNKNOWN_STATE,
                "the agent stopped without declaring the plan or the end",
            )

    async def _on_missing(
        self,
        run: AutomationRun,
        worktree: Worktree,
        workflow: ResolvedWorkflow,
        session: AgentSession | None,
    ) -> None:
        cycles = self._missing.get(run.id, 0) + 1

        self._missing[run.id] = cycles

        if cycles < MISSING_CYCLES:
            _log.debug("%s: agent not listed by Orca (%d)", run.issue_key, cycles)

            return

        self._missing.pop(run.id, None)

        if (
            run.status is RunStatus.RUNNING
            and run.restarts < self._agents.max_restarts
            and workflow.phases[run.phase] in ("plan", "implement")
        ):
            self._runs.count_restart(run.id)

            _log.info("%s: agent lost; started again", run.issue_key)

            await self._tell(run, "el agente ya no estaba en Orca; Spyhop lo arranca otra vez")
            await self._enter(run, worktree, workflow, run.phase, session)

            return

        await self._wait(run, WaitingReason.UNKNOWN_STATE, f"no agent running in {worktree.path}")

    def _with_hook_state(self, agent: AgentRow | None) -> AgentRow | None:
        """The agent with the state that its hooks recorded, when there is
        one. Orca still tells whether its pane exists."""

        if agent is None or agent.pane_key is None or self._agent_states_dir is None:
            return agent

        recorded = read_state(self._agent_states_dir, agent.pane_key)

        if recorded is None:
            return agent

        if recorded.state == "ended":
            return None

        return dataclasses.replace(
            agent,
            state=recorded.state,
            question=recorded.question,
            state_started_at=recorded.at,
        )

    async def _change_plan(
        self, run: AutomationRun, worktree: Worktree, session: AgentSession | None
    ) -> None:
        """Starts a new plan agent with the changes that a person asked for
        with `spyhop replan`, while the plan waits for its review."""

        changes = run.plan_changes or ""

        self._runs.request_plan_changes(run.id, None)

        if run.waiting_reason is not WaitingReason.PLAN_REVIEW:
            _log.warning("%s: plan changes ignored: no plan waits for a review", run.issue_key)

            return

        await self._launch(
            run,
            worktree,
            session,
            self._agents.plan_model,
            self._context.goal(run.issue_key, "replan", changes),
            "plan",
            "plan",
            "use `spyhop replan` again or start the agent yourself",
        )

    async def _prompt_stopped(self, run: AutomationRun, session: AgentSession | None) -> bool:
        """Tells an agent that stopped without declaring the plan or the end
        to continue. Returns False when the run must wait for a person."""

        if (
            run.status is not RunStatus.RUNNING
            or run.restarts >= self._agents.max_restarts
            or session is None
            or session.agent_handle is None
        ):
            return False

        try:
            await self._orca.send_prompt(session.agent_handle, CONTINUE_PROMPT)
        except OrcaError as error:
            _log.warning("%s: stopped agent not prompted: %s", run.issue_key, error)

            return False

        self._runs.count_restart(run.id)

        # The stopped state is now older than the run's last change.

        self._runs.mark_resumed(run.id)

        _log.info("%s: agent stopped without a declaration; told to continue", run.issue_key)

        await self._tell(
            run, "el agente se paró sin declarar el plan ni el fin; Spyhop le pide seguir"
        )

        return True

    async def _on_plan_agent_closed(self, run: AutomationRun, worktree: Worktree) -> None:
        """Tells the person once that only an agent that they start can now
        revise the plan."""

        if run.id in self._plan_agent_closed:
            return

        self._plan_agent_closed.add(run.id)

        _log.info("%s: plan agent closed while the plan waits for its review", run.issue_key)

        await self._tell(
            run,
            f"el agente del plan está cerrado; aprueba el plan con `spyhop approve "
            f"{run.issue_key}`, o abre un agente en {worktree.path} para revisar {PLAN_FILE}",
        )

    async def _enter(
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

            await self._tell(run, f"run completada; el resultado está en {RESULT_FILE}")

            return

        if index != run.phase:
            self._runs.set_phase(run.id, index)

            run = self._reload(run)

        phase = workflow.phases[index]

        if isinstance(phase, ChecksPhase):
            await self._checks(run, worktree, workflow, session)
        elif phase == "plan":
            await self._launch(
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

            await self._launch(
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

    async def _launch(
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
            await self._wait(
                run,
                WaitingReason.UNKNOWN_STATE,
                f"{role} launch outcome unknown; check Orca",
            )

            return

        assert session.launch_operation_id is not None

        try:
            result = await self._orca.launch_agent(
                worktree.orca_worktree_id,
                goal,
                session.launch_operation_id,
                session.model,
                self._agents.effort,
            )
        except OrcaCommandError as error:
            self._sessions.mark_failed(session.id)
            await self._wait(
                run,
                WaitingReason.UNKNOWN_STATE,
                f"Orca refused the {role} launch: {error}; {advice}",
            )

            return

        if previous is not None:
            self._sessions.mark_finished(previous.id)

        self._sessions.mark_launched(session.id, result.agent_handle, result.pane_key)
        self._runs.mark_resumed(run.id)
        await label_agent(self._orca, run.issue_key, result.agent_handle, session)

        _log.info(
            "%s: %s agent %s started (%s)", run.issue_key, role, result.agent_handle, session.model
        )

        await self._tell(run, f"agente de {role} arrancado ({session.model})")

    async def _checks(
        self,
        run: AutomationRun,
        worktree: Worktree,
        workflow: ResolvedWorkflow,
        session: AgentSession | None,
        agent_busy: bool = False,
    ) -> None:
        if self._check_runner is None:
            await self._wait(run, WaitingReason.UNKNOWN_STATE, "checkpoints cannot run here")

            return

        try:
            outcome = await self._check_runner.advance(run, worktree, workflow, agent_busy)
        except (OrcaUnavailableError, OrcaUncertainError):
            raise
        except SpyhopError as error:
            await self._wait(run, WaitingReason.UNKNOWN_STATE, f"checkpoints cannot run: {error}")

            return

        phase = workflow.phases[run.phase]

        assert isinstance(phase, ChecksPhase)

        if outcome.state == "running":
            await self._resume(run)
        elif outcome.state == "blocked":
            await self._wait(run, WaitingReason.UNKNOWN_STATE, outcome.message)
        elif outcome.state == "passed":
            await self._enter(run, worktree, workflow, run.phase + 1, session)
        elif phase.on_fail == "fix" and outcome.round < phase.rounds:
            await self._request_fix(run, session, outcome.failed)
        else:
            rounds = f" after {outcome.round} rounds" if phase.on_fail == "fix" else ""

            await self._wait(
                run,
                WaitingReason.CHECKPOINTS_FAILED,
                f"checkpoints {outcome.message}{rounds}; see {worktree.path / VERDICTS_FILE}",
            )

    async def _request_fix(
        self, run: AutomationRun, session: AgentSession | None, failed: tuple[str, ...]
    ) -> None:
        if session is None or session.agent_handle is None:
            await self._wait(run, WaitingReason.CHECKPOINTS_FAILED, "no agent to fix the findings")

            return

        # Recorded first: a prompt whose outcome is unknown is not sent twice.

        self._runs.mark_fix_requested(run.id)

        try:
            await self._orca.send_prompt(
                session.agent_handle, self._context.fix_prompt(run.issue_key, failed)
            )
        except (OrcaCommandError, OrcaUncertainError) as error:
            await self._wait(
                run,
                WaitingReason.UNKNOWN_STATE,
                f"the fix prompt for {', '.join(failed)} may not have reached the agent: {error}",
            )

            return

        _log.info("%s: asked the agent to fix %s", run.issue_key, ", ".join(failed))

        await self._tell(run, f"el agente corrige los hallazgos de {', '.join(failed)}")

    def _workflow(self, run: AutomationRun) -> ResolvedWorkflow | None:
        workflow = self._resolver.resolve_for_run(run)

        if workflow is None or run.phase >= len(workflow.phases):
            return None

        return workflow

    def _reload(self, run: AutomationRun) -> AutomationRun:
        return self._runs.get(run.id) or run

    async def _resume(self, run: AutomationRun) -> None:
        if run.status is RunStatus.RUNNING:
            return

        self._runs.mark_resumed(run.id)
        self._unalerted.discard(run.id)

        _log.info("%s: agent working again (was waiting: %s)", run.issue_key, run.waiting_reason)

        await self._tell(run, f"la run sigue (esperaba: {run.waiting_reason})")

    async def _wait(self, run: AutomationRun, reason: WaitingReason, message: str) -> None:
        if run.status is RunStatus.WAITING_FOR_HUMAN and run.waiting_reason is reason:
            if run.id not in self._unalerted:
                return
        else:
            self._runs.mark_waiting(run.id, reason, message)

            _log.info("%s: waiting for a person (%s): %s", run.issue_key, reason, message)

        worktree = self._session_worktree(run)

        if worktree is None:
            return

        assert self._supervisor_session is not None

        if await self._supervisor_session.alert(worktree, f"{reason}: {message}"):
            self._unalerted.discard(run.id)
        else:
            self._unalerted.add(run.id)

    async def _tell(self, run: AutomationRun, text: str) -> None:
        worktree = self._session_worktree(run)

        if worktree is not None:
            assert self._supervisor_session is not None

            await self._supervisor_session.tell(worktree, text)

    def _session_worktree(self, run: AutomationRun) -> Worktree | None:
        """The worktree of the run when it has a supervisor session."""

        if self._supervisor_session is None or run.worktree_id is None:
            return None

        return self._worktrees.get(run.worktree_id)


def _question(agent: AgentRow | None) -> str:
    """The wait message of an agent that asks something, with its question
    on one line."""

    question = " ".join((agent.question or "").split()) if agent else ""

    if not question:
        return "the agent is waiting for an answer"

    return f"the agent is waiting for an answer: {question[:MAX_QUESTION_LENGTH]}"


def _run_agent(rows: Sequence[AgentRow], session: AgentSession | None) -> AgentRow | None:
    """The run's agent among the worktree's: the pane it was launched in, or
    the most recent Claude when the pane is unknown (for example, a run whose
    launch outcome was lost)."""

    claude = [row for row in rows if row.agent_type == AGENT]

    if session is not None and session.pane_key is not None:
        return next((row for row in claude if row.pane_key == session.pane_key), None)

    epoch = datetime.min.replace(tzinfo=UTC)

    return max(claude, key=lambda row: row.state_started_at or epoch, default=None)


def _since(run: AutomationRun, session: AgentSession | None) -> datetime | None:
    if session is not None and session.started_at is not None:
        return session.started_at

    return run.started_at


def _written_since(path: Path, since: datetime | None) -> bool:
    """Files from an earlier run of the same ticket do not count."""

    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return False

    return since is None or modified >= since
