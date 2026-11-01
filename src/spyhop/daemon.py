"""The long-running process started by `spyhop run`."""

import asyncio
import logging
import signal
import sqlite3
from contextlib import AsyncExitStack, suppress
from datetime import timedelta
from pathlib import Path

from spyhop.application.context_builder import ContextBuilder, template_environment
from spyhop.application.launcher import AgentLauncher
from spyhop.application.monitor import JiraMonitor
from spyhop.application.orchestrator import Orchestrator
from spyhop.application.supervisor import AgentSupervisor
from spyhop.application.transition_detector import TransitionDetector
from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.config.credentials import credentials_from_environment
from spyhop.config.paths import database_file, state_dir, user_templates_dir
from spyhop.config.settings import Settings
from spyhop.domain.exceptions import (
    JiraAuthenticationError,
    JiraError,
    JiraRateLimitedError,
    OrcaError,
)
from spyhop.infrastructure.jira.client import JiraClient
from spyhop.infrastructure.orca.client import OrcaClient
from spyhop.infrastructure.orca.discovery import find_orca_executable
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import (
    ObservationRepository,
    PollerStateRepository,
    RunRepository,
    SessionRepository,
    WorktreeRepository,
)

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
        agents = None if dry_run else _agents(settings, connection, resolver, jira)
        monitor = JiraMonitor(
            detector=TransitionDetector(jira, resolver, user.time_zone),
            orchestrator=Orchestrator(RunRepository(connection), dry_run),
            poller_state=PollerStateRepository(connection),
            observations=ObservationRepository(connection),
            margin=timedelta(minutes=settings.jira.transition_margin_minutes),
        )

        stop = _stop_event()

        while not stop.is_set():
            delay = float(settings.application.polling_interval)

            try:
                await monitor.poll()
            except JiraAuthenticationError:
                raise
            except JiraRateLimitedError as error:
                delay = max(delay, error.retry_after or delay)

                _log.warning("JIRA rate limit reached; next poll in %.0fs", delay)
                monitor.record_error(str(error))
            except JiraError as error:
                _log.warning("Poll failed, will retry: %s", error)
                monitor.record_error(str(error))

            if agents is not None:
                supervisor, launcher = agents

                # Supervising first frees the slots of finished runs for the
                # launches of this same cycle.

                try:
                    await supervisor.supervise()
                except OrcaError as error:
                    _log.warning("Supervision failed, will retry: %s", error)

                try:
                    await launcher.launch_pending()
                except JiraAuthenticationError:
                    raise
                except (JiraError, OrcaError) as error:
                    _log.warning("Launching failed, will retry: %s", error)

            if once:
                return

            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), delay)

        _log.info("Spyhop stopped")


def _agents(
    settings: Settings,
    connection: sqlite3.Connection,
    resolver: WorkflowResolver,
    jira: JiraClient,
) -> tuple[AgentSupervisor, AgentLauncher] | None:
    try:
        executable = find_orca_executable(settings.orca.executable)
    except OrcaError as error:
        _log.error("Agents will not be launched until this is fixed: %s", error)

        return None

    # Orca replays a lost launch only when it is retried from the same
    # directory, so every command runs from Spyhop's state directory.

    cwd = state_dir()

    cwd.mkdir(parents=True, exist_ok=True)

    orca = OrcaClient(
        executable, cwd, settings.orca.startup_timeout, settings.orca.operation_timeout
    )

    _log.info("Launching and supervising agents through %s", executable)

    runs = RunRepository(connection)
    worktrees = WorktreeRepository(connection)
    sessions = SessionRepository(connection)
    context = ContextBuilder(
        template_environment(user_templates_dir()),
        executable,
        settings.orca.plan_review_status,
        settings.orca.plan_approved_status,
    )
    supervisor = AgentSupervisor(
        runs=runs,
        worktrees=worktrees,
        sessions=sessions,
        orca=orca,
        context=context,
        plan_review_status=settings.orca.plan_review_status,
        plan_approved_status=settings.orca.plan_approved_status,
    )
    launcher = AgentLauncher(
        runs=runs,
        worktrees=worktrees,
        sessions=sessions,
        resolver=resolver,
        jira=jira,
        jira_settings=settings.jira,
        orca=orca,
        context=context,
        agents=settings.agents,
        max_concurrent_agents=settings.application.max_concurrent_agents,
    )

    return supervisor, launcher


def _stop_event() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_number, stop.set)

    return stop
