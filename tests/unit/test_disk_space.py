from pathlib import Path

import pytest

from spyhop.application.janitor import TICKET_CLOSED
from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.infrastructure.disk import free_bytes
from tests.factories import settings
from tests.fakes import Harness


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    harness = Harness(tmp_path, settings(tmp_path, application={"min_free_disk_gb": 30}))
    harness.config.application.max_concurrent_agents = 2
    harness.launcher._max_concurrent_agents = 2

    return harness


async def test_no_worktree_is_created_below_the_threshold(harness: Harness) -> None:
    harness.free_gb = 12.5
    run = harness.discover()

    assert await harness.launcher.launch_pending() == []

    waiting = harness.run(run.id)

    assert waiting.status is RunStatus.DISCOVERED
    assert waiting.waiting_reason is WaitingReason.DISK_SPACE
    assert waiting.error_message is not None
    assert "12.5 GB free" in waiting.error_message and "30 GB required" in waiting.error_message
    assert harness.orca.created == []
    assert harness.tickets.calls == []  # no JIRA request while waiting
    assert harness.measured == [harness.orca.root / "wt"]  # Orca's worktree directory


async def test_waiting_run_resumes_once_space_is_freed(harness: Harness) -> None:
    harness.free_gb = 10
    run = harness.discover()

    await harness.launcher.launch_pending()
    await harness.launcher.launch_pending()

    assert harness.run(run.id).status is RunStatus.DISCOVERED

    harness.free_gb = 31

    [launched] = await harness.launcher.launch_pending()

    resumed = harness.run(launched.id)

    assert resumed.status is RunStatus.RUNNING
    assert resumed.waiting_reason is None
    assert resumed.error_message is None


async def test_runs_reusing_a_worktree_do_not_wait(harness: Harness) -> None:
    first = harness.discover("LPD-1")

    await harness.launcher.launch_pending()
    harness.runs.mark_failed(first.id, "boom")

    harness.free_gb = 1
    blocked = harness.discover("LPD-2")
    retried = harness.runs.create_retry(harness.run(first.id))

    assert retried is not None

    [launched] = await harness.launcher.launch_pending()

    assert launched.id == retried.id
    assert harness.run(blocked.id).waiting_reason is WaitingReason.DISK_SPACE


async def test_waiting_run_holds_no_agent_slot(harness: Harness) -> None:
    harness.config.application.max_concurrent_agents = 1
    harness.launcher._max_concurrent_agents = 1
    harness.free_gb = 1

    harness.discover("LPD-1")
    await harness.launcher.launch_pending()

    assert harness.runs.count_by_status(RunStatus.RUNNING, RunStatus.WAITING_FOR_HUMAN) == 0


async def test_zero_disables_the_check(tmp_path: Path) -> None:
    harness = Harness(tmp_path, settings(tmp_path, application={"min_free_disk_gb": 0}))
    harness.free_gb = 0
    harness.discover()

    assert len(await harness.launcher.launch_pending()) == 1
    assert harness.measured == []


async def test_waiting_run_of_a_closed_ticket_is_cancelled(harness: Harness) -> None:
    harness.free_gb = 1
    run = harness.discover()

    await harness.launcher.launch_pending()
    harness.tickets.closed.add("LPD-1")
    await harness.janitor.sweep()

    cancelled = harness.run(run.id)

    assert cancelled.status is RunStatus.CANCELLED
    assert cancelled.error_message == TICKET_CLOSED


def test_free_bytes_measures_the_nearest_existing_directory(tmp_path: Path) -> None:
    assert free_bytes(tmp_path / "not" / "created" / "yet") == free_bytes(tmp_path)
