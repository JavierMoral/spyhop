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
now. PhaseRunner starts each phase. A checks phase runs without an agent
through CheckRunner. When it
passes, the next phase starts: the plan agent, the implementation agent, or
the end of the run. When it fails, the run waits for a person, or, with
on_fail: fix, the implementation agent gets the reports and fixes the
findings. Its new CLAUDE-RESULT.md starts the next round of the checks.

The first cycle after a start also reconciles: runs left RUNNING or
WAITING_FOR_HUMAN are checked against Orca with the same table. Nothing is
ever COMPLETED without the agent's explicit signal, and without the checks
after the implementation, if the pipeline has them.

AgentRecovery handles a lost agent, one that Orca no longer lists while the
run is RUNNING in a plan or implement phase: the agent starts again. It also
handles a stopped agent, one that is idle without the plan or the end: the
agent gets CONTINUE_PROMPT. Each one counts as a restart; after
agents.max_restarts, the run waits for a person.

Through RunAlerts, the supervisor session of the worktree, when there is one,
gets the progress and each reason why the run waits for a person. A wait that does not reach the
session goes again on the next cycle. Its pane is not an agent of the run.
"""

import dataclasses
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from spyhop.application.agent_recovery import MISSING_AFTER, AgentRecovery
from spyhop.application.checkpoint_runner import CheckRunner
from spyhop.application.context_builder import PLAN_FILE, RESULT_FILE, ContextBuilder
from spyhop.application.knowledge import KnowledgeWriter
from spyhop.application.phase_runner import PhaseRunner
from spyhop.application.run_alerts import RunAlerts
from spyhop.application.supervisor_session import SupervisorSession
from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.config.settings import AgentSettings
from spyhop.domain.clock import Clock, utc_now
from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.exceptions import (
    OrcaCommandError,
    OrcaUnavailableError,
    OrcaUncertainError,
)
from spyhop.domain.models import (
    AgentRow,
    AgentSession,
    AutomationRun,
    ChecksPhase,
    ResolvedWorkflow,
)
from spyhop.domain.protocols import TerminalHost, WorktreeHost
from spyhop.infrastructure.agent_state import read_state
from spyhop.infrastructure.persistence.repositories import (
    RunRepository,
    SessionRepository,
    WorktreeRepository,
)

_log = logging.getLogger(__name__)

SUPERVISED = (RunStatus.RUNNING, RunStatus.WAITING_FOR_HUMAN)

AGENT = "claude"

MAX_QUESTION_LENGTH = 1500


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
        worktree_host: WorktreeHost,
        terminal_host: TerminalHost,
        context: ContextBuilder,
        agents: AgentSettings,
        plan_review_status: str,
        plan_approved_status: str,
        resolver: WorkflowResolver,
        check_runner: CheckRunner | None = None,
        supervisor_session: SupervisorSession | None = None,
        knowledge: KnowledgeWriter | None = None,
        agent_states_dir: Path | None = None,
        missing_after: timedelta = MISSING_AFTER,
        now: Clock = utc_now,
    ) -> None:
        self._runs = runs
        self._agent_states_dir = agent_states_dir
        self._resolver = resolver
        self._worktrees = worktrees
        self._sessions = sessions
        self._worktree_host = worktree_host
        self._terminal_host = terminal_host
        self._plan_review_status = plan_review_status
        self._plan_approved_status = plan_approved_status
        self._alerts = RunAlerts(runs, worktrees, supervisor_session)
        self._phases = PhaseRunner(
            runs,
            sessions,
            terminal_host,
            context,
            agents,
            plan_review_status,
            self._alerts,
            check_runner,
            knowledge,
            now,
        )
        self._recovery = AgentRecovery(
            runs, terminal_host, agents, self._phases, self._alerts, missing_after, now
        )
        # Only for the log: the first cycle after a start reconciles the runs.
        self._reconciled = False

    async def supervise(self) -> None:
        runs = self._runs.list_by_status(*SUPERVISED)

        if not runs:
            self._reconciled = True

            return

        try:
            # The worktree host is asked only for a missing worktree; an
            # error there stops only that run.

            await self._terminal_host.ensure_ready()

            agents = await self._terminal_host.worktree_agents()
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
            await self._alerts.wait(
                run, WaitingReason.UNKNOWN_STATE, "no worktree recorded for this run"
            )

            return

        # `worktree ps` lists the worktrees of Orca, also the ones without
        # agents, but it can leave some out (`truncated`). The supervisor asks
        # Orca for the worktree only when the worktree is not in that list, or
        # for the card of a run from before `spyhop declare`.

        card = None

        if not worktree.path.exists():
            await self._alerts.wait(
                run, WaitingReason.UNKNOWN_STATE, f"worktree {worktree.path} is gone"
            )

            return

        if run.signals == "card" or worktree.orca_worktree_id not in agents:
            try:
                card = await self._worktree_host.show_worktree(worktree.orca_worktree_id)
            except OrcaCommandError as error:
                if error.code != "selector_not_found":
                    raise

                await self._alerts.wait(
                    run, WaitingReason.UNKNOWN_STATE, f"worktree {worktree.path} is gone"
                )

                return

        workflow = self._workflow(run)

        if workflow is None:
            await self._alerts.wait(
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
            await self._phases.change_plan(run, worktree, session)

            return

        if isinstance(workflow.phases[run.phase], ChecksPhase) and run.fix_requested_at is None:
            busy = any(row.agent_type == AGENT and row.state == "working" for row in rows)

            await self._phases.checks(run, worktree, workflow, session, busy)

            return

        # While the agent fixes findings, only a CLAUDE-RESULT.md written
        # after the request counts.

        since = run.fix_requested_at or _since(run, session)

        if run.signals == "card":
            # Fallback for the runs from before `spyhop declare`, whose agent
            # moves its card. Remove it once no such run is active.

            # A card run always asks Orca for its worktree above.

            assert card is not None

            status = card.workspace_status
            declared = {
                self._plan_review_status: "plan",
                self._plan_approved_status: "done",
            }.get(status or "")
            approved = status == self._plan_approved_status
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
            await self._recovery.on_missing(run, worktree, workflow, session)

            return

        if run.missing_cycles:
            self._runs.clear_missing(run.id)

        if signal is Signal.NONE and seen.agent is None:
            await self._recovery.on_plan_agent_closed(run, worktree)

            return

        if run.plan_agent_closed_told:
            self._runs.set_plan_agent_closed_told(run.id, False)

        if signal is Signal.COMPLETED:
            if run.fix_requested_at is not None:
                # A request, not an argument: it stays when the next round
                # cannot start yet, for example on uncommitted changes.

                self._runs.request_checks(run.id)
                self._runs.clear_fix_request(run.id)

                _log.info("%s: fixes done; checkpoints run again", run.issue_key)

                await self._phases.checks(self._reload(run), worktree, workflow, session)
            else:
                # The declared end is the end of the implementation, also
                # when the plan agent did it.

                await self._phases.enter(
                    run, worktree, workflow, workflow.phases.index("implement") + 1, session
                )
        elif signal is Signal.WORKING:
            await self._alerts.resume(run)
        elif signal is Signal.QUESTION:
            await self._alerts.wait(run, WaitingReason.AGENT_QUESTION, _question(seen.agent))
        elif signal is Signal.PLAN_READY:
            await self._alerts.wait(
                run, WaitingReason.PLAN_REVIEW, f"review {worktree.path / PLAN_FILE}"
            )
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

            self._runs.consume(run.id, "approve")

            await self._phases.enter(run, worktree, workflow, after, session)
        elif signal is Signal.STOPPED and not await self._recovery.prompt_stopped(run, session):
            await self._alerts.wait(
                run,
                WaitingReason.UNKNOWN_STATE,
                "the agent stopped without declaring the plan or the end",
            )

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

    def _workflow(self, run: AutomationRun) -> ResolvedWorkflow | None:
        workflow = self._resolver.resolve_for_run(run)

        if workflow is None or run.phase >= len(workflow.phases):
            return None

        if run.pipeline is None:
            # A run from before the column: from now on, a change of the
            # configuration does not move its phase.

            self._runs.set_pipeline(run.id, workflow.phases)

        return workflow

    def _reload(self, run: AutomationRun) -> AutomationRun:
        return self._runs.get(run.id) or run


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
