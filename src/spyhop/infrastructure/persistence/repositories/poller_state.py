"""The state of the JIRA poller."""

import sqlite3
from datetime import datetime

from spyhop.infrastructure.persistence.repositories._mapping import (
    _to_datetime,
    _to_text,
    utc_now,
)


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
