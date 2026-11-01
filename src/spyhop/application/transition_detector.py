"""Finds issues that entered a trigger status after a point in time.

JQL narrows the candidates; the changelog then gives the exact transition time
and a stable history id, which is what makes detection idempotent.
"""

import logging
from collections.abc import Iterable
from datetime import datetime
from zoneinfo import ZoneInfo

from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.domain.models import IssueEntry, StatusChange
from spyhop.domain.protocols import JiraGateway
from spyhop.infrastructure.jira.mapping import (
    SEARCH_FIELDS,
    changelog_is_complete,
    to_issue,
)

_log = logging.getLogger(__name__)


class TransitionDetector:
    def __init__(self, jira: JiraGateway, resolver: WorkflowResolver, jira_time_zone: str) -> None:
        self._jira = jira
        self._resolver = resolver
        self._jira_time_zone = ZoneInfo(jira_time_zone)

    async def discover_entries(self, since: datetime) -> list[IssueEntry]:
        jql = self.build_jql(since)

        if jql is None:
            _log.warning("No enabled project has workflows; nothing to watch")

            return []

        entries = []

        for payload in await self._jira.search(jql, SEARCH_FIELDS):
            histories = None

            if not changelog_is_complete(payload):
                histories = await self._jira.changelog(payload["key"])

            issue = to_issue(payload, histories)
            workflow = self._resolver.resolve(issue.project_key, issue.issue_type)

            if workflow is None or issue.status not in workflow.trigger_statuses:
                continue

            change = latest_entry(issue.status_changes, workflow.trigger_statuses, since)

            if change is None:
                continue

            entries.append(
                IssueEntry(
                    issue=issue,
                    workflow=workflow,
                    history_id=change.history_id,
                    to_status=change.to_status,
                    entered_at=change.created,
                )
            )

        return entries

    def build_jql(self, since: datetime) -> str | None:
        projects = self._resolver.project_keys()
        statuses = self._resolver.trigger_statuses()

        if not projects or not statuses:
            return None

        # JQL interprets dates in the user's JIRA profile time zone, to the minute.

        after = since.astimezone(self._jira_time_zone).strftime("%Y/%m/%d %H:%M")
        status_list = _jql_list(statuses)

        return (
            f"project in ({_jql_list(projects)}) "
            "AND assignee = currentUser() "
            f"AND status in ({status_list}) "
            f'AND status CHANGED TO ({status_list}) AFTER "{after}" '
            "ORDER BY updated ASC"
        )


def latest_entry(
    changes: Iterable[StatusChange], trigger_statuses: Iterable[str], since: datetime
) -> StatusChange | None:
    triggers = set(trigger_statuses)
    candidates = [
        change for change in changes if change.to_status in triggers and change.created > since
    ]

    return max(candidates, key=lambda change: change.created, default=None)


def _jql_list(values: Iterable[str]) -> str:
    return ", ".join(_jql_string(value) for value in values)


def _jql_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
