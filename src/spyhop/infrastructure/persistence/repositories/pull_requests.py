"""The pull requests of the worktrees: a cache, and the events that Spyhop
found and told the supervisor session."""

import sqlite3
from datetime import datetime

from spyhop.domain.clock import Clock, utc_now
from spyhop.domain.models import CachedPullRequest, PullRequest
from spyhop.infrastructure.persistence.repositories._mapping import _to_text


class PullRequestRepository:
    def __init__(self, connection: sqlite3.Connection, now: Clock = utc_now) -> None:
        self._connection = connection
        self._now = now

    def list(self, worktree_id: str) -> list[CachedPullRequest]:
        rows = self._connection.execute(
            "SELECT * FROM pull_requests WHERE worktree_id = ? ORDER BY created_at", (worktree_id,)
        )

        return [
            CachedPullRequest(
                PullRequest(
                    repository=row["repository"],
                    number=row["number"],
                    author=row["author"],
                    head_branch=row["head_branch"],
                    head_sha=row["head_sha"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    state=row["state"],
                ),
                row["forward_of"],
                row["ci"],
                row["verdict"],
            )
            for row in rows
        ]

    def save(self, worktree_id: str, cached: CachedPullRequest) -> None:
        pull_request = cached.pull_request

        self._connection.execute(
            """
            INSERT OR REPLACE INTO pull_requests (
                worktree_id, repository, number, author, head_branch, head_sha, state,
                forward_of, created_at, ci, verdict
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                worktree_id,
                pull_request.repository,
                pull_request.number,
                pull_request.author,
                pull_request.head_branch,
                pull_request.head_sha,
                pull_request.state,
                cached.forward_of,
                _to_text(pull_request.created_at),
                cached.ci,
                cached.verdict,
            ),
        )

    def has_merged(self, worktree_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM pull_requests WHERE worktree_id = ? AND verdict = 'merged'",
            (worktree_id,),
        ).fetchone()

        return row is not None

    def event_state(self, worktree_id: str, kind: str, key: str) -> bool | None:
        """None: not found before. False: found, not told. True: told."""

        row = self._connection.execute(
            "SELECT delivered_at FROM pr_events WHERE worktree_id = ? AND kind = ? AND key = ?",
            (worktree_id, kind, key),
        ).fetchone()

        return None if row is None else row["delivered_at"] is not None

    def record_event(self, worktree_id: str, kind: str, key: str, delivered: bool) -> None:
        now = _to_text(self._now())

        self._connection.execute(
            "INSERT OR IGNORE INTO pr_events (worktree_id, kind, key, detected_at, delivered_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (worktree_id, kind, key, now, now if delivered else None),
        )

    def mark_delivered(self, worktree_id: str, kind: str, key: str) -> None:
        self._connection.execute(
            "UPDATE pr_events SET delivered_at = ? "
            "WHERE worktree_id = ? AND kind = ? AND key = ? AND delivered_at IS NULL",
            (_to_text(self._now()), worktree_id, kind, key),
        )
