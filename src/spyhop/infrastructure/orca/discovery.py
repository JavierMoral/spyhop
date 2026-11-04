"""Finds and validates the Orca CLI.

On Linux `orca` is usually the GNOME screen reader, a Python script that would
start reading the screen aloud if executed. Spyhop therefore only autodetects
`orca-ide`, and refuses any candidate with a Python shebang before running it.

Orca registers its CLI in ~/.local/bin, which a service started before the
desktop session may not have in PATH, so that directory is looked at too.
"""

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from spyhop.domain.exceptions import OrcaError

AUTODETECTED_NAMES = ("orca-ide",)

# Where Orca registers its CLI, looked at when it is not in PATH.

REGISTERED_CLI_DIR = Path("~/.local/bin")


@dataclass(frozen=True)
class OrcaRuntimeInfo:
    executable: Path
    app_version: str | None
    app_running: bool
    runtime_state: str | None


def find_orca_executable(configured: Path | None) -> Path:
    if configured is not None:
        candidate = configured if configured.is_absolute() else _which(str(configured))

        if candidate is None or not os.access(candidate, os.X_OK):
            raise OrcaError(f"orca.executable is not an executable file: {configured}")

        _reject_screen_reader(candidate)

        return candidate

    registered_dir = REGISTERED_CLI_DIR.expanduser()

    for name in AUTODETECTED_NAMES:
        candidate = _which(name) or _registered(registered_dir / name)

        if candidate is not None:
            _reject_screen_reader(candidate)

            return candidate

    raise OrcaError(
        "Orca CLI not found: register it from Orca's settings or set orca.executable "
        f"(looked for {', '.join(AUTODETECTED_NAMES)} in PATH and in {registered_dir})"
    )


async def check_runtime(executable: Path, seconds: float) -> OrcaRuntimeInfo:
    process = await asyncio.create_subprocess_exec(
        str(executable),
        "status",
        "--json",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        async with asyncio.timeout(seconds):
            stdout, _ = await process.communicate()
    except TimeoutError:
        process.kill()
        await process.wait()

        raise OrcaError(f"`{executable} status` did not answer within {seconds:g}s") from None

    try:
        data = json.loads(stdout)
    except ValueError:
        raise OrcaError(f"`{executable} status --json` did not return JSON; is it Orca?") from None

    if not isinstance(data, dict) or "result" not in data:
        raise OrcaError(f"`{executable} status --json` returned an unexpected payload")

    result = data["result"] or {}
    app = result.get("app") or {}
    runtime = result.get("runtime") or {}

    return OrcaRuntimeInfo(
        executable=executable,
        app_version=runtime.get("appVersion"),
        app_running=bool(app.get("running")),
        runtime_state=runtime.get("state"),
    )


def _which(name: str) -> Path | None:
    found = shutil.which(name)

    return Path(found) if found else None


def _registered(candidate: Path) -> Path | None:
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


def _reject_screen_reader(candidate: Path) -> None:
    try:
        with candidate.resolve().open("rb") as file:
            first_line = file.readline(200)
    except OSError as error:
        raise OrcaError(f"Cannot read {candidate}: {error}") from None

    if first_line.startswith(b"#!") and b"python" in first_line:
        raise OrcaError(
            f"{candidate} is a Python script, most likely the GNOME screen reader, not the Orca CLI"
        )
