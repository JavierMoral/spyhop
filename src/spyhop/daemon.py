"""The long-running process started by `spyhop run`."""

import asyncio
import logging
import os
import signal
import sqlite3
from contextlib import AsyncExitStack, suppress
from datetime import timedelta
from pathlib import Path

from spyhop.application.checkpoint_planner import CheckpointPlanner
from spyhop.application.checkpoint_runner import CheckRunner
from spyhop.application.context_builder import ContextBuilder, template_environment
from spyhop.application.janitor import WorktreeJanitor, WorktreeRemover
from spyhop.application.knowledge import KnowledgeWriter
from spyhop.application.launcher import AgentLauncher
from spyhop.application.monitor import JiraMonitor
from spyhop.application.orchestrator import Orchestrator
from spyhop.application.supervisor import AgentSupervisor
from spyhop.application.supervisor_session import SupervisorSession
from spyhop.application.transition_detector import TransitionDetector
from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.config.credentials import credentials_from_environment
from spyhop.config.paths import (
    claude_agents_dir,
    database_file,
    spyhop_executable,
    state_dir,
    user_templates_dir,
)
from spyhop.config.settings import Settings
from spyhop.domain.exceptions import (
    JiraAuthenticationError,
    JiraError,
    JiraRateLimitedError,
    OrcaError,
)
from spyhop.infrastructure.agent_state import AGENT_STATES
from spyhop.infrastructure.desktop import notify
from spyhop.infrastructure.jira.client import JiraClient
from spyhop.infrastructure.orca.client import OrcaClient
from spyhop.infrastructure.orca.discovery import find_orca_executable
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import (
    CheckpointRunRepository,
    ObservationRepository,
    PollerStateRepository,
    RunRepository,
    SessionRepository,
    WorktreeRepository,
)
from spyhop.infrastructure.process.units import SystemdUnits
from spyhop.infrastructure.service.systemd import WAKE_SIGNAL

_log = logging.getLogger(__name__)


async def run_daemon(settings: Settings, once: bool = False, database: Path | None = None) -> None:
    dry_run = settings.application.dry_run
    database = database or database_file(dry_run)

    async with AsyncExitStack() as stack:
        jira = await stack.enter_async_context(
            JiraClient(
                settings.jira.base_url,
                credentials_from_environment(settings.jira),
                settings.jira.request_timeout,
            )
        )

        user = await jira.myself()

        _log.info(
            "Spyhop started%s as %s (JIRA time zone %s), database %s",
            " in dry-run mode" if dry_run else "",
            user.display_name,
            user.time_zone,
            database,
        )

        connection = connect(database)

        stack.callback(connection.close)

        resolver = WorkflowResolver(settings)
        agents = None
        agents_error: str | None = None
        monitor = JiraMonitor(
            detector=TransitionDetector(jira, resolver, user.time_zone),
            orchestrator=Orchestrator(RunRepository(connection), dry_run),
            poller_state=PollerStateRepository(connection),
            observations=ObservationRepository(connection),
            margin=timedelta(minutes=settings.jira.transition_margin_minutes),
        )

        stop, wake = _signal_events()
        loop = asyncio.get_running_loop()
        next_poll = loop.time()
        rate_limited = False

        while not stop.is_set():
            wake.clear()

            # A wake from a local command supervises and launches now. The
            # JIRA poll and the cleanup stay on the poll interval, and no
            # launch reads JIRA while its rate limit lasts.

            polled = loop.time() >= next_poll

            if polled:
                delay = float(settings.application.polling_interval)
                rate_limited = False

                try:
                    await monitor.poll()
                except JiraAuthenticationError:
                    raise
                except JiraRateLimitedError as error:
                    rate_limited = True
                    delay = max(delay, error.retry_after or delay)

                    _log.warning("JIRA rate limit reached; next poll in %.0fs", delay)
                    monitor.record_error(str(error))
                except JiraError as error:
                    _log.warning("Poll failed, will retry: %s", error)
                    monitor.record_error(str(error))

                next_poll = loop.time() + delay

            if agents is None and not dry_run:
                # Orca may be missing at start, for example while the session
                # is still coming up, so it is looked for again every cycle.

                try:
                    agents = _agents(settings, connection, resolver, jira)
                except OrcaError as error:
                    if str(error) != agents_error:
                        _log.error("Agents will not be launched until this is fixed: %s", error)

                    agents_error = str(error)

            if agents is not None:
                janitor, supervisor, launcher = agents

                # Cleaning up and supervising first free the slots of closed and
                # finished runs for the launches of this same cycle. The cleanup
                # reads JIRA, so it runs only with the poll.

                if polled:
                    try:
                        await janitor.sweep()
                    except JiraAuthenticationError:
                        raise
                    except (JiraError, OrcaError) as error:
                        _log.warning("Cleanup failed, will retry: %s", error)

                try:
                    await supervisor.supervise()
                except OrcaError as error:
                    _log.warning("Supervision failed, will retry: %s", error)

                if polled or not rate_limited:
                    try:
                        await launcher.launch_pending()
                    except JiraAuthenticationError:
                        raise
                    except (JiraError, OrcaError) as error:
                        rate_limited = rate_limited or isinstance(error, JiraRateLimitedError)

                        _log.warning("Launching failed, will retry: %s", error)

            if once:
                return

            with suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), max(next_poll - loop.time(), 0))

        _log.info("Spyhop stopped")


def _agents(
    settings: Settings,
    connection: sqlite3.Connection,
    resolver: WorkflowResolver,
    jira: JiraClient,
) -> tuple[WorktreeJanitor, AgentSupervisor, AgentLauncher]:
    orca = orca_client(settings)
    executable = orca.executable

    _log.info("Launching and supervising agents through %s", executable)

    runs = RunRepository(connection)
    worktrees = WorktreeRepository(connection)
    sessions = SessionRepository(connection)
    context = ContextBuilder(
        template_environment(user_templates_dir()),
        executable,
        settings.orca.plan_review_status,
        settings.orca.plan_approved_status,
        settings.agents.review_command,
        settings.knowledge.recall_command,
        spyhop_executable(),
    )
    supervisor_session = SupervisorSession(
        worktrees=worktrees,
        terminal_host=orca,
        board=orca,
        context=context,
        spyhop=spyhop_executable(),
        model=settings.agents.model,
    )
    supervisor = AgentSupervisor(
        runs=runs,
        worktrees=worktrees,
        sessions=sessions,
        worktree_host=orca,
        terminal_host=orca,
        context=context,
        agents=settings.agents,
        plan_review_status=settings.orca.plan_review_status,
        plan_approved_status=settings.orca.plan_approved_status,
        resolver=resolver,
        check_runner=CheckRunner(
            checkpoint_runs=CheckpointRunRepository(connection),
            units=SystemdUnits(),
            planner=CheckpointPlanner(claude_agents_dir()),
            tickets=jira,
            spyhop=spyhop_executable(),
            results_dir=state_dir() / "checkpoints",
            timeout=timedelta(minutes=settings.agents.checkpoint_timeout_minutes),
            supervisor_session=supervisor_session,
        ),
        supervisor_session=supervisor_session,
        agent_states_dir=state_dir() / AGENT_STATES,
        knowledge=KnowledgeWriter(
            CheckpointRunRepository(connection),
            settings.knowledge.write_script,
            settings.knowledge.write_timeout,
        )
        if settings.knowledge.write_script
        else None,
    )
    launcher = AgentLauncher(
        runs=runs,
        worktrees=worktrees,
        sessions=sessions,
        resolver=resolver,
        jira=jira,
        jira_settings=settings.jira,
        worktree_host=orca,
        terminal_host=orca,
        context=context,
        agents=settings.agents,
        max_concurrent_agents=settings.application.max_concurrent_agents,
        min_free_disk_gb=settings.application.min_free_disk_gb,
        notify=notify,
        supervisor_session=supervisor_session,
    )

    janitor = WorktreeJanitor(
        remover=WorktreeRemover(
            runs,
            worktrees,
            worktree_host=orca,
            terminal_host=orca,
            board=orca,
            units=SystemdUnits(),
            repositories=settings.repositories,
        ),
        runs=runs,
        worktrees=worktrees,
        jira=jira,
        settings=settings.cleanup,
    )

    return janitor, supervisor, launcher


def orca_client(settings: Settings) -> OrcaClient:
    executable = find_orca_executable(settings.orca.executable)

    # Orca replays a lost launch only when it is retried from the same
    # directory, so every command runs from Spyhop's state directory.

    cwd = state_dir()

    cwd.mkdir(parents=True, exist_ok=True)

    return OrcaClient(
        executable, cwd, settings.orca.startup_timeout, settings.orca.operation_timeout
    )


def _signal_events() -> tuple[asyncio.Event, asyncio.Event]:
    """SIGINT and SIGTERM stop the daemon. WAKE_SIGNAL, from wake_service,
    starts a cycle now. A stop also wakes the daemon from its wait."""

    stop = asyncio.Event()
    wake = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_stop() -> None:
        stop.set()
        wake.set()

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_number, on_stop)

    # Only under systemd: in a terminal, each resize of the window sends
    # SIGWINCH, and wake_service reaches only the service.

    if os.environ.get("INVOCATION_ID"):
        loop.add_signal_handler(WAKE_SIGNAL, wake.set)

    return stop, wake
