"""Maps JIRA REST payloads to domain objects."""

from datetime import datetime
from typing import Any

from spyhop.domain.models import JiraComment, JiraIssue, StatusChange, TicketDetails
from spyhop.infrastructure.jira.adf import adf_to_text

SEARCH_FIELDS = ["assignee", "issuetype", "project", "status", "summary", "updated"]

TICKET_FIELDS = ["description", "issuetype", "parent", "status", "summary"]


def parse_jira_datetime(value: str) -> datetime:
    # JIRA sends "2026-11-01T10:15:30.123+0100".

    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def changelog_is_complete(issue: dict[str, Any]) -> bool:
    changelog = issue.get("changelog") or {}
    histories = changelog.get("histories", [])

    return int(changelog.get("total", len(histories))) <= len(histories)


def to_issue(issue: dict[str, Any], histories: list[dict[str, Any]] | None = None) -> JiraIssue:
    fields = issue["fields"]
    status = fields["status"]
    assignee = fields.get("assignee") or {}

    if histories is None:
        histories = (issue.get("changelog") or {}).get("histories", [])

    return JiraIssue(
        key=issue["key"],
        project_key=fields["project"]["key"],
        issue_type=fields["issuetype"]["name"],
        status=status["name"],
        status_category=status.get("statusCategory", {}).get("key", ""),
        assignee=assignee.get("accountId"),
        summary=fields.get("summary") or "",
        updated_at=parse_jira_datetime(fields["updated"]),
        status_changes=to_status_changes(histories),
    )


def to_status_changes(histories: list[dict[str, Any]]) -> tuple[StatusChange, ...]:
    changes = [
        StatusChange(
            history_id=str(history["id"]),
            created=parse_jira_datetime(history["created"]),
            from_status=item.get("fromString"),
            to_status=item["toString"],
        )
        for history in histories
        for item in history.get("items", [])
        if item.get("field") == "status" and item.get("toString")
    ]

    return tuple(sorted(changes, key=lambda change: change.created))


def to_ticket_details(
    issue: dict[str, Any],
    comments: list[dict[str, Any]],
    base_url: str,
    acceptance_criteria_fields: list[str],
) -> TicketDetails:
    fields = issue["fields"]
    parent = fields.get("parent") or {}
    criteria = [adf_to_text(fields.get(field)) for field in acceptance_criteria_fields]

    return TicketDetails(
        key=issue["key"],
        url=f"{base_url.rstrip('/')}/browse/{issue['key']}",
        summary=fields.get("summary") or "",
        issue_type=(fields.get("issuetype") or {}).get("name", ""),
        status=(fields.get("status") or {}).get("name", ""),
        description=adf_to_text(fields.get("description")),
        acceptance_criteria="\n\n".join(text for text in criteria if text),
        comments=tuple(
            JiraComment(
                author=(comment.get("author") or {}).get("displayName", "unknown"),
                created=parse_jira_datetime(comment["created"]),
                body=adf_to_text(comment.get("body")),
            )
            for comment in comments
        ),
        parent_key=parent.get("key"),
        parent_summary=(parent.get("fields") or {}).get("summary"),
    )
