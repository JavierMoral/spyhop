from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spyhop.application.transition_detector import TransitionDetector, latest_entry
from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.domain.models import JiraUser, StatusChange
from spyhop.infrastructure.jira.mapping import to_status_changes
from tests.factories import history, issue, settings

SINCE = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


class FakeJira:
    def __init__(
        self,
        issues: list[dict[str, Any]],
        changelogs: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.issues = issues
        self.changelogs = changelogs or {}
        self.jql: list[str] = []
        self.changelog_calls: list[str] = []

    async def myself(self) -> JiraUser:
        return JiraUser("abc", "Me", "Europe/Madrid")

    async def search(self, jql: str, fields: list[str]) -> list[dict[str, Any]]:
        self.jql.append(jql)

        return self.issues

    async def changelog(self, issue_key: str) -> list[dict[str, Any]]:
        self.changelog_calls.append(issue_key)

        return self.changelogs[issue_key]


def detector(tmp_path: Path, jira: FakeJira) -> TransitionDetector:
    return TransitionDetector(jira, WorkflowResolver(settings(tmp_path)), "Europe/Madrid")


def test_jql_uses_enabled_projects_trigger_statuses_and_jira_time_zone(tmp_path: Path) -> None:
    jql = detector(tmp_path, FakeJira([])).build_jql(SINCE)

    assert jql == (
        'project in ("LPD") AND assignee = currentUser() '
        'AND status in ("In Development", "In Progress") '
        'AND status CHANGED TO ("In Development", "In Progress") AFTER "2026/11/01 10:00" '
        "ORDER BY updated ASC"
    )


def test_latest_entry_takes_the_last_reentry_after_since() -> None:
    changes = to_status_changes(
        [
            history("1", "2026-11-01T08:00:00.000+0000", "In Progress"),
            history("2", "2026-11-01T09:30:00.000+0000", "Done", "In Progress"),
            history("3", "2026-11-01T09:45:00.000+0000", "In Progress", "Done"),
        ]
    )

    entry = latest_entry(changes, ["In Progress"], SINCE)

    assert entry is not None
    assert entry.history_id == "3"


def test_latest_entry_ignores_entries_before_since() -> None:
    change = StatusChange("1", datetime(2026, 11, 1, 8, 59, tzinfo=UTC), "Open", "In Progress")

    assert latest_entry([change], ["In Progress"], SINCE) is None


async def test_discovers_entry_with_workflow_and_exact_time(tmp_path: Path) -> None:
    jira = FakeJira(
        [issue("LPD-7", histories=[history("42", "2026-11-01T10:15:30.000+0100", "In Progress")])]
    )

    [entry] = await detector(tmp_path, jira).discover_entries(SINCE)

    assert entry.issue.key == "LPD-7"
    assert entry.history_id == "42"
    assert entry.entered_at == datetime(2026, 11, 1, 9, 15, 30, tzinfo=UTC)
    assert entry.workflow.name == "bug_fix"
    assert entry.workflow.base_branch == "master"
    assert jira.changelog_calls == []


async def test_trigger_statuses_are_checked_per_issue_type(tmp_path: Path) -> None:
    # "In Progress" triggers Bug but not Story, whose trigger is "In Development".

    jira = FakeJira(
        [
            issue(
                "LPD-8",
                issue_type="Story",
                histories=[history("1", "2026-11-01T10:15:00.000+0100", "In Progress")],
            )
        ]
    )

    assert await detector(tmp_path, jira).discover_entries(SINCE) == []


async def test_truncated_changelog_is_fetched_in_full(tmp_path: Path) -> None:
    jira = FakeJira(
        [issue("LPD-9", changelog_total=150)],
        changelogs={"LPD-9": [history("99", "2026-11-01T10:30:00.000+0100", "In Progress")]},
    )

    [entry] = await detector(tmp_path, jira).discover_entries(SINCE)

    assert entry.history_id == "99"
    assert jira.changelog_calls == ["LPD-9"]


async def test_issue_types_without_workflow_are_ignored(tmp_path: Path) -> None:
    jira = FakeJira(
        [
            issue(
                "LPD-10",
                issue_type="Epic",
                histories=[history("1", "2026-11-01T10:15:00.000+0100", "In Progress")],
            )
        ]
    )

    assert await detector(tmp_path, jira).discover_entries(SINCE) == []
