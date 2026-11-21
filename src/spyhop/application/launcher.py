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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from spyhop.application.agent_starter import AgentStarter, LaunchOutcomeUnknown
from spyhop.application.context_builder import ContextBuilder, ContextError
from spyhop.application.launch_errors import NotYet, RunFailed, Waiting
from spyhop.application.supervisor_session import SupervisorSession
from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.application.worktree_provisioner import WorktreeProvisioner
from spyhop.config.settings import AgentSettings, JiraSettings
from spyhop.domain.clock import Clock, utc_now
from spyhop.domain.enums import RunStatus, WaitingReason, WorktreeStatus
from spyhop.domain.exceptions import (
    JiraAuthenticationError,
    JiraError,
    OrcaCommandError,
    OrcaUnavailableError,
    OrcaUncertainError,
)
from spyhop.domain.models import (
    AutomationRun,
    ChecksPhase,
    ResolvedWorkflow,
    TicketDetails,
    TicketRef,
    Worktree,
)
from spyhop.domain.protocols import TerminalHost, TicketGateway, WorktreeHost, ensure_ready
from spyhop.infrastructure.jira.mapping import (
    ANCESTOR_FIELDS,
    TICKET_FIELDS,
    parent_key,
    to_ticket_details,
    to_ticket_ref,
)
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

# Safety bound on the parent chain; JIRA hierarchies are a few levels deep.

MAX_ANCESTORS = 10


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
        worktree_host: WorktreeHost,
        terminal_host: TerminalHost,
        context: ContextBuilder,
        agents: AgentSettings,
        max_concurrent_agents: int,
        provisioner: WorktreeProvisioner,
        supervisor_session: SupervisorSession | None = None,
        notify: Callable[[str, str], Awaitable[None]] | None = None,
        now: Clock = utc_now,
    ) -> None:
        self._runs = runs
        self._supervisor_session = supervisor_session
        self._notify = notify
        self._worktrees = worktrees
        self._sessions = sessions
        self._resolver = resolver
        self._jira = jira
        self._jira_settings = jira_settings
        self._worktree_host = worktree_host
        self._terminal_host = terminal_host
        self._context = context
        self._agents = agents
        self._max_concurrent_agents = max_concurrent_agents
        self._provisioner = provisioner
        self._starter = AgentStarter(sessions, terminal_host, agents, now)

    async def launch_pending(self) -> list[AutomationRun]:
        """Advances pending runs, oldest first, while there are free slots.
        Returns the runs that reached RUNNING."""

        pending = self._runs.list_by_status(*PENDING)

        if not pending:
            return []

        try:
            await ensure_ready(self._worktree_host, self._terminal_host)
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
        except RunFailed as error:
            _log.error("%s: run %s failed: %s", run.issue_key, run.id, error)

            self._runs.mark_failed(run.id, str(error))

            # Only a run with a worktree can have a supervisor session.

            worktree = self._worktrees.find_current(run.issue_key)
            text = f"la run ha fallado al arrancar: {error}"

            if worktree is not None:
                await self._tell_notices(run, worktree)
                await self._tell(worktree, text)
            else:
                await self._notice(run, text, queue=False)
        except Waiting as waiting:
            if run.waiting_reason is not waiting.reason:
                _log.warning("%s: will retry every cycle: %s", run.issue_key, waiting)

                await self._notice(run, f"espera ({waiting.reason}): {waiting}")

            self._runs.note_wait(run.id, waiting.reason, str(waiting))
        except JiraAuthenticationError:
            raise
        except (JiraError, OrcaUnavailableError, OrcaUncertainError, NotYet) as error:
            _log.warning("%s: will retry next cycle: %s", run.issue_key, error)

        return False

    async def _waiting_for_disk(self, run: AutomationRun) -> bool:
        """Whether the run waits for disk space for a new worktree. The
        launcher checks this before it reads the ticket, so a waiting run
        costs no JIRA request. The run keeps its pending status: it holds no
        agent slot and continues by itself when the disk has space again, for
        example after the janitor removes the worktree of a closed ticket."""

        message = await self._provisioner.disk_shortage(run.issue_key, lambda: self._workflow(run))

        if message is None:
            return False

        if run.waiting_reason is not WaitingReason.DISK_SPACE:
            _log.warning("%s: %s", run.issue_key, message)

            await self._notice(run, f"espera ({WaitingReason.DISK_SPACE}): {message}")

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
            worktree = await self._provisioner.worktree(ticket, workflow)

            self._context.prepare(worktree.path, ticket, workflow)
        except (OrcaCommandError, ContextError) as error:
            raise RunFailed(str(error)) from error

        # CLAUDE-TASK.md describes this pipeline, and run.phase is an index
        # into it from now on.

        if run.pipeline is None:
            self._runs.set_pipeline(run.id, workflow.phases)

        self._runs.mark_ready(run.id, worktree.id)
        await self._tell_notices(run, worktree)

        return _Prepared(workflow, worktree)

    async def _launch(self, run: AutomationRun, prepared: _Prepared) -> bool:
        # The implementation of a plan comes later, from a new agent on
        # agents.model (see AgentSupervisor).

        if isinstance(prepared.workflow.phases[0], ChecksPhase):
            # The supervisor runs the checks, and then starts the agent.

            self._runs.mark_running(run.id)

            _log.info(
                "%s: checkpoints start in %s (branch %s)",
                run.issue_key,
                prepared.worktree.path,
                prepared.worktree.branch_name,
            )

            await self._tell(
                prepared.worktree,
                f"worktree listo en {prepared.worktree.path} (rama "
                f"{prepared.worktree.branch_name}); empiezan los checkpoints",
            )

            return True

        plan_review = prepared.workflow.plan_review

        try:
            session = await self._starter.start(
                run,
                prepared.worktree,
                self._agents.plan_model if plan_review else self._agents.model,
                self._context.launch_goal(run.issue_key, prepared.workflow),
                "plan" if plan_review else "implement",
            )
        except LaunchOutcomeUnknown:
            self._runs.mark_waiting(
                run.id, WaitingReason.UNKNOWN_STATE, "launch outcome unknown; check Orca"
            )

            if self._supervisor_session is not None:
                await self._supervisor_session.alert(
                    prepared.worktree, "unknown_state: launch outcome unknown; check Orca"
                )

            return False
        except OrcaCommandError as error:
            raise RunFailed(f"Orca refused the launch: {error}") from error

        self._runs.mark_running(run.id)
        await self._starter.label(run, session)

        _log.info(
            "%s: agent %s started in %s (branch %s, %s, %s)",
            run.issue_key,
            session.agent_handle,
            prepared.worktree.path,
            prepared.worktree.branch_name,
            "plan review" if prepared.workflow.plan_review else "no plan review",
            session.model,
        )

        await self._tell(
            prepared.worktree,
            f"worktree listo en {prepared.worktree.path} (rama {prepared.worktree.branch_name}); "
            f"agente de {session.role} arrancado ({session.model})",
        )

        return True

    async def _tell(self, worktree: Worktree, text: str) -> None:
        if self._supervisor_session is not None:
            await self._supervisor_session.tell(worktree, text)

    async def _notice(self, run: AutomationRun, text: str, queue: bool = True) -> None:
        """Tells about a run that has no worktree, so no supervisor session:
        with a desktop notification now, and, with ``queue``, to the session
        once the worktree exists."""

        if self._notify is not None:
            await self._notify(f"Spyhop: {run.issue_key}", text)

        if queue:
            self._runs.add_notice(run.id, text)

    async def _tell_notices(self, run: AutomationRun, worktree: Worktree) -> None:
        if self._supervisor_session is None:
            return

        # The loop consumes a notice only when the notice reaches the session.

        for command_id, text in self._runs.pending_notices(run.id):
            if not await self._supervisor_session.tell(worktree, f"antes del worktree: {text}"):
                return

            self._runs.consume_command(command_id)

    def _workflow(self, run: AutomationRun) -> ResolvedWorkflow:
        workflow = self._resolver.resolve_for_run(run)

        if workflow is None:
            raise RunFailed(f"workflow {run.workflow_name!r} is no longer configured")

        return workflow

    async def _read_ticket(self, issue_key: str) -> TicketDetails:
        try:
            issue = await self._jira.issue(issue_key, TICKET_FIELDS)
        except JiraError as error:
            if error.status_code == 404:
                raise RunFailed(f"{issue_key} no longer exists or is not visible") from error

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
