import builtins
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from spyhop.domain.enums import RunStatus, SessionStatus, WaitingReason, WorktreeStatus
from spyhop.domain.models import (
    AgentSession,
    AutomationRun,
    IssueEntry,
    JiraIssue,
    OrcaWorktree,
    Worktree,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _to_text(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _to_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class RunRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create_discovered(self, entry: IssueEntry) -> AutomationRun | None:
        """Returns None when the transition is already recorded or the issue
        already has an active run."""

        now = utc_now()
        run_id = str(uuid.uuid4())

        try:
            self._connection.execute(
                """
                INSERT INTO automation_runs (
                    id, issue_key, workflow_name, trigger_status, trigger_history_id,
                    triggered_at, status, created_at, status_changed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    entry.issue.key,
                    entry.workflow.name,
                    entry.to_status,
                    entry.history_id,
                    _to_text(entry.entered_at),
                    RunStatus.DISCOVERED,
                    _to_text(now),
                    _to_text(now),
                ),
            )
        except sqlite3.IntegrityError:
            return None

        return self.get(run_id)

    def get(self, run_id: str) -> AutomationRun | None:
        row = self._connection.execute(
            "SELECT * FROM automation_runs WHERE id = ?", (run_id,)
        ).fetchone()

        return _to_run(row) if row else None

    def has_transition(self, issue_key: str, history_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM automation_runs WHERE issue_key = ? AND trigger_history_id = ?",
            (issue_key, history_id),
        ).fetchone()

        return row is not None

    def list(
        self, issue_key: str | None = None, include_terminal: bool = False
    ) -> list[AutomationRun]:
        query = "SELECT * FROM automation_runs"
        conditions = []
        parameters: list[str] = []

        if issue_key:
            conditions.append("issue_key = ?")
            parameters.append(issue_key)

        if not include_terminal:
            conditions.append("status NOT IN (?, ?, ?)")
            parameters.extend(status for status in RunStatus if status.terminal)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY created_at DESC"

        return [_to_run(row) for row in self._connection.execute(query, parameters)]

    def list_by_status(self, *statuses: RunStatus) -> builtins.list[AutomationRun]:
        """Oldest first, the order in which pending runs are launched."""

        placeholders = ", ".join("?" for _ in statuses)
        rows = self._connection.execute(
            f"SELECT * FROM automation_runs WHERE status IN ({placeholders}) "
            "ORDER BY triggered_at, created_at",
            statuses,
        )

        return [_to_run(row) for row in rows]

    def count_by_status(self, *statuses: RunStatus) -> int:
        placeholders = ", ".join("?" for _ in statuses)
        row = self._connection.execute(
            f"SELECT COUNT(*) FROM automation_runs WHERE status IN ({placeholders})", statuses
        ).fetchone()

        return int(row[0])

    def mark_preparing(self, run_id: str) -> None:
        self._update(run_id, RunStatus.PREPARING)

    def mark_ready(self, run_id: str, worktree_id: str) -> None:
        self._update(run_id, RunStatus.READY, worktree_id=worktree_id)

    def mark_running(self, run_id: str) -> None:
        self._update(run_id, RunStatus.RUNNING, started_at=_to_text(utc_now()))

    def mark_resumed(self, run_id: str) -> None:
        """Back to RUNNING after waiting for a person."""

        self._update(
            run_id,
            RunStatus.RUNNING,
            waiting_reason=None,
            error_message=None,
            implement_request_id=None,
        )

    def mark_waiting(self, run_id: str, reason: WaitingReason, message: str | None = None) -> None:
        self._update(
            run_id, RunStatus.WAITING_FOR_HUMAN, waiting_reason=reason, error_message=message
        )

    def mark_completed(self, run_id: str) -> None:
        self._update(
            run_id,
            RunStatus.COMPLETED,
            waiting_reason=None,
            error_message=None,
            implement_request_id=None,
            finished_at=_to_text(utc_now()),
        )

    def set_implement_request(self, run_id: str, request_id: str | None) -> None:
        """Stored before sending the implementation /goal, so a send whose
        answer was lost is replayed instead of repeated."""

        self._connection.execute(
            "UPDATE automation_runs SET implement_request_id = ? WHERE id = ?",
            (request_id, run_id),
        )

    def mark_failed(self, run_id: str, error_message: str) -> None:
        self._update(
            run_id,
            RunStatus.FAILED,
            waiting_reason=None,
            implement_request_id=None,
            error_message=error_message[:2000],
            finished_at=_to_text(utc_now()),
        )

    def _update(self, run_id: str, status: RunStatus, **columns: str | None) -> None:
        columns["status_changed_at"] = _to_text(utc_now())
        assignments = ", ".join(f"{column} = ?" for column in ["status", *columns])

        self._connection.execute(
            f"UPDATE automation_runs SET {assignments} WHERE id = ?",
            (status, *columns.values(), run_id),
        )


class WorktreeRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, worktree_id: str) -> Worktree | None:
        row = self._connection.execute(
            "SELECT * FROM worktrees WHERE id = ?", (worktree_id,)
        ).fetchone()

        return _to_worktree(row) if row else None

    def find_current(self, issue_key: str) -> Worktree | None:
        """The ticket's worktree unless it was removed."""

        row = self._connection.execute(
            "SELECT * FROM worktrees WHERE issue_key = ? AND status != ?",
            (issue_key, WorktreeStatus.REMOVED),
        ).fetchone()

        return _to_worktree(row) if row else None

    def create(
        self,
        issue_key: str,
        orca_worktree: OrcaWorktree,
        repository_path: Path,
        base_branch: str,
    ) -> Worktree:
        worktree_id = str(uuid.uuid4())

        self._connection.execute(
            """
            INSERT INTO worktrees (
                id, issue_key, orca_worktree_id, repository_path, path, branch_name,
                base_branch, created_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                worktree_id,
                issue_key,
                orca_worktree.id,
                str(repository_path),
                str(orca_worktree.path),
                orca_worktree.branch,
                base_branch,
                _to_text(utc_now()),
                WorktreeStatus.ACTIVE,
            ),
        )

        worktree = self.get(worktree_id)

        assert worktree is not None

        return worktree

    def mark_removed(self, worktree_id: str, reason: str | None = None) -> None:
        self._connection.execute(
            "UPDATE worktrees SET status = ?, cleanup_error = ?, removed_at = ? WHERE id = ?",
            (WorktreeStatus.REMOVED, reason, _to_text(utc_now()), worktree_id),
        )


class SessionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def find_launching(self, run_id: str) -> AgentSession | None:
        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE run_id = ? AND status = ?",
            (run_id, SessionStatus.LAUNCHING),
        ).fetchone()

        return _to_session(row) if row else None

    def create_launching(
        self, run_id: str, provider: str, model: str | None, operation_id: str
    ) -> AgentSession:
        """Stored before calling Orca, so a launch interrupted at any point is
        retried with the same operation id."""

        session_id = str(uuid.uuid4())

        self._connection.execute(
            """
            INSERT INTO agent_sessions (
                id, run_id, provider, model, launch_operation_id, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, run_id, provider, model, operation_id, SessionStatus.LAUNCHING),
        )

        session = self.get(session_id)

        assert session is not None

        return session

    def mark_launched(self, session_id: str, agent_handle: str, pane_key: str | None) -> None:
        self._connection.execute(
            """
            UPDATE agent_sessions SET status = ?, agent_handle = ?, pane_key = ?, started_at = ?
            WHERE id = ?
            """,
            (SessionStatus.LAUNCHED, agent_handle, pane_key, _to_text(utc_now()), session_id),
        )

    def mark_finished(self, session_id: str) -> None:
        self._connection.execute(
            "UPDATE agent_sessions SET status = ?, finished_at = ? WHERE id = ?",
            (SessionStatus.FINISHED, _to_text(utc_now()), session_id),
        )

    def find_launched(self, run_id: str) -> AgentSession | None:
        """The run's agent: its latest launched session."""

        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE run_id = ? AND status = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (run_id, SessionStatus.LAUNCHED),
        ).fetchone()

        return _to_session(row) if row else None

    def mark_failed(self, session_id: str) -> None:
        self._connection.execute(
            "UPDATE agent_sessions SET status = ?, finished_at = ? WHERE id = ?",
            (SessionStatus.FAILED, _to_text(utc_now()), session_id),
        )

    def get(self, session_id: str) -> AgentSession | None:
        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
        ).fetchone()

        return _to_session(row) if row else None

    def list_for_run(self, run_id: str) -> list[AgentSession]:
        rows = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE run_id = ? ORDER BY rowid", (run_id,)
        )

        return [_to_session(row) for row in rows]


class PollerStateRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def watermark(self) -> datetime | None:
        row = self._connection.execute("SELECT watermark FROM poller_state WHERE id = 1").fetchone()

        return _to_datetime(row["watermark"]) if row else None

    def last_success_at(self) -> datetime | None:
        row = self._connection.execute(
            "SELECT last_success_at FROM poller_state WHERE id = 1"
        ).fetchone()

        return _to_datetime(row["last_success_at"]) if row else None

    def last_error(self) -> str | None:
        row = self._connection.execute(
            "SELECT last_error FROM poller_state WHERE id = 1"
        ).fetchone()

        return row["last_error"] if row else None

    def record_success(self, watermark: datetime) -> None:
        self._connection.execute(
            """
            INSERT INTO poller_state (id, watermark, last_success_at, last_error)
            VALUES (1, ?, ?, NULL)
            ON CONFLICT (id) DO UPDATE SET
                watermark = excluded.watermark,
                last_success_at = excluded.last_success_at,
                last_error = NULL
            """,
            (_to_text(watermark), _to_text(utc_now())),
        )

    def record_error(self, message: str) -> None:
        self._connection.execute(
            "UPDATE poller_state SET last_error = ? WHERE id = 1", (message[:1000],)
        )


class ObservationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def record(self, issue: JiraIssue) -> None:
        self._connection.execute(
            """
            INSERT INTO issue_observations (
                issue_key, last_status, last_assignee, last_updated_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (issue_key) DO UPDATE SET
                last_status = excluded.last_status,
                last_assignee = excluded.last_assignee,
                last_updated_at = excluded.last_updated_at,
                last_seen_at = excluded.last_seen_at
            """,
            (
                issue.key,
                issue.status,
                issue.assignee,
                _to_text(issue.updated_at),
                _to_text(utc_now()),
            ),
        )


def _to_run(row: sqlite3.Row) -> AutomationRun:
    return AutomationRun(
        id=row["id"],
        issue_key=row["issue_key"],
        worktree_id=row["worktree_id"],
        workflow_name=row["workflow_name"],
        trigger_status=row["trigger_status"],
        trigger_history_id=row["trigger_history_id"],
        triggered_at=datetime.fromisoformat(row["triggered_at"]),
        status=RunStatus(row["status"]),
        waiting_reason=WaitingReason(row["waiting_reason"]) if row["waiting_reason"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=_to_datetime(row["started_at"]),
        finished_at=_to_datetime(row["finished_at"]),
        error_message=row["error_message"],
        retry_of=row["retry_of"],
        status_changed_at=_to_datetime(row["status_changed_at"]),
        implement_request_id=row["implement_request_id"],
    )


def _to_worktree(row: sqlite3.Row) -> Worktree:
    return Worktree(
        id=row["id"],
        issue_key=row["issue_key"],
        orca_worktree_id=row["orca_worktree_id"],
        repository_path=Path(row["repository_path"]),
        path=Path(row["path"]),
        branch_name=row["branch_name"],
        base_branch=row["base_branch"],
        created_at=datetime.fromisoformat(row["created_at"]),
        status=WorktreeStatus(row["status"]),
        cleanup_error=row["cleanup_error"],
        removed_at=_to_datetime(row["removed_at"]),
    )


def _to_session(row: sqlite3.Row) -> AgentSession:
    return AgentSession(
        id=row["id"],
        run_id=row["run_id"],
        provider=row["provider"],
        model=row["model"],
        agent_handle=row["agent_handle"],
        launch_operation_id=row["launch_operation_id"],
        status=SessionStatus(row["status"]),
        started_at=_to_datetime(row["started_at"]),
        finished_at=_to_datetime(row["finished_at"]),
        pane_key=row["pane_key"],
    )
