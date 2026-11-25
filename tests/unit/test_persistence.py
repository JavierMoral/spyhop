import sqlite3
from pathlib import Path

import pytest

from spyhop.domain.enums import SessionStatus, WaitingReason, WorktreeStatus
from spyhop.domain.models import ChecksPhase, OrcaWorktree
from spyhop.infrastructure.persistence.database import _MIGRATIONS, connect, transaction
from spyhop.infrastructure.persistence.repositories import (
    RunRepository,
    SessionRepository,
    StepRunRepository,
    WorktreeRepository,
)


def orca_worktree(path: str = "/wt/LPD-1") -> OrcaWorktree:
    return OrcaWorktree(
        id=f"repo::{path}",
        path=Path(path),
        branch="me/LPD-1",
        base_ref="master",
        workspace_status="in-progress",
        comment="",
        unread=False,
    )


def test_phase_one_database_is_migrated(tmp_path: Path) -> None:
    database = tmp_path / "spyhop.db"
    legacy = sqlite3.connect(database, isolation_level=None)

    legacy.executescript(f"BEGIN; {_MIGRATIONS[0]}; PRAGMA user_version = 1; COMMIT;")
    legacy.close()

    connection = connect(database)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == len(_MIGRATIONS)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_removed_worktree_does_not_block_a_new_one(tmp_path: Path) -> None:
    worktrees = WorktreeRepository(connect(tmp_path / "spyhop.db"))
    first = worktrees.create("LPD-1", orca_worktree(), Path("/repo"), "master")

    with pytest.raises(sqlite3.IntegrityError):
        worktrees.create("LPD-1", orca_worktree("/wt/LPD-1-2"), Path("/repo"), "master")

    worktrees.mark_removed(first.id)

    second = worktrees.create("LPD-1", orca_worktree("/wt/LPD-1-2"), Path("/repo"), "master")

    assert worktrees.find_current("LPD-1") == second
    assert second.status is WorktreeStatus.ACTIVE
    assert second.branch_name == "me/LPD-1"


def test_launching_session_keeps_its_operation_id(tmp_path: Path) -> None:
    connection = connect(tmp_path / "spyhop.db")

    connection.execute(
        "INSERT INTO automation_runs (id, issue_key, workflow_name, trigger_status, "
        "trigger_history_id, triggered_at, status, created_at) "
        "VALUES ('r1', 'LPD-1', 'bug_fix', 'In Progress', '7', '2026-11-01', 'READY', '2026-11-01')"
    )

    sessions = SessionRepository(connection)
    session = sessions.create_launching("r1", "claude", "sonnet", "1-abc", "plan")

    assert sessions.find_launching("r1") == session

    sessions.mark_launched(session.id, "term_1", "tab:leaf")

    [launched] = sessions.list_for_run("r1")

    assert sessions.find_launching("r1") is None
    assert launched.status is SessionStatus.LAUNCHED
    assert launched.agent_handle == "term_1"
    assert launched.launch_operation_id == "1-abc"
    assert launched.role == "plan"


def test_status_changes_are_timestamped(tmp_path: Path) -> None:
    connection = connect(tmp_path / "spyhop.db")

    connection.execute(
        "INSERT INTO automation_runs (id, issue_key, workflow_name, trigger_status, "
        "trigger_history_id, triggered_at, status, created_at) "
        "VALUES ('r1', 'LPD-1', 'bug_fix', 'In Progress', '7', '2026-11-01', 'RUNNING', "
        "'2026-11-01')"
    )

    runs = RunRepository(connection)

    runs.mark_waiting("r1", WaitingReason.PLAN_REVIEW, "review CLAUDE-PLAN.md")

    waiting = runs.get("r1")

    assert waiting is not None and waiting.status_changed_at is not None

    runs.mark_resumed("r1")

    resumed = runs.get("r1")

    assert resumed is not None
    assert resumed.waiting_reason is None and resumed.error_message is None
    assert resumed.status_changed_at is not None
    assert resumed.status_changed_at >= waiting.status_changed_at


def _running_run(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO automation_runs (id, issue_key, workflow_name, trigger_status, "
        "trigger_history_id, triggered_at, status, created_at) "
        "VALUES ('r1', 'LPD-1', 'bug_fix', 'In Progress', '7', '2026-11-01', 'RUNNING', "
        "'2026-11-01')"
    )


def test_a_status_change_is_not_stored_without_its_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = connect(tmp_path / "spyhop.db")

    _running_run(connection)

    def fail(*_: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("spyhop.infrastructure.persistence.repositories.runs._record_event", fail)

    runs = RunRepository(connection)

    with pytest.raises(sqlite3.OperationalError):
        runs.mark_fix_requested("r1")

    run = runs.get("r1")

    assert run is not None and run.fix_requested_at is None
    assert not connection.in_transaction


def test_a_transaction_keeps_the_writes_of_another_process_out(tmp_path: Path) -> None:
    database = tmp_path / "spyhop.db"
    daemon = connect(database)
    cli = connect(database)

    _running_run(daemon)
    cli.execute("PRAGMA busy_timeout = 0")

    with transaction(daemon):
        with transaction(daemon):
            daemon.execute("UPDATE automation_runs SET restarts = 1")

        assert daemon.in_transaction

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            RunRepository(cli).request_plan_changes("r1", "split the fix")

    RunRepository(cli).request_plan_changes("r1", "split the fix")

    assert RunRepository(daemon).take_plan_changes("r1") == "split the fix"
    assert cli.execute("SELECT restarts FROM automation_runs").fetchone()[0] == 1


def test_the_pipeline_of_a_run_is_stored_and_a_broken_one_is_ignored(tmp_path: Path) -> None:
    connection = connect(tmp_path / "spyhop.db")

    connection.execute(
        "INSERT INTO automation_runs (id, issue_key, workflow_name, trigger_status, "
        "trigger_history_id, triggered_at, status, created_at) "
        "VALUES ('r1', 'LPD-1', 'bug_fix', 'In Progress', '7', '2026-11-01', 'READY', "
        "'2026-11-01')"
    )

    runs = RunRepository(connection)
    pipeline = ("implement", ChecksPhase(("regressions", "security"), "fix", 2))

    runs.set_pipeline("r1", pipeline)

    stored = runs.get("r1")

    assert stored is not None and stored.pipeline == pipeline

    for broken in (
        "not json",
        '[{"checkpoints": 1}]',
        '["implement", {"checkpoints": ["a"], "on_fail": "x"}]',
        '["plan"]',
    ):
        connection.execute("UPDATE automation_runs SET pipeline = ?", (broken,))

        run = runs.get("r1")

        assert run is not None and run.pipeline is None, broken


def test_the_requests_in_columns_move_to_run_commands(tmp_path: Path) -> None:
    database = tmp_path / "spyhop.db"
    legacy = sqlite3.connect(database, isolation_level=None)

    for index, script in enumerate(_MIGRATIONS[:15], start=1):
        legacy.executescript(f"BEGIN; {script}; PRAGMA user_version = {index}; COMMIT;")

    legacy.execute(
        "INSERT INTO automation_runs (id, issue_key, workflow_name, trigger_status, "
        "trigger_history_id, triggered_at, status, created_at, approved_at, plan_changes, "
        "checks_requested_at, checks_failed_only, notices) "
        "VALUES ('r1', 'LPD-1', 'bug_fix', 'In Progress', '7', '2026-11-01', "
        "'WAITING_FOR_HUMAN', '2026-11-01', '2026-11-02T10:00:00+00:00', 'Add a test', "
        "'2026-11-03T10:00:00+00:00', 1, '[\"first\", \"second\"]')"
    )
    legacy.close()

    runs = RunRepository(connect(database))
    run = runs.get("r1")

    assert run is not None
    assert run.approved_at is not None and run.approved_at.day == 2
    assert run.plan_changes == "Add a test"
    assert run.checks_requested_at is not None and run.checks_requested_at.day == 3
    assert run.checks_failed_only
    [(first_id, first), (_, second)] = runs.pending_notices("r1")

    runs.consume_command(first_id)

    assert (first, second) == ("first", "second")
    assert [text for _, text in runs.pending_notices("r1")] == ["second"]

    # The last replan wins, and a replan after the read stays for later.

    runs.request_plan_changes("r1", "Add another test")

    assert runs.take_plan_changes("r1") == "Add another test"

    runs.request_plan_changes("r1", "Rename the class")

    assert runs.get("r1").plan_changes == "Rename the class"  # type: ignore[union-attr]
    assert runs.take_plan_changes("r1") == "Rename the class"
    assert runs.take_plan_changes("r1") is None


def test_a_failed_commit_ends_the_transaction(tmp_path: Path) -> None:
    connection = connect(tmp_path / "spyhop.db")

    _running_run(connection)

    # A deferred foreign key fails at the commit.

    connection.execute("PRAGMA defer_foreign_keys = ON")

    with pytest.raises(sqlite3.IntegrityError), transaction(connection):
        connection.execute("UPDATE automation_runs SET worktree_id = 'none'")

    assert not connection.in_transaction

    RunRepository(connection).count_restart("r1")

    assert sqlite3.connect(tmp_path / "spyhop.db").execute(
        "SELECT restarts, worktree_id FROM automation_runs"
    ).fetchone() == (1, None)


def test_the_checkpoint_rows_become_check_steps(tmp_path: Path) -> None:
    database = tmp_path / "spyhop.db"
    legacy = sqlite3.connect(database, isolation_level=None)

    for index, script in enumerate(_MIGRATIONS[:-1], start=1):
        legacy.executescript(f"BEGIN; {script}; PRAGMA user_version = {index}; COMMIT;")

    _running_run(legacy)
    legacy.execute(
        "INSERT INTO checkpoint_runs (id, run_id, phase, round, name, inputs, base, head, "
        "output_file, status, verdict, started_at) "
        "VALUES ('c1', 'r1', 0, 1, 'security', 'x', 'b', 'h', '/results/c1.json', 'DONE', "
        "'PASS', '2026-11-01T10:00:00+00:00')"
    )
    legacy.close()

    connection = connect(database)
    steps = StepRunRepository(connection)

    # A work row of the same phase and round is not a checkpoint.

    connection.execute(
        "INSERT INTO step_runs (id, run_id, phase, round, name, inputs, base, head, "
        "output_file, status, started_at, kind, session_id, turn) "
        "VALUES ('w1', 'r1', 0, 2, 'implement', '', 'b', 'h', '/results/w1.json', "
        "'DONE', '2026-11-01T11:00:00+00:00', 'work', 's1', 'launch')"
    )

    [row] = steps.list_for_run("r1")

    assert (row.id, row.kind, row.verdict) == ("c1", "check", "PASS")
    assert steps.last_round("r1", 0) == 1
    assert steps.list_round("r1", 0, 2) == []
    assert list(steps.latest("r1")) == ["security"]
    assert steps.get("w1") is not None and steps.get("w1").session_id == "s1"  # type: ignore[union-attr]
