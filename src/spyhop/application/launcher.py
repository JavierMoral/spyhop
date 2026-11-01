"""Turns registered runs into agents working in Orca.

DISCOVERED → PREPARING → READY → RUNNING, or FAILED with error_message.

- PREPARING reads the ticket, gets or creates the ticket's worktree and writes
  the agent's context. Every step is safe to repeat, so a run interrupted here
  (Orca or JIRA down, timeout, restart) simply prepares again next cycle.
- READY launches the agent. The operation id is stored before calling Orca and
  reused on every attempt, so the launch is never duplicated (see OrcaClient).
- Only a definite refusal or a broken setup fails the run. Spyhop does not
  retry FAILED runs by itself.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from spyhop.application.context_builder import ContextBuilder, ContextError
from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.config.settings import AgentSettings, JiraSettings
from spyhop.domain.enums import RunStatus, WaitingReason, WorktreeStatus
from spyhop.domain.exceptions import (
    JiraAuthenticationError,
    JiraError,
    OrcaCommandError,
    OrcaUnavailableError,
    OrcaUncertainError,
)
from spyhop.domain.models import (
    AgentSession,
    AutomationRun,
    ResolvedWorkflow,
    TicketDetails,
    Worktree,
)
from spyhop.domain.protocols import OrcaGateway, TicketGateway
from spyhop.infrastructure.git import branch_exists
from spyhop.infrastructure.jira.mapping import TICKET_FIELDS, to_ticket_details
from spyhop.infrastructure.orca.client import new_operation_id
from spyhop.infrastructure.persistence.repositories import (
    RunRepository,
    SessionRepository,
    WorktreeRepository,
)

_log = logging.getLogger(__name__)

PENDING = (RunStatus.DISCOVERED, RunStatus.PREPARING, RunStatus.READY)

# Runs that hold one of the max_concurrent_agents slots. A run waiting for a
# person keeps its slot: its agent resumes as soon as they answer.

BUSY = (RunStatus.RUNNING, RunStatus.WAITING_FOR_HUMAN)

# A launch whose answer was lost is replayed with its operation id. Past this
# age Orca may have forgotten the id and would start a second agent, so the
# run is handed to a person instead.

REPLAY_WINDOW_SECONDS = 15 * 60


class _RunFailed(Exception):
    pass


class _NotYet(Exception):
    """The run cannot advance now but may next cycle."""


@dataclass(frozen=True)
class _Prepared:
    workflow: ResolvedWorkflow
    worktree: Worktree


class AgentLauncher:
    def __init__(
        self,
        runs: RunRepository,
        worktrees: WorktreeRepository,
        sessions: SessionRepository,
        resolver: WorkflowResolver,
        jira: TicketGateway,
        jira_settings: JiraSettings,
        orca: OrcaGateway,
        context: ContextBuilder,
        agents: AgentSettings,
        max_concurrent_agents: int,
        branch_exists: Callable[[Path, str], bool] = branch_exists,
    ) -> None:
        self._runs = runs
        self._worktrees = worktrees
        self._sessions = sessions
        self._resolver = resolver
        self._jira = jira
        self._jira_settings = jira_settings
        self._orca = orca
        self._context = context
        self._agents = agents
        self._max_concurrent_agents = max_concurrent_agents
        self._branch_exists = branch_exists

    async def launch_pending(self) -> list[AutomationRun]:
        """Advances pending runs, oldest first, while there are free slots.
        Returns the runs that reached RUNNING."""

        pending = self._runs.list_by_status(*PENDING)

        if not pending:
            return []

        try:
            await self._orca.ensure_ready()
        except OrcaUnavailableError as error:
            _log.info("%d run(s) waiting for Orca: %s", len(pending), error)

            return []

        launched: list[AutomationRun] = []

        for run in pending:
            busy = self._runs.count_by_status(*BUSY)

            if busy >= self._max_concurrent_agents:
                _log.info(
                    "%d run(s) waiting for a free agent slot (%d of %d busy)",
                    len(pending) - len(launched),
                    busy,
                    self._max_concurrent_agents,
                )

                break

            if await self._advance(run):
                launched.append(run)

        return launched

    async def _advance(self, run: AutomationRun) -> bool:
        try:
            prepared = await self._prepare(run)

            return await self._launch(run, prepared)
        except _RunFailed as error:
            _log.error("%s: run %s failed: %s", run.issue_key, run.id, error)

            self._runs.mark_failed(run.id, str(error))
        except JiraAuthenticationError:
            raise
        except (JiraError, OrcaUnavailableError, OrcaUncertainError, _NotYet) as error:
            _log.warning("%s: will retry next cycle: %s", run.issue_key, error)

        return False

    async def _prepare(self, run: AutomationRun) -> _Prepared:
        workflow = self._workflow(run)

        if run.status is RunStatus.READY and run.worktree_id:
            worktree = self._worktrees.get(run.worktree_id)

            if worktree is not None and worktree.status is WorktreeStatus.ACTIVE:
                return _Prepared(workflow, worktree)

        self._runs.mark_preparing(run.id)

        ticket = await self._read_ticket(run.issue_key)

        try:
            worktree = await self._worktree(run.issue_key, workflow)

            self._context.prepare(worktree.path, ticket, workflow)
        except (OrcaCommandError, ContextError) as error:
            raise _RunFailed(str(error)) from error

        self._runs.mark_ready(run.id, worktree.id)

        return _Prepared(workflow, worktree)

    async def _launch(self, run: AutomationRun, prepared: _Prepared) -> bool:
        session = self._sessions.find_launching(run.id)

        if session is None:
            session = self._sessions.create_launching(
                run.id, self._agents.provider, self._agents.model, new_operation_id()
            )
        elif _replay_expired(session):
            _log.warning(
                "%s: the answer to launch %s was lost too long ago to replay it safely; "
                "check the worktree in Orca",
                run.issue_key,
                session.launch_operation_id,
            )

            self._sessions.mark_failed(session.id)
            self._runs.mark_waiting(
                run.id, WaitingReason.UNKNOWN_STATE, "launch outcome unknown; check Orca"
            )

            return False

        assert session.launch_operation_id is not None

        try:
            result = await self._orca.launch_agent(
                prepared.worktree.orca_worktree_id,
                self._context.launch_goal(run.issue_key, prepared.workflow),
                session.launch_operation_id,
                self._agents.model,
                self._agents.effort,
            )
        except OrcaCommandError as error:
            self._sessions.mark_failed(session.id)

            raise _RunFailed(f"Orca refused the launch: {error}") from error

        self._sessions.mark_launched(session.id, result.agent_handle, result.pane_key)
        self._runs.mark_running(run.id)

        _log.info(
            "%s: agent %s started in %s (branch %s, %s)",
            run.issue_key,
            result.agent_handle,
            prepared.worktree.path,
            prepared.worktree.branch_name,
            "plan review" if prepared.workflow.plan_review else "no plan review",
        )

        return True

    def _workflow(self, run: AutomationRun) -> ResolvedWorkflow:
        project_key = run.issue_key.rsplit("-", 1)[0]
        workflow = self._resolver.resolve_by_name(project_key, run.workflow_name)

        if workflow is None:
            raise _RunFailed(f"workflow {run.workflow_name!r} is no longer configured")

        return workflow

    async def _read_ticket(self, issue_key: str) -> TicketDetails:
        fields = TICKET_FIELDS + self._jira_settings.acceptance_criteria_fields

        try:
            issue = await self._jira.issue(issue_key, fields)
        except JiraError as error:
            if error.status_code == 404:
                raise _RunFailed(f"{issue_key} no longer exists or is not visible") from error

            raise

        comments = await self._jira.comments(issue_key, self._jira_settings.max_comments)

        return to_ticket_details(
            issue,
            comments,
            self._jira_settings.base_url,
            self._jira_settings.acceptance_criteria_fields,
        )

    async def _worktree(self, issue_key: str, workflow: ResolvedWorkflow) -> Worktree:
        """The ticket's worktree: SQLite first, then Orca's reference to the
        ticket, and only then a new one."""

        current = self._worktrees.find_current(issue_key)

        if current is not None:
            if current.status is WorktreeStatus.REMOVING:
                raise _NotYet(f"its worktree {current.path} is being removed")

            if current.status is WorktreeStatus.CLEANUP_BLOCKED:
                # Reopened before its changes were dealt with: the worktree
                # still holds them, so the work goes on there.

                _log.info(
                    "%s: reopened; reusing %s, which could not be removed", issue_key, current.path
                )

                self._worktrees.mark_active(current.id)

            try:
                await self._orca.show_worktree(current.orca_worktree_id)

                return current
            except OrcaCommandError as error:
                if error.code != "selector_not_found":
                    raise

                _log.warning(
                    "%s: worktree %s is gone from Orca; looking for another one",
                    issue_key,
                    current.path,
                )

                self._worktrees.mark_removed(current.id, "not found in Orca")

        orca_worktree = await self._orca.find_worktree_by_reference(issue_key)
        base_branch = workflow.base_branch

        if orca_worktree is None:
            base_branch = self._reopened_base(issue_key, workflow)
            orca_worktree = await self._orca.create_worktree(
                workflow.repository_path,
                issue_key,
                base_branch,
                issue_key,
                workflow.setup,
            )

            _log.info(
                "%s: created worktree %s on branch %s from %s",
                issue_key,
                orca_worktree.path,
                orca_worktree.branch,
                base_branch,
            )
        else:
            _log.info("%s: reusing worktree %s linked in Orca", issue_key, orca_worktree.path)

        return self._worktrees.create(
            issue_key, orca_worktree, workflow.repository_path, base_branch
        )

    def _reopened_base(self, issue_key: str, workflow: ResolvedWorkflow) -> str:
        """A reopened ticket continues from the branch of its removed
        worktree when Git still has it (Orca keeps unmerged branches). Orca
        then checks that branch out again instead of creating a suffixed
        one."""

        previous = self._worktrees.find_last_removed(issue_key)

        if (
            previous is None
            or previous.repository_path != workflow.repository_path
            or not self._branch_exists(previous.repository_path, previous.branch_name)
        ):
            return workflow.base_branch

        _log.info(
            "%s: reopened; continuing from its previous branch %s", issue_key, previous.branch_name
        )

        return previous.branch_name


def _replay_expired(session: AgentSession, now: float | None = None) -> bool:
    operation_id = session.launch_operation_id or ""

    try:
        issued_at = int(operation_id.split("-", 1)[0]) / 1000
    except ValueError:
        return True

    return (now or time.time()) - issued_at > REPLAY_WINDOW_SECONDS
