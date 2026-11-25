"""What a person asks of a run: approve or change the plan, give a message
to its agent, retry a run, change a completed run or give it up. The CLI only
reads the arguments and shows the result.

The board card moves only as a reflection, so a failure of the board is a
warning."""

from collections.abc import Callable
from dataclasses import dataclass

from spyhop.domain.enums import RunStatus, WaitingReason, WorktreeStatus
from spyhop.domain.exceptions import SpyhopError
from spyhop.domain.models import AutomationRun, Worktree
from spyhop.domain.protocols import BoardMirror
from spyhop.infrastructure.persistence.repositories import RunRepository, WorktreeRepository


class RunCommandError(SpyhopError):
    """The command does not apply to the run. The message says why."""


@dataclass(frozen=True)
class CommandResult:
    message: str
    warnings: tuple[str, ...] = ()


class RunCommands:
    def __init__(
        self,
        runs: RunRepository,
        worktrees: WorktreeRepository,
        board: Callable[[], BoardMirror],
        plan_review_status: str,
        plan_approved_status: str,
    ) -> None:
        self._runs = runs
        self._worktrees = worktrees
        self._board = board
        self._statuses = {
            "plan_review_status": plan_review_status,
            "plan_approved_status": plan_approved_status,
        }

    def fail(self, issue_key: str, reason: str) -> CommandResult:
        active = self._active(issue_key)

        for run in active:
            self._runs.mark_failed(run.id, reason)

        return CommandResult(
            "\n".join(f"{run.issue_key}: {run.status} -> FAILED ({reason})" for run in active)
        )

    def retry(self, target: str) -> CommandResult:
        matches = self._runs.find(target)

        if len(matches) != 1:
            raise RunCommandError(
                f"No run matches {target!r}."
                if not matches
                else f"{target!r} matches several runs."
            )

        [original] = matches

        if not original.status.terminal:
            raise RunCommandError(
                f"{original.issue_key}: run {original.id[:8]} is still {original.status}."
            )

        retried = self._runs.create_retry(original)

        if retried is None:
            raise RunCommandError(f"{original.issue_key} already has an active run.")

        return CommandResult(
            f"{retried.issue_key}: run {retried.id[:8]} created as a retry of {original.id[:8]} "
            f"({original.status})"
        )

    def message(self, issue_key: str, text: str) -> CommandResult:
        """A message for the agent of the active run. It goes as the next
        turn of the agent: at once when the run waits, or when the turn that
        runs ends."""

        [run, *_] = self._active(issue_key)

        if not text.strip():
            raise RunCommandError("Give the message.")

        self._runs.add_message(run.id, text)

        when = (
            "on the next cycle"
            if run.status is RunStatus.WAITING_FOR_HUMAN
            else "when the turn that runs ends"
        )

        return CommandResult(f"{run.issue_key}: the agent gets the message {when}.")

    def change(self, issue_key: str, change: str) -> CommandResult:
        """A new run of the completed ticket, where a new implementation agent
        makes the change on the same branch."""

        if not change.strip():
            raise RunCommandError("Give the change.")

        latest = self._runs.find(issue_key.upper())

        if not latest or latest[0].status is not RunStatus.COMPLETED:
            raise RunCommandError(f"{issue_key.upper()} has no active or completed run.")

        [original] = latest
        worktree = self._worktree(original)

        if (
            worktree is None
            or worktree.status is not WorktreeStatus.ACTIVE
            or not worktree.path.exists()
        ):
            raise RunCommandError(f"{original.issue_key}: the worktree of the run is gone.")

        retried = self._runs.create_retry(original, change)

        if retried is None:
            raise RunCommandError(f"{original.issue_key} already has an active run.")

        return CommandResult(
            f"{retried.issue_key}: run {retried.id[:8]} created; a new implementation agent "
            "makes the change, and the checkpoints after the implementation run again."
        )

    async def approve(self, issue_key: str) -> CommandResult:
        run = self._in_plan_review(issue_key)
        worktree = self._worktree(run)

        if worktree is None:
            raise RunCommandError(f"{run.issue_key} has no worktree.")

        warnings: tuple[str, ...] = ()

        try:
            await self._board().set_card_status(
                worktree.orca_worktree_id, self._statuses["plan_approved_status"]
            )
        except SpyhopError as error:
            warnings = (f"{run.issue_key}: the card did not move: {error}",)

        self._runs.approve(run.id)

        return CommandResult(
            f"{run.issue_key}: plan approved; the next phase starts on the next cycle.", warnings
        )

    def replan(self, issue_key: str, changes: str) -> CommandResult:
        run = self._in_plan_review(issue_key)

        if not changes.strip():
            raise RunCommandError("Give the changes to the plan.")

        self._runs.request_plan_changes(run.id, changes)

        return CommandResult(
            f"{run.issue_key}: a new plan agent makes the changes on the next cycle."
        )

    def _active(self, issue_key: str) -> list[AutomationRun]:
        active = self._runs.list(issue_key=issue_key.upper())

        if not active:
            raise RunCommandError(f"{issue_key.upper()} has no active run.")

        return active

    def _in_plan_review(self, issue_key: str) -> AutomationRun:
        active = self._runs.list(issue_key=issue_key.upper())

        if not active or active[0].waiting_reason is not WaitingReason.PLAN_REVIEW:
            raise RunCommandError(f"{issue_key.upper()} has no plan that waits for a review.")

        return active[0]

    def _worktree(self, run: AutomationRun) -> Worktree | None:
        return self._worktrees.get(run.worktree_id) if run.worktree_id else None
