from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from spyhop.domain.enums import RunStatus, WaitingReason


@dataclass(frozen=True)
class JiraUser:
    account_id: str
    display_name: str
    time_zone: str


@dataclass(frozen=True)
class StatusChange:
    """One status item of a JIRA changelog history entry."""

    history_id: str
    created: datetime
    from_status: str | None
    to_status: str


@dataclass(frozen=True)
class JiraIssue:
    key: str
    project_key: str
    issue_type: str
    status: str
    status_category: str
    assignee: str | None
    summary: str
    updated_at: datetime
    status_changes: tuple[StatusChange, ...] = ()


@dataclass(frozen=True)
class ResolvedWorkflow:
    name: str
    trigger_statuses: tuple[str, ...]
    repository_alias: str
    repository_path: Path
    base_branch: str
    prompt: str
    plan_review: bool


@dataclass(frozen=True)
class IssueEntry:
    """An issue entering one of its workflow's trigger statuses."""

    issue: JiraIssue
    workflow: ResolvedWorkflow
    history_id: str
    to_status: str
    entered_at: datetime


@dataclass(frozen=True)
class AutomationRun:
    id: str
    issue_key: str
    worktree_id: str | None
    workflow_name: str
    trigger_status: str
    trigger_history_id: str
    triggered_at: datetime
    status: RunStatus
    waiting_reason: WaitingReason | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    retry_of: str | None
