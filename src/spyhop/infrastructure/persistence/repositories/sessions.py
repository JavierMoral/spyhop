"""The agent sessions of a run."""

import sqlite3
import uuid

from spyhop.domain.enums import SessionStatus
from spyhop.domain.models import (
    AgentSession,
)
from spyhop.infrastructure.persistence.repositories._mapping import (
    _to_session,
    _to_text,
    utc_now,
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
        self, run_id: str, provider: str, model: str | None, operation_id: str, role: str
    ) -> AgentSession:
        """Stored before calling Orca, so a launch interrupted at any point is
        retried with the same operation id."""

        session_id = str(uuid.uuid4())

        self._connection.execute(
            """
            INSERT INTO agent_sessions (
                id, run_id, provider, model, launch_operation_id, status, role
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, run_id, provider, model, operation_id, SessionStatus.LAUNCHING, role),
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
