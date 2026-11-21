"""SQLite storage. The schema is versioned with PRAGMA user_version."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
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
    # A ticket reopened after its worktree was removed gets a new worktree, so
    # issue_key is only unique among worktrees that still exist. Phase 1 never
    # wrote this table, so it can be rebuilt without touching references.
    """
    CREATE TABLE worktrees_new (
        id TEXT PRIMARY KEY,
        issue_key TEXT NOT NULL,
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

    INSERT INTO worktrees_new SELECT * FROM worktrees;

    DROP TABLE worktrees;

    ALTER TABLE worktrees_new RENAME TO worktrees;

    CREATE UNIQUE INDEX idx_one_worktree_per_issue
    ON worktrees(issue_key)
    WHERE status != 'REMOVED';

    CREATE INDEX idx_agent_sessions_run ON agent_sessions(run_id);
    """,
    # Supervision: when the run last changed status (agent states older than
    # that are stale), the request id of the implementation /goal sent on plan
    # approval, and the Orca pane of each agent.
    """
    ALTER TABLE automation_runs ADD COLUMN status_changed_at TEXT;

    ALTER TABLE automation_runs ADD COLUMN implement_request_id TEXT;

    ALTER TABLE agent_sessions ADD COLUMN pane_key TEXT;
    """,
    # Pipelines: the phase of the run's pipeline that runs now, when Spyhop
    # last asked the agent to fix the findings of failed checkpoints, when a
    # person last asked to run the checkpoints again, and each checkpoint
    # that Spyhop started.
    """
    ALTER TABLE automation_runs ADD COLUMN phase INTEGER NOT NULL DEFAULT 0;

    ALTER TABLE automation_runs ADD COLUMN fix_requested_at TEXT;

    ALTER TABLE automation_runs ADD COLUMN checks_requested_at TEXT;

    CREATE TABLE checkpoint_runs (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES automation_runs(id),
        phase INTEGER NOT NULL,
        round INTEGER NOT NULL,
        name TEXT NOT NULL,
        inputs TEXT NOT NULL,
        base TEXT NOT NULL,
        head TEXT NOT NULL,
        output_file TEXT NOT NULL,
        terminal TEXT,
        status TEXT NOT NULL,
        verdict TEXT,
        detail TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT
    );

    CREATE INDEX idx_checkpoint_runs_run ON checkpoint_runs(run_id, phase, round);
    """,
    # Supervisor session: the Orca terminal and pane of the Claude session that
    # tells the user about the runs of the worktree.
    """
    ALTER TABLE worktrees ADD COLUMN supervisor_terminal TEXT;

    ALTER TABLE worktrees ADD COLUMN supervisor_pane_key TEXT;
    """,
    # Checkpoints run in systemd user units instead of Orca terminals.
    """
    ALTER TABLE checkpoint_runs RENAME COLUMN terminal TO unit;
    """,
    # The step of the pipeline that each agent does: plan or implement.
    """
    ALTER TABLE agent_sessions ADD COLUMN role TEXT;
    """,
    # Whether the last request to run the checkpoints again is only for the
    # failed and stale checkpoints.
    """
    ALTER TABLE automation_runs ADD COLUMN checks_failed_only INTEGER NOT NULL DEFAULT 0;
    """,
    # How many times Spyhop relaunched or prompted a lost or stopped agent.
    """
    ALTER TABLE automation_runs ADD COLUMN restarts INTEGER NOT NULL DEFAULT 0;
    """,
    # The changes to the plan that a new plan agent makes on the next cycle.
    """
    ALTER TABLE automation_runs ADD COLUMN plan_changes TEXT;
    """,
    # The notices of a run without a worktree, as a JSON list, for its
    # supervisor session once the worktree exists.
    """
    ALTER TABLE automation_runs ADD COLUMN notices TEXT;
    """,
    # No code reads the request id of the implementation /goal any more.
    """
    ALTER TABLE automation_runs DROP COLUMN implement_request_id;
    """,
    # Spyhop data instead of the Orca card: what the agent declared and when,
    # when a person approved the plan, and whether the run still reads the
    # card ("card", the runs from before) or the declarations ("declare").
    """
    ALTER TABLE automation_runs ADD COLUMN declared TEXT;

    ALTER TABLE automation_runs ADD COLUMN declared_at TEXT;

    ALTER TABLE automation_runs ADD COLUMN approved_at TEXT;

    ALTER TABLE automation_runs ADD COLUMN signals TEXT NOT NULL DEFAULT 'card';
    """,
    # The pipeline of the run, as JSON, from when its first phase started:
    # run.phase is an index into it. NULL: a run from before, which reads the
    # pipeline of the current configuration.
    """
    ALTER TABLE automation_runs ADD COLUMN pipeline TEXT;
    """,
    # The supervisor state that a restart of the service must not lose: the
    # cycles without the agent and since when, whether the person knows that
    # the plan agent is closed, and whether a wait did not reach the
    # supervisor session.
    """
    ALTER TABLE automation_runs ADD COLUMN missing_cycles INTEGER NOT NULL DEFAULT 0;

    ALTER TABLE automation_runs ADD COLUMN missing_since TEXT;

    ALTER TABLE automation_runs ADD COLUMN plan_agent_closed_told INTEGER NOT NULL DEFAULT 0;

    ALTER TABLE automation_runs ADD COLUMN alert_pending INTEGER NOT NULL DEFAULT 0;
    """,
    # Requests to the daemon go to run_commands, not to columns of the run:
    # approve, replan (payload: the changes), checks (payload: all or failed)
    # and notice (payload: the text). run_view gives the run with the values
    # that the code reads. run_events records what happened to each run.
    """
    CREATE TABLE run_commands (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES automation_runs(id),
        kind TEXT NOT NULL,
        payload TEXT,
        created_at TEXT NOT NULL,
        consumed_at TEXT
    );

    CREATE INDEX run_commands_by_run ON run_commands(run_id, kind);

    CREATE TABLE run_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES automation_runs(id),
        at TEXT NOT NULL,
        kind TEXT NOT NULL,
        detail TEXT
    );

    CREATE INDEX run_events_by_run ON run_events(run_id, id);

    INSERT INTO run_commands (run_id, kind, created_at)
    SELECT id, 'approve', approved_at
    FROM automation_runs WHERE approved_at IS NOT NULL;

    INSERT INTO run_commands (run_id, kind, payload, created_at)
    SELECT id, 'replan', plan_changes, COALESCE(status_changed_at, created_at)
    FROM automation_runs WHERE plan_changes IS NOT NULL;

    INSERT INTO run_commands (run_id, kind, payload, created_at)
    SELECT id, 'checks', CASE WHEN checks_failed_only THEN 'failed' ELSE 'all' END,
        checks_requested_at
    FROM automation_runs WHERE checks_requested_at IS NOT NULL;

    INSERT INTO run_commands (run_id, kind, payload, created_at)
    SELECT runs.id, 'notice', notice.value, COALESCE(runs.status_changed_at, runs.created_at)
    FROM automation_runs AS runs, json_each(runs.notices) AS notice
    WHERE runs.notices IS NOT NULL AND json_valid(runs.notices);

    ALTER TABLE automation_runs DROP COLUMN approved_at;

    ALTER TABLE automation_runs DROP COLUMN plan_changes;

    ALTER TABLE automation_runs DROP COLUMN checks_requested_at;

    ALTER TABLE automation_runs DROP COLUMN checks_failed_only;

    ALTER TABLE automation_runs DROP COLUMN notices;

    CREATE VIEW run_view AS
    SELECT runs.*,
        (SELECT MAX(created_at) FROM run_commands
            WHERE run_id = runs.id AND kind = 'approve') AS approved_at,
        (SELECT payload FROM run_commands
            WHERE run_id = runs.id AND kind = 'replan' AND consumed_at IS NULL
            ORDER BY id DESC LIMIT 1) AS plan_changes,
        (SELECT MAX(created_at) FROM run_commands
            WHERE run_id = runs.id AND kind = 'checks') AS checks_requested_at,
        COALESCE((SELECT payload = 'failed' FROM run_commands
            WHERE run_id = runs.id AND kind = 'checks'
            ORDER BY id DESC LIMIT 1), 0) AS checks_failed_only
    FROM automation_runs AS runs;
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


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Makes the statements in the block one write: all of them or none.
    BEGIN IMMEDIATE takes the write lock at the start, so another process
    cannot write between a read and a write of the block. A block in a
    transaction that is already open is part of that transaction, and only
    the outer block commits or rolls back."""

    if connection.in_transaction:
        yield

        return

    connection.execute("BEGIN IMMEDIATE")

    try:
        yield
        connection.commit()
    except BaseException:
        # A failed commit also rolls back, so the connection does not stay in
        # a transaction. An error of the rollback does not hide the first one.

        with suppress(sqlite3.Error):
            connection.rollback()

        raise


def _migrate(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]

    for index, script in enumerate(_MIGRATIONS[version:], start=version + 1):
        connection.executescript(f"BEGIN; {script}; PRAGMA user_version = {index}; COMMIT;")
