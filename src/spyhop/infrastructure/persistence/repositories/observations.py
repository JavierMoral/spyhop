"""The tickets that the poller saw."""

import sqlite3

from spyhop.domain.clock import Clock, utc_now
from spyhop.domain.models import (
    JiraIssue,
)
from spyhop.infrastructure.persistence.repositories._mapping import (
    _to_text,
)


class ObservationRepository:
    def __init__(self, connection: sqlite3.Connection, now: Clock = utc_now) -> None:
        self._connection = connection
        self._now = now

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
                _to_text(self._now()),
            ),
        )
