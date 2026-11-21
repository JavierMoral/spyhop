"""Why the launcher cannot prepare or start a run now. The launcher and the
worktree provisioner raise them, and the launcher handles them."""

from spyhop.domain.enums import WaitingReason


class RunFailed(Exception):
    """The run cannot start. The launcher marks it FAILED."""


class NotYet(Exception):
    """The run cannot advance now but may next cycle."""


class Waiting(Exception):
    """Like NotYet, but the launcher records the reason on the run for `status`."""

    def __init__(self, reason: WaitingReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
