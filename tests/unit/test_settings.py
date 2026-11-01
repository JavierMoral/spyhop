from importlib.resources import files
from pathlib import Path

import pytest

from spyhop.config.settings import load_settings
from spyhop.domain.exceptions import ConfigurationError
from tests.factories import settings


def test_example_configuration_is_valid(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"

    config_file.write_text((files("spyhop") / "templates" / "config.example.yaml").read_text())

    loaded = load_settings(config_file)

    assert loaded.application.dry_run is True
    assert loaded.repositories["main"].path == Path.home() / "git" / "your-repository"
    assert loaded.service.env_source == "env_file"


def test_environment_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.yaml"

    config_file.write_text(
        "application:\n  dry_run: true\n  polling_interval: 60\n"
        "jira:\n  base_url: https://example.atlassian.net\n"
    )
    monkeypatch.setenv("SPYHOP_APPLICATION__DRY_RUN", "false")

    loaded = load_settings(config_file)

    assert loaded.application.dry_run is False
    assert loaded.application.polling_interval == 60


def test_paths_expand_home_and_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOS", str(tmp_path))

    loaded = settings(tmp_path, repositories={"portal": {"path": "${REPOS}/portal"}})

    assert loaded.repositories["portal"].path == tmp_path / "portal"


def test_unknown_repository_alias_is_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"

    config_file.write_text(
        "jira:\n  base_url: https://example.atlassian.net\n"
        "projects:\n  LPD:\n    workflows:\n      Bug:\n        name: bug_fix\n"
        "        trigger_statuses: [In Progress]\n        repository: missing\n"
        "        prompt: bug_fix.md\n"
    )

    with pytest.raises(ConfigurationError, match="unknown repository alias 'missing'"):
        load_settings(config_file)


def test_missing_configuration_file_suggests_init(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="spyhop config init"):
        load_settings(tmp_path / "absent.yaml")
