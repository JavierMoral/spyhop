"""Follows each launched run through Orca until its agent declares the end.

Orca emits no event when Claude reaches its /goal, so the agent declares its
own progress: it moves its card to the plan review column with CLAUDE-PLAN.md written,
or to in-review with CLAUDE-RESULT.md written. Each cycle reads the agents of every
worktree (`worktree ps`), the run's card (`worktree show`) and those two files,
and applies the signal table in `observe`.

Plan approval: a person moves the card from the plan review column to the
approved column, and Spyhop types the implementation /goal into the agent's
terminal. The request id is stored before sending, so a send whose answer was
lost is replayed rather than repeated.

The first cycle after a start also reconciles: runs left RUNNING or
WAITING_FOR_HUMAN are checked against Orca with the same table. Nothing is
ever COMPLETED without the agent's explicit signal.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from spyhop.application.context_builder import PLAN_FILE, RESULT_FILE, ContextBuilder
from spyhop.config.settings import REVIEW_STATUS
from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.exceptions import (
    OrcaCommandError,
    OrcaUnavailableError,
    OrcaUncertainError,
)
from spyhop.domain.models import AgentRow, AgentSession, AutomationRun, Worktree
from spyhop.domain.protocols import OrcaGateway
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
    card_status: str | None
    agent: AgentRow | None
    plan_written: bool  # CLAUDE-PLAN.md written by this run's agent
    result_written: bool  # CLAUDE-RESULT.md written by this run's agent


def observe(
    run: AutomationRun, seen: Observation, plan_review_status: str, plan_approved_status: str
) -> Signal:
    """The signal table. Order matters: the agent's explicit declaration of
    the end wins, then what the agent is doing right now, then the card."""

    if seen.card_status == REVIEW_STATUS and seen.result_written:
        return Signal.COMPLETED

    agent = seen.agent

    if agent is None:
        return Signal.MISSING

    if agent.state == "working":
        return Signal.WORKING

    if agent.state in ("waiting", "blocked"):
        return Signal.QUESTION

    if seen.card_status == plan_review_status and seen.plan_written and not seen.result_written:
        return Signal.PLAN_READY

    if run.waiting_reason is WaitingReason.PLAN_REVIEW and seen.card_status == plan_approved_status:
        return Signal.PLAN_APPROVED

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
        plan_review_status: str,
        plan_approved_status: str,
    ) -> None:
        self._runs = runs
        self._worktrees = worktrees
        self._sessions = sessions
        self._orca = orca
        self._context = context
        self._plan_review_status = plan_review_status
        self._plan_approved_status = plan_approved_status
        self._missing: dict[str, int] = {}
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
            self._wait(run, WaitingReason.UNKNOWN_STATE, "no worktree recorded for this run")

            return

        try:
            card = await self._orca.show_worktree(worktree.orca_worktree_id)
        except OrcaCommandError as error:
            if error.code != "selector_not_found":
                raise

            self._wait(run, WaitingReason.UNKNOWN_STATE, f"worktree {worktree.path} is gone")

            return

        session = self._sessions.find_launched(run.id)
        since = _since(run, session)
        seen = Observation(
            card_status=card.workspace_status,
            agent=_run_agent(agents.get(worktree.orca_worktree_id, []), session),
            plan_written=_written_since(worktree.path / PLAN_FILE, since),
            result_written=_written_since(worktree.path / RESULT_FILE, since),
        )
        signal = observe(run, seen, self._plan_review_status, self._plan_approved_status)

        if signal is Signal.MISSING:
            self._on_missing(run, worktree)

            return

        self._missing.pop(run.id, None)

        if signal is Signal.COMPLETED:
            self._runs.mark_completed(run.id)

            if session is not None:
                self._sessions.mark_finished(session.id)

            _log.info("%s: completed; CLAUDE-RESULT.md is in %s", run.issue_key, worktree.path)
        elif signal is Signal.WORKING:
            self._resume(run)
        elif signal is Signal.QUESTION:
            self._wait(run, WaitingReason.AGENT_QUESTION, "the agent is waiting for an answer")
        elif signal is Signal.PLAN_READY:
            self._wait(run, WaitingReason.PLAN_REVIEW, f"review {worktree.path / PLAN_FILE}")
        elif signal is Signal.PLAN_APPROVED:
            assert seen.agent is not None

            await self._approve(run, worktree, seen.agent, session)
        elif signal is Signal.STOPPED:
            self._wait(
                run,
                WaitingReason.UNKNOWN_STATE,
                "the agent stopped without declaring the plan or the end",
            )

    def _on_missing(self, run: AutomationRun, worktree: Worktree) -> None:
        cycles = self._missing.get(run.id, 0) + 1

        self._missing[run.id] = cycles

        if cycles < MISSING_CYCLES:
            _log.debug("%s: agent not listed by Orca (%d)", run.issue_key, cycles)

            return

        self._missing.pop(run.id, None)
        self._wait(run, WaitingReason.UNKNOWN_STATE, f"no agent running in {worktree.path}")

    async def _approve(
        self,
        run: AutomationRun,
        worktree: Worktree,
        agent: AgentRow,
        session: AgentSession | None,
    ) -> None:
        request_id = run.implement_request_id

        if request_id is None:
            request_id = str(uuid.uuid4())

            self._runs.set_implement_request(run.id, request_id)

        handle = await self._terminal_handle(worktree, agent, session)

        if handle is None:
            _log.warning(
                "%s: plan approved, but the agent's terminal is not open; "
                "will try again next cycle",
                run.issue_key,
            )

            return

        try:
            delivery = await self._orca.send_prompt(
                handle, self._context.goal(run.issue_key, "implement"), request_id
            )
        except OrcaCommandError as error:
            # The id is bound to the text and to the terminal process: after
            # either changes, the next attempt needs a new one.

            if error.code in ("request_mismatch", "terminal_handle_stale"):
                self._runs.set_implement_request(run.id, None)

            _log.warning("%s: could not send the implementation goal: %s", run.issue_key, error)

            return

        self._runs.mark_resumed(run.id)

        _log.info(
            "%s: plan approved; implementation goal %s (%s)",
            run.issue_key,
            "already sent" if delivery.replayed else "sent",
            ", ".join(delivery.stages) or "no confirmation",
        )

    async def _terminal_handle(
        self, worktree: Worktree, agent: AgentRow, session: AgentSession | None
    ) -> str | None:
        """The handle returned by the launch while it is still live, else the
        terminal of the agent's pane: handles change when Orca restarts. A
        terminal without a tab in the window reports a placeholder pane
        ("pty:…"), so the launch handle comes first."""

        terminals = await self._orca.terminals(worktree.orca_worktree_id)

        launched = session.agent_handle if session is not None else None

        if launched is not None and any(terminal.handle == launched for terminal in terminals):
            return launched

        if agent.pane_key is None:
            return None

        for terminal in terminals:
            if terminal.pane_key == agent.pane_key:
                return terminal.handle

        return None

    def _resume(self, run: AutomationRun) -> None:
        if run.status is RunStatus.RUNNING:
            return

        self._runs.mark_resumed(run.id)

        _log.info("%s: agent working again (was waiting: %s)", run.issue_key, run.waiting_reason)

    def _wait(self, run: AutomationRun, reason: WaitingReason, message: str) -> None:
        if run.status is RunStatus.WAITING_FOR_HUMAN and run.waiting_reason is reason:
            return

        self._runs.mark_waiting(run.id, reason, message)

        _log.info("%s: waiting for a person (%s): %s", run.issue_key, reason, message)


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
