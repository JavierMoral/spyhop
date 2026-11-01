import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from spyhop.application.context_builder import ContextBuilder, template_environment
from spyhop.application.launcher import AgentLauncher
from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.config.settings import Settings
from spyhop.domain.enums import RunStatus, SessionStatus, WaitingReason, WorktreeStatus
from spyhop.domain.exceptions import (
    OrcaCommandError,
    OrcaUnavailableError,
    OrcaUncertainError,
)
from spyhop.domain.models import AgentRow, AutomationRun, IssueEntry, LaunchResult, OrcaWorktree
from spyhop.infrastructure.jira.mapping import to_issue
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import (
    RunRepository,
    SessionRepository,
    WorktreeRepository,
)
from tests.factories import issue, settings

ORCA = Path("/opt/orca/orca-ide")


class FakeTickets:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def issue(self, issue_key: str, fields: list[str]) -> dict[str, Any]:
        self.calls.append(issue_key)

        return {
            "key": issue_key,
            "fields": {
                "summary": f"Summary of {issue_key}",
                "issuetype": {"name": "Bug"},
                "status": {"name": "In Progress"},
                "description": {
                    "type": "doc",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "Broken"}]}
                    ],
                },
            },
        }

    async def comments(self, issue_key: str, limit: int) -> list[dict[str, Any]]:
        return []


class FakeOrca:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.worktrees: dict[str, OrcaWorktree] = {}
        self.references: dict[str, str] = {}
        self.launches: list[dict[str, Any]] = []
        self.created: list[str] = []
        self.unavailable = False
        self.launch_errors: list[Exception] = []
        self.create_errors: list[Exception] = []

    @property
    def executable(self) -> Path:
        return ORCA

    async def ensure_ready(self) -> None:
        if self.unavailable:
            raise OrcaUnavailableError("Orca is closed")

    async def find_worktree_by_reference(self, issue_key: str) -> OrcaWorktree | None:
        worktree_id = self.references.get(issue_key)

        return self.worktrees[worktree_id] if worktree_id else None

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

        worktree = self.add_worktree(name, reference)

        if self.create_errors:
            raise self.create_errors.pop(0)

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
            {"worktree": orca_worktree_id, "prompt": prompt, "operation_id": operation_id}
        )

        if self.launch_errors:
            raise self.launch_errors.pop(0)

        return LaunchResult(f"term_{len(self.launches)}", operation_id, "terminal", "submitted")

    async def worktree_agents(self) -> dict[str, list[AgentRow]]:
        return {}


class Harness:
    def __init__(self, tmp_path: Path, config: Settings | None = None) -> None:
        self.config = config or settings(tmp_path)
        self.connection = connect(tmp_path / "spyhop.db")
        self.runs = RunRepository(self.connection)
        self.worktrees = WorktreeRepository(self.connection)
        self.sessions = SessionRepository(self.connection)
        self.orca = FakeOrca(tmp_path)
        self.tickets = FakeTickets()
        self.history = 0
        self.launcher = AgentLauncher(
            runs=self.runs,
            worktrees=self.worktrees,
            sessions=self.sessions,
            resolver=WorkflowResolver(self.config),
            jira=self.tickets,
            jira_settings=self.config.jira,
            orca=self.orca,
            context=ContextBuilder(
                template_environment(tmp_path / "templates"), ORCA, "plan-review"
            ),
            agents=self.config.agents,
            max_concurrent_agents=self.config.application.max_concurrent_agents,
        )

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


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


async def test_first_run_creates_the_worktree_writes_context_and_launches(
    harness: Harness,
) -> None:
    run = harness.discover()

    [launched] = await harness.launcher.launch_pending()

    run = harness.run(run.id)
    worktree = harness.worktrees.find_current("LPD-1")
    [session] = harness.sessions.list_for_run(run.id)
    [launch] = harness.orca.launches

    assert launched.id == run.id
    assert run.status is RunStatus.RUNNING
    assert run.started_at is not None
    assert worktree is not None and run.worktree_id == worktree.id
    assert worktree.branch_name == "me/LPD-1"
    assert harness.orca.created == ["LPD-1"]
    assert (worktree.path / "TASK.md").read_text().startswith("# Task: LPD-1")
    assert (worktree.path / ".claude" / "settings.local.json").exists()
    assert launch["worktree"] == worktree.orca_worktree_id
    assert launch["prompt"].startswith("/goal Lee TASK.md")
    assert launch["operation_id"] == session.launch_operation_id
    assert session.status is SessionStatus.LAUNCHED
    assert session.agent_handle == "term_1"


async def test_next_run_of_the_ticket_reuses_its_worktree(harness: Harness) -> None:
    first = harness.discover()

    await harness.launcher.launch_pending()
    harness.finish(first.id)

    second = harness.discover()

    await harness.launcher.launch_pending()

    assert harness.run(second.id).status is RunStatus.RUNNING
    assert harness.run(second.id).worktree_id == harness.run(first.id).worktree_id
    assert harness.orca.created == ["LPD-1"]
    assert len(harness.orca.launches) == 2


async def test_worktree_linked_in_orca_is_adopted(harness: Harness) -> None:
    existing = harness.orca.add_worktree("LPD-1", reference="LPD-1")
    run = harness.discover()

    await harness.launcher.launch_pending()

    worktree = harness.worktrees.find_current("LPD-1")

    assert harness.orca.created == []
    assert worktree is not None and worktree.orca_worktree_id == existing.id
    assert harness.run(run.id).status is RunStatus.RUNNING


async def test_worktree_removed_behind_spyhops_back_is_replaced(harness: Harness) -> None:
    first = harness.discover()

    await harness.launcher.launch_pending()
    harness.finish(first.id)

    old = harness.worktrees.find_current("LPD-1")

    assert old is not None

    del harness.orca.worktrees[old.orca_worktree_id]
    del harness.orca.references["LPD-1"]

    second = harness.discover()

    await harness.launcher.launch_pending()

    replacement = harness.worktrees.find_current("LPD-1")

    assert harness.worktrees.get(old.id).status is WorktreeStatus.REMOVED  # type: ignore[union-attr]
    assert replacement is not None and replacement.id != old.id
    assert harness.run(second.id).status is RunStatus.RUNNING


async def test_runs_wait_while_orca_is_unavailable(harness: Harness) -> None:
    run = harness.discover()
    harness.orca.unavailable = True

    assert await harness.launcher.launch_pending() == []
    assert harness.run(run.id).status is RunStatus.DISCOVERED
    assert harness.tickets.calls == []


async def test_lost_launch_answer_is_replayed_with_the_same_operation_id(
    harness: Harness,
) -> None:
    run = harness.discover()
    harness.orca.launch_errors.append(OrcaUncertainError("timed out"))

    assert await harness.launcher.launch_pending() == []
    assert harness.run(run.id).status is RunStatus.READY

    [launching] = harness.sessions.list_for_run(run.id)

    assert launching.status is SessionStatus.LAUNCHING

    await harness.launcher.launch_pending()

    first, second = harness.orca.launches

    assert first["operation_id"] == second["operation_id"] == launching.launch_operation_id
    assert first["prompt"] == second["prompt"]
    assert harness.run(run.id).status is RunStatus.RUNNING
    assert harness.orca.created == ["LPD-1"]
    assert len(harness.sessions.list_for_run(run.id)) == 1


async def test_old_lost_launch_is_handed_to_a_person(harness: Harness) -> None:
    run = harness.discover()
    harness.orca.launch_errors.append(OrcaUncertainError("timed out"))

    await harness.launcher.launch_pending()

    stale = f"{int((time.time() - 3600) * 1000)}-{'0' * 32}"

    harness.connection.execute("UPDATE agent_sessions SET launch_operation_id = ?", (stale,))

    await harness.launcher.launch_pending()

    run = harness.run(run.id)

    assert run.status is RunStatus.WAITING_FOR_HUMAN
    assert run.waiting_reason is WaitingReason.UNKNOWN_STATE
    assert len(harness.orca.launches) == 1


async def test_uncertain_worktree_creation_is_found_by_reference_next_time(
    harness: Harness,
) -> None:
    run = harness.discover()
    harness.orca.create_errors.append(OrcaUncertainError("timed out"))

    await harness.launcher.launch_pending()

    assert harness.run(run.id).status is RunStatus.PREPARING

    await harness.launcher.launch_pending()

    assert harness.orca.created == ["LPD-1"]
    assert harness.run(run.id).status is RunStatus.RUNNING


async def test_refused_launch_fails_the_run(harness: Harness) -> None:
    run = harness.discover()
    harness.orca.launch_errors.append(OrcaCommandError("agent_not_found", "agent_not_found"))

    await harness.launcher.launch_pending()

    run = harness.run(run.id)
    [session] = harness.sessions.list_for_run(run.id)

    assert run.status is RunStatus.FAILED
    assert run.error_message == "Orca refused the launch: agent_not_found"
    assert run.finished_at is not None
    assert session.status is SessionStatus.FAILED


async def test_busy_slots_hold_pending_runs(harness: Harness) -> None:
    first = harness.discover("LPD-1")
    second = harness.discover("LPD-2")

    launched = await harness.launcher.launch_pending()

    assert [run.id for run in launched] == [first.id]
    assert harness.run(second.id).status is RunStatus.DISCOVERED

    harness.finish(first.id)

    await harness.launcher.launch_pending()

    assert harness.run(second.id).status is RunStatus.RUNNING


async def test_run_whose_workflow_was_removed_fails(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    run = harness.discover()
    harness.launcher._resolver = WorkflowResolver(settings(tmp_path, projects={}))

    await harness.launcher.launch_pending()

    assert harness.run(run.id).status is RunStatus.FAILED
    assert "no longer configured" in (harness.run(run.id).error_message or "")


def test_harness_uses_a_real_database(harness: Harness) -> None:
    assert isinstance(harness.connection, sqlite3.Connection)
