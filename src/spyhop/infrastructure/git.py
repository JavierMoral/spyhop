"""The few local Git questions Orca does not answer. Read-only. A Git command
on a large repository can take seconds, so the async code runs these
functions in a thread (`asyncio.to_thread`)."""

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


def head(worktree: Path) -> str | None:
    """The full SHA of HEAD, or None when Git cannot tell."""

    return _rev_parse(worktree, "HEAD")


def top_level(path: Path) -> Path | None:
    """The root of the worktree that holds ``path``, or None outside Git."""

    found = _rev_parse(path, "--show-toplevel")

    return Path(found) if found else None


def merge_base(worktree: Path, base_branch: str) -> str | None:
    """The SHA where HEAD leaves the base branch, or None when Git cannot
    tell. The base branch is the first of ``upstream/<base_branch>``,
    ``origin/<base_branch>`` and ``<base_branch>`` that exists."""

    for ref in (f"upstream/{base_branch}", f"origin/{base_branch}", base_branch):
        if _git(worktree, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}") is not None:
            return _git(worktree, "merge-base", ref, "HEAD")

    return None


def _rev_parse(path: Path, argument: str) -> str | None:
    return _git(path, "rev-parse", argument)


def _git(path: Path, *arguments: str) -> str | None:
    """The output of a read-only Git command, or None when it fails."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(path), *arguments],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if completed.returncode != 0:
        return None

    return completed.stdout.strip() or None
