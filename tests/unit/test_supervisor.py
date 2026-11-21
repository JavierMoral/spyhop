import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spyhop.application.supervisor import (
    CONTINUE_PROMPT,
    MISSING_AFTER,
    MISSING_CYCLES,
    Observation,
    Signal,
    observe,
)
from spyhop.domain.enums import RunStatus, SessionStatus, WaitingReason
from spyhop.domain.exceptions import OrcaCommandError, OrcaUncertainError
from spyhop.domain.models import AgentRow, AutomationRun, OrcaWorktree, Worktree
from spyhop.infrastructure.agent_state import write_state
from tests.factories import settings
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


DONE = {"declared": "done", "approved": True}
PLAN = {"declared": "plan", "approved": False}
NOTHING = {"declared": None, "approved": False}


def seen(signals: dict[str, object], row: AgentRow | None, plan: bool, result: bool) -> Observation:
    return Observation(agent=row, plan_written=plan, result_written=result, **signals)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("run", "observation", "expected"),
    [
        # The agent's declaration of the end wins, even mid-turn.
        (RUNNING, seen(DONE, agent("working"), True, True), Signal.COMPLETED),
        # A declared end without CLAUDE-RESULT.md is not the end.
        (RUNNING, seen(DONE, agent("done", LATER), True, False), Signal.STOPPED),
        (RUNNING, seen(NOTHING, None, False, False), Signal.MISSING),
        (RUNNING, seen(NOTHING, agent("working"), False, False), Signal.WORKING),
        (RUNNING, seen(NOTHING, agent("waiting"), False, False), Signal.QUESTION),
        (RUNNING, seen(NOTHING, agent("blocked"), False, False), Signal.QUESTION),
        # Revising the plan after feedback: working wins over the declaration.
        (PLAN_WAIT, seen(PLAN, agent("working"), True, False), Signal.WORKING),
        (RUNNING, seen(PLAN, agent("done", LATER), True, False), Signal.PLAN_READY),
        # The declaration alone, without a CLAUDE-PLAN.md of this run, is not a plan.
        (RUNNING, seen(PLAN, agent("done", LATER), False, False), Signal.STOPPED),
        (PLAN_WAIT, seen(DONE, agent("done"), True, False), Signal.PLAN_APPROVED),
        # An approval wins over the declared plan that it approves.
        (
            PLAN_WAIT,
            seen({"declared": "plan", "approved": True}, agent("done"), True, False),
            Signal.PLAN_APPROVED,
        ),
        # A plan agent that is gone does not stop the plan review or the approval.
        (PLAN_WAIT, seen(PLAN, None, True, False), Signal.NONE),
        (PLAN_WAIT, seen(DONE, None, True, False), Signal.PLAN_APPROVED),
        # Only a run waiting for its plan review is approved.
        (RUNNING, seen(DONE, agent("done", LATER), True, False), Signal.STOPPED),
        # A state from before the run's last transition is stale.
        (
            RUNNING,
            seen(DONE, agent("done", NOW - timedelta(seconds=5)), True, False),
            Signal.NONE,
        ),
    ],
)
def test_signal_table(run: AutomationRun, observation: Observation, expected: Signal) -> None:
    assert observe(run, observation) is expected


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
    harness.set_card(worktree.orca_worktree_id, PLAN_REVIEW)
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

    harness.set_card(worktree.orca_worktree_id, PLAN_APPROVED)

    await harness.supervisor.supervise()

    plan_launch, launch = harness.orca.launches
    plan_session, session = harness.sessions.list_for_run(run.id)
    approved = harness.run(run.id)

    assert plan_launch["model"] == "opus"
    assert launch["model"] == "sonnet"
    assert launch["effort"] == "high"
    assert launch["worktree"] == worktree.orca_worktree_id
    assert launch["prompt"].startswith("/goal Lee CLAUDE-TASK.md, vuelve a leer los comentarios")
    assert launch["prompt"].endswith("spyhop declare LPD-1 done")
    assert launch["operation_id"] == session.launch_operation_id
    assert plan_session.status is SessionStatus.FINISHED
    assert session.status is SessionStatus.LAUNCHED
    assert plan_session.role == "plan"
    assert session.model == "sonnet"
    assert session.role == "implement"
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

    harness.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
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

    harness.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
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

    harness.set_card(worktree.orca_worktree_id, PLAN_REVIEW)

    await harness.supervisor.supervise()

    assert harness.run(run.id).waiting_reason is WaitingReason.PLAN_REVIEW

    harness.set_card(worktree.orca_worktree_id, PLAN_APPROVED)

    await harness.supervisor.supervise()

    _, first, second = harness.orca.launches

    assert second["operation_id"] != first["operation_id"]
    assert harness.run(run.id).status is RunStatus.RUNNING


async def test_approval_launch_lost_too_long_ago_is_handed_to_a_person(
    harness: Harness,
) -> None:
    run, worktree, _ = await plan_ready(harness)

    harness.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
    harness.sessions.create_launching(run.id, "claude", "sonnet", "1000-" + "0" * 32, "implement")

    await harness.supervisor.supervise()

    lost = harness.run(run.id)

    assert len(harness.orca.launches) == 1
    assert lost.waiting_reason is WaitingReason.UNKNOWN_STATE
    assert lost.error_message == "implementation launch outcome unknown; check Orca"


async def test_declared_end_completes_the_run_and_frees_its_slot(harness: Harness) -> None:
    run, worktree, pane = await launched(harness)
    queued = harness.discover("LPD-2")

    (worktree.path / "CLAUDE-RESULT.md").write_text("done")
    harness.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
    harness.orca.set_agent(worktree.orca_worktree_id, "done", pane)

    await harness.supervisor.supervise()
    await harness.launcher.launch_pending()

    completed = harness.run(run.id)
    [session] = harness.sessions.list_for_run(run.id)

    assert completed.status is RunStatus.COMPLETED
    assert completed.finished_at is not None
    assert session.status is SessionStatus.FINISHED
    assert harness.run(queued.id).status is RunStatus.RUNNING


async def test_files_from_an_earlier_run_are_no_signal(tmp_path: Path) -> None:
    harness = Harness(tmp_path, settings(tmp_path, agents={"max_restarts": 0}))
    run, worktree, pane = await launched(harness)
    result = worktree.path / "CLAUDE-RESULT.md"
    old = (datetime.now(UTC) - timedelta(days=1)).timestamp()

    result.write_text("from the previous run")
    os.utime(result, (old, old))
    harness.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
    harness.orca.set_agent(
        worktree.orca_worktree_id, "done", pane, datetime.now(UTC) + timedelta(seconds=1)
    )

    await harness.supervisor.supervise()

    stopped = harness.run(run.id)

    assert stopped.status is RunStatus.WAITING_FOR_HUMAN
    assert stopped.waiting_reason is WaitingReason.UNKNOWN_STATE


async def test_agent_question_waits_for_a_person(harness: Harness) -> None:
    run, worktree, pane = await launched(harness)

    harness.orca.set_agent(worktree.orca_worktree_id, "waiting", pane, question="A\n or B?")

    await harness.supervisor.supervise()

    waiting = harness.run(run.id)

    assert waiting.waiting_reason is WaitingReason.AGENT_QUESTION
    assert waiting.error_message == "the agent is waiting for an answer: A or B?"


async def test_agent_of_an_earlier_run_is_ignored(harness: Harness) -> None:
    run, worktree, _ = await launched(harness)

    harness.orca.set_agent(worktree.orca_worktree_id, "waiting", "old-tab:old-leaf")

    await harness.supervisor.supervise()

    assert harness.run(run.id).status is RunStatus.RUNNING


async def test_missing_agent_is_handed_to_a_person_after_a_few_cycles(tmp_path: Path) -> None:
    harness = Harness(tmp_path, settings(tmp_path, agents={"max_restarts": 0}))
    run, worktree, pane = await launched(harness)

    harness.orca.agents.clear()

    for _ in range(MISSING_CYCLES - 1):
        await harness.supervisor.supervise()

    # The count is in the run, so a restart of the service keeps it.

    assert harness.run(run.id).status is RunStatus.RUNNING
    assert harness.run(run.id).missing_cycles == MISSING_CYCLES - 1

    # Seen again: the count starts over.

    harness.orca.set_agent(worktree.orca_worktree_id, "working", pane)

    await harness.supervisor.supervise()

    assert harness.run(run.id).missing_cycles == 0
    assert harness.run(run.id).missing_since is None

    harness.orca.agents.clear()

    for _ in range(MISSING_CYCLES - 1):
        await harness.supervisor.supervise()

    assert harness.run(run.id).status is RunStatus.RUNNING

    await harness.supervisor.supervise()

    lost = harness.run(run.id)

    assert lost.status is RunStatus.WAITING_FOR_HUMAN
    assert lost.waiting_reason is WaitingReason.UNKNOWN_STATE
    assert lost.error_message == f"no agent running in {worktree.path}"


async def test_an_agent_is_lost_only_after_the_missing_time_also_with_many_cycles(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, settings(tmp_path, agents={"max_restarts": 0}))
    run, worktree, _ = await launched(harness)

    harness.supervisor._missing_after = MISSING_AFTER
    harness.orca.agents.clear()

    # Wakes of the service can start many cycles in a few seconds.

    for _ in range(MISSING_CYCLES + 2):
        await harness.supervisor.supervise()

    assert harness.run(run.id).status is RunStatus.RUNNING

    harness.connection.execute(
        "UPDATE automation_runs SET missing_since = ?",
        ((datetime.now(UTC) - MISSING_AFTER).isoformat(),),
    )
    harness.supervisor._started_at -= MISSING_AFTER

    await harness.supervisor.supervise()

    assert harness.run(run.id).error_message == f"no agent running in {worktree.path}"


async def test_the_missing_count_survives_a_restart_but_the_time_starts_again(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, settings(tmp_path, agents={"max_restarts": 0}))
    run, worktree, _ = await launched(harness)

    harness.orca.agents.clear()

    for _ in range(MISSING_CYCLES - 1):
        await harness.supervisor.supervise()

    # Missing for long before the stop of the service.

    harness.connection.execute(
        "UPDATE automation_runs SET missing_since = ?",
        ((datetime.now(UTC) - 10 * MISSING_AFTER).isoformat(),),
    )
    harness.restart_supervisor()
    harness.supervisor._missing_after = MISSING_AFTER

    await harness.supervisor.supervise()

    assert harness.run(run.id).status is RunStatus.RUNNING

    harness.restart_supervisor()

    await harness.supervisor.supervise()

    assert harness.run(run.id).error_message == f"no agent running in {worktree.path}"


async def test_nothing_changes_while_orca_is_unavailable(harness: Harness) -> None:
    run, worktree, _ = await launched(harness)

    harness.orca.agents.clear()
    harness.orca.unavailable = True

    for _ in range(MISSING_CYCLES + 1):
        await harness.supervisor.supervise()

    assert harness.run(run.id).status is RunStatus.RUNNING


async def test_only_a_run_that_reads_its_card_asks_orca_for_the_worktree(
    harness: Harness,
) -> None:
    run, worktree, _ = await launched(harness)

    await harness.supervisor.supervise()

    declared = list(harness.orca.shown)

    harness.connection.execute(
        "UPDATE automation_runs SET signals = 'card' WHERE id = ?", (run.id,)
    )

    await harness.supervisor.supervise()

    assert declared == []
    assert harness.orca.shown == [worktree.orca_worktree_id]


async def test_a_worktree_that_ps_leaves_out_is_asked_for_and_followed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, worktree, pane = await launched(harness)
    listed = harness.orca.worktree_agents

    async def truncated() -> dict[str, list[AgentRow]]:
        return {
            key: rows for key, rows in (await listed()).items() if key != worktree.orca_worktree_id
        }

    monkeypatch.setattr(harness.orca, "worktree_agents", truncated)
    harness.orca.set_agent(worktree.orca_worktree_id, "working", pane)

    await harness.supervisor.supervise()

    assert harness.orca.shown == [worktree.orca_worktree_id]
    assert harness.run(run.id).waiting_reason is not WaitingReason.UNKNOWN_STATE


async def test_a_deleted_worktree_folder_is_handed_to_a_person(harness: Harness) -> None:
    run, worktree, _ = await launched(harness)

    shutil.rmtree(worktree.path)

    await harness.supervisor.supervise()

    gone = harness.run(run.id)

    assert gone.waiting_reason is WaitingReason.UNKNOWN_STATE
    assert gone.error_message == f"worktree {worktree.path} is gone"
    assert harness.orca.shown == []


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

    # Only a card run asks Orca for its worktree in each cycle.

    harness.connection.execute(
        "UPDATE automation_runs SET signals = 'card' WHERE id = ?", (first.id,)
    )
    harness.orca.show_worktree = broken_first  # type: ignore[method-assign]
    harness.orca.set_agent(second_worktree.orca_worktree_id, "waiting", second_pane)

    await harness.supervisor.supervise()

    assert harness.run(first.id).status is RunStatus.RUNNING
    assert harness.run(second.id).waiting_reason is WaitingReason.AGENT_QUESTION


async def test_a_lost_agent_starts_again_once_and_then_the_run_waits(harness: Harness) -> None:
    run, _, _ = await launched(harness)

    harness.orca.agents.clear()

    for _ in range(MISSING_CYCLES):
        await harness.supervisor.supervise()

    assert len(harness.orca.launches) == 2
    assert harness.run(run.id).status is RunStatus.RUNNING
    assert harness.run(run.id).restarts == 1

    harness.orca.agents.clear()

    for _ in range(MISSING_CYCLES):
        await harness.supervisor.supervise()

    assert len(harness.orca.launches) == 2
    assert harness.run(run.id).waiting_reason is WaitingReason.UNKNOWN_STATE


async def test_a_stopped_agent_is_told_to_continue_once_and_then_the_run_waits(
    harness: Harness,
) -> None:
    run, worktree, pane = await launched(harness)
    session = harness.sessions.find_launched(run.id)

    assert session is not None

    harness.orca.set_agent(
        worktree.orca_worktree_id, "done", pane, datetime.now(UTC) + timedelta(seconds=1)
    )

    await harness.supervisor.supervise()

    assert harness.orca.prompts == [(session.agent_handle, CONTINUE_PROMPT)]
    assert harness.run(run.id).status is RunStatus.RUNNING

    harness.orca.set_agent(
        worktree.orca_worktree_id, "done", pane, datetime.now(UTC) + timedelta(seconds=2)
    )

    await harness.supervisor.supervise()

    assert len(harness.orca.prompts) == 1
    assert harness.run(run.id).waiting_reason is WaitingReason.UNKNOWN_STATE


async def test_the_hook_state_wins_over_the_state_in_orca(harness: Harness) -> None:
    run, worktree, pane = await launched(harness)
    question = {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "A or B?"}]},
    }

    write_state(harness.agent_states_dir, pane, question)
    await harness.supervisor.supervise()

    waiting = harness.run(run.id)

    assert waiting.waiting_reason is WaitingReason.AGENT_QUESTION
    assert waiting.error_message == "the agent is waiting for an answer: A or B?"


async def test_an_ended_session_is_a_lost_agent(tmp_path: Path) -> None:
    harness = Harness(tmp_path, settings(tmp_path, agents={"max_restarts": 0}))
    run, worktree, pane = await launched(harness)

    write_state(harness.agent_states_dir, pane, {"hook_event_name": "SessionEnd"})

    for _ in range(MISSING_CYCLES):
        await harness.supervisor.supervise()

    assert harness.run(run.id).error_message == f"no agent running in {worktree.path}"


async def test_an_approval_from_before_the_review_does_not_approve_the_plan(
    harness: Harness,
) -> None:
    run, worktree, pane = await launched(harness)

    harness.runs.approve(run.id)
    (worktree.path / "CLAUDE-PLAN.md").write_text("plan")
    harness.runs.declare(run.id, "plan")
    harness.orca.set_agent(worktree.orca_worktree_id, "done", pane)

    await harness.supervisor.supervise()
    await harness.supervisor.supervise()

    assert harness.run(run.id).waiting_reason is WaitingReason.PLAN_REVIEW
    assert len(harness.orca.launches) == 1

    harness.runs.approve(run.id)
    await harness.supervisor.supervise()

    assert len(harness.orca.launches) == 2


async def test_the_steps_of_a_run_are_events_and_the_approval_is_consumed(
    harness: Harness,
) -> None:
    run, worktree, pane = await launched(harness)

    (worktree.path / "CLAUDE-PLAN.md").write_text("plan")
    harness.runs.declare(run.id, "plan")
    harness.orca.set_agent(worktree.orca_worktree_id, "done", pane)
    await harness.supervisor.supervise()
    harness.runs.approve(run.id)
    await harness.supervisor.supervise()

    events = [(event.kind, event.detail) for event in harness.runs.events(run.id)]
    pending = harness.connection.execute(
        "SELECT kind FROM run_commands WHERE consumed_at IS NULL"
    ).fetchall()

    assert events[0] == ("created", "In Progress (bug_fix)")
    assert ("declared", "plan") in events
    assert (
        "status",
        f"WAITING_FOR_HUMAN plan_review: review {worktree.path / 'CLAUDE-PLAN.md'}",
    ) in events
    assert ("request", "approve") in events
    assert events[-1] == ("status", "RUNNING")
    assert pending == []


async def test_a_run_from_before_declarations_reads_the_card(harness: Harness) -> None:
    run, worktree, pane = await launched(harness)

    harness.connection.execute(
        "UPDATE automation_runs SET signals = 'card' WHERE id = ?", (run.id,)
    )
    (worktree.path / "CLAUDE-PLAN.md").write_text("plan")
    harness.orca.set_card(worktree.orca_worktree_id, PLAN_REVIEW)
    harness.orca.set_agent(worktree.orca_worktree_id, "done", pane)

    await harness.supervisor.supervise()

    assert harness.run(run.id).waiting_reason is WaitingReason.PLAN_REVIEW
