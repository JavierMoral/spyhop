"""Machine-specific locations, resolved from XDG so nothing depends on one host."""

import os
from pathlib import Path

from platformdirs import user_config_path, user_state_path

APP_NAME = "spyhop"

CONFIG_ENV_VAR = "SPYHOP_CONFIG"


def config_dir() -> Path:
    return user_config_path(APP_NAME)


def state_dir() -> Path:
    return user_state_path(APP_NAME)


def default_config_file() -> Path:
    override = os.environ.get(CONFIG_ENV_VAR)

    if override:
        return expand_path(override)

    return config_dir() / "config.yaml"


def default_env_file() -> Path:
    return config_dir() / "env"


def database_file(dry_run: bool) -> Path:
    # A dry run keeps its own database so turning it off never launches the
    # entries it only pretended to process.

    return state_dir() / ("spyhop-dry-run.db" if dry_run else "spyhop.db")


def systemd_user_unit_file() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")

    return Path(config_home) / "systemd" / "user" / "spyhop.service"


def expand_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value))))
