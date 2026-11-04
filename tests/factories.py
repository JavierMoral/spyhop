"""Builders for JIRA REST payloads, settings and Orca profiles used across tests."""

import json
import sqlite3
from pathlib import Path
from typing import Any

from spyhop.config.settings import Settings


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


DEFAULT_COLUMNS = [
    {"id": "todo", "label": "Todo"},
    {"id": "in-progress", "label": "In progress"},
    {"id": "in-review", "label": "In review"},
    {"id": "completed", "label": "Done"},
]


def orca_dir(
    tmp_path: Path, columns: list[dict[str, str]] | None = None, profile: str = "local-default"
) -> Path:
    directory = tmp_path / "orca"
    profile_dir = directory / "profiles" / profile

    profile_dir.mkdir(parents=True)
    (directory / "orca-profile-index.json").write_text(json.dumps({"activeProfileId": profile}))

    connection = sqlite3.connect(profile_dir / "profile-state.db")

    connection.execute("CREATE TABLE profile_state_documents (domain TEXT, payload TEXT)")
    connection.execute(
        "INSERT INTO profile_state_documents VALUES ('ui', ?)",
        (json.dumps({"workspaceStatuses": DEFAULT_COLUMNS if columns is None else columns}),),
    )
    connection.commit()
    connection.close()

    return directory


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
