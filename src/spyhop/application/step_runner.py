"""Runs the steps of a run in systemd user units, outside the Spyhop service.

Each step is a row that Spyhop stores before its unit starts. The unit runs
`spyhop steps exec`, which writes the result of the agent to the output file
of the row, and its steps to a log file next to it. The unit has a fixed
name, and its command does nothing when another one runs for the same row
or its result exists, so a step never runs twice.
"""

import fcntl
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal

from spyhop.domain.clock import Clock, utc_now
from spyhop.domain.exceptions import UnitError
from spyhop.domain.models import CheckpointRun
from spyhop.domain.protocols import UnitGateway
from spyhop.infrastructure.persistence.repositories import CheckpointRunRepository

_log = logging.getLogger(__name__)

UNIT_PREFIX = "spyhop-checkpoint-"


def unit_pattern(run_id: str) -> str:
    """The units of the steps of a run."""

    return f"{UNIT_PREFIX}{run_id}-*"


def log_file(row: CheckpointRun) -> Path:
    return row.output_file.with_suffix(".log")


@dataclass(frozen=True)
class Poll:
    """``result``: the output file exists. ``unstarted``: Spyhop stopped
    between the row and the start of its unit. ``timeout``: no result in
    time, and the process stopped. ``blocked``: no result in time, and the
    process still runs; see ``message``. ``running``: none of these."""

    state: Literal["result", "unstarted", "timeout", "blocked", "running"]
    message: str = ""


class StepRunner:
    def __init__(
        self,
        rows: CheckpointRunRepository,
        units: UnitGateway,
        spyhop: Path,
        timeout: timedelta,
        now: Clock = utc_now,
    ) -> None:
        self._rows = rows
        self._units = units
        self._spyhop = spyhop
        self._timeout = timeout
        self._now = now

    async def start(self, row: CheckpointRun, arguments: Sequence[str], cwd: Path) -> None:
        """Starts the unit of the row with `spyhop steps exec` and
        ``arguments``. A second start of the unit does nothing. Raises
        UnitError when the unit does not start."""

        unit = f"{UNIT_PREFIX}{row.run_id}-{row.id}"

        await self._units.start(
            unit, [str(self._spyhop), "steps", "exec", *arguments], cwd, log_file(row)
        )

        self._rows.set_unit(row.id, unit, self._now())

    async def poll(self, row: CheckpointRun) -> Poll:
        """The state of a row that runs. Stops the unit of a row without a
        result in time. The timeout counts from the start of the unit."""

        if row.output_file.is_file():
            return Poll("result")

        if row.unit is None:
            return Poll("unstarted")

        if self._now() - row.started_at <= self._timeout:
            return Poll("running")

        try:
            await self._units.stop(row.unit)
        except UnitError as error:
            _log.warning("%s: %s", row.name, error)

        if _still_runs(row.output_file):
            return Poll(
                "blocked",
                f"checkpoint {row.name} has no result after {self._timeout} and its "
                f"process still runs; stop it with `systemctl --user stop {row.unit}`",
            )

        return Poll("timeout", f"no result after {self._timeout}")


def _still_runs(output_file: Path) -> bool:
    """Whether `spyhop steps exec` holds the lock of the result file."""

    lock_file = output_file.parent / f"{output_file.name}.lock"

    if not lock_file.is_file():
        return False

    with lock_file.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True

        fcntl.flock(lock, fcntl.LOCK_UN)

    return False
