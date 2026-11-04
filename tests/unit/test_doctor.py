from pathlib import Path

from spyhop.config.settings import ProjectSettings
from spyhop.doctor import Level, _check_board, _check_project, _check_templates
from tests.factories import DEFAULT_COLUMNS, orca_dir, settings


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


def test_templates_directory_is_reported_without_customizations(tmp_path: Path) -> None:
    check = _check_templates(settings(tmp_path), tmp_path / "templates")

    assert f"customized in {tmp_path / 'templates'}: none" in check.detail


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


def test_default_board_columns_are_ok(tmp_path: Path) -> None:
    check = _check_board(settings(tmp_path), orca_dir(tmp_path))

    assert check.level is Level.OK
    assert "completed (Done)" in check.detail


def test_missing_board_column_is_an_error_listing_the_columns(tmp_path: Path) -> None:
    config = settings(tmp_path)
    config.orca.plan_review_status = "status-6"

    check = _check_board(config, orca_dir(tmp_path))

    assert check.level is Level.ERROR
    assert "'status-6'" in check.detail and "in-progress (In progress)" in check.detail


def test_finished_work_column_is_required(tmp_path: Path) -> None:
    columns = [column for column in DEFAULT_COLUMNS if column["id"] != "in-review"]

    check = _check_board(settings(tmp_path), orca_dir(tmp_path, columns))

    assert check.level is Level.ERROR
    assert "finished work: 'in-review'" in check.detail


def test_unreadable_board_is_a_warning(tmp_path: Path) -> None:
    check = _check_board(settings(tmp_path), tmp_path / "no-orca")

    assert check.level is Level.WARNING
