"""Maps JIRA REST payloads to domain objects."""

from datetime import datetime
from typing import Any

from spyhop.domain.models import JiraIssue, StatusChange, TicketDetails, TicketRef

SEARCH_FIELDS = ["assignee", "issuetype", "project", "status", "summary", "updated"]

TICKET_FIELDS = ["issuetype", "parent", "status", "summary"]

ANCESTOR_FIELDS = ["issuetype", "parent", "summary"]


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
    issue: dict[str, Any], base_url: str, ancestors: tuple[TicketRef, ...] = ()
) -> TicketDetails:
    fields = issue["fields"]

    return TicketDetails(
        key=issue["key"],
        url=f"{base_url.rstrip('/')}/browse/{issue['key']}",
        summary=fields.get("summary") or "",
        issue_type=(fields.get("issuetype") or {}).get("name", ""),
        status=(fields.get("status") or {}).get("name", ""),
        ancestors=ancestors,
    )


def to_ticket_ref(issue: dict[str, Any]) -> TicketRef:
    fields = issue["fields"]

    return TicketRef(
        key=issue["key"],
        issue_type=(fields.get("issuetype") or {}).get("name", ""),
        summary=fields.get("summary") or "",
    )


def parent_key(issue: dict[str, Any]) -> str | None:
    parent = issue["fields"].get("parent") or {}
    key = parent.get("key")

    return key if isinstance(key, str) and key else None


# Fields of `spyhop ticket` that say nothing about the work.

NOISE_FIELDS = {
    "aggregateprogress",
    "comment",
    "creator",
    "issuerestriction",
    "lastViewed",
    "progress",
    "statusCategory",
    "statuscategorychangedate",
    "votes",
    "watches",
    "workratio",
    "worklog",
}

NOISE_NAMES = {"Development", "Rank"}


def ticket_text(issue: dict[str, Any]) -> str:
    """The ticket as text for an agent: each field with a value, by its
    display name, and then the comments. ``issue`` comes from
    ``fields=*all`` and ``expand=names``."""

    fields = issue.get("fields") or {}
    names = issue.get("names") or {}
    lines = [f"# {issue.get('key')}: {fields.get('summary', '')}"]

    # The description first: it is where most tickets give the work.

    for key, value in sorted(fields.items(), key=lambda item: item[0] != "description"):
        name = names.get(key, key)

        if key in NOISE_FIELDS or key == "summary" or name in NOISE_NAMES:
            continue

        text = field_text(value).strip()

        if text:
            lines += ["", f"## {name}", "", text]

    comment_page = fields.get("comment") or {}
    comments = comment_page.get("comments") or []
    total = int(comment_page.get("total", len(comments)))

    if comments:
        lines += ["", "## Comments"]

    if total > len(comments):
        lines += ["", f"JIRA gave only {len(comments)} of the {total} comments."]

    for comment in comments:
        author = (comment.get("author") or {}).get("displayName", "?")

        lines += ["", f"### {author}, {comment.get('created', '')}", ""]
        lines.append(field_text(comment.get("body")).strip())

    return "\n".join(lines) + "\n"


def field_text(value: Any) -> str:
    """A field value as text. Rich text (ADF) becomes plain text."""

    if value is None or value == {}:
        return ""

    if isinstance(value, list):
        return "\n".join(text for item in value if (text := field_text(item).strip()))

    if isinstance(value, dict):
        if value.get("type") == "doc":
            return _adf_text(value)

        for key in ("displayName", "name", "value", "key", "filename"):
            if isinstance(value.get(key), str):
                return str(value[key])

        return ""

    return str(value)


# ADF nodes whose children are blocks, one on each line.

_BLOCKS = {"blockquote", "bulletList", "doc", "listItem", "panel", "table"}


def _adf_text(node: dict[str, Any]) -> str:
    node_type = node.get("type")
    attrs = node.get("attrs") or {}
    children = node.get("content") or []

    if node_type == "text":
        return str(node.get("text", ""))

    if node_type == "hardBreak":
        return "\n"

    if node_type in ("mention", "emoji", "status", "date"):
        return str(attrs.get("text") or attrs.get("shortName") or attrs.get("timestamp") or "")

    if node_type in ("inlineCard", "blockCard", "embedCard"):
        return str(attrs.get("url", ""))

    if node_type == "codeBlock":
        return "```\n" + "".join(_adf_text(child) for child in children) + "\n```"

    if node_type == "orderedList":
        # Each item starts with the "- " of a listItem.

        start = int(attrs.get("order", 1))

        return "\n".join(
            f"{number}. {_adf_text(child)[2:]}" for number, child in enumerate(children, start)
        )

    if node_type in _BLOCKS:
        text = "\n".join(_adf_text(child) for child in children)

        return "- " + text.replace("\n", "\n  ") if node_type == "listItem" else text

    if node_type == "tableRow":
        cells = (_adf_text(child).replace("\n", "; ") for child in children)

        return "| " + " | ".join(cells) + " |"

    text = "".join(_adf_text(child) for child in children)

    if node_type == "heading":
        # Below the "##" of the field.

        return "#" * (int(attrs.get("level", 1)) + 2) + " " + text

    return text
