"""In-memory Orca and JIRA, and a harness wiring them to a real SQLite
database, the launcher and the supervisor."""

import dataclasses
import shutil
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from spyhop.application.checkpoint_planner import CheckpointPlanner
from spyhop.application.checkpoint_runner import CheckRunner
from spyhop.application.context_builder import ContextBuilder, template_environment
from spyhop.application.janitor import WorktreeJanitor, WorktreeRemover
from spyhop.application.launcher import AgentLauncher
from spyhop.application.supervisor import AgentSupervisor
from spyhop.application.supervisor_session import SupervisorSession
from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.config.settings import Settings
from spyhop.domain.exceptions import JiraError, OrcaCommandError, OrcaUnavailableError
from spyhop.domain.models import (
    AgentRow,
    AutomationRun,
    IssueEntry,
    JiraUser,
    LaunchResult,
    OrcaTerminal,
    OrcaWorktree,
    RemoveResult,
    Worktree,
)
from spyhop.infrastructure.jira.mapping import to_issue
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import (
    CheckpointRunRepository,
    RunRepository,
    SessionRepository,
    WorktreeRepository,
)
from tests.factories import issue, settings

PLAN_REVIEW = "plan-review"
PLAN_APPROVED = "in-progress"

ORCA = Path("/opt/orca/orca-ide")

SPYHOP = Path("/opt/spyhop/bin/spyhop")


class FakeTickets:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed: set[str] = set()
        self.queries: list[str] = []
        self.search_error: Exception | None = None
        self.parents: dict[str, str] = {}
        self.hidden: set[str] = set()
        self.summaries: dict[str, str] = {}
        self.updated: dict[str, str] = {}

    async def issue(self, issue_key: str, fields: list[str]) -> dict[str, Any]:
        self.calls.append(issue_key)

        if issue_key in self.hidden:
            raise JiraError(f"{issue_key} not found", status_code=404)

        parent = self.parents.get(issue_key)

        return {
            "key": issue_key,
            "fields": {
                "summary": self.summaries.get(issue_key, f"Summary of {issue_key}"),
                "issuetype": {"name": "Bug"},
                "status": {"name": "In Progress"},
                "parent": {"key": parent} if parent else None,
                "updated": self.updated.get(issue_key, "2026-11-01T09:00:00.000+0000"),
            },
        }

    async def myself(self) -> JiraUser:
        return JiraUser("me", "Me", "UTC")

    async def changelog(self, issue_key: str) -> list[dict[str, Any]]:
        return []

    async def search(
        self, jql: str, fields: list[str], changelog: bool = True
    ) -> list[dict[str, Any]]:
        """Answers the janitor's query: the closed tickets among its keys."""

        self.queries.append(jql)

        if self.search_error is not None:
            raise self.search_error

        keys = jql.split("key in (", 1)[1].split(")", 1)[0].split(", ")

        return [{"key": key} for key in keys if key in self.closed]


def git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=t@example.com",
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def init_repository(path: Path) -> None:
    git(path, "init", "--quiet", "--initial-branch", "master")
    (path / "App.java").write_text("class App {}\n")
    git(path, "add", "App.java")
    git(path, "commit", "--quiet", "-m", "First")


class FakeUnits:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.start_errors: list[Exception] = []
        self.stopped: list[str] = []

    async def start(self, unit: str, command: Sequence[str], cwd: Path, log: Path) -> None:
        self.started.append({"unit": unit, "command": list(command), "cwd": cwd, "log": log})

        if self.start_errors:
            raise self.start_errors.pop(0)

    async def stop(self, pattern: str) -> None:
        self.stopped.append(pattern)


class FakeOrca:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.worktrees: dict[str, OrcaWorktree] = {}
        self.references: dict[str, str] = {}
        self.launches: list[dict[str, Any]] = []
        self.created: list[str] = []
        self.bases: list[str] = []
        self.unavailable = False
        self.launch_errors: list[Exception] = []
        self.create_errors: list[Exception] = []
        self.linked: list[str] = []
        self.link_errors: list[Exception] = []
        self.agents: dict[str, list[AgentRow]] = {}
        self.open_terminals: dict[str, int] = {}
        self.closed_terminals: list[str] = []
        self.removed: list[str] = []
        self.remove_errors: list[Exception] = []
        self.cards: dict[str, str] = {}
        self.terminals: list[dict[str, str]] = []
        self.terminal_errors: list[Exception] = []
        self.prompts: list[tuple[str, str]] = []
        self.prompt_errors: list[Exception] = []
        # New worktrees are real Git repositories with one commit on master.
        self.real_git = False

    @property
    def executable(self) -> Path:
        return ORCA

    async def ensure_ready(self) -> None:
        if self.unavailable:
            raise OrcaUnavailableError("Orca is closed")

    async def find_worktree_by_reference(self, issue_key: str) -> OrcaWorktree | None:
        worktree_id = self.references.get(issue_key)

        return self.worktrees[worktree_id] if worktree_id else None

    async def worktree_base_path(self, repository_path: Path) -> Path | None:
        return self.root / "wt"

    async def show_worktree(self, orca_worktree_id: str) -> OrcaWorktree:
        if orca_worktree_id not in self.worktrees:
            raise OrcaCommandError("selector_not_found", "selector_not_found")

        return self.worktrees[orca_worktree_id]

    def add_worktree(self, name: str, reference: str | None = None) -> OrcaWorktree:
        path = self.root / "wt" / name
        suffix = 1

        while path.exists():
            # Orca suffixes a name that is taken.

            suffix += 1
            path = self.root / "wt" / f"{name}-{suffix}"

        if self.real_git:
            path.mkdir(parents=True)
            init_repository(path)
        else:
            (path / ".git").mkdir(parents=True)

        worktree = OrcaWorktree(
            id=f"repo::{path}",
            path=path,
            branch=f"me/{name}",
            base_ref="master",
            workspace_status="in-progress",
            comment="",
            unread=False,
        )

        self.worktrees[worktree.id] = worktree

        if reference:
            self.references[reference] = worktree.id

        return worktree

    async def create_worktree(
        self, repository_path: Path, name: str, base_branch: str, reference: str, setup: str
    ) -> OrcaWorktree:
        self.created.append(name)
        self.bases.append(base_branch)

        worktree = self.add_worktree(name, reference)

        if self.create_errors:
            raise self.create_errors.pop(0)

        return worktree

    async def link_worktree(self, path: Path, reference: str) -> OrcaWorktree:
        self.linked.append(str(path))

        if self.link_errors:
            raise self.link_errors.pop(0)

        worktree = OrcaWorktree(
            id=f"repo::{path}",
            path=path,
            branch=path.name,
            base_ref=None,
            workspace_status="in-progress",
            comment="",
            unread=False,
        )

        self.worktrees[worktree.id] = worktree
        self.references[reference] = worktree.id

        return worktree

    async def launch_agent(
        self,
        orca_worktree_id: str,
        prompt: str,
        operation_id: str,
        model: str | None,
        effort: str | None,
    ) -> LaunchResult:
        self.launches.append(
            {
                "worktree": orca_worktree_id,
                "prompt": prompt,
                "operation_id": operation_id,
                "model": model,
                "effort": effort,
            }
        )

        if self.launch_errors:
            raise self.launch_errors.pop(0)

        number = len(self.launches)
        pane_key = f"tab{number}:leaf{number}"

        self.open_terminals[orca_worktree_id] = self.open_terminals.get(orca_worktree_id, 0) + 1

        return LaunchResult(f"term_{number}", operation_id, "terminal", "submitted", pane_key)

    async def worktree_agents(self) -> dict[str, list[AgentRow]]:
        if self.unavailable:
            raise OrcaUnavailableError("Orca is closed")

        return self.agents

    async def close_terminals(self, orca_worktree_id: str) -> int:
        if orca_worktree_id not in self.worktrees:
            raise OrcaCommandError("selector_not_found", "selector_not_found")

        self.closed_terminals.append(orca_worktree_id)
        self.agents.pop(orca_worktree_id, None)

        return self.open_terminals.pop(orca_worktree_id, 0)

    async def remove_worktree(self, orca_worktree_id: str) -> RemoveResult:
        if self.remove_errors:
            raise self.remove_errors.pop(0)

        if orca_worktree_id not in self.worktrees:
            raise OrcaCommandError("selector_not_found", "selector_not_found")

        worktree = self.worktrees.pop(orca_worktree_id)

        self.removed.append(orca_worktree_id)
        self.references = {
            key: value for key, value in self.references.items() if value != orca_worktree_id
        }
        shutil.rmtree(worktree.path)

        return RemoveResult(preserved_branch=worktree.branch)

    async def mark_card(self, orca_worktree_id: str, comment: str) -> None:
        self.cards[orca_worktree_id] = comment

    async def create_terminal(
        self, orca_worktree_id: str, title: str, command: str
    ) -> OrcaTerminal:
        self.terminals.append({"worktree": orca_worktree_id, "title": title, "command": command})

        if self.terminal_errors:
            raise self.terminal_errors.pop(0)

        number = len(self.terminals)

        return OrcaTerminal(f"term_check_{number}", f"pane_term_{number}")

    async def send_prompt(self, terminal: str, prompt: str) -> None:
        self.prompts.append((terminal, prompt))

        if self.prompt_errors:
            raise self.prompt_errors.pop(0)

    def set_card(self, orca_worktree_id: str, status: str) -> None:
        self.worktrees[orca_worktree_id] = dataclasses.replace(
            self.worktrees[orca_worktree_id], workspace_status=status
        )

    def set_agent(
        self, orca_worktree_id: str, state: str, pane_key: str, since: datetime | None = None
    ) -> None:
        """Replaces the agent of that pane, keeping the worktree's others."""

        others = [row for row in self.agents.get(orca_worktree_id, []) if row.pane_key != pane_key]
        row = AgentRow(
            agent_type="claude",
            state=state,
            pane_key=pane_key,
            has_live_pty=True,
            prompt=None,
            state_started_at=since or datetime.now(UTC),
        )

        self.agents[orca_worktree_id] = [*others, row]


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        config: Settings | None = None,
        with_supervisor_session: bool = False,
    ) -> None:
        self.config = config or settings(tmp_path)
        self.connection = connect(tmp_path / "spyhop.db")
        self.runs = RunRepository(self.connection)
        self.worktrees = WorktreeRepository(self.connection)
        self.sessions = SessionRepository(self.connection)
        self.orca = FakeOrca(tmp_path)
        self.tickets = FakeTickets()
        self.history = 0
        self.branches: set[str] = set()
        self.free_gb = 500.0
        self.measured: list[Path] = []
        self.dirty: set[Path] = set()
        self.context = ContextBuilder(
            template_environment(tmp_path / "templates"), ORCA, PLAN_REVIEW, PLAN_APPROVED
        )
        self.launcher = AgentLauncher(
            runs=self.runs,
            worktrees=self.worktrees,
            sessions=self.sessions,
            resolver=WorkflowResolver(self.config),
            jira=self.tickets,
            jira_settings=self.config.jira,
            orca=self.orca,
            context=self.context,
            agents=self.config.agents,
            max_concurrent_agents=self.config.application.max_concurrent_agents,
            min_free_disk_gb=self.config.application.min_free_disk_gb,
            branch_exists=lambda repository, branch: branch in self.branches,
            free_bytes=lambda path: self.free_bytes(path),
        )
        self.cleaned: list[Path] = []
        self.cleanup_errors: list[Exception] = []
        self.units = FakeUnits()
        self.remover = WorktreeRemover(
            self.runs,
            self.worktrees,
            self.orca,
            self.units,
            self.config.repositories,
            cleanup_script=self.cleanup_script,
        )
        self.janitor = WorktreeJanitor(
            remover=self.remover,
            runs=self.runs,
            worktrees=self.worktrees,
            jira=self.tickets,
            orca=self.orca,
            settings=self.config.cleanup,
            has_changes=lambda path: path in self.dirty,
        )
        self.agents_dir = tmp_path / "agents"
        self.results_dir = tmp_path / "results"
        self.checkpoint_runs = CheckpointRunRepository(self.connection)
        self.supervisor_session = (
            SupervisorSession(
                worktrees=self.worktrees,
                orca=self.orca,
                context=self.context,
                spyhop=SPYHOP,
                model=self.config.agents.model,
            )
            if with_supervisor_session
            else None
        )
        self.check_runner = CheckRunner(
            checkpoint_runs=self.checkpoint_runs,
            units=self.units,
            planner=CheckpointPlanner(self.agents_dir),
            tickets=self.tickets,
            spyhop=SPYHOP,
            results_dir=self.results_dir,
            timeout=timedelta(hours=3),
            supervisor_session=self.supervisor_session,
        )
        self.supervisor = AgentSupervisor(
            runs=self.runs,
            worktrees=self.worktrees,
            sessions=self.sessions,
            orca=self.orca,
            context=self.context,
            agents=self.config.agents,
            plan_review_status=PLAN_REVIEW,
            plan_approved_status=PLAN_APPROVED,
            resolver=WorkflowResolver(self.config),
            check_runner=self.check_runner,
            supervisor_session=self.supervisor_session,
        )

    async def cleanup_script(self, script: Path, worktree: Worktree, time_limit: float) -> None:
        if self.cleanup_errors:
            raise self.cleanup_errors.pop(0)

        self.cleaned.append(worktree.path)

    def free_bytes(self, path: Path) -> int:
        self.measured.append(path)

        return int(self.free_gb * 1024**3)

    def discover(self, key: str = "LPD-1") -> AutomationRun:
        self.history += 1

        jira_issue = to_issue(issue(key))
        workflow = WorkflowResolver(self.config).resolve(jira_issue.project_key, "Bug")

        assert workflow is not None

        run = self.runs.create_discovered(
            IssueEntry(
                issue=jira_issue,
                workflow=workflow,
                history_id=str(self.history),
                to_status="In Progress",
                entered_at=datetime(2026, 11, 1, 9, self.history, tzinfo=UTC),
            )
        )

        assert run is not None

        return run

    def run(self, run_id: str) -> AutomationRun:
        run = self.runs.get(run_id)

        assert run is not None

        return run

    def finish(self, run_id: str) -> None:
        self.connection.execute(
            "UPDATE automation_runs SET status = 'COMPLETED' WHERE id = ?", (run_id,)
        )
