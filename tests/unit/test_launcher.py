import sqlite3
import time
from pathlib import Path

import pytest

from spyhop.application.launcher import worktree_name
from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.domain.enums import RunStatus, SessionStatus, WaitingReason, WorktreeStatus
from spyhop.domain.exceptions import (
    OrcaCommandError,
    OrcaUncertainError,
)
from tests.factories import settings
from tests.fakes import Harness


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


async def test_first_run_creates_the_worktree_writes_context_and_launches(
    harness: Harness,
) -> None:
    run = harness.discover()

    [launched] = await harness.launcher.launch_pending()

    run = harness.run(run.id)
    worktree = harness.worktrees.find_current("LPD-1")
    [session] = harness.sessions.list_for_run(run.id)
    [launch] = harness.orca.launches

    assert launched.id == run.id
    assert run.status is RunStatus.RUNNING
    assert run.started_at is not None
    assert worktree is not None and run.worktree_id == worktree.id
    assert worktree.branch_name == "me/LPD-1-summary-of-lpd-1"
    assert harness.orca.created == ["LPD-1-summary-of-lpd-1"]
    assert (worktree.path / "TASK.md").read_text().startswith("# Task: LPD-1")
    assert (worktree.path / ".claude" / "settings.local.json").exists()
    assert launch["worktree"] == worktree.orca_worktree_id
    assert launch["prompt"].startswith("/goal Lee TASK.md")
    assert launch["operation_id"] == session.launch_operation_id
    assert session.status is SessionStatus.LAUNCHED
    assert session.agent_handle == "term_1"


async def test_task_lists_the_ticket_ancestors_up_to_the_root(harness: Harness) -> None:
    harness.tickets.parents.update({"LPD-1": "LPD-2", "LPD-2": "LPD-3"})
    harness.discover()

    await harness.launcher.launch_pending()

    worktree = harness.worktrees.find_current("LPD-1")

    assert worktree is not None
    assert (
        "- LPD-2 (Bug) · Summary of LPD-2\n- LPD-3 (Bug) · Summary of LPD-3\n"
        in (worktree.path / "TASK.md").read_text()
    )


async def test_hidden_or_looping_parents_end_the_ancestor_chain(harness: Harness) -> None:
    harness.tickets.parents.update({"LPD-1": "LPD-2", "LPD-2": "LPD-1"})
    run = harness.discover()

    await harness.launcher.launch_pending()

    assert harness.tickets.calls == ["LPD-1", "LPD-2"]

    harness.finish(run.id)
    harness.tickets.calls.clear()
    harness.tickets.parents["LPD-1"] = "LPD-9"
    harness.tickets.hidden.add("LPD-9")
    second = harness.discover()

    await harness.launcher.launch_pending()

    assert harness.run(second.id).status is RunStatus.RUNNING


async def test_next_run_of_the_ticket_reuses_its_worktree(harness: Harness) -> None:
    first = harness.discover()

    await harness.launcher.launch_pending()
    harness.finish(first.id)

    second = harness.discover()

    await harness.launcher.launch_pending()

    assert harness.run(second.id).status is RunStatus.RUNNING
    assert harness.run(second.id).worktree_id == harness.run(first.id).worktree_id
    assert harness.orca.created == ["LPD-1-summary-of-lpd-1"]
    assert len(harness.orca.launches) == 2


async def test_worktree_linked_in_orca_is_adopted(harness: Harness) -> None:
    existing = harness.orca.add_worktree("LPD-1", reference="LPD-1")
    run = harness.discover()

    await harness.launcher.launch_pending()

    worktree = harness.worktrees.find_current("LPD-1")

    assert harness.orca.created == []
    assert worktree is not None and worktree.orca_worktree_id == existing.id
    assert harness.run(run.id).status is RunStatus.RUNNING


async def test_worktree_removed_behind_spyhops_back_is_replaced(harness: Harness) -> None:
    first = harness.discover()

    await harness.launcher.launch_pending()
    harness.finish(first.id)

    old = harness.worktrees.find_current("LPD-1")

    assert old is not None

    del harness.orca.worktrees[old.orca_worktree_id]
    del harness.orca.references["LPD-1"]

    second = harness.discover()

    await harness.launcher.launch_pending()

    replacement = harness.worktrees.find_current("LPD-1")

    assert harness.worktrees.get(old.id).status is WorktreeStatus.REMOVED  # type: ignore[union-attr]
    assert replacement is not None and replacement.id != old.id
    assert harness.run(second.id).status is RunStatus.RUNNING


async def test_runs_wait_while_orca_is_unavailable(harness: Harness) -> None:
    run = harness.discover()
    harness.orca.unavailable = True

    assert await harness.launcher.launch_pending() == []
    assert harness.run(run.id).status is RunStatus.DISCOVERED
    assert harness.tickets.calls == []


async def test_lost_launch_answer_is_replayed_with_the_same_operation_id(
    harness: Harness,
) -> None:
    run = harness.discover()
    harness.orca.launch_errors.append(OrcaUncertainError("timed out"))

    assert await harness.launcher.launch_pending() == []
    assert harness.run(run.id).status is RunStatus.READY

    [launching] = harness.sessions.list_for_run(run.id)

    assert launching.status is SessionStatus.LAUNCHING

    await harness.launcher.launch_pending()

    first, second = harness.orca.launches

    assert first["operation_id"] == second["operation_id"] == launching.launch_operation_id
    assert first["prompt"] == second["prompt"]
    assert harness.run(run.id).status is RunStatus.RUNNING
    assert harness.orca.created == ["LPD-1-summary-of-lpd-1"]
    assert len(harness.sessions.list_for_run(run.id)) == 1


async def test_old_lost_launch_is_handed_to_a_person(harness: Harness) -> None:
    run = harness.discover()
    harness.orca.launch_errors.append(OrcaUncertainError("timed out"))

    await harness.launcher.launch_pending()

    stale = f"{int((time.time() - 3600) * 1000)}-{'0' * 32}"

    harness.connection.execute("UPDATE agent_sessions SET launch_operation_id = ?", (stale,))

    await harness.launcher.launch_pending()

    run = harness.run(run.id)

    assert run.status is RunStatus.WAITING_FOR_HUMAN
    assert run.waiting_reason is WaitingReason.UNKNOWN_STATE
    assert len(harness.orca.launches) == 1


async def test_uncertain_worktree_creation_is_found_by_reference_next_time(
    harness: Harness,
) -> None:
    run = harness.discover()
    harness.orca.create_errors.append(OrcaUncertainError("timed out"))

    await harness.launcher.launch_pending()

    assert harness.run(run.id).status is RunStatus.PREPARING

    await harness.launcher.launch_pending()

    assert harness.orca.created == ["LPD-1-summary-of-lpd-1"]
    assert harness.run(run.id).status is RunStatus.RUNNING


async def test_refused_launch_fails_the_run(harness: Harness) -> None:
    run = harness.discover()
    harness.orca.launch_errors.append(OrcaCommandError("agent_not_found", "agent_not_found"))

    await harness.launcher.launch_pending()

    run = harness.run(run.id)
    [session] = harness.sessions.list_for_run(run.id)

    assert run.status is RunStatus.FAILED
    assert run.error_message == "Orca refused the launch: agent_not_found"
    assert run.finished_at is not None
    assert session.status is SessionStatus.FAILED


async def test_busy_slots_hold_pending_runs(harness: Harness) -> None:
    first = harness.discover("LPD-1")
    second = harness.discover("LPD-2")

    launched = await harness.launcher.launch_pending()

    assert [run.id for run in launched] == [first.id]
    assert harness.run(second.id).status is RunStatus.DISCOVERED

    harness.finish(first.id)

    await harness.launcher.launch_pending()

    assert harness.run(second.id).status is RunStatus.RUNNING


async def test_run_whose_workflow_was_removed_fails(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    run = harness.discover()
    harness.launcher._resolver = WorkflowResolver(settings(tmp_path, projects={}))

    await harness.launcher.launch_pending()

    assert harness.run(run.id).status is RunStatus.FAILED
    assert "no longer configured" in (harness.run(run.id).error_message or "")


def test_harness_uses_a_real_database(harness: Harness) -> None:
    assert isinstance(harness.connection, sqlite3.Connection)


def test_worktree_name_is_the_key_and_the_title_as_a_slug() -> None:
    assert worktree_name("LPD-1234", "Completar task") == "LPD-1234-completar-task"
    assert worktree_name("LPD-1", "Corrección: ¿por qué falla el/la import?") == (
        "LPD-1-correccion-por-que-falla-el-la-import"
    )


def test_long_titles_are_cut_at_a_word() -> None:
    name = worktree_name(
        "LPD-1", "The preview selector offers no CMS entries for a display page template"
    )

    assert name == "LPD-1-the-preview-selector-offers-no-cms-entries-for-a"


def test_title_without_letters_or_digits_leaves_the_key() -> None:
    assert worktree_name("LPD-1", " ¿? ") == "LPD-1"
