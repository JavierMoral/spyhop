"""The long-running process started by `spyhop run`."""

import asyncio
import logging
import signal
from contextlib import AsyncExitStack, suppress
from datetime import timedelta
from pathlib import Path

from spyhop.application.monitor import JiraMonitor
from spyhop.application.orchestrator import Orchestrator
from spyhop.application.transition_detector import TransitionDetector
from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.config.credentials import credentials_from_environment
from spyhop.config.paths import database_file
from spyhop.config.settings import Settings
from spyhop.domain.exceptions import JiraAuthenticationError, JiraError, JiraRateLimitedError
from spyhop.infrastructure.jira.client import JiraClient
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import (
    ObservationRepository,
    PollerStateRepository,
    RunRepository,
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

            if once:
                return

            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), delay)

        _log.info("Spyhop stopped")


def _stop_event() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_number, stop.set)

    return stop
