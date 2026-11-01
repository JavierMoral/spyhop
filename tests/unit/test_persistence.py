import sqlite3
from pathlib import Path

import pytest

from spyhop.domain.enums import SessionStatus, WorktreeStatus
from spyhop.domain.models import OrcaWorktree
from spyhop.infrastructure.persistence.database import _MIGRATIONS, connect
from spyhop.infrastructure.persistence.repositories import SessionRepository, WorktreeRepository


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
    session = sessions.create_launching("r1", "claude", "sonnet", "1-abc")

    assert sessions.find_launching("r1") == session

    sessions.mark_launched(session.id, "term_1")

    [launched] = sessions.list_for_run("r1")

    assert sessions.find_launching("r1") is None
    assert launched.status is SessionStatus.LAUNCHED
    assert launched.agent_handle == "term_1"
    assert launched.launch_operation_id == "1-abc"
