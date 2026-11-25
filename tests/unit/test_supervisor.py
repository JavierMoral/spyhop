"""The supervisor: the cycle that follows each started run."""

import shutil
from datetime import UTC, datetime
from pathlib import Path

from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.models import AutomationRun
from tests.fakes import Harness

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


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


async def test_a_deleted_worktree_folder_is_handed_to_a_person(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    run = harness.discover()

    await harness.launcher.launch_pending()

    worktree = harness.worktrees.find_current("LPD-1")

    assert worktree is not None

    shutil.rmtree(worktree.path)

    await harness.supervisor.supervise()

    gone = harness.run(run.id)

    assert gone.waiting_reason is WaitingReason.UNKNOWN_STATE
    assert gone.error_message == f"worktree {worktree.path} is gone"


async def test_a_run_without_its_workflow_is_handed_to_a_person(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    run = harness.discover()

    await harness.launcher.launch_pending()

    harness.connection.execute("UPDATE automation_runs SET workflow_name = 'gone', pipeline = NULL")

    await harness.supervisor.supervise()

    assert harness.run(run.id).error_message == "workflow 'gone' or its phase 0 is not configured"
