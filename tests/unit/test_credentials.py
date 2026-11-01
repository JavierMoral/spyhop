import stat
from pathlib import Path

import pytest

from spyhop.config.credentials import (
    credentials_from_environment,
    read_env_file,
    write_env_file,
)
from spyhop.config.settings import JiraSettings
from spyhop.domain.exceptions import ConfigurationError


def test_env_file_round_trip_with_mode_600(tmp_path: Path) -> None:
    env_file = tmp_path / "spyhop" / "env"
    values = {"JIRA_API_USER": "me@example.com", "JIRA_API_TOKEN": 'to"k\\en'}

    write_env_file(env_file, values)

    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_file.parent.stat().st_mode) == 0o700
    assert read_env_file(env_file) == values


def test_env_file_is_never_overwritten(tmp_path: Path) -> None:
    env_file = tmp_path / "env"

    env_file.write_text("JIRA_API_TOKEN=old\n")

    with pytest.raises(FileExistsError):
        write_env_file(env_file, {"JIRA_API_TOKEN": "new"})

    assert read_env_file(env_file) == {"JIRA_API_TOKEN": "old"}


def test_read_env_file_accepts_export_and_comments(tmp_path: Path) -> None:
    env_file = tmp_path / "env"

    env_file.write_text("# comment\n\nexport A=\"1\"\nB='2'\nC=3\n")

    assert read_env_file(env_file) == {"A": "1", "B": "2", "C": "3"}


def test_credentials_use_configured_variable_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_USER", "me@example.com")
    monkeypatch.setenv("MY_TOKEN", "secret")

    credentials = credentials_from_environment(
        JiraSettings(base_url="https://x", user_env="MY_USER", token_env="MY_TOKEN")
    )

    assert credentials.user == "me@example.com"
    assert "secret" not in repr(credentials)


def test_missing_credentials_name_the_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JIRA_API_USER", raising=False)
    monkeypatch.setenv("JIRA_API_TOKEN", "secret")

    with pytest.raises(ConfigurationError, match="JIRA_API_USER") as error:
        credentials_from_environment(JiraSettings(base_url="https://x"))

    assert "secret" not in str(error.value)
