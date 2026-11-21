import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
import yaml
from typer.testing import CliRunner

from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.cli import app
from spyhop.config.paths import database_file
from spyhop.config.settings import load_settings
from spyhop.domain.enums import WaitingReason
from spyhop.domain.exceptions import OrcaCommandError
from spyhop.domain.models import IssueEntry, OrcaWorktree
from spyhop.infrastructure.jira.mapping import to_issue
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import (
    RunRepository,
    SessionRepository,
    WorktreeRepository,
)
from tests.factories import issue
from tests.unit.test_checkpoint_planner import checkpoint

UPDATED = "2026-11-16T10:21:33.123+0100"
ISSUE_URL = "https://example.atlassian.net/rest/api/3/issue/LPD-1"


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
        "service": {"env_file": str(tmp_path / "env")},
        "pipelines": {
            "bug": [
                "plan",
                "plan_review",
                "implement",
                {"checks": ["regressions", "security"], "on_fail": "fix"},
            ]
        },
        "repositories": {"portal": {"path": str(tmp_path / "portal")}},
        "projects": {
            "LPD": {
                "workflows": {
                    "Bug": {
                        **workflow,
                        "name": "bug_fix",
                        "prompt": "bug_fix.md",
                        "pipeline": "bug",
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


def check(config_file: Path, *arguments: str) -> tuple[int, dict[str, object]]:
    result = CliRunner().invoke(
        app, ["checkpoints", "check", "--config", str(config_file), *arguments]
    )

    return result.exit_code, json.loads(result.stdout)


def git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def repository(path: Path) -> str:
    """A Git repository with one commit on master. Returns its SHA."""

    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--quiet", "--initial-branch", "master")

    return commit(path, "First")


def commit(path: Path, message: str) -> str:
    git(path, "commit", "--allow-empty", "--quiet", "-m", message)

    return git(path, "rev-parse", "HEAD")


@pytest.fixture
def run_worktree(config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The worktree of a bug_fix run for LPD-1, a Git repository with one
    commit, with the current directory in one of its subdirectories."""

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
    repository(path)
    monkeypatch.chdir(path / "modules")

    return path


@pytest.fixture
def other_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Git repository that is not the worktree of a run, as the current
    directory."""

    path = tmp_path / "repository"

    repository(path)
    monkeypatch.chdir(path)

    return path


def verdicts(path: Path, *rows: tuple[str, str, str]) -> None:
    (path / "CLAUDE-CHECKPOINTS.md").write_text(
        "| Checkpoint | Verdict | Inputs |\n| --- | --- | --- |\n"
        + "".join(f"| {name} | {verdict} | {inputs} |\n" for name, verdict, inputs in rows)
    )


def ticket_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Makes security read the ticket, with JIRA credentials in the environment."""

    checkpoint(tmp_path / "claude" / "agents", "security", mode="read", inputs=["ticket", "head"])
    monkeypatch.setenv("JIRA_API_USER", "me@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "secret")


def test_the_workflow_comes_from_the_run_of_the_current_worktree(
    config_file: Path, run_worktree: Path
) -> None:
    first = git(run_worktree, "rev-parse", "HEAD")

    git(run_worktree, "checkout", "--quiet", "-b", "LPD-1")
    head = commit(run_worktree, "Fix")

    exit_code, output = plan(config_file)

    assert exit_code == 0
    assert json.loads(output) == {
        "workflow": "bug_fix",
        "base_branch": "master",
        "base": first,
        "head": head,
        "phases": [
            {
                "phase": 3,
                "on_fail": "fix",
                "rounds": 3,
                "stages": [
                    {
                        "mode": "read",
                        "checkpoints": [
                            {
                                "name": "security",
                                "agent": "checkpoint-security",
                                "inputs": f"head={head}",
                            }
                        ],
                    },
                    {
                        "mode": "exclusive",
                        "checkpoints": [
                            {
                                "name": "regressions",
                                "agent": "checkpoint-regressions",
                                "inputs": f"head={head}",
                            }
                        ],
                    },
                ],
            }
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


def test_the_workflow_option_selects_a_workflow_by_name(
    config_file: Path, other_repository: Path
) -> None:
    head = git(other_repository, "rev-parse", "HEAD")

    exit_code, output = plan(config_file, "--workflow", "bug_fix")

    assert exit_code == 0
    assert json.loads(output)["phases"][0]["stages"][1] == {
        "mode": "exclusive",
        "checkpoints": [
            {"name": "regressions", "agent": "checkpoint-regressions", "inputs": f"head={head}"}
        ],
    }


def test_a_workflow_without_checkpoints_prints_no_stages(
    config_file: Path, other_repository: Path
) -> None:
    exit_code, output = plan(config_file, "--workflow", "subtask")

    assert exit_code == 0
    assert json.loads(output) == {
        "workflow": "subtask",
        "base_branch": "master",
        "base": None,
        "head": None,
        "phases": [],
    }


def test_plan_fails_without_the_base_branch(config_file: Path, other_repository: Path) -> None:
    git(other_repository, "branch", "--quiet", "--move", "master", "main")

    exit_code, output = plan(config_file, "--workflow", "bug_fix")

    assert exit_code == 1
    assert "Cannot find where HEAD leaves master" in output


@respx.mock
def test_the_ticket_input_is_the_updated_field_of_the_issue(
    config_file: Path, run_worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket_checkpoint(tmp_path, monkeypatch)
    route = respx.get(ISSUE_URL).mock(
        return_value=httpx.Response(200, json={"fields": {"updated": UPDATED}})
    )
    head = git(run_worktree, "rev-parse", "HEAD")

    exit_code, output = plan(config_file)

    assert exit_code == 0
    assert json.loads(output)["phases"][0]["stages"][0]["checkpoints"][0]["inputs"] == (
        f"ticket={UPDATED} head={head}"
    )
    assert route.calls.last.request.url.params["fields"] == "updated"


@respx.mock
def test_the_credentials_can_come_from_the_env_file_of_the_service(
    config_file: Path, run_worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket_checkpoint(tmp_path, monkeypatch)
    monkeypatch.delenv("JIRA_API_USER")
    monkeypatch.delenv("JIRA_API_TOKEN")
    (tmp_path / "env").write_text('JIRA_API_USER="me@example.com"\nJIRA_API_TOKEN="secret"\n')
    route = respx.get(ISSUE_URL).mock(
        return_value=httpx.Response(200, json={"fields": {"updated": UPDATED}})
    )

    exit_code, _ = plan(config_file)

    assert exit_code == 0
    assert route.calls.last.request.headers["Authorization"].startswith("Basic ")


def test_the_ticket_input_needs_the_run_of_the_worktree(
    config_file: Path, other_repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket_checkpoint(tmp_path, monkeypatch)

    exit_code, output = plan(config_file, "--workflow", "bug_fix")

    assert exit_code == 0
    assert json.loads(output)["phases"][0]["stages"][0]["checkpoints"][0]["inputs"] is None

    result = CliRunner().invoke(
        app, ["checkpoints", "check", "--config", str(config_file), "--workflow", "bug_fix"]
    )

    assert result.exit_code == 1
    assert "needs the run of the current worktree, not --workflow" in result.output


def test_rerun_asks_for_a_new_round_of_the_current_checks_phase(
    config_file: Path, run_worktree: Path
) -> None:
    runs = RunRepository(connect(database_file(dry_run=False)))
    [automation_run] = runs.list(issue_key="LPD-1")

    result = CliRunner().invoke(
        app, ["checkpoints", "rerun", "LPD-1", "--config", str(config_file)]
    )

    assert result.exit_code == 1
    assert "does not run checkpoints now" in result.output

    runs.set_phase(automation_run.id, 3)

    result = CliRunner().invoke(
        app, ["checkpoints", "rerun", "lpd-1", "--config", str(config_file)]
    )

    assert result.exit_code == 0
    assert runs.list(issue_key="LPD-1")[0].checks_requested_at is not None
    assert not runs.list(issue_key="LPD-1")[0].checks_failed_only

    result = CliRunner().invoke(
        app, ["checkpoints", "rerun", "LPD-1", "--failed", "--config", str(config_file)]
    )

    assert result.exit_code == 0
    assert runs.list(issue_key="LPD-1")[0].checks_failed_only


def test_an_unknown_workflow_is_an_error(config_file: Path) -> None:
    exit_code, output = plan(config_file, "--workflow", "feature")

    assert exit_code == 1
    assert "No enabled project has a workflow named 'feature'" in output


def test_check_accepts_verdicts_that_pass_on_head(config_file: Path, run_worktree: Path) -> None:
    head = git(run_worktree, "rev-parse", "HEAD")

    verdicts(
        run_worktree, ("regressions", "PASS", f"head={head}"), ("security", "PASS", f"head={head}")
    )

    exit_code, outcome = check(config_file)

    assert exit_code == 0
    assert outcome == {
        "workflow": "bug_fix",
        "head": head,
        "accepted": True,
        "reason": None,
        "problems": [],
    }


def test_check_rejects_a_verdict_of_an_older_head(config_file: Path, run_worktree: Path) -> None:
    old = git(run_worktree, "rev-parse", "HEAD")
    head = commit(run_worktree, "Fix")

    verdicts(
        run_worktree, ("regressions", "PASS", f"head={head}"), ("security", "PASS", f"head={old}")
    )

    exit_code, outcome = check(config_file)

    assert exit_code == 1
    assert outcome["reason"] == "checkpoints_stale"
    assert outcome["problems"] == [
        {
            "checkpoint": "security",
            "reason": "checkpoints_stale",
            "detail": f"PASS on head={old[:12]}, now head={head[:12]}",
        }
    ]


def test_check_without_the_file_reports_missing_verdicts(
    config_file: Path, run_worktree: Path
) -> None:
    exit_code, outcome = check(config_file)

    assert exit_code == 1
    assert outcome["reason"] == "checkpoints_missing"


def test_check_with_the_workflow_option_reads_the_current_repository(
    config_file: Path, other_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = git(other_repository, "rev-parse", "HEAD")

    (other_repository / "modules").mkdir()
    verdicts(
        other_repository,
        ("regressions", "PASS", f"head={head}"),
        ("security", "FAIL", f"head={head}"),
    )
    monkeypatch.chdir(other_repository / "modules")

    exit_code, outcome = check(config_file, "--workflow", "bug_fix")

    assert exit_code == 1
    assert outcome["reason"] == "checkpoints_failed"


@respx.mock
def test_check_rejects_a_verdict_of_an_older_ticket(
    config_file: Path, run_worktree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket_checkpoint(tmp_path, monkeypatch)
    respx.get(ISSUE_URL).mock(
        return_value=httpx.Response(
            200, json={"fields": {"updated": "2026-11-17T08:00:00.000+0100"}}
        )
    )
    head = git(run_worktree, "rev-parse", "HEAD")

    verdicts(
        run_worktree,
        ("regressions", "PASS", f"head={head}"),
        ("security", "PASS", f"ticket={UPDATED} head={head}"),
    )

    exit_code, outcome = check(config_file)

    assert exit_code == 1
    assert outcome["problems"] == [
        {
            "checkpoint": "security",
            "reason": "checkpoints_stale",
            "detail": f"PASS on ticket={UPDATED}, now ticket=2026-11-17T08:00:00.000+0100",
        }
    ]


def fake_claude(directory: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A claude command that prints a stream-json run and records its arguments."""

    events = [
        {"type": "system", "subtype": "init"},
        {
            "type": "assistant",
            "message": {
                # A content item that is not an object is skipped.
                "content": [
                    "plain text",
                    {"type": "tool_use", "name": "Bash", "input": {"command": "git diff"}},
                ]
            },
        },
        {
            "type": "result",
            "is_error": False,
            "structured_output": {"verdict": "PASS", "head": "h", "commands": [], "findings": []},
        },
    ]
    script = directory / "claude"
    calls = directory / "calls"

    directory.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {calls}\n'
        + "".join(f"echo '{json.dumps(event)}'\n" for event in events)
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")

    return calls


def test_exec_runs_the_subagent_and_writes_its_result_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_claude(tmp_path / "bin", monkeypatch)
    output = tmp_path / "results" / "run" / "row.json"
    arguments = [
        "checkpoints",
        "exec",
        "security",
        "--base",
        "b" * 40,
        "--head",
        "h" * 40,
        "--root",
        str(tmp_path),
        "--output",
        str(output),
    ]

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 0
    assert '> Bash: {"command": "git diff"}' in result.output
    assert "Checkpoint security: PASS." in result.output
    assert json.loads(output.read_text())["output"]["structured_output"]["verdict"] == "PASS"
    assert "--agent checkpoint-security" in calls.read_text()
    assert "--json-schema" in calls.read_text()

    again = CliRunner().invoke(app, arguments)

    assert "already has a result" in again.output
    assert calls.read_text().count("--dangerously-skip-permissions") == 1


def test_exec_writes_a_result_when_it_stops_on_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "results" / "run" / "row.json"

    def broken(*arguments: object) -> None:
        raise KeyError("message")

    monkeypatch.setattr("spyhop.cli._run_checkpoint", broken)

    result = CliRunner().invoke(
        app,
        [
            "checkpoints",
            "exec",
            "security",
            "--base",
            "b" * 40,
            "--head",
            "h" * 40,
            "--root",
            str(tmp_path),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(output.read_text()) == {"exit_code": 1, "output": None}


class FakeBoard:
    def __init__(self) -> None:
        self.cards: list[tuple[str, str]] = []

    async def set_card_status(self, orca_worktree_id: str, status: str) -> None:
        self.cards.append((orca_worktree_id, status))


def test_approve_moves_the_card_of_a_plan_that_waits_for_its_review(
    config_file: Path, run_worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = FakeBoard()
    runs = RunRepository(connect(database_file(dry_run=False)))
    [automation_run] = runs.list(issue_key="LPD-1")

    monkeypatch.setattr("spyhop.cli.orca_client", lambda settings: board)

    result = CliRunner().invoke(app, ["approve", "LPD-1", "--config", str(config_file)])

    assert result.exit_code == 1
    assert "LPD-1 has no plan that waits for a review." in result.output
    assert board.cards == []

    runs.mark_waiting(automation_run.id, WaitingReason.PLAN_REVIEW, "review the plan")

    result = CliRunner().invoke(app, ["approve", "lpd-1", "--config", str(config_file)])

    assert result.exit_code == 0
    assert board.cards == [(f"repo::{run_worktree}", "in-progress")]


class FakeTerminals:
    def __init__(self, error: Exception | None = None) -> None:
        self.prompts: list[tuple[str, str]] = []
        self.error = error

    async def send_prompt(self, terminal: str, prompt: str) -> None:
        if self.error is not None:
            raise self.error

        self.prompts.append((terminal, prompt))


def test_tell_sends_the_message_to_the_agent_of_the_active_run(
    config_file: Path, run_worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminals = FakeTerminals()
    connection = connect(database_file(dry_run=False))
    [automation_run] = RunRepository(connection).list(issue_key="LPD-1")
    sessions = SessionRepository(connection)

    monkeypatch.setattr("spyhop.cli.orca_client", lambda settings: terminals)

    result = CliRunner().invoke(app, ["tell", "LPD-1", "Add a test", "--config", str(config_file)])

    assert result.exit_code == 1
    assert "LPD-1 has no agent." in result.output

    session = sessions.create_launching(automation_run.id, "claude", "opus", "1-abc", "plan")

    sessions.mark_launched(session.id, "term_1", "tab:leaf")

    result = CliRunner().invoke(app, ["tell", "lpd-1", "Add a test", "--config", str(config_file)])

    assert result.exit_code == 0
    assert "LPD-1: message sent to the plan agent term_1." in result.output
    assert terminals.prompts == [("term_1", "Add a test")]


def test_tell_reports_an_agent_that_orca_refuses(
    config_file: Path, run_worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminals = FakeTerminals(OrcaCommandError("terminal_not_found", "no terminal term_1"))
    connection = connect(database_file(dry_run=False))
    [automation_run] = RunRepository(connection).list(issue_key="LPD-1")
    sessions = SessionRepository(connection)
    session = sessions.create_launching(automation_run.id, "claude", "opus", "1-abc", "plan")

    sessions.mark_launched(session.id, "term_1", "tab:leaf")
    monkeypatch.setattr("spyhop.cli.orca_client", lambda settings: terminals)

    result = CliRunner().invoke(app, ["tell", "LPD-1", "Add a test", "--config", str(config_file)])

    assert result.exit_code == 1
    assert "Orca refused the message for the plan agent term_1" in result.output


def test_tell_needs_an_active_run(
    config_file: Path, run_worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminals = FakeTerminals()

    monkeypatch.setattr("spyhop.cli.orca_client", lambda settings: terminals)

    result = CliRunner().invoke(app, ["tell", "LPD-2", "Add a test", "--config", str(config_file)])

    assert result.exit_code == 1
    assert "LPD-2 has no active run." in result.output
    assert terminals.prompts == []


@respx.mock
def test_ticket_shows_all_the_fields_of_the_issue(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket_checkpoint(tmp_path, monkeypatch)
    route = respx.get(ISSUE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "key": "LPD-1",
                "names": {"versions": "Affects versions"},
                "fields": {"summary": "Broken page", "versions": [{"name": "Master"}]},
            },
        )
    )

    result = CliRunner().invoke(app, ["ticket", "lpd-1", "--config", str(config_file)])

    assert result.exit_code == 0
    assert result.output == "# LPD-1: Broken page\n\n## Affects versions\n\nMaster\n"
    assert route.calls.last.request.url.params["fields"] == "*all"
    assert route.calls.last.request.url.params["expand"] == "names"


def test_stats_show_the_runs_of_the_last_days(config_file: Path, run_worktree: Path) -> None:
    result = CliRunner().invoke(app, ["stats", "--config", str(config_file)])

    assert result.exit_code == 0
    assert "Runs of the last 30 days: 1 (READY 1)" in result.output
    assert "Median time to complete: -" in result.output
