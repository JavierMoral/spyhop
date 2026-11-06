"""The worktree script runs for real: small shell scripts in a temporary
directory."""

import stat
from pathlib import Path

import pytest

from spyhop.infrastructure.worktree_script import (
    WorktreeRequest,
    WorktreeScriptError,
    WorktreeScriptTryAgainError,
    run_cleanup_script,
    run_worktree_script,
)
from tests.factories import worktree_record


def script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "create-worktree"
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)

    return path


def request(tmp_path: Path) -> WorktreeRequest:
    repository = tmp_path / "portal"
    repository.mkdir(exist_ok=True)

    return WorktreeRequest("LPD-1", "LPD-1-fix-it", "LPD-1-fix-it", "master", repository)


def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "agents" / "LPD-1-fix-it" / "portal"
    (path / ".git").mkdir(parents=True)

    return path


async def test_script_gets_the_request_and_answers_with_the_last_line(tmp_path: Path) -> None:
    path = worktree(tmp_path)
    created = script(
        tmp_path,
        'echo "$SPYHOP_ISSUE_KEY $SPYHOP_WORKTREE_NAME $SPYHOP_BRANCH $SPYHOP_BASE_BRANCH '
        '$SPYHOP_REPOSITORY_PATH $PWD" > "$0.log"\n'
        f"echo 'Preparing worktree'\necho '{path}'\necho",
    )

    answer = await run_worktree_script(created, request(tmp_path), 10)

    assert answer == path
    assert (tmp_path / "create-worktree.log").read_text().split() == [
        "LPD-1",
        "LPD-1-fix-it",
        "LPD-1-fix-it",
        "master",
        str(tmp_path / "portal"),
        str(tmp_path / "portal"),
    ]


async def test_failed_script_reports_its_exit_code_and_error_output(tmp_path: Path) -> None:
    failing = script(tmp_path, "echo 'branch already checked out' >&2\nexit 3")

    with pytest.raises(WorktreeScriptError, match="exited with 3: branch already checked out"):
        await run_worktree_script(failing, request(tmp_path), 10)


async def test_script_asks_to_try_again_with_exit_code_75(tmp_path: Path) -> None:
    waiting = script(tmp_path, "echo 'Permission denied (publickey).' >&2\nexit 75")

    with pytest.raises(WorktreeScriptTryAgainError, match="asked to try again: Permission denied"):
        await run_worktree_script(waiting, request(tmp_path), 10)


@pytest.mark.parametrize(
    ("output", "problem"),
    [
        ("", "printed no worktree path"),
        ("echo agents/LPD-1", "not an absolute path"),
        ("echo /nowhere/LPD-1", "not a Git worktree"),
    ],
)
async def test_script_must_print_an_existing_worktree(
    tmp_path: Path, output: str, problem: str
) -> None:
    with pytest.raises(WorktreeScriptError, match=problem):
        await run_worktree_script(script(tmp_path, output), request(tmp_path), 10)


async def test_slow_script_is_stopped(tmp_path: Path) -> None:
    with pytest.raises(WorktreeScriptError, match="no answer within"):
        await run_worktree_script(script(tmp_path, "sleep 5"), request(tmp_path), 0.2)


async def test_missing_script_is_reported(tmp_path: Path) -> None:
    with pytest.raises(WorktreeScriptError, match="cannot run"):
        await run_worktree_script(tmp_path / "missing", request(tmp_path), 10)


async def test_cleanup_script_gets_the_removed_worktree(tmp_path: Path) -> None:
    removed = worktree_record(tmp_path)
    removed.repository_path.mkdir(parents=True, exist_ok=True)
    cleanup = script(
        tmp_path,
        'echo "$SPYHOP_ISSUE_KEY $SPYHOP_WORKTREE_PATH $SPYHOP_BRANCH '
        '$SPYHOP_REPOSITORY_PATH $PWD" > "$0.log"',
    )

    await run_cleanup_script(cleanup, removed, 10)

    assert (tmp_path / "create-worktree.log").read_text().split() == [
        removed.issue_key,
        str(removed.path),
        removed.branch_name,
        str(removed.repository_path),
        str(removed.repository_path),
    ]


async def test_failed_cleanup_script_is_reported(tmp_path: Path) -> None:
    removed = worktree_record(tmp_path)
    removed.repository_path.mkdir(parents=True, exist_ok=True)

    with pytest.raises(WorktreeScriptError, match="exited with 2: tomcat still running"):
        await run_cleanup_script(
            script(tmp_path, "echo 'tomcat still running' >&2\nexit 2"), removed, 10
        )
