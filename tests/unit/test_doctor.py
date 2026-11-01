from pathlib import Path

from spyhop.config.settings import ProjectSettings
from spyhop.doctor import Level, _check_project, _check_templates
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


def test_packaged_templates_cover_the_configured_workflows(tmp_path: Path) -> None:
    check = _check_templates(settings(tmp_path), tmp_path / "templates")

    assert check.level is Level.OK


def test_missing_workflow_template_is_an_error(tmp_path: Path) -> None:
    config = settings(tmp_path)
    config.projects["LPD"].workflows["Bug"].prompt = "missing.md"

    check = _check_templates(config, tmp_path / "templates")

    assert check.level is Level.ERROR
    assert "missing.md" in check.detail


def test_customized_templates_are_reported(tmp_path: Path) -> None:
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "bug_fix.md").write_text("Mine")

    check = _check_templates(settings(tmp_path), tmp_path / "templates")

    assert "customized" in check.detail and "bug_fix.md" in check.detail
