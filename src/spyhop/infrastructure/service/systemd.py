"""Registers Spyhop as a systemd user service, with this machine's paths."""

import getpass
import shlex
import subprocess
from importlib.resources import files
from pathlib import Path

from jinja2 import Environment

from spyhop.config.settings import ServiceSettings

UNIT_NAME = "spyhop.service"


def render_unit(executable: Path, config_file: Path, service: ServiceSettings) -> str:
    command = [str(executable), "run", "--config", str(config_file)]

    if service.env_source == "env_file":
        exec_start = " ".join(_systemd_quote(part) for part in command)
    else:
        exec_start = "/bin/bash -lc " + _systemd_quote("exec " + shlex.join(command))

    template = (files("spyhop") / "templates" / "spyhop.service.j2").read_text()

    return (
        Environment(keep_trailing_newline=True)
        .from_string(template)
        .render(
            env_file=_systemd_quote(str(service.env_file)),
            env_source=service.env_source,
            exec_start=exec_start,
        )
    )


def install_unit(unit_file: Path, content: str, start: bool) -> None:
    unit_file.parent.mkdir(parents=True, exist_ok=True)
    unit_file.write_text(content)

    _systemctl("daemon-reload")
    _systemctl("enable", UNIT_NAME)

    if start:
        _systemctl("restart", UNIT_NAME)


def uninstall_unit(unit_file: Path) -> None:
    if unit_file.exists():
        _systemctl("disable", "--now", UNIT_NAME, check=False)
        unit_file.unlink()

    _systemctl("daemon-reload")


def linger_enabled() -> bool | None:
    try:
        completed = subprocess.run(
            ["loginctl", "show-user", getpass.getuser(), "--property=Linger", "--value"],
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError:
        return None

    if completed.returncode != 0:
        return None

    return completed.stdout.strip() == "yes"


def unit_active() -> bool:
    completed = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", UNIT_NAME], check=False
    )

    return completed.returncode == 0


def _systemctl(*arguments: str, check: bool = True) -> None:
    subprocess.run(["systemctl", "--user", *arguments], check=check)


def _systemd_quote(value: str) -> str:
    if not value or any(character in value for character in " \t\"'\\;$%"):
        escaped = (
            value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        )

        return f'"{escaped}"'

    return value
