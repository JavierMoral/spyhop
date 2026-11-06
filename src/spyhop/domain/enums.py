from enum import StrEnum


class RunStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    PREPARING = "PREPARING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED)


class WaitingReason(StrEnum):
    PLAN_REVIEW = "plan_review"
    AGENT_QUESTION = "agent_question"
    UNKNOWN_STATE = "unknown_state"
    # Not a WAITING_FOR_HUMAN reason: a pending run waiting for free disk
    # space to create its worktree. It resumes by itself.
    DISK_SPACE = "disk_space"
    # Not a WAITING_FOR_HUMAN reason either: a preparing run whose worktree
    # script exited with TRY_AGAIN_EXIT_CODE. It runs again every cycle.
    WORKTREE_SCRIPT = "worktree_script"


class WorktreeStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REMOVING = "REMOVING"
    CLEANUP_BLOCKED = "CLEANUP_BLOCKED"
    REMOVED = "REMOVED"


class SessionStatus(StrEnum):
    LAUNCHING = "LAUNCHING"  # operation id stored, Orca's answer not received yet
    LAUNCHED = "LAUNCHED"
    FINISHED = "FINISHED"  # the agent declared the end of its work
    FAILED = "FAILED"
