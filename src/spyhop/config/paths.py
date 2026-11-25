"""Machine-specific locations, resolved from XDG so nothing depends on one host."""

import os
import shutil
import sys
from pathlib import Path

from platformdirs import user_config_path, user_data_path, user_state_path

APP_NAME = "spyhop"

CONFIG_ENV_VAR = "SPYHOP_CONFIG"


def config_dir() -> Path:
    return user_config_path(APP_NAME)


def state_dir() -> Path:
    return user_state_path(APP_NAME)


def evals_dir() -> Path:
    """The eval cases. They hold private tickets, so they live here and not
    in a repository."""

    return user_data_path(APP_NAME) / "evals"


def default_config_file() -> Path:
    override = os.environ.get(CONFIG_ENV_VAR)

    if override:
        return expand_path(override)

    return config_dir() / "config.yaml"


def user_templates_dir() -> Path:
    return config_dir() / "templates"


def default_env_file() -> Path:
    return config_dir() / "env"


def claude_dir() -> Path:
    # Claude Code reads the user's subagents and skills from here;
    # CLAUDE_CONFIG_DIR moves the whole ~/.claude directory.

    return expand_path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def claude_agents_dir() -> Path:
    return claude_dir() / "agents"


def bundled_claude_dir() -> Path:
    """The subagents, skills, and scripts that Spyhop owns, in agents/,
    skills/, and scripts/."""

    return Path(__file__).parent.parent / "claude"


def database_file(dry_run: bool) -> Path:
    # A dry run keeps its own database so turning it off never launches the
    # entries it only pretended to process.

    return state_dir() / ("spyhop-dry-run.db" if dry_run else "spyhop.db")


def systemd_user_unit_file() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")

    return Path(config_home) / "systemd" / "user" / "spyhop.service"


def expand_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def spyhop_executable() -> Path:
    """The installed spyhop command, for the service and the checkpoint
    terminals."""

    found = shutil.which("spyhop")

    if found:
        return Path(found).resolve()

    return Path(sys.argv[0]).resolve()
