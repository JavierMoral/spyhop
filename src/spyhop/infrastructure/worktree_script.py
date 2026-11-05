"""Runs the scripts that create a ticket's worktree in place of Orca and
clean up after Orca removes it.

Both get what to work on in SPYHOP_* environment variables and run from the
repository; anything they want to say goes to standard error. Both may run
again for the same worktree after an interruption, so they must be idempotent.

The creation script prints the worktree's absolute path as the last line of
its standard output, and answers with the existing worktree when it is
already there. The cleanup script runs once Orca has removed the worktree, to
remove what lives next to it; a folder that is already gone is not an error.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from spyhop.domain.exceptions import SpyhopError
from spyhop.domain.models import Worktree
from spyhop.infrastructure.process.command_runner import (
    CommandResult,
    CommandRunner,
    CommandTimeoutError,
    SubprocessRunner,
)

# Characters of the script's standard error kept in the failure message.

MAX_DETAIL_LENGTH = 500


class WorktreeScriptError(SpyhopError):
    pass


@dataclass(frozen=True)
class WorktreeRequest:
    issue_key: str
    name: str
    branch: str
    base_branch: str
    repository_path: Path


async def run_worktree_script(
    script: Path,
    request: WorktreeRequest,
    time_limit: float,
    runner: CommandRunner | None = None,
) -> Path:
    completed = await _run(
        script,
        {
            "SPYHOP_ISSUE_KEY": request.issue_key,
            "SPYHOP_WORKTREE_NAME": request.name,
            "SPYHOP_BRANCH": request.branch,
            "SPYHOP_BASE_BRANCH": request.base_branch,
            "SPYHOP_REPOSITORY_PATH": str(request.repository_path),
        },
        request.repository_path,
        time_limit,
        runner,
    )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]

    if not lines:
        raise WorktreeScriptError(f"worktree script {script} printed no worktree path")

    path = Path(lines[-1])

    if not path.is_absolute():
        raise WorktreeScriptError(
            f"worktree script {script} printed {lines[-1]!r}, not an absolute path"
        )

    if not (path / ".git").exists():
        raise WorktreeScriptError(f"worktree script {script} printed {path}, not a Git worktree")

    return path


async def run_cleanup_script(
    script: Path,
    worktree: Worktree,
    time_limit: float,
    runner: CommandRunner | None = None,
) -> None:
    await _run(
        script,
        {
            "SPYHOP_ISSUE_KEY": worktree.issue_key,
            "SPYHOP_WORKTREE_PATH": str(worktree.path),
            "SPYHOP_BRANCH": worktree.branch_name,
            "SPYHOP_REPOSITORY_PATH": str(worktree.repository_path),
        },
        worktree.repository_path,
        time_limit,
        runner,
    )


async def _run(
    script: Path,
    variables: dict[str, str],
    cwd: Path,
    time_limit: float,
    runner: CommandRunner | None,
) -> CommandResult:
    try:
        completed = await (runner or SubprocessRunner()).run(
            [str(script)], time_limit, cwd=cwd, env={**os.environ, **variables}
        )
    except CommandTimeoutError as error:
        raise WorktreeScriptError(f"worktree script {script} gave {error}") from None
    except OSError as error:
        raise WorktreeScriptError(f"cannot run worktree script {script}: {error}") from None

    if completed.exit_code != 0:
        detail = completed.stderr.strip()[-MAX_DETAIL_LENGTH:]

        raise WorktreeScriptError(
            f"worktree script {script} exited with {completed.exit_code}: {detail}"
        )

    return completed
