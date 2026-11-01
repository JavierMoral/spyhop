"""JIRA credentials, read by variable name so each machine can choose its own."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from spyhop.config.settings import JiraSettings
from spyhop.domain.exceptions import ConfigurationError

_ENV_LINE = re.compile(r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$""")


@dataclass(frozen=True)
class JiraCredentials:
    user: str
    token: str = field(repr=False)


def credentials_from_environment(settings: JiraSettings) -> JiraCredentials:
    missing = [name for name in (settings.user_env, settings.token_env) if not os.environ.get(name)]

    if missing:
        raise ConfigurationError(
            "Missing JIRA credentials in the environment: " + ", ".join(missing)
        )

    return JiraCredentials(user=os.environ[settings.user_env], token=os.environ[settings.token_env])


def read_env_file(env_file: Path) -> dict[str, str]:
    """Parses the KEY="value" subset of systemd EnvironmentFile syntax."""

    values = {}

    for line in env_file.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        match = _ENV_LINE.match(line)

        if match:
            values[match.group(1)] = _unquote(match.group(2).strip())

    return values


def write_env_file(env_file: Path, values: dict[str, str]) -> None:
    """Creates the file with mode 600; refuses to overwrite an existing one."""

    env_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    descriptor = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    with os.fdopen(descriptor, "w") as file:
        file.write("# Spyhop JIRA credentials, loaded by systemd with EnvironmentFile=.\n")

        for name, value in values.items():
            file.write(f'{name}="{_escape(value)}"\n')


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _unquote(value: str) -> str:
    if len(value) < 2 or value[0] != value[-1] or value[0] not in "\"'":
        return value

    if value[0] == "'":
        return value[1:-1]

    return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
