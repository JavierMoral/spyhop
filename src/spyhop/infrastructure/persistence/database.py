"""SQLite storage. The schema is versioned with PRAGMA user_version."""

import sqlite3
from pathlib import Path

_MIGRATIONS = [
    """
    CREATE TABLE worktrees (
        id TEXT PRIMARY KEY,
        issue_key TEXT NOT NULL UNIQUE,
        orca_worktree_id TEXT NOT NULL,
        repository_path TEXT NOT NULL,
        path TEXT NOT NULL,
        branch_name TEXT NOT NULL,
        base_branch TEXT NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL,
        cleanup_error TEXT,
        removed_at TEXT
    );

    CREATE TABLE automation_runs (
        id TEXT PRIMARY KEY,
        issue_key TEXT NOT NULL,
        worktree_id TEXT REFERENCES worktrees(id),
        workflow_name TEXT NOT NULL,
        trigger_status TEXT NOT NULL,
        trigger_history_id TEXT NOT NULL,
        triggered_at TEXT NOT NULL,
        status TEXT NOT NULL,
        waiting_reason TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        error_message TEXT,
        retry_of TEXT REFERENCES automation_runs(id)
    );

    CREATE UNIQUE INDEX idx_one_active_run_per_issue
    ON automation_runs(issue_key)
    WHERE status IN ('DISCOVERED', 'PREPARING', 'READY', 'RUNNING', 'WAITING_FOR_HUMAN');

    CREATE UNIQUE INDEX idx_one_run_per_transition
    ON automation_runs(issue_key, trigger_history_id)
    WHERE retry_of IS NULL;

    CREATE INDEX idx_automation_runs_status ON automation_runs(status);

    CREATE TABLE agent_sessions (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES automation_runs(id),
        provider TEXT NOT NULL,
        model TEXT,
        agent_handle TEXT,
        launch_operation_id TEXT,
        status TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT
    );

    CREATE TABLE issue_observations (
        issue_key TEXT PRIMARY KEY,
        last_status TEXT,
        last_assignee TEXT,
        last_updated_at TEXT,
        last_seen_at TEXT NOT NULL
    );

    CREATE TABLE poller_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        watermark TEXT NOT NULL,
        last_success_at TEXT,
        last_error TEXT
    );
    """,
]


def connect(database_file: Path) -> sqlite3.Connection:
    if str(database_file) != ":memory:":
        database_file.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_file, isolation_level=None)

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")

    _migrate(connection)

    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]

    for index, script in enumerate(_MIGRATIONS[version:], start=version + 1):
        connection.executescript(f"BEGIN; {script}; PRAGMA user_version = {index}; COMMIT;")
