"""The worktrees of the tickets."""

import sqlite3
import uuid
from pathlib import Path

from spyhop.domain.clock import Clock, utc_now
from spyhop.domain.enums import WorktreeStatus
from spyhop.domain.models import (
    OrcaWorktree,
    Worktree,
)
from spyhop.infrastructure.persistence.repositories._mapping import (
    _to_text,
    _to_worktree,
)


class WorktreeRepository:
    def __init__(self, connection: sqlite3.Connection, now: Clock = utc_now) -> None:
        self._connection = connection
        self._now = now

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
                _to_text(self._now()),
                WorktreeStatus.ACTIVE,
            ),
        )

        worktree = self.get(worktree_id)

        assert worktree is not None

        return worktree

    def find_last_removed(self, issue_key: str) -> Worktree | None:
        row = self._connection.execute(
            "SELECT * FROM worktrees WHERE issue_key = ? AND status = ? "
            "ORDER BY removed_at DESC, rowid DESC LIMIT 1",
            (issue_key, WorktreeStatus.REMOVED),
        ).fetchone()

        return _to_worktree(row) if row else None

    def list_by_status(self, *statuses: WorktreeStatus) -> list[Worktree]:
        placeholders = ", ".join("?" for _ in statuses)
        rows = self._connection.execute(
            f"SELECT * FROM worktrees WHERE status IN ({placeholders}) ORDER BY created_at",
            statuses,
        )

        return [_to_worktree(row) for row in rows]

    def mark_removing(self, worktree_id: str) -> None:
        self._set_status(worktree_id, WorktreeStatus.REMOVING, None)

    def mark_cleanup_blocked(self, worktree_id: str, reason: str) -> None:
        self._set_status(worktree_id, WorktreeStatus.CLEANUP_BLOCKED, reason[:2000])

    def mark_active(self, worktree_id: str) -> None:
        self._set_status(worktree_id, WorktreeStatus.ACTIVE, None)

    def mark_removed(self, worktree_id: str, reason: str | None = None) -> None:
        self._connection.execute(
            "UPDATE worktrees SET status = ?, cleanup_error = ?, removed_at = ? WHERE id = ?",
            (WorktreeStatus.REMOVED, reason, _to_text(self._now()), worktree_id),
        )

    def set_supervisor(self, worktree_id: str, terminal: str | None, pane_key: str | None) -> None:
        self._connection.execute(
            "UPDATE worktrees SET supervisor_terminal = ?, supervisor_pane_key = ? WHERE id = ?",
            (terminal, pane_key, worktree_id),
        )

    def _set_status(self, worktree_id: str, status: WorktreeStatus, error: str | None) -> None:
        self._connection.execute(
            "UPDATE worktrees SET status = ?, cleanup_error = ? WHERE id = ?",
            (status, error, worktree_id),
        )
