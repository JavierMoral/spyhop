"""Turns registered runs into agents working in Orca.

DISCOVERED → PREPARING → READY → RUNNING, or FAILED with error_message.

- PREPARING reads the ticket, gets or creates the ticket's worktree and writes
  the agent's context. Every step is safe to repeat, so a run interrupted here
  (Orca or JIRA down, timeout, restart) simply prepares again next cycle.
- READY launches the agent. The operation id is stored before calling Orca and
  reused on every attempt, so the launch is never duplicated (see OrcaClient).
- A worktree script that asks to try again keeps the run in PREPARING with
  waiting_reason worktree_script, and runs again next cycle.
- Only a definite refusal or a broken setup fails the run. Spyhop does not
  retry FAILED runs by itself.
"""

import logging
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    OrcaWorktree,
    ResolvedWorkflow,
    TicketDetails,
    TicketRef,
    Worktree,
)
from spyhop.domain.protocols import OrcaGateway, TicketGateway
from spyhop.infrastructure.disk import GIB, free_bytes
from spyhop.infrastructure.git import branch_exists
from spyhop.infrastructure.jira.mapping import (
    ANCESTOR_FIELDS,
    TICKET_FIELDS,
    parent_key,
    to_ticket_details,
    to_ticket_ref,
)
from spyhop.infrastructure.orca.client import new_operation_id
from spyhop.infrastructure.persistence.repositories import (
    RunRepository,
    SessionRepository,
    WorktreeRepository,
)
from spyhop.infrastructure.worktree_script import (
    WorktreeRequest,
    WorktreeScriptError,
    WorktreeScriptTryAgainError,
    run_worktree_script,
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

# Safety bound on the parent chain; JIRA hierarchies are a few levels deep.

MAX_ANCESTORS = 10

# Characters of the ticket title kept in a worktree's name, cut at a word.

MAX_SLUG_LENGTH = 50


class _RunFailed(Exception):
    pass


class _NotYet(Exception):
    """The run cannot advance now but may next cycle."""


class _Waiting(Exception):
    """Like _NotYet, but the reason is recorded on the run for `status`."""

    def __init__(self, reason: WaitingReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


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
        min_free_disk_gb: float = 0,
        branch_exists: Callable[[Path, str], bool] = branch_exists,
        free_bytes: Callable[[Path], int] = free_bytes,
        worktree_script: Callable[
            [Path, WorktreeRequest, float], Awaitable[Path]
        ] = run_worktree_script,
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
        self._min_free_bytes = int(min_free_disk_gb * GIB)
        self._branch_exists = branch_exists
        self._free_bytes = free_bytes
        self._worktree_script = worktree_script

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
            if await self._waiting_for_disk(run):
                return False

            prepared = await self._prepare(run)

            return await self._launch(run, prepared)
        except _RunFailed as error:
            _log.error("%s: run %s failed: %s", run.issue_key, run.id, error)

            self._runs.mark_failed(run.id, str(error))
        except _Waiting as waiting:
            if run.waiting_reason is not waiting.reason:
                _log.warning("%s: will retry every cycle: %s", run.issue_key, waiting)

            self._runs.note_wait(run.id, waiting.reason, str(waiting))
        except JiraAuthenticationError:
            raise
        except (JiraError, OrcaUnavailableError, OrcaUncertainError, _NotYet) as error:
            _log.warning("%s: will retry next cycle: %s", run.issue_key, error)

        return False

    async def _waiting_for_disk(self, run: AutomationRun) -> bool:
        """Whether the run needs a new worktree and the disk that would hold
        it is below min_free_disk_gb. Checked before reading the ticket, so a
        waiting run costs no JIRA request. The run keeps its pending status:
        it holds no agent slot and resumes by itself once space is freed,
        for example when a closed ticket's worktree is removed."""

        if self._min_free_bytes <= 0 or self._worktrees.find_current(run.issue_key):
            return False

        workflow = self._workflow(run)

        try:
            location = await self._orca.worktree_base_path(workflow.repository_path)
        except OrcaCommandError:
            location = None

        location = location or workflow.repository_path
        free = self._free_bytes(location)

        if free >= self._min_free_bytes:
            return False

        message = (
            f"waiting for disk space: {free / GIB:.1f} GB free in {location}, "
            f"{self._min_free_bytes / GIB:g} GB required to create a worktree"
        )

        if run.waiting_reason is not WaitingReason.DISK_SPACE:
            _log.warning("%s: %s", run.issue_key, message)

        self._runs.note_wait(run.id, WaitingReason.DISK_SPACE, message)

        return True

    async def _prepare(self, run: AutomationRun) -> _Prepared:
        workflow = self._workflow(run)

        if run.status is RunStatus.READY and run.worktree_id:
            worktree = self._worktrees.get(run.worktree_id)

            if worktree is not None and worktree.status is WorktreeStatus.ACTIVE:
                return _Prepared(workflow, worktree)

        self._runs.mark_preparing(run.id)

        ticket = await self._read_ticket(run.issue_key)

        try:
            worktree = await self._worktree(ticket, workflow)

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
        try:
            issue = await self._jira.issue(issue_key, TICKET_FIELDS)
        except JiraError as error:
            if error.status_code == 404:
                raise _RunFailed(f"{issue_key} no longer exists or is not visible") from error

            raise

        return to_ticket_details(issue, self._jira_settings.base_url, await self._ancestors(issue))

    async def _ancestors(self, issue: dict[str, Any]) -> tuple[TicketRef, ...]:
        """The ticket's parent, grandparent and so on, so the agent knows which
        tickets to read. A parent that is not visible ends the chain."""

        ancestors: list[TicketRef] = []
        key = parent_key(issue)

        while key is not None and len(ancestors) < MAX_ANCESTORS:
            if key == issue["key"] or any(ancestor.key == key for ancestor in ancestors):
                break

            try:
                parent = await self._jira.issue(key, ANCESTOR_FIELDS)
            except JiraError as error:
                if error.status_code == 404:
                    break

                raise

            ancestors.append(to_ticket_ref(parent))
            key = parent_key(parent)

        return tuple(ancestors)

    async def _worktree(self, ticket: TicketDetails, workflow: ResolvedWorkflow) -> Worktree:
        """The ticket's worktree: SQLite first, then Orca's reference to the
        ticket, and only then a new one."""

        issue_key = ticket.key
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
            name = worktree_name(issue_key, ticket.summary)
            branch = name
            previous = self._reopened(issue_key, workflow)

            if previous is not None:
                base_branch = previous.branch_name
                branch = previous.branch_name

            if workflow.worktree_script is not None:
                orca_worktree = await self._scripted_worktree(
                    WorktreeRequest(issue_key, name, branch, base_branch, workflow.repository_path),
                    workflow.worktree_script,
                    workflow.worktree_script_timeout,
                )
            else:
                if previous is not None:
                    # The same name makes Orca check the kept branch out
                    # again instead of creating a new one, even if the
                    # title changed.

                    name = previous.path.name

                orca_worktree = await self._orca.create_worktree(
                    workflow.repository_path,
                    name,
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

    async def _scripted_worktree(
        self, request: WorktreeRequest, script: Path, time_limit: float
    ) -> OrcaWorktree:
        """Creates the worktree with the repository's script and links it to
        the ticket in Orca. Until Orca has seen the new worktree in Git, the
        run waits; next cycle the script runs again and answers with the
        worktree it already created. A script that asks to try again runs
        again next cycle too."""

        try:
            path = await self._worktree_script(script, request, time_limit)
        except WorktreeScriptTryAgainError as error:
            raise _Waiting(WaitingReason.WORKTREE_SCRIPT, str(error)) from error
        except WorktreeScriptError as error:
            raise _RunFailed(str(error)) from error

        try:
            return await self._orca.link_worktree(path, request.issue_key)
        except OrcaCommandError as error:
            if error.code != "selector_not_found":
                raise

            raise _NotYet(f"Orca does not list {path} yet") from error

    def _reopened(self, issue_key: str, workflow: ResolvedWorkflow) -> Worktree | None:
        """The removed worktree a reopened ticket continues from, when Git
        still has its branch (Orca keeps unmerged branches)."""

        previous = self._worktrees.find_last_removed(issue_key)

        if (
            previous is None
            or previous.repository_path != workflow.repository_path
            or not self._branch_exists(previous.repository_path, previous.branch_name)
        ):
            return None

        _log.info(
            "%s: reopened; continuing from its previous branch %s", issue_key, previous.branch_name
        )

        return previous


def worktree_name(issue_key: str, summary: str) -> str:
    """The ticket key followed by its title as a slug: LPD-1234-fix-the-import."""

    ascii_summary = unicodedata.normalize("NFKD", summary).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", ascii_summary.lower())
    slug = ""

    for word in words:
        longer = f"{slug}-{word}" if slug else word

        if len(longer) > MAX_SLUG_LENGTH:
            break

        slug = longer

    if not slug and words:
        slug = words[0][:MAX_SLUG_LENGTH]

    return f"{issue_key}-{slug}" if slug else issue_key


def _replay_expired(session: AgentSession, now: float | None = None) -> bool:
    operation_id = session.launch_operation_id or ""

    try:
        issued_at = int(operation_id.split("-", 1)[0]) / 1000
    except ValueError:
        return True

    return (now or time.time()) - issued_at > REPLAY_WINDOW_SECONDS
