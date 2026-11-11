import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spyhop.application.supervisor import MISSING_CYCLES, Observation, Signal, observe
from spyhop.domain.enums import RunStatus, SessionStatus, WaitingReason
from spyhop.domain.exceptions import OrcaCommandError, OrcaUncertainError
from spyhop.domain.models import AgentRow, AutomationRun, OrcaWorktree, Worktree
from tests.fakes import PLAN_APPROVED, PLAN_REVIEW, Harness

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def agent(state: str, since: datetime = NOW) -> AgentRow:
    return AgentRow("claude", state, "tab:leaf", True, None, since)


def run_in(status: RunStatus, reason: WaitingReason | None = None) -> AutomationRun:
    return AutomationRun(
        id="r1",
        issue_key="LPD-1",
        worktree_id="w1",
        workflow_name="bug_fix",
        trigger_status="In Progress",
        trigger_history_id="1",
        triggered_at=NOW,
        status=status,
        waiting_reason=reason,
        created_at=NOW,
        started_at=NOW,
        finished_at=None,
        error_message=None,
        retry_of=None,
        status_changed_at=NOW,
    )


RUNNING = run_in(RunStatus.RUNNING)
PLAN_WAIT = run_in(RunStatus.WAITING_FOR_HUMAN, WaitingReason.PLAN_REVIEW)
LATER = NOW + timedelta(minutes=1)


@pytest.mark.parametrize(
    ("run", "seen", "expected"),
    [
        # The agent's declaration of the end wins, even mid-turn.
        (RUNNING, Observation(PLAN_APPROVED, agent("working"), True, True), Signal.COMPLETED),
        # The approved column without CLAUDE-RESULT.md is not the end.
        (RUNNING, Observation(PLAN_APPROVED, agent("done", LATER), True, False), Signal.STOPPED),
        (RUNNING, Observation("in-progress", None, False, False), Signal.MISSING),
        (RUNNING, Observation("in-progress", agent("working"), False, False), Signal.WORKING),
        (RUNNING, Observation("in-progress", agent("waiting"), False, False), Signal.QUESTION),
        (RUNNING, Observation("in-progress", agent("blocked"), False, False), Signal.QUESTION),
        # Revising the plan after feedback: working wins over the card.
        (PLAN_WAIT, Observation(PLAN_REVIEW, agent("working"), True, False), Signal.WORKING),
        (RUNNING, Observation(PLAN_REVIEW, agent("done", LATER), True, False), Signal.PLAN_READY),
        # The card alone, without a CLAUDE-PLAN.md of this run, is not a plan.
        (RUNNING, Observation(PLAN_REVIEW, agent("done", LATER), False, False), Signal.STOPPED),
        (PLAN_WAIT, Observation(PLAN_APPROVED, agent("done"), True, False), Signal.PLAN_APPROVED),
        # Only a run waiting for its plan review is approved by the card.
        (RUNNING, Observation(PLAN_APPROVED, agent("done", LATER), True, False), Signal.STOPPED),
        # A state from before the run's last transition is stale.
        (
            RUNNING,
            Observation(PLAN_APPROVED, agent("done", NOW - timedelta(seconds=5)), True, False),
            Signal.NONE,
        ),
    ],
)
def test_signal_table(run: AutomationRun, seen: Observation, expected: Signal) -> None:
    assert observe(run, seen, PLAN_REVIEW, PLAN_APPROVED) is expected


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


async def launched(harness: Harness, key: str = "LPD-1") -> tuple[AutomationRun, Worktree, str]:
    """A run with its agent working; returns the run, worktree and pane."""

    run = harness.discover(key)

    await harness.launcher.launch_pending()

    worktree = harness.worktrees.find_current(key)
    session = harness.sessions.find_launched(run.id)

    assert worktree is not None and session is not None and session.pane_key is not None

    harness.orca.set_agent(worktree.orca_worktree_id, "working", session.pane_key)

    return harness.run(run.id), worktree, session.pane_key


async def plan_ready(harness: Harness) -> tuple[AutomationRun, Worktree, str]:
    run, worktree, pane = await launched(harness)

    (worktree.path / "CLAUDE-PLAN.md").write_text("plan")
    harness.orca.set_card(worktree.orca_worktree_id, PLAN_REVIEW)
    harness.orca.set_agent(worktree.orca_worktree_id, "done", pane)

    await harness.supervisor.supervise()

    return harness.run(run.id), worktree, pane


async def test_working_agent_keeps_the_run_running(harness: Harness) -> None:
    run, _, _ = await launched(harness)

    await harness.supervisor.supervise()

    assert harness.run(run.id).status is RunStatus.RUNNING


async def test_plan_ready_waits_for_review(harness: Harness) -> None:
    run, worktree, _ = await plan_ready(harness)

    assert run.status is RunStatus.WAITING_FOR_HUMAN
    assert run.waiting_reason is WaitingReason.PLAN_REVIEW
    assert run.error_message == f"review {worktree.path / 'CLAUDE-PLAN.md'}"


async def test_feedback_on_the_plan_resumes_the_run(harness: Harness) -> None:
    run, worktree, pane = await plan_ready(harness)

    harness.orca.set_agent(worktree.orca_worktree_id, "working", pane)

    await harness.supervisor.supervise()

    resumed = harness.run(run.id)

    assert resumed.status is RunStatus.RUNNING
    assert resumed.waiting_reason is None
    assert resumed.error_message is None
    assert resumed.started_at == run.started_at
    assert len(harness.orca.launches) == 1


async def test_approving_the_plan_launches_a_new_agent_on_the_model(harness: Harness) -> None:
    run, worktree, plan_pane = await plan_ready(harness)

    harness.orca.set_card(worktree.orca_worktree_id, PLAN_APPROVED)

    await harness.supervisor.supervise()

    plan_launch, launch = harness.orca.launches
    plan_session, session = harness.sessions.list_for_run(run.id)
    approved = harness.run(run.id)

    assert plan_launch["model"] == "opus"
    assert launch["model"] == "sonnet"
    assert launch["effort"] == "high"
    assert launch["worktree"] == worktree.orca_worktree_id
    assert launch["prompt"].startswith("/goal Lee CLAUDE-TASK.md, vuelve a leer los comentarios")
    assert f"--workspace-status {PLAN_APPROVED}" in launch["prompt"]
    assert launch["operation_id"] == session.launch_operation_id
    assert plan_session.status is SessionStatus.FINISHED
    assert session.status is SessionStatus.LAUNCHED
    assert session.model == "sonnet"
    assert session.pane_key is not None and session.pane_key != plan_pane
    assert approved.status is RunStatus.RUNNING
    assert approved.waiting_reason is None

    # The new agent still shows a state from before the approval: not a stop.
    # The idle plan agent is not the run's agent anymore.

    harness.orca.set_agent(
        worktree.orca_worktree_id,
        "done",
        session.pane_key,
        datetime.now(UTC) - timedelta(minutes=1),
    )

    await harness.supervisor.supervise()

    assert harness.run(run.id).status is RunStatus.RUNNING
    assert len(harness.orca.launches) == 2


async def test_lost_approval_answer_is_replayed_with_the_same_operation(
    harness: Harness,
) -> None:
    run, worktree, _ = await plan_ready(harness)

    harness.orca.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
    harness.orca.launch_errors.append(OrcaUncertainError("timed out"))

    await harness.supervisor.supervise()

    waiting = harness.run(run.id)

    assert waiting.status is RunStatus.WAITING_FOR_HUMAN
    assert waiting.waiting_reason is WaitingReason.PLAN_REVIEW

    await harness.supervisor.supervise()

    _, first, second = harness.orca.launches
    [_, session] = harness.sessions.list_for_run(run.id)

    assert second["operation_id"] == first["operation_id"]
    assert session.status is SessionStatus.LAUNCHED
    assert harness.run(run.id).status is RunStatus.RUNNING


async def test_refused_approval_waits_until_the_plan_is_approved_again(
    harness: Harness,
) -> None:
    run, worktree, _ = await plan_ready(harness)

    harness.orca.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
    harness.orca.launch_errors.append(OrcaCommandError("invalid_argument", "bad model"))

    await harness.supervisor.supervise()

    refused = harness.run(run.id)
    [plan_session, failed] = harness.sessions.list_for_run(run.id)

    assert refused.waiting_reason is WaitingReason.UNKNOWN_STATE
    assert refused.error_message is not None
    assert refused.error_message.endswith(
        f"move the card back to {PLAN_REVIEW} and approve the plan again"
    )
    assert plan_session.status is SessionStatus.LAUNCHED
    assert failed.status is SessionStatus.FAILED

    harness.orca.set_card(worktree.orca_worktree_id, PLAN_REVIEW)

    await harness.supervisor.supervise()

    assert harness.run(run.id).waiting_reason is WaitingReason.PLAN_REVIEW

    harness.orca.set_card(worktree.orca_worktree_id, PLAN_APPROVED)

    await harness.supervisor.supervise()

    _, first, second = harness.orca.launches

    assert second["operation_id"] != first["operation_id"]
    assert harness.run(run.id).status is RunStatus.RUNNING


async def test_approval_launch_lost_too_long_ago_is_handed_to_a_person(
    harness: Harness,
) -> None:
    run, worktree, _ = await plan_ready(harness)

    harness.orca.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
    harness.sessions.create_launching(run.id, "claude", "sonnet", "1000-" + "0" * 32)

    await harness.supervisor.supervise()

    lost = harness.run(run.id)

    assert len(harness.orca.launches) == 1
    assert lost.waiting_reason is WaitingReason.UNKNOWN_STATE
    assert lost.error_message == "implementation launch outcome unknown; check Orca"


async def test_declared_end_completes_the_run_and_frees_its_slot(harness: Harness) -> None:
    run, worktree, pane = await launched(harness)
    queued = harness.discover("LPD-2")

    (worktree.path / "CLAUDE-RESULT.md").write_text("done")
    harness.orca.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
    harness.orca.set_agent(worktree.orca_worktree_id, "done", pane)

    await harness.supervisor.supervise()
    await harness.launcher.launch_pending()

    completed = harness.run(run.id)
    [session] = harness.sessions.list_for_run(run.id)

    assert completed.status is RunStatus.COMPLETED
    assert completed.finished_at is not None
    assert session.status is SessionStatus.FINISHED
    assert harness.run(queued.id).status is RunStatus.RUNNING


async def test_files_from_an_earlier_run_are_no_signal(harness: Harness) -> None:
    run, worktree, pane = await launched(harness)
    result = worktree.path / "CLAUDE-RESULT.md"
    old = (datetime.now(UTC) - timedelta(days=1)).timestamp()

    result.write_text("from the previous run")
    os.utime(result, (old, old))
    harness.orca.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
    harness.orca.set_agent(
        worktree.orca_worktree_id, "done", pane, datetime.now(UTC) + timedelta(seconds=1)
    )

    await harness.supervisor.supervise()

    stopped = harness.run(run.id)

    assert stopped.status is RunStatus.WAITING_FOR_HUMAN
    assert stopped.waiting_reason is WaitingReason.UNKNOWN_STATE


async def test_agent_question_waits_for_a_person(harness: Harness) -> None:
    run, worktree, pane = await launched(harness)

    harness.orca.set_agent(worktree.orca_worktree_id, "waiting", pane)

    await harness.supervisor.supervise()

    assert harness.run(run.id).waiting_reason is WaitingReason.AGENT_QUESTION


async def test_agent_of_an_earlier_run_is_ignored(harness: Harness) -> None:
    run, worktree, _ = await launched(harness)

    harness.orca.set_agent(worktree.orca_worktree_id, "waiting", "old-tab:old-leaf")

    await harness.supervisor.supervise()

    assert harness.run(run.id).status is RunStatus.RUNNING


async def test_missing_agent_is_handed_to_a_person_after_a_few_cycles(
    harness: Harness,
) -> None:
    run, worktree, pane = await launched(harness)

    harness.orca.agents.clear()

    for _ in range(MISSING_CYCLES - 1):
        await harness.supervisor.supervise()

    assert harness.run(run.id).status is RunStatus.RUNNING

    # Seen again: the count starts over.

    harness.orca.set_agent(worktree.orca_worktree_id, "working", pane)

    await harness.supervisor.supervise()

    harness.orca.agents.clear()

    for _ in range(MISSING_CYCLES - 1):
        await harness.supervisor.supervise()

    assert harness.run(run.id).status is RunStatus.RUNNING

    await harness.supervisor.supervise()

    lost = harness.run(run.id)

    assert lost.status is RunStatus.WAITING_FOR_HUMAN
    assert lost.waiting_reason is WaitingReason.UNKNOWN_STATE
    assert lost.error_message == f"no agent running in {worktree.path}"


async def test_nothing_changes_while_orca_is_unavailable(harness: Harness) -> None:
    run, worktree, _ = await launched(harness)

    harness.orca.agents.clear()
    harness.orca.unavailable = True

    for _ in range(MISSING_CYCLES + 1):
        await harness.supervisor.supervise()

    assert harness.run(run.id).status is RunStatus.RUNNING


async def test_worktree_removed_in_orca_is_handed_to_a_person(harness: Harness) -> None:
    run, worktree, _ = await launched(harness)

    del harness.orca.worktrees[worktree.orca_worktree_id]

    await harness.supervisor.supervise()

    gone = harness.run(run.id)

    assert gone.waiting_reason is WaitingReason.UNKNOWN_STATE
    assert gone.error_message == f"worktree {worktree.path} is gone"


async def test_run_without_a_known_pane_follows_the_latest_agent(harness: Harness) -> None:
    run, worktree, _ = await launched(harness)

    harness.connection.execute("UPDATE agent_sessions SET pane_key = NULL")
    harness.orca.agents.clear()
    harness.orca.set_agent(
        worktree.orca_worktree_id, "done", "a:1", datetime.now(UTC) - timedelta(hours=1)
    )
    harness.orca.set_agent(worktree.orca_worktree_id, "waiting", "b:2")

    await harness.supervisor.supervise()

    assert harness.run(run.id).waiting_reason is WaitingReason.AGENT_QUESTION


async def test_one_failing_run_does_not_stop_the_others(harness: Harness) -> None:
    harness.config.application.max_concurrent_agents = 2
    harness.launcher._max_concurrent_agents = 2

    first, first_worktree, _ = await launched(harness, "LPD-1")
    second, second_worktree, second_pane = await launched(harness, "LPD-2")
    show = harness.orca.show_worktree

    async def broken_first(orca_worktree_id: str) -> OrcaWorktree:
        if orca_worktree_id == first_worktree.orca_worktree_id:
            raise OrcaCommandError("runtime_error", "boom")

        return await show(orca_worktree_id)

    harness.orca.show_worktree = broken_first  # type: ignore[method-assign]
    harness.orca.set_agent(second_worktree.orca_worktree_id, "waiting", second_pane)

    await harness.supervisor.supervise()

    assert harness.run(first.id).status is RunStatus.RUNNING
    assert harness.run(second.id).waiting_reason is WaitingReason.AGENT_QUESTION
