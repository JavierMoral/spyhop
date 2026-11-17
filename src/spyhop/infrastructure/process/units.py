"""Runs a command in its own systemd user unit.

A unit is not in the control group of the Spyhop service, so a restart of the
service does not stop it. Its output goes to a log file. The unit name is
fixed by the caller, so a second start of the same unit does nothing.
"""

from collections.abc import Sequence
from pathlib import Path

from spyhop.domain.exceptions import UnitError
from spyhop.infrastructure.process.command_runner import (
    CommandResult,
    CommandRunner,
    CommandTimeoutError,
    SubprocessRunner,
)

TIME_LIMIT = 30

# systemctl stop: the unit is not loaded.

NOT_LOADED = 5


class SystemdUnits:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or SubprocessRunner()

    async def start(self, unit: str, command: Sequence[str], cwd: Path, log: Path) -> None:
        """Starts ``command`` in the unit ``unit``. Does nothing when the unit
        is already loaded."""

        log.parent.mkdir(parents=True, exist_ok=True)

        result = await self._run(
            [
                "systemd-run",
                "--user",
                f"--unit={unit}",
                "--collect",
                "--quiet",
                f"--working-directory={cwd}",
                f"--property=StandardOutput=append:{log}",
                f"--property=StandardError=append:{log}",
                "--",
                *command,
            ]
        )

        if result.exit_code != 0 and "already loaded" not in result.stderr:
            raise UnitError(f"systemd-run {unit}: {result.stderr.strip() or result.exit_code}")

    async def stop(self, pattern: str) -> None:
        """Stops the units whose names match ``pattern``, for example
        ``spyhop-checkpoint-<run>-*``."""

        result = await self._run(["systemctl", "--user", "stop", pattern])

        if result.exit_code not in (0, NOT_LOADED):
            raise UnitError(
                f"systemctl stop {pattern}: {result.stderr.strip() or result.exit_code}"
            )

    async def _run(self, args: Sequence[str]) -> CommandResult:
        try:
            return await self._runner.run(args, TIME_LIMIT)
        except (OSError, CommandTimeoutError) as error:
            raise UnitError(f"{args[0]}: {error}") from error
