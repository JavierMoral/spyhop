from pathlib import Path

from spyhop.config.settings import ServiceSettings
from spyhop.infrastructure.service.systemd import render_unit

EXECUTABLE = Path("/home/u/.local/bin/spyhop")
CONFIG = Path("/home/u/.config/spyhop/config.yaml")


def test_env_file_unit_loads_credentials_file() -> None:
    unit = render_unit(
        EXECUTABLE,
        CONFIG,
        ServiceSettings(env_source="env_file", env_file="/home/u/.config/spyhop/env"),
    )

    assert "EnvironmentFile=/home/u/.config/spyhop/env\n" in unit
    assert f"ExecStart={EXECUTABLE} run --config {CONFIG}\n" in unit
    assert "bash -lc" not in unit
    assert "WantedBy=default.target" in unit


def test_login_shell_unit_runs_through_bash() -> None:
    unit = render_unit(EXECUTABLE, CONFIG, ServiceSettings(env_source="login_shell"))

    assert f'ExecStart=/bin/bash -lc "exec {EXECUTABLE} run --config {CONFIG}"\n' in unit
    assert "EnvironmentFile" not in unit


def test_paths_with_spaces_and_percent_are_quoted() -> None:
    unit = render_unit(
        Path("/home/u/my tools/spyhop"),
        Path("/home/u/100%/config.yaml"),
        ServiceSettings(env_source="env_file", env_file="/home/u/env"),
    )

    assert 'ExecStart="/home/u/my tools/spyhop" run --config "/home/u/100%%/config.yaml"' in unit
