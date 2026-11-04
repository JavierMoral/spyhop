from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from spyhop.domain.enums import RunStatus, SessionStatus, WaitingReason, WorktreeStatus


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
    setup: str = "inherit"
    build_commands: tuple[str, ...] = ()


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
    status_changed_at: datetime | None = None
    implement_request_id: str | None = None


@dataclass(frozen=True)
class TicketRef:
    key: str
    issue_type: str
    summary: str


@dataclass(frozen=True)
class TicketDetails:
    """What Spyhop tells the agent about a ticket, read right before launching.
    The agent reads the tickets' content from JIRA itself, so it is current.

    Every text field comes from JIRA and is untrusted data, never instructions."""

    key: str
    url: str
    summary: str
    issue_type: str
    status: str
    # Parent, grandparent and so on up to the root.
    ancestors: tuple[TicketRef, ...] = ()


@dataclass(frozen=True)
class Worktree:
    id: str
    issue_key: str
    orca_worktree_id: str
    repository_path: Path
    path: Path
    branch_name: str
    base_branch: str
    created_at: datetime
    status: WorktreeStatus
    cleanup_error: str | None
    removed_at: datetime | None


@dataclass(frozen=True)
class AgentSession:
    id: str
    run_id: str
    provider: str
    model: str | None
    agent_handle: str | None
    launch_operation_id: str | None
    status: SessionStatus
    started_at: datetime | None
    finished_at: datetime | None
    pane_key: str | None = None


@dataclass(frozen=True)
class OrcaWorktree:
    """A worktree as Orca reports it. ``id`` is Orca's ``<repoId>::<path>``."""

    id: str
    path: Path
    branch: str
    base_ref: str | None
    workspace_status: str | None
    comment: str
    unread: bool


@dataclass(frozen=True)
class LaunchResult:
    agent_handle: str
    operation_id: str
    surface: str
    prompt_outcome: str | None
    pane_key: str | None = None


@dataclass(frozen=True)
class AgentRow:
    """One agent of a worktree in `orca worktree ps`."""

    agent_type: str
    state: str
    pane_key: str | None
    has_live_pty: bool
    prompt: str | None
    state_started_at: datetime | None = None


@dataclass(frozen=True)
class TerminalRow:
    """One live terminal in `orca terminal list`."""

    handle: str
    pane_key: str | None
    title: str


@dataclass(frozen=True)
class PromptDelivery:
    request_id: str
    replayed: bool
    stages: tuple[str, ...]


@dataclass(frozen=True)
class RemoveResult:
    # Orca reports the branch it kept because it is not merged, but not
    # always (not when the branch was the worktree's base), so a missing
    # value does not prove the branch is gone.

    preserved_branch: str | None
