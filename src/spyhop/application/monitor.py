"""One polling cycle: detect entries since the watermark and register them.

The watermark only advances after a successful cycle, so transitions that
happen while JIRA is unreachable or the machine is off are picked up later.
"""

import logging
from datetime import datetime, timedelta

from spyhop.application.orchestrator import Orchestrator
from spyhop.application.transition_detector import TransitionDetector
from spyhop.domain.models import AutomationRun
from spyhop.infrastructure.persistence.repositories import (
    ObservationRepository,
    PollerStateRepository,
    utc_now,
)

_log = logging.getLogger(__name__)


class JiraMonitor:
    def __init__(
        self,
        detector: TransitionDetector,
        orchestrator: Orchestrator,
        poller_state: PollerStateRepository,
        observations: ObservationRepository,
        margin: timedelta,
    ) -> None:
        self._detector = detector
        self._orchestrator = orchestrator
        self._poller_state = poller_state
        self._observations = observations
        self._margin = margin

    async def poll(self, now: datetime | None = None) -> list[AutomationRun]:
        started_at = now or utc_now()
        watermark = self._poller_state.watermark()

        if watermark is None:
            # First start: nothing that happened before Spyhop existed is processed.

            _log.info("First run: watching transitions from %s on", started_at.isoformat())

            self._poller_state.record_success(started_at)

            return []

        entries = await self._detector.discover_entries(watermark - self._margin)

        runs = []

        for entry in entries:
            self._observations.record(entry.issue)

            run = self._orchestrator.process_entry(entry)

            if run is not None:
                runs.append(run)

        self._poller_state.record_success(started_at)

        return runs

    def record_error(self, message: str) -> None:
        self._poller_state.record_error(message)
