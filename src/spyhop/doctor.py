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
from spyhop.config.credentials import JiraCredentials, read_env_file
from spyhop.config.paths import systemd_user_unit_file, user_templates_dir
from spyhop.config.settings import ProjectSettings, Settings, load_settings
from spyhop.domain.exceptions import SpyhopError
from spyhop.infrastructure.jira.client import JiraClient
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
    except SpyhopError as error:
        return [Check(Level.ERROR, "jira", str(error))]

    return checks


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

    return checks


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
    detail = f"{len(names)} found"

    if custom:
        detail += f"; customized in {user_templates}: {', '.join(custom)}"

    return Check(Level.OK, "templates", detail)


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

    if linger is False:
        checks.append(
            Check(
                Level.WARNING,
                "linger",
                'disabled: the service only starts at login; run `loginctl enable-linger "$USER"`',
            )
        )
    elif linger:
        checks.append(Check(Level.OK, "linger", "enabled: the service starts at boot"))

    return checks
