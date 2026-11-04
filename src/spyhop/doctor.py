"""`spyhop doctor`: checks everything a machine needs before Spyhop can run.

Credential values are compared, never printed.
"""

import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from jinja2 import TemplateError

from spyhop.application.context_builder import template_environment
from spyhop.application.janitor import closed_tickets_jql
from spyhop.config.credentials import JiraCredentials, read_env_file
from spyhop.config.paths import orca_config_dir, systemd_user_unit_file, user_templates_dir
from spyhop.config.settings import REVIEW_STATUS, ProjectSettings, Settings, load_settings
from spyhop.domain.exceptions import JiraError, SpyhopError
from spyhop.infrastructure.disk import GIB, free_bytes
from spyhop.infrastructure.jira.client import JiraClient
from spyhop.infrastructure.orca.board import read_board_columns
from spyhop.infrastructure.orca.discovery import check_runtime, find_orca_executable
from spyhop.infrastructure.service.systemd import linger_enabled, unit_active


class Level(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class Check:
    level: Level
    name: str
    detail: str


async def run_checks(config_file: Path) -> list[Check]:
    try:
        settings = load_settings(config_file)
    except SpyhopError as error:
        return [Check(Level.ERROR, "configuration", str(error))]

    checks = [Check(Level.OK, "configuration", str(config_file))]

    credentials, credential_checks = _check_credentials(settings)

    checks.extend(credential_checks)
    checks.extend(await _check_jira(settings, credentials))
    checks.extend(_check_repositories(settings))
    checks.append(_check_templates(settings))
    checks.append(await _check_orca(settings))
    checks.append(_check_board(settings))
    checks.extend(_check_service())

    return checks


def _check_credentials(settings: Settings) -> tuple[JiraCredentials | None, list[Check]]:
    names = (settings.jira.user_env, settings.jira.token_env)
    session = {name: os.environ.get(name) for name in names}
    checks = []

    if all(session.values()):
        checks.append(Check(Level.OK, "session credentials", ", ".join(names) + " are set"))
    else:
        missing = ", ".join(name for name, value in session.items() if not value)
        level = Level.WARNING if settings.service.env_source == "env_file" else Level.ERROR

        checks.append(Check(level, "session credentials", f"not set: {missing}"))

    if settings.service.env_source == "login_shell":
        return _credentials(settings, session), checks

    env_file = settings.service.env_file

    if not env_file.is_file():
        checks.append(
            Check(
                Level.ERROR, "credentials file", f"{env_file} not found; run `spyhop config init`"
            )
        )

        return None, checks

    mode = stat.S_IMODE(env_file.stat().st_mode)

    if mode & 0o077:
        checks.append(
            Check(Level.WARNING, "credentials file", f"{env_file} is mode {mode:o}; use 600")
        )

    values = read_env_file(env_file)
    missing_in_file = [name for name in names if not values.get(name)]

    if missing_in_file:
        checks.append(
            Check(Level.ERROR, "credentials file", f"{env_file} lacks {', '.join(missing_in_file)}")
        )

        return None, checks

    stale = [name for name in names if session.get(name) and session[name] != values[name]]

    if stale:
        checks.append(
            Check(
                Level.WARNING,
                "credentials file",
                f"{', '.join(stale)} differ from the session; update {env_file} after rotating",
            )
        )
    else:
        checks.append(Check(Level.OK, "credentials file", str(env_file)))

    return _credentials(settings, {name: values.get(name) for name in names}), checks


def _credentials(settings: Settings, values: dict[str, str | None]) -> JiraCredentials | None:
    user = values.get(settings.jira.user_env)
    token = values.get(settings.jira.token_env)

    return JiraCredentials(user=user, token=token) if user and token else None


async def _check_jira(settings: Settings, credentials: JiraCredentials | None) -> list[Check]:
    if credentials is None:
        return [Check(Level.ERROR, "jira", "skipped: no usable credentials")]

    try:
        async with JiraClient(
            settings.jira.base_url, credentials, settings.jira.request_timeout
        ) as jira:
            user = await jira.myself()
            checks = [
                Check(
                    Level.OK,
                    "jira",
                    f"{settings.jira.base_url} as {user.display_name} ({user.time_zone})",
                )
            ]

            for project_key, project in settings.enabled_projects().items():
                statuses = await jira.project_statuses(project_key)

                checks.append(_check_project(project_key, project, statuses))

            checks.append(await _check_cleanup(settings, jira))
    except SpyhopError as error:
        return [Check(Level.ERROR, "jira", str(error))]

    return checks


async def _check_cleanup(settings: Settings, jira: JiraClient) -> Check:
    """Runs the janitor's query once: JIRA rejects the whole query when one of
    close_statuses does not exist."""

    if not settings.cleanup.enabled:
        return Check(Level.OK, "cleanup", "disabled")

    sample = [f"{key}-1" for key in settings.enabled_projects()] or ["SPYHOP-1"]

    try:
        await jira.search(closed_tickets_jql(sample, settings.cleanup), ["status"], changelog=False)
    except JiraError as error:
        return Check(Level.ERROR, "cleanup", f"the closed-ticket query fails: {error}")

    closed = settings.cleanup.close_status_categories + settings.cleanup.close_statuses

    return Check(Level.OK, "cleanup", f"worktrees removed when tickets reach {', '.join(closed)}")


def _check_project(
    project_key: str, project: ProjectSettings, statuses: dict[str, list[str]] | None
) -> Check:
    name = f"project {project_key}"

    if statuses is None:
        return Check(Level.ERROR, name, "does not exist or is not visible to this user")

    problems = []

    for issue_type, workflow in project.workflows.items():
        if issue_type not in statuses:
            problems.append(f"no issue type {issue_type!r}")

            continue

        unknown = [
            status for status in workflow.trigger_statuses if status not in statuses[issue_type]
        ]

        if unknown:
            problems.append(f"{issue_type} has no status {', '.join(map(repr, unknown))}")

    if problems:
        return Check(Level.ERROR, name, "; ".join(problems))

    return Check(Level.OK, name, f"{len(project.workflows)} workflows match JIRA")


def _check_repositories(settings: Settings) -> list[Check]:
    if not settings.repositories:
        return [Check(Level.WARNING, "repositories", "none configured")]

    checks = []

    for alias, repository in settings.repositories.items():
        if not (repository.path / ".git").exists():
            checks.append(
                Check(
                    Level.ERROR, f"repository {alias}", f"{repository.path} is not a git checkout"
                )
            )
        else:
            checks.append(Check(Level.OK, f"repository {alias}", str(repository.path)))
            checks.append(
                _check_disk(alias, repository.path, settings.application.min_free_disk_gb)
            )

    return checks


def _check_disk(alias: str, path: Path, min_free_gb: float) -> Check:
    """Measured on the repository's disk; Orca's worktree directory, which the
    daemon measures, is usually on the same one."""

    free = free_bytes(path) / GIB
    name = f"disk {alias}"

    if min_free_gb <= 0:
        return Check(Level.OK, name, f"{free:.0f} GB free (no minimum)")

    if free < min_free_gb:
        return Check(
            Level.WARNING,
            name,
            f"{free:.1f} GB free, below min_free_disk_gb ({min_free_gb:g} GB): "
            "no new worktrees until space is freed",
        )

    return Check(Level.OK, name, f"{free:.0f} GB free (minimum {min_free_gb:g} GB)")


def _check_templates(settings: Settings, user_templates: Path | None = None) -> Check:
    user_templates = user_templates or user_templates_dir()
    environment = template_environment(user_templates)
    names = {"task.md", "goal.md", "settings.local.json.j2"} | {
        workflow.prompt
        for project in settings.enabled_projects().values()
        for workflow in project.workflows.values()
    }
    problems = []

    for name in sorted(names):
        try:
            environment.get_template(name)
        except TemplateError as error:
            problems.append(f"{name}: {error}")

    if problems:
        return Check(Level.ERROR, "templates", "; ".join(problems))

    custom = sorted(path.name for path in user_templates.glob("*") if path.name in names)

    return Check(
        Level.OK,
        "templates",
        f"{len(names)} found; customized in {user_templates}: {', '.join(custom) or 'none'}",
    )


async def _check_orca(settings: Settings) -> Check:
    try:
        executable = find_orca_executable(settings.orca.executable)
        info = await check_runtime(executable, settings.orca.startup_timeout)
    except SpyhopError as error:
        return Check(Level.ERROR, "orca", str(error))

    detail = f"{info.executable} (version {info.app_version or 'unknown'})"

    if not info.app_running:
        return Check(Level.WARNING, "orca", detail + "; the app is not running")

    return Check(Level.OK, "orca", f"{detail}, runtime {info.runtime_state}")


def _check_board(settings: Settings, orca_dir: Path | None = None) -> Check:
    used = {
        "orca.plan_review_status": settings.orca.plan_review_status,
        "orca.plan_approved_status": settings.orca.plan_approved_status,
        "finished work": REVIEW_STATUS,
    }

    try:
        columns = {
            column.id: column.label for column in read_board_columns(orca_dir or orca_config_dir())
        }
    except SpyhopError as error:
        return Check(
            Level.WARNING,
            "board columns",
            f"could not read Orca's board ({error}); check by hand that "
            f"{', '.join(dict.fromkeys(used.values()))} are column ids",
        )

    missing = [f"{name}: {status!r}" for name, status in used.items() if status not in columns]
    available = "columns: " + ", ".join(f"{id} ({label})" for id, label in columns.items())

    if missing:
        return Check(
            Level.ERROR,
            "board columns",
            f"not on the Orca board: {'; '.join(missing)}; {available}",
        )

    using = ", ".join(f"{name}={status}" for name, status in used.items())

    return Check(Level.OK, "board columns", f"{using}; {available}")


def _check_service() -> list[Check]:
    unit_file = systemd_user_unit_file()

    if not unit_file.exists():
        return [Check(Level.WARNING, "service", "not installed; run `spyhop service install`")]

    checks = [
        Check(
            Level.OK if unit_active() else Level.WARNING,
            "service",
            f"{unit_file} ({'active' if unit_active() else 'inactive'})",
        )
    ]

    linger = linger_enabled()

    if linger is not None:
        starts = "at boot" if linger else "when you log in"

        checks.append(
            Check(
                Level.OK, "linger", f"{'en' if linger else 'dis'}abled: the service starts {starts}"
            )
        )

    return checks
