"""RunCommands: the logic of approve, declare, replan, retry and fail, without the CLI."""

from pathlib import Path

import pytest

from spyhop.application.run_commands import RunCommandError, RunCommands
from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.exceptions import OrcaUnavailableError
from spyhop.domain.protocols import BoardMirror
from tests.fakes import PLAN_APPROVED, PLAN_REVIEW, Harness


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def commands(harness: Harness, board: BoardMirror | None = None) -> RunCommands:
    def broken() -> BoardMirror:
        raise OrcaUnavailableError("Orca is closed")

    return RunCommands(
        harness.runs,
        harness.worktrees,
        (lambda: board) if board else broken,
        PLAN_REVIEW,
        PLAN_APPROVED,
    )


async def test_approve_stores_the_approval_and_moves_the_card(harness: Harness) -> None:
    run = harness.discover()

    await harness.launcher.launch_pending()

    harness.runs.mark_waiting(run.id, WaitingReason.PLAN_REVIEW)
    worktree = harness.worktrees.find_current("LPD-1")

    assert worktree is not None

    result = await commands(harness, harness.orca).approve("lpd-1")

    assert harness.run(run.id).approved_at is not None
    assert harness.orca.worktrees[worktree.orca_worktree_id].workspace_status == PLAN_APPROVED
    assert result.warnings == ()


async def test_a_board_failure_is_a_warning_except_for_a_card_run(harness: Harness) -> None:
    run = harness.discover()

    await harness.launcher.launch_pending()

    harness.runs.mark_waiting(run.id, WaitingReason.PLAN_REVIEW)

    result = await commands(harness).approve("LPD-1")

    assert result.warnings == ("LPD-1: the card did not move: Orca is closed",)

    harness.connection.execute("UPDATE automation_runs SET signals = 'card'")

    with pytest.raises(RunCommandError, match="plan not approved"):
        await commands(harness).approve("LPD-1")


async def test_declare_needs_a_known_step_and_an_active_run(harness: Harness) -> None:
    with pytest.raises(RunCommandError, match="Declare one of: plan, done"):
        await commands(harness).declare("LPD-1", "maybe")

    with pytest.raises(RunCommandError, match="LPD-1 has no active run"):
        await commands(harness).declare("LPD-1", "plan")

    run = harness.discover()
    result = await commands(harness).declare("LPD-1", "plan")

    assert harness.run(run.id).declared == "plan"
    assert result.message == "LPD-1: plan declared."


def test_replan_needs_a_plan_in_review_and_the_changes(harness: Harness) -> None:
    run = harness.discover()

    with pytest.raises(RunCommandError, match="no plan that waits for a review"):
        commands(harness).replan("LPD-1", "Add a test")

    harness.runs.mark_waiting(run.id, WaitingReason.PLAN_REVIEW)

    with pytest.raises(RunCommandError, match="Give the changes"):
        commands(harness).replan("LPD-1", "  ")

    commands(harness).replan("LPD-1", "Add a test")

    assert harness.run(run.id).plan_changes == "Add a test"


def test_fail_and_retry(harness: Harness) -> None:
    run = harness.discover()

    with pytest.raises(RunCommandError, match="is still DISCOVERED"):
        commands(harness).retry("LPD-1")

    failed = commands(harness).fail("LPD-1", "given up")
    retried = commands(harness).retry("LPD-1")

    assert failed.message == "LPD-1: DISCOVERED -> FAILED (given up)"
    assert harness.run(run.id).status is RunStatus.FAILED
    assert retried.message.endswith(f"as a retry of {run.id[:8]} (FAILED)")
