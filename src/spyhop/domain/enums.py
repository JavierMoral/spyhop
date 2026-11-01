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


class WorktreeStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REMOVING = "REMOVING"
    CLEANUP_BLOCKED = "CLEANUP_BLOCKED"
    REMOVED = "REMOVED"
