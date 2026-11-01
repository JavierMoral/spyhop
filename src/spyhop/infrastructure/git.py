"""The few local Git questions Orca does not answer. Read-only and fast, so
they run synchronously."""

import subprocess
from pathlib import Path

_TIMEOUT = 30


def branch_exists(repository: Path, branch: str) -> bool:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            capture_output=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    return completed.returncode == 0


def has_changes(worktree: Path) -> bool:
    """Whether `orca worktree rm` would refuse the worktree: modified or
    untracked files, excluded ones aside. An unreadable worktree counts as
    changed, so nothing is retried on a guess."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True

    return completed.returncode != 0 or bool(completed.stdout.strip())
