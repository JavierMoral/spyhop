from pathlib import Path

import pytest

from spyhop.application.janitor import TICKET_CLOSED, Cleanup, closed_tickets_jql
from spyhop.config.settings import CleanupSettings
from spyhop.domain.enums import RunStatus, WorktreeStatus
from spyhop.domain.exceptions import (
    JiraError,
    OrcaCommandError,
    OrcaUnavailableError,
    OrcaUncertainError,
)
from spyhop.domain.models import AutomationRun, Worktree
from tests.factories import settings
from tests.fakes import Harness

DIRTY = OrcaCommandError(
    "runtime_error", "Failed to delete worktree at /wt/LPD-1. ?? untracked.txt"
)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


async def launched(harness: Harness, key: str = "LPD-1") -> tuple[AutomationRun, Worktree]:
    run = harness.discover(key)

    await harness.launcher.launch_pending()

    worktree = harness.worktrees.find_current(key)

    assert worktree is not None

    return harness.run(run.id), worktree


def worktree_status(harness: Harness, worktree: Worktree) -> WorktreeStatus:
    current = harness.worktrees.get(worktree.id)

    assert current is not None

    return current.status


def test_closed_tickets_query() -> None:
    assert closed_tickets_jql(["LPD-1", "LPD-2"], CleanupSettings()) == (
        'key in (LPD-1, LPD-2) AND (statusCategory in ("done"))'
    )
    assert (
        closed_tickets_jql(
            ["LPD-1"],
            CleanupSettings(close_status_categories=["done"], close_statuses=['Won"t Fix']),
        )
        == 'key in (LPD-1) AND (statusCategory in ("done") OR status in ("Won\\"t Fix"))'
    )


async def test_closed_ticket_cancels_its_run_and_removes_its_worktree(harness: Harness) -> None:
    run, worktree = await launched(harness)
    queued = harness.discover("LPD-2")

    harness.tickets.closed.add("LPD-1")

    [removed] = await harness.janitor.sweep()
    await harness.launcher.launch_pending()

    cancelled = harness.run(run.id)

    assert removed.id == worktree.id
    assert cancelled.status is RunStatus.CANCELLED
    assert cancelled.error_message == TICKET_CLOSED
    assert cancelled.finished_at is not None
    assert harness.orca.closed_terminals == [worktree.orca_worktree_id]
    assert harness.orca.removed == [worktree.orca_worktree_id]
    assert worktree_status(harness, worktree) is WorktreeStatus.REMOVED
    assert not worktree.path.exists()
    assert harness.run(queued.id).status is RunStatus.RUNNING


async def test_open_tickets_are_left_alone(harness: Harness) -> None:
    run, worktree = await launched(harness)

    assert await harness.janitor.sweep() == []
    assert harness.tickets.queries == ['key in (LPD-1) AND (statusCategory in ("done"))']
    assert harness.run(run.id).status is RunStatus.RUNNING
    assert worktree_status(harness, worktree) is WorktreeStatus.ACTIVE


async def test_nothing_is_asked_without_worktrees(harness: Harness) -> None:
    assert await harness.janitor.sweep() == []
    assert harness.tickets.queries == []


async def test_disabled_cleanup_never_removes(tmp_path: Path) -> None:
    harness = Harness(tmp_path, settings(tmp_path, cleanup={"enabled": False}))
    _, worktree = await launched(harness)

    harness.tickets.closed.add("LPD-1")

    assert await harness.janitor.sweep() == []
    assert worktree_status(harness, worktree) is WorktreeStatus.ACTIVE


async def test_uncommitted_changes_block_the_removal(harness: Harness) -> None:
    _, worktree = await launched(harness)

    harness.tickets.closed.add("LPD-1")
    harness.orca.remove_errors.append(DIRTY)

    assert await harness.janitor.sweep() == []

    blocked = harness.worktrees.get(worktree.id)

    assert blocked is not None
    assert blocked.status is WorktreeStatus.CLEANUP_BLOCKED
    assert blocked.cleanup_error == DIRTY.message
    assert "cambios sin commitear" in harness.orca.cards[worktree.orca_worktree_id]

    # Not retried while the changes are there; retried once they are gone.

    harness.dirty.add(worktree.path)

    assert await harness.janitor.sweep() == []
    assert harness.orca.removed == []

    harness.dirty.clear()

    [removed] = await harness.janitor.sweep()

    assert removed.id == worktree.id
    assert worktree_status(harness, worktree) is WorktreeStatus.REMOVED


async def test_removal_left_halfway_is_resumed(harness: Harness) -> None:
    _, worktree = await launched(harness)

    harness.tickets.closed.add("LPD-1")
    harness.orca.remove_errors.append(OrcaUncertainError("timed out"))

    assert await harness.janitor.sweep() == []
    assert worktree_status(harness, worktree) is WorktreeStatus.REMOVING

    # Resumed even if JIRA cannot be read now.

    harness.tickets.search_error = JiraError("JIRA is down")

    [removed] = await harness.janitor.sweep()

    assert removed.id == worktree.id
    assert worktree_status(harness, worktree) is WorktreeStatus.REMOVED


async def test_worktree_already_gone_from_orca_counts_as_removed(harness: Harness) -> None:
    _, worktree = await launched(harness)

    harness.tickets.closed.add("LPD-1")
    del harness.orca.worktrees[worktree.orca_worktree_id]

    assert len(await harness.janitor.sweep()) == 1
    assert worktree_status(harness, worktree) is WorktreeStatus.REMOVED


async def test_nothing_happens_while_orca_is_unavailable(harness: Harness) -> None:
    run, worktree = await launched(harness)

    harness.tickets.closed.add("LPD-1")
    harness.orca.unavailable = True

    assert await harness.janitor.sweep() == []
    assert harness.run(run.id).status is RunStatus.RUNNING
    assert worktree_status(harness, worktree) is WorktreeStatus.ACTIVE


async def test_jira_errors_postpone_the_check(harness: Harness) -> None:
    _, worktree = await launched(harness)

    harness.tickets.closed.add("LPD-1")
    harness.tickets.search_error = JiraError("JIRA is down")

    assert await harness.janitor.sweep() == []
    assert worktree_status(harness, worktree) is WorktreeStatus.ACTIVE


async def test_clean_up_on_request_removes_the_worktree_of_an_open_ticket(
    harness: Harness,
) -> None:
    run, worktree = await launched(harness)

    assert await harness.remover.clean_up(worktree, "cleaned up by a person") is Cleanup.REMOVED
    assert harness.run(run.id).error_message == "cleaned up by a person"
    assert worktree_status(harness, worktree) is WorktreeStatus.REMOVED


async def test_reopened_ticket_continues_from_its_kept_branch(harness: Harness) -> None:
    first, worktree = await launched(harness)

    harness.tickets.closed.add("LPD-1")
    await harness.janitor.sweep()
    harness.tickets.closed.clear()
    harness.branches.add(worktree.branch_name)
    harness.tickets.summaries["LPD-1"] = "A title changed while closed"

    second = harness.discover()

    await harness.launcher.launch_pending()

    reopened = harness.worktrees.find_current("LPD-1")

    assert harness.run(second.id).status is RunStatus.RUNNING
    assert reopened is not None and reopened.id != worktree.id
    assert harness.orca.bases == ["master", worktree.branch_name]
    assert reopened.base_branch == worktree.branch_name
    # The previous name, so that Orca checks the kept branch out again.
    assert harness.orca.created == [worktree.path.name, worktree.path.name]


async def test_reopened_ticket_starts_over_when_its_branch_was_merged(
    harness: Harness,
) -> None:
    await launched(harness)

    harness.tickets.closed.add("LPD-1")
    await harness.janitor.sweep()
    harness.tickets.closed.clear()

    harness.discover()
    await harness.launcher.launch_pending()

    assert harness.orca.bases == ["master", "master"]


async def test_reopened_ticket_reuses_a_worktree_that_could_not_be_removed(
    harness: Harness,
) -> None:
    _, worktree = await launched(harness)

    harness.tickets.closed.add("LPD-1")
    harness.orca.remove_errors.append(DIRTY)
    await harness.janitor.sweep()
    harness.tickets.closed.clear()

    second = harness.discover()

    await harness.launcher.launch_pending()

    assert harness.run(second.id).status is RunStatus.RUNNING
    assert harness.run(second.id).worktree_id == worktree.id
    assert worktree_status(harness, worktree) is WorktreeStatus.ACTIVE
    assert harness.orca.created == ["LPD-1-summary-of-lpd-1"]


async def test_run_waits_while_its_worktree_is_being_removed(harness: Harness) -> None:
    _, worktree = await launched(harness)

    harness.tickets.closed.add("LPD-1")
    harness.orca.remove_errors.append(OrcaUnavailableError("Orca closed mid-way"))
    await harness.janitor.sweep()

    second = harness.discover()

    assert await harness.launcher.launch_pending() == []
    assert harness.run(second.id).status is RunStatus.PREPARING
    assert worktree_status(harness, worktree) is WorktreeStatus.REMOVING


async def test_retry_creates_a_new_run_on_the_same_worktree(harness: Harness) -> None:
    run, worktree = await launched(harness)

    harness.runs.mark_failed(run.id, "boom")

    failed = harness.run(run.id)
    retried = harness.runs.create_retry(failed)

    assert retried is not None
    assert retried.retry_of == run.id
    assert retried.trigger_history_id == run.trigger_history_id
    assert harness.runs.create_retry(failed) is None  # one active run per ticket

    await harness.launcher.launch_pending()

    assert harness.run(retried.id).status is RunStatus.RUNNING
    assert harness.run(retried.id).worktree_id == worktree.id
    assert harness.runs.find(run.id[:8]) == [harness.run(run.id)]
    assert harness.runs.find("lpd-1") == [harness.run(retried.id)]
