from pathlib import Path

from spyhop.config.settings import ProjectSettings
from spyhop.doctor import Level, _check_project
from tests.factories import settings


def project(tmp_path: Path) -> ProjectSettings:
    return settings(tmp_path).projects["LPD"]


def test_project_matching_jira_is_ok(tmp_path: Path) -> None:
    check = _check_project(
        "LPD", project(tmp_path), {"Bug": ["In Progress"], "Story": ["In Development"]}
    )

    assert check.level is Level.OK


def test_unknown_project_is_an_error(tmp_path: Path) -> None:
    check = _check_project("LPD", project(tmp_path), None)

    assert check.level is Level.ERROR
    assert "does not exist" in check.detail


def test_misspelled_issue_types_and_statuses_are_reported(tmp_path: Path) -> None:
    check = _check_project("LPD", project(tmp_path), {"Bug": ["In progress"]})

    assert check.level is Level.ERROR
    assert "Bug has no status 'In Progress'" in check.detail
    assert "no issue type 'Story'" in check.detail
