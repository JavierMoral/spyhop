from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

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


# The agent steps of a pipeline. Spyhop knows what each one does.
STEPS = ("plan", "plan_review", "implement")


@dataclass(frozen=True)
class ChecksPhase:
    """Checkpoints that Spyhop runs at one point of the pipeline. When one
    fails, the run waits for a person (``human``), or the implementation
    agent fixes the findings and the checkpoints run again (``fix``).

    ``rounds`` is the most rounds of the phase with ``fix``. Each round
    counts, also a round that a person or a change of the inputs starts.
    The agent fixes after each failed round except the last one, so
    ``rounds: 3`` gives at most two fixes."""

    checkpoints: tuple[str, ...]
    on_fail: Literal["human", "fix"] = "human"
    rounds: int = 3


Phase = ChecksPhase | str


def validate_pipeline(phases: tuple[Phase, ...]) -> None:
    """Raises ValueError when the pipeline is not one that Spyhop can run."""

    steps = [phase for phase in phases if isinstance(phase, str)]

    for step in steps:
        if step not in STEPS:
            raise ValueError(f"unknown step {step!r}; use checks or one of {', '.join(STEPS)}")

    for step in STEPS:
        if steps.count(step) > 1:
            raise ValueError(f"{step} is in the pipeline more than once")

    if "implement" not in steps:
        raise ValueError("the pipeline has no implement step")

    if ("plan" in steps) != ("plan_review" in steps):
        raise ValueError("plan and plan_review go together")

    if [step for step in STEPS if step in steps] != steps:
        raise ValueError("the steps must be in the order plan, plan_review, implement")

    if "plan" in steps and phases.index("plan_review") != phases.index("plan") + 1:
        raise ValueError("checks between plan and plan_review are not supported")

    implement = phases.index("implement")
    names: list[str] = []

    for index, phase in enumerate(phases):
        if isinstance(phase, str):
            continue

        if phase.on_fail == "fix" and index < implement:
            raise ValueError("on_fail: fix needs checks after the implement step")

        names.extend(phase.checkpoints)

    for name in names:
        if names.count(name) > 1:
            raise ValueError(f"checkpoint {name!r} is in the pipeline more than once")


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
    worktree_script: Path | None = None
    worktree_script_timeout: float = 600
    # Empty: the default pipeline, which plan_review gives.
    pipeline: tuple[Phase, ...] = ()

    @property
    def phases(self) -> tuple[Phase, ...]:
        if self.pipeline:
            return self.pipeline

        return ("plan", "plan_review", "implement") if self.plan_review else ("implement",)

    @property
    def checkpoints(self) -> tuple[str, ...]:
        """All the checkpoints of the pipeline, in its order."""

        return tuple(
            name
            for phase in self.phases
            if isinstance(phase, ChecksPhase)
            for name in phase.checkpoints
        )

    def checkpoints_around_implement(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The checkpoints before the implement step, and after it."""

        implement = self.phases.index("implement")
        before = self.phases[:implement]
        after = self.phases[implement + 1 :]

        return (
            tuple(
                name
                for phase in before
                if isinstance(phase, ChecksPhase)
                for name in phase.checkpoints
            ),
            tuple(
                name
                for phase in after
                if isinstance(phase, ChecksPhase)
                for name in phase.checkpoints
            ),
        )


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
    # The index of the pipeline phase that runs now.
    phase: int = 0
    # When Spyhop last asked the agent to fix failed checkpoints. Set while
    # the agent fixes them.
    fix_requested_at: datetime | None = None
    # When a person last asked to run the checkpoints of the phase again.
    checks_requested_at: datetime | None = None
    # Whether that request runs only the failed and stale checkpoints.
    checks_failed_only: bool = False
    # How many times Spyhop relaunched or prompted a lost or stopped agent.
    restarts: int = 0
    # The changes to the plan that a new plan agent makes on the next cycle.
    plan_changes: str | None = None


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
    # The Orca terminal and pane of the supervisor session of the worktree.
    supervisor_terminal: str | None = None
    supervisor_pane_key: str | None = None


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
    # The step of the pipeline that the agent does: plan or implement.
    role: str | None = None


@dataclass(frozen=True)
class CheckpointRun:
    """One checkpoint that Spyhop started in a systemd user unit. The unit
    writes the result of the agent to ``output_file``."""

    id: str
    run_id: str
    phase: int
    round: int
    name: str
    inputs: str  # the fingerprint that the verdict is for
    base: str
    head: str
    output_file: Path
    unit: str | None  # set once the unit started
    status: Literal["RUNNING", "DONE"]
    verdict: Literal["PASS", "FAIL"] | None
    detail: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class OrcaTerminal:
    """A terminal that Orca opened. Orca does not always give the handle or
    the pane."""

    handle: str | None
    pane_key: str | None


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
    # What the agent asks or last said, when Orca gives it.
    question: str | None = None


@dataclass(frozen=True)
class RemoveResult:
    # Orca reports the branch it kept because it is not merged, but not
    # always (not when the branch was the worktree's base), so a missing
    # value does not prove the branch is gone.

    preserved_branch: str | None
