import json
import stat
from pathlib import Path

import pytest

from spyhop.domain.exceptions import OrcaError
from spyhop.infrastructure.orca.discovery import check_runtime, find_orca_executable


def script(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)

    return path


def test_python_screen_reader_is_refused(tmp_path: Path) -> None:
    screen_reader = script(tmp_path / "orca", "#!/usr/bin/python3\nprint('reading screen')\n")

    with pytest.raises(OrcaError, match="screen reader"):
        find_orca_executable(screen_reader)


def test_autodetects_orca_ide_in_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script(tmp_path / "orca", "#!/usr/bin/python3\n")
    orca_ide = script(tmp_path / "orca-ide", "#!/usr/bin/env bash\n")

    monkeypatch.setenv("PATH", str(tmp_path))

    assert find_orca_executable(None) == orca_ide


def test_missing_cli_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(OrcaError, match="Orca CLI not found"):
        find_orca_executable(None)


async def test_check_runtime_parses_status(tmp_path: Path) -> None:
    payload = {
        "ok": True,
        "result": {
            "app": {"running": True},
            "runtime": {"state": "ready", "appVersion": "1.4.245"},
        },
    }
    orca = script(tmp_path / "orca-ide", f"#!/usr/bin/env bash\necho '{json.dumps(payload)}'\n")

    info = await check_runtime(orca, seconds=5)

    assert info.app_running is True
    assert info.app_version == "1.4.245"
    assert info.runtime_state == "ready"


async def test_check_runtime_rejects_non_json(tmp_path: Path) -> None:
    orca = script(tmp_path / "orca-ide", "#!/usr/bin/env bash\necho usage\n")

    with pytest.raises(OrcaError, match="did not return JSON"):
        await check_runtime(orca, seconds=5)
