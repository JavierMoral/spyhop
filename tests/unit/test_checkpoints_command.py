import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.cli import app
from spyhop.config.paths import database_file
from spyhop.config.settings import load_settings
from spyhop.domain.models import IssueEntry, OrcaWorktree
from spyhop.infrastructure.jira.mapping import to_issue
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import RunRepository, WorktreeRepository
from tests.factories import issue
from tests.unit.test_checkpoint_planner import checkpoint


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    checkpoint(tmp_path / "claude" / "agents", "regressions")
    checkpoint(tmp_path / "claude" / "agents", "security", mode="read")

    workflow = {"repository": "portal", "trigger_statuses": ["In Progress"]}
    config = {
        "application": {"dry_run": False},
        "jira": {"base_url": "https://example.atlassian.net"},
        "repositories": {"portal": {"path": str(tmp_path / "portal")}},
        "projects": {
            "LPD": {
                "workflows": {
                    "Bug": {
                        **workflow,
                        "name": "bug_fix",
                        "prompt": "bug_fix.md",
                        "checkpoints": ["regressions", "security"],
                    },
                    "Task": {**workflow, "name": "subtask", "prompt": "technical_task.md"},
                }
            }
        },
    }
    config_file = tmp_path / "config.yaml"

    config_file.write_text(yaml.safe_dump(config))

    return config_file


def plan(config_file: Path, *arguments: str) -> tuple[int, str]:
    result = CliRunner().invoke(
        app, ["checkpoints", "plan", "--config", str(config_file), *arguments]
    )

    return result.exit_code, result.output


def test_the_workflow_comes_from_the_run_of_the_current_worktree(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "worktrees" / "LPD-1"
    connection = connect(database_file(dry_run=False))
    workflow = WorkflowResolver(load_settings(config_file)).resolve("LPD", "Bug")

    assert workflow is not None

    automation_run = RunRepository(connection).create_discovered(
        IssueEntry(
            issue=to_issue(issue("LPD-1")),
            workflow=workflow,
            history_id="1",
            to_status="In Progress",
            entered_at=datetime(2026, 11, 1, tzinfo=UTC),
        )
    )
    worktree = WorktreeRepository(connection).create(
        "LPD-1",
        OrcaWorktree(f"repo::{path}", path, "LPD-1", "master", None, "", False),
        tmp_path / "portal",
        "master",
    )

    assert automation_run is not None

    RunRepository(connection).mark_ready(automation_run.id, worktree.id)
    (path / "modules").mkdir(parents=True)
    monkeypatch.chdir(path / "modules")

    exit_code, output = plan(config_file)

    assert exit_code == 0
    assert json.loads(output) == {
        "workflow": "bug_fix",
        "stages": [
            {"mode": "read", "checkpoints": [{"name": "security", "agent": "checkpoint-security"}]},
            {
                "mode": "exclusive",
                "checkpoints": [{"name": "regressions", "agent": "checkpoint-regressions"}],
            },
        ],
    }


def test_outside_a_worktree_the_workflow_must_be_given(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect(database_file(dry_run=False))
    monkeypatch.chdir(tmp_path)

    exit_code, output = plan(config_file)

    assert exit_code == 1
    assert "give the workflow with --workflow" in output


def test_the_workflow_option_selects_a_workflow_by_name(config_file: Path) -> None:
    exit_code, output = plan(config_file, "--workflow", "bug_fix", "--from", "regressions")

    assert exit_code == 0
    assert json.loads(output)["stages"] == [
        {
            "mode": "exclusive",
            "checkpoints": [{"name": "regressions", "agent": "checkpoint-regressions"}],
        }
    ]


def test_a_workflow_without_checkpoints_prints_no_stages(config_file: Path) -> None:
    exit_code, output = plan(config_file, "--workflow", "subtask")

    assert exit_code == 0
    assert json.loads(output) == {"workflow": "subtask", "stages": []}


def test_an_unknown_workflow_is_an_error(config_file: Path) -> None:
    exit_code, output = plan(config_file, "--workflow", "feature")

    assert exit_code == 1
    assert "No enabled project has a workflow named 'feature'" in output
