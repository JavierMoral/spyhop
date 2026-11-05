"""Runs external commands without a shell and without blocking the event loop."""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


class CommandTimeoutError(Exception):
    def __init__(self, seconds: float) -> None:
        super().__init__(f"no answer within {seconds:g}s")

        self.seconds = seconds


class CommandRunner(Protocol):
    async def run(
        self,
        args: Sequence[str],
        time_limit: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


class SubprocessRunner:
    async def run(
        self,
        args: Sequence[str],
        time_limit: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            async with asyncio.timeout(time_limit):
                stdout, stderr = await process.communicate()
        except TimeoutError:
            process.kill()
            await process.wait()

            raise CommandTimeoutError(time_limit) from None

        return CommandResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )
