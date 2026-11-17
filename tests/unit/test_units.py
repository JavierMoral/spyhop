"""Checkpoints run in systemd user units."""

from collections.abc import Sequence
from pathlib import Path

import pytest

from spyhop.domain.exceptions import UnitError
from spyhop.infrastructure.process.command_runner import CommandResult
from spyhop.infrastructure.process.units import SystemdUnits


class FakeRunner:
    def __init__(self, *answers: CommandResult | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[list[str]] = []

    async def run(
        self, args: Sequence[str], time_limit: float, cwd: Path | None = None
    ) -> CommandResult:
        self.calls.append(list(args))

        answer = self.answers.pop(0)

        if isinstance(answer, Exception):
            raise answer

        return answer


OK = CommandResult(0, "", "")


async def test_start_runs_the_command_in_its_own_unit_with_a_log(tmp_path: Path) -> None:
    runner = FakeRunner(OK)
    log = tmp_path / "results" / "row.log"

    await SystemdUnits(runner).start("spyhop-checkpoint-r-1", ["spyhop", "x"], tmp_path, log)

    assert runner.calls == [
        [
            "systemd-run",
            "--user",
            "--unit=spyhop-checkpoint-r-1",
            "--collect",
            "--quiet",
            f"--working-directory={tmp_path}",
            f"--property=StandardOutput=append:{log}",
            f"--property=StandardError=append:{log}",
            "--",
            "spyhop",
            "x",
        ]
    ]
    assert log.parent.is_dir()


async def test_a_second_start_of_a_loaded_unit_is_not_an_error(tmp_path: Path) -> None:
    loaded = CommandResult(
        1, "", "Failed to start transient service unit: Unit u.service was already loaded"
    )

    await SystemdUnits(FakeRunner(loaded)).start("u", ["x"], tmp_path, tmp_path / "u.log")


async def test_a_start_that_fails_raises(tmp_path: Path) -> None:
    runner = FakeRunner(CommandResult(1, "", "Failed to connect to bus"))

    with pytest.raises(UnitError, match="Failed to connect to bus"):
        await SystemdUnits(runner).start("u", ["x"], tmp_path, tmp_path / "u.log")


async def test_a_missing_systemd_run_raises(tmp_path: Path) -> None:
    runner = FakeRunner(FileNotFoundError("systemd-run"))

    with pytest.raises(UnitError):
        await SystemdUnits(runner).start("u", ["x"], tmp_path, tmp_path / "u.log")


async def test_stop_of_a_unit_that_is_not_loaded_is_not_an_error() -> None:
    runner = FakeRunner(CommandResult(5, "", "Unit u.service not loaded."))

    await SystemdUnits(runner).stop("spyhop-checkpoint-r-*")

    assert runner.calls == [["systemctl", "--user", "stop", "spyhop-checkpoint-r-*"]]


async def test_stop_that_fails_raises() -> None:
    runner = FakeRunner(CommandResult(1, "", "Access denied"))

    with pytest.raises(UnitError, match="Access denied"):
        await SystemdUnits(runner).stop("u")
