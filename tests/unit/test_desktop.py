from collections.abc import Sequence
from pathlib import Path

from spyhop.infrastructure.desktop import notify
from spyhop.infrastructure.process.command_runner import CommandResult


class Runner:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[list[str]] = []

    async def run(
        self, args: Sequence[str], time_limit: float, cwd: Path | None = None, **_: object
    ) -> CommandResult:
        self.calls.append(list(args))

        if self.error:
            raise self.error

        return CommandResult(0, "", "")


async def test_notify_runs_notify_send() -> None:
    runner = Runner()

    await notify("Spyhop: LPD-1", "waiting", runner)

    assert runner.calls == [["notify-send", "--app-name=Spyhop", "Spyhop: LPD-1", "waiting"]]


async def test_a_machine_without_notify_send_is_no_error() -> None:
    await notify("Spyhop: LPD-1", "waiting", Runner(FileNotFoundError("notify-send")))
