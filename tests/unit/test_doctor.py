from pathlib import Path
from typing import Any

from spyhop.config.settings import ProjectSettings
from spyhop.doctor import (
    Level,
    _check_checkpoints,
    _check_claude_links,
    _check_github,
    _check_project,
    _check_templates,
    _check_worktree_script,
)
from spyhop.infrastructure.claude_links import link_claude_files
from spyhop.infrastructure.process.command_runner import CommandResult
from tests.factories import settings
from tests.unit.test_checkpoint_planner import checkpoint
from tests.unit.test_claude_links import bundled


def project(tmp_path: Path) -> ProjectSettings:
    return settings(tmp_path).projects["LPD"]


def test_project_matching_jira_is_ok(tmp_path: Path) -> None:
    check = _check_project(
        "LPD", project(tmp_path), {"Bug": ["In Progress"], "Story": ["In Development"]}
    )

    assert check.level is Level.OK


def test_unknown_project_is_an_error(tmp_path: Path) -> None:
    check = _check_project("LPD", project(tmp_path), None)

    assert check.level is Level.ERROR
    assert "does not exist" in check.detail


def test_misspelled_issue_types_and_statuses_are_reported(tmp_path: Path) -> None:
    check = _check_project("LPD", project(tmp_path), {"Bug": ["In progress"]})

    assert check.level is Level.ERROR
    assert "Bug has no status 'In Progress'" in check.detail
    assert "no issue type 'Story'" in check.detail


def test_packaged_templates_cover_the_configured_workflows(tmp_path: Path) -> None:
    check = _check_templates(settings(tmp_path), tmp_path / "templates")

    assert check.level is Level.OK


def test_templates_directory_is_reported_without_customizations(tmp_path: Path) -> None:
    check = _check_templates(settings(tmp_path), tmp_path / "templates")

    assert f"customized in {tmp_path / 'templates'}: none" in check.detail


def test_missing_workflow_template_is_an_error(tmp_path: Path) -> None:
    config = settings(tmp_path)
    config.projects["LPD"].workflows["Bug"].prompt = "missing.md"

    check = _check_templates(config, tmp_path / "templates")

    assert check.level is Level.ERROR
    assert "missing.md" in check.detail


def test_customized_templates_are_reported(tmp_path: Path) -> None:
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "bug_fix.md").write_text("Mine")

    check = _check_templates(settings(tmp_path), tmp_path / "templates")

    assert "customized" in check.detail and "bug_fix.md" in check.detail


def test_worktree_script_must_be_executable(tmp_path: Path) -> None:
    script = tmp_path / "create-worktree"
    script.write_text("#!/bin/sh\n")

    assert _check_worktree_script("main", script).level is Level.ERROR
    assert _check_worktree_script("main", tmp_path / "missing").level is Level.ERROR

    script.chmod(0o755)

    assert _check_worktree_script("main", script).level is Level.OK


def test_valid_checkpoints_of_a_pipeline_are_ok(tmp_path: Path) -> None:
    checkpoint(tmp_path / "agents", "regressions")
    checkpoint(tmp_path / "agents", "security", mode="read")
    config = settings(
        tmp_path, pipelines={"bug": ["implement", {"checks": ["regressions", "security"]}]}
    )

    [check] = _check_checkpoints(config, tmp_path / "agents")

    assert check.level is Level.OK
    assert check.name == "pipeline bug"
    assert check.detail == "2 checkpoints are valid"


def test_missing_checkpoint_file_is_an_error(tmp_path: Path) -> None:
    checkpoint(tmp_path / "agents", "regressions")
    config = settings(
        tmp_path, pipelines={"bug": ["implement", {"checks": ["regressions", "security"]}]}
    )

    [check] = _check_checkpoints(config, tmp_path / "agents")

    assert check.level is Level.ERROR
    assert "security" in check.detail


def test_linked_claude_files_are_ok(tmp_path: Path) -> None:
    root = bundled(tmp_path)

    link_claude_files(root, tmp_path / "claude")

    check = _check_claude_links(root, tmp_path / "claude")

    assert check.level is Level.OK
    assert check.detail == "3 subagents, skills, and script directories are linked"


def test_a_claude_file_that_is_not_linked_is_an_error(tmp_path: Path) -> None:
    root = bundled(tmp_path)
    target = tmp_path / "claude" / "agents" / "checkpoint-security.md"

    link_claude_files(root, tmp_path / "claude")
    target.unlink()
    target.write_text("mine")

    check = _check_claude_links(root, tmp_path / "claude")

    assert check.level is Level.ERROR
    assert check.detail == (f"not linked to Spyhop: {target}; run `spyhop claude install`")


class Gh:
    """A runner of gh that answers like a signed in gh or like no gh."""

    def __init__(self, error: Exception | None = None) -> None:
        self.commands: list[list[str]] = []
        self._error = error

    async def run(self, args: Any, time_limit: float, **_: Any) -> CommandResult:
        self.commands.append(list(args))

        if self._error:
            raise self._error

        return CommandResult(0, '{"login": "me"}', "")


async def test_with_the_watcher_on_doctor_checks_that_gh_is_signed_in(tmp_path: Path) -> None:
    config = settings(tmp_path, pull_requests={"enabled": True})

    [ok] = await _check_github(config, Gh())
    [missing] = await _check_github(config, Gh(FileNotFoundError("gh")))

    assert (ok.level, ok.detail) == (Level.OK, "gh signed in as me")
    assert missing.level is Level.ERROR


async def test_with_the_watcher_off_doctor_does_not_run_gh(tmp_path: Path) -> None:
    runner = Gh()

    assert await _check_github(settings(tmp_path), runner) == []
    assert runner.commands == []
