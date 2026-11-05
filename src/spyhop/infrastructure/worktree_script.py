"""Runs the script that creates a ticket's worktree in place of Orca.

The script gets what to create in SPYHOP_* environment variables, runs from the
repository and prints the worktree's absolute path as the last line of its
standard output; anything else it wants to say goes to standard error. A run
interrupted after the script may run it again for the same ticket, so the
script must answer with the existing worktree when it is already there.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from spyhop.infrastructure.process.command_runner import (
    CommandRunner,
    CommandTimeoutError,
    SubprocessRunner,
)

# Characters of the script's standard error kept in the failure message.

MAX_DETAIL_LENGTH = 500


class WorktreeScriptError(Exception):
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
    environment = {
        **os.environ,
        "SPYHOP_ISSUE_KEY": request.issue_key,
        "SPYHOP_WORKTREE_NAME": request.name,
        "SPYHOP_BRANCH": request.branch,
        "SPYHOP_BASE_BRANCH": request.base_branch,
        "SPYHOP_REPOSITORY_PATH": str(request.repository_path),
    }

    try:
        completed = await (runner or SubprocessRunner()).run(
            [str(script)], time_limit, cwd=request.repository_path, env=environment
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
