import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spyhop.application.supervisor import MISSING_CYCLES, Observation, Signal, observe
from spyhop.config.settings import REVIEW_STATUS
from spyhop.domain.enums import RunStatus, SessionStatus, WaitingReason
from spyhop.domain.exceptions import OrcaCommandError, OrcaUncertainError
from spyhop.domain.models import AgentRow, AutomationRun, OrcaWorktree, TerminalRow, Worktree
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
        (RUNNING, Observation(REVIEW_STATUS, agent("working"), True, True), Signal.COMPLETED),
        # in-review without CLAUDE-RESULT.md is not the end (and may be a plan column).
        (RUNNING, Observation(REVIEW_STATUS, agent("done", LATER), True, False), Signal.STOPPED),
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
    assert harness.orca.prompts == []


async def test_approving_the_plan_sends_the_implementation_goal(harness: Harness) -> None:
    run, worktree, pane = await plan_ready(harness)

    harness.orca.set_card(worktree.orca_worktree_id, PLAN_APPROVED)

    await harness.supervisor.supervise()

    [prompt] = harness.orca.prompts
    approved = harness.run(run.id)

    assert prompt["terminal"] == "term_1"
    assert prompt["text"].startswith("/goal Implementa CLAUDE-PLAN.md")
    assert f"--workspace-status {REVIEW_STATUS}" in prompt["text"]
    assert approved.status is RunStatus.RUNNING
    assert approved.implement_request_id is None

    # The agent still shows the turn that wrote the plan: not a stop.

    harness.orca.set_agent(
        worktree.orca_worktree_id, "done", pane, datetime.now(UTC) - timedelta(minutes=1)
    )

    await harness.supervisor.supervise()

    assert harness.run(run.id).status is RunStatus.RUNNING
    assert len(harness.orca.prompts) == 1


async def test_lost_approval_answer_is_replayed_with_the_same_request(harness: Harness) -> None:
    run, worktree, _ = await plan_ready(harness)

    harness.orca.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
    harness.orca.prompt_errors.append(OrcaUncertainError("timed out"))

    await harness.supervisor.supervise()

    waiting = harness.run(run.id)

    assert waiting.status is RunStatus.WAITING_FOR_HUMAN
    assert waiting.implement_request_id == harness.orca.prompts[0]["request_id"]

    await harness.supervisor.supervise()

    first, second = harness.orca.prompts

    assert second["request_id"] == first["request_id"]
    assert harness.run(run.id).status is RunStatus.RUNNING


@pytest.mark.parametrize("code", ["request_mismatch", "terminal_handle_stale"])
async def test_refused_approval_is_retried_with_a_new_request(harness: Harness, code: str) -> None:
    run, worktree, _ = await plan_ready(harness)

    harness.orca.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
    harness.orca.prompt_errors.append(OrcaCommandError(code, code))

    await harness.supervisor.supervise()

    assert harness.run(run.id).implement_request_id is None

    await harness.supervisor.supervise()

    first, second = harness.orca.prompts

    assert second["request_id"] != first["request_id"]
    assert harness.run(run.id).status is RunStatus.RUNNING


async def test_approval_waits_for_the_agents_terminal(harness: Harness) -> None:
    run, worktree, _ = await plan_ready(harness)

    harness.orca.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
    harness.orca.terminal_rows.clear()

    await harness.supervisor.supervise()

    assert harness.orca.prompts == []
    assert harness.run(run.id).waiting_reason is WaitingReason.PLAN_REVIEW


async def test_declared_end_completes_the_run_and_frees_its_slot(harness: Harness) -> None:
    run, worktree, pane = await launched(harness)
    queued = harness.discover("LPD-2")

    (worktree.path / "CLAUDE-RESULT.md").write_text("done")
    harness.orca.set_card(worktree.orca_worktree_id, REVIEW_STATUS)
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
    harness.orca.set_card(worktree.orca_worktree_id, REVIEW_STATUS)
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


async def test_approval_uses_the_launch_handle_of_a_terminal_without_a_tab(
    harness: Harness,
) -> None:
    run, worktree, _ = await plan_ready(harness)

    harness.orca.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
    harness.orca.terminal_rows[worktree.orca_worktree_id] = [
        TerminalRow("term_1", "pty:repo::/wt@@1:pty:repo::/wt@@1", "Claude Code")
    ]

    await harness.supervisor.supervise()

    assert [prompt["terminal"] for prompt in harness.orca.prompts] == ["term_1"]


async def test_approval_after_an_orca_restart_finds_the_terminal_by_pane(
    harness: Harness,
) -> None:
    run, worktree, pane = await plan_ready(harness)

    harness.orca.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
    harness.orca.terminal_rows[worktree.orca_worktree_id] = [
        TerminalRow("term_shell", "other:pane", "bash"),
        TerminalRow("term_new", pane, "Claude Code"),
    ]

    await harness.supervisor.supervise()

    assert [prompt["terminal"] for prompt in harness.orca.prompts] == ["term_new"]


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
