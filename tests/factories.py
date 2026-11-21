"""Builders for JIRA REST payloads, settings and Orca profiles used across tests."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spyhop.config.settings import Settings
from spyhop.domain.enums import WorktreeStatus
from spyhop.domain.models import Worktree


def settings(tmp_path: Path, **overrides: Any) -> Settings:
    data: dict[str, Any] = {
        "jira": {"base_url": "https://example.atlassian.net"},
        "repositories": {"portal": {"path": str(tmp_path / "portal")}},
        "projects": {
            "LPD": {
                "workflows": {
                    "Bug": {
                        "name": "bug_fix",
                        "trigger_statuses": ["In Progress"],
                        "repository": "portal",
                        "prompt": "bug_fix.md",
                    },
                    "Story": {
                        "name": "feature",
                        "trigger_statuses": ["In Development"],
                        "repository": "portal",
                        "base_branch": "release",
                        "prompt": "feature.md",
                    },
                }
            },
            "OFF": {"enabled": False, "workflows": {}},
        },
        "service": {"env_file": str(tmp_path / "env")},
    }
    data.update(overrides)

    return Settings(**data)


def history(
    history_id: str, created: str, to_status: str, from_status: str = "Open"
) -> dict[str, Any]:
    return {
        "id": history_id,
        "created": created,
        "items": [
            {"field": "assignee", "fromString": None, "toString": "Someone"},
            {"field": "status", "fromString": from_status, "toString": to_status},
        ],
    }


def issue(
    key: str = "LPD-1",
    issue_type: str = "Bug",
    status: str = "In Progress",
    histories: list[dict[str, Any]] | None = None,
    changelog_total: int | None = None,
) -> dict[str, Any]:
    histories = histories or []

    return {
        "key": key,
        "fields": {
            "assignee": {"accountId": "abc"},
            "issuetype": {"name": issue_type},
            "project": {"key": key.split("-")[0]},
            "status": {"name": status, "statusCategory": {"key": "indeterminate"}},
            "summary": f"Summary of {key}",
            "updated": "2026-11-01T10:00:00.000+0100",
        },
        "changelog": {
            "histories": histories,
            "maxResults": len(histories),
            "startAt": 0,
            "total": len(histories) if changelog_total is None else changelog_total,
        },
    }


def worktree_record(tmp_path: Path, key: str = "LPD-1") -> Worktree:
    path = tmp_path / "agents" / key / "portal"

    return Worktree(
        id="worktree-1",
        issue_key=key,
        orca_worktree_id=f"repo::{path}",
        repository_path=tmp_path / "portal",
        path=path,
        branch_name=key,
        base_branch="master",
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
        status=WorktreeStatus.REMOVING,
        cleanup_error=None,
        removed_at=None,
    )
