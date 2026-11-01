import sqlite3
import uuid
from datetime import UTC, datetime

from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.models import AutomationRun, IssueEntry, JiraIssue


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
                    triggered_at, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
    )
