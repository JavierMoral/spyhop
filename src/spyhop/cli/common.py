"""The typer apps, the options and the helpers that the commands share."""

import asyncio
import inspect
import logging
import os
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer

from spyhop.application.checkpoint_inputs import input_values
from spyhop.application.run_commands import CommandResult, RunCommandError, RunCommands
from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.config.credentials import (
    credentials_from_environment,
    read_env_file,
)
from spyhop.config.paths import (
    database_file,
    default_config_file,
)
from spyhop.config.settings import Settings, load_settings
from spyhop.daemon import orca_client
from spyhop.domain.enums import WorktreeStatus
from spyhop.domain.exceptions import ConfigurationError, SpyhopError
from spyhop.domain.models import ResolvedWorkflow
from spyhop.infrastructure.git import top_level
from spyhop.infrastructure.jira.client import JiraClient
from spyhop.infrastructure.orca.client import OrcaClient
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import (
    RunRepository,
    WorktreeRepository,
)
from spyhop.infrastructure.service.systemd import (
    wake_service,
)

app = typer.Typer(
    help="Watches JIRA and feeds tickets to Claude Code agents running in Orca.",
    no_args_is_help=True,
)
checkpoints_app = typer.Typer(
    help="Plan, check and run again the checkpoints of a run.", no_args_is_help=True
)
config_app = typer.Typer(help="Manage the configuration.", no_args_is_help=True)
service_app = typer.Typer(help="Manage the systemd user service.", no_args_is_help=True)

app.add_typer(checkpoints_app, name="checkpoints")
app.add_typer(config_app, name="config")
app.add_typer(service_app, name="service")

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config", "-c", help="Configuration file [default: ~/.config/spyhop/config.yaml]"
    ),
]


def _run_commands(config: Path | None, dry_run: bool | None) -> RunCommands:
    settings = _load(config)
    connection = connect(_existing_database(config, dry_run, settings))

    return RunCommands(
        RunRepository(connection),
        WorktreeRepository(connection),
        lambda: _orca(settings),
        settings.orca.plan_review_status,
        settings.orca.plan_approved_status,
    )


def _finish(command: Callable[[], CommandResult | Coroutine[Any, Any, CommandResult]]) -> None:
    """Runs a command of RunCommands, shows its result and wakes the service."""

    try:
        outcome = command()
        result = asyncio.run(outcome) if inspect.iscoroutine(outcome) else outcome
    except RunCommandError as error:
        typer.echo(str(error), err=True)

        raise typer.Exit(1) from None

    assert isinstance(result, CommandResult)

    for warning in result.warnings:
        typer.echo(warning, err=True)

    _wake()
    typer.echo(result.message)


def _existing_database(
    config: Path | None, dry_run: bool | None, settings: Settings | None = None
) -> Path:
    settings = settings or _load(config)
    database = database_file(settings.application.dry_run if dry_run is None else dry_run)

    if not database.exists():
        typer.echo(f"No database yet at {database}.", err=True)

        raise typer.Exit(1)

    return database


def _named_workflow(settings: Settings, name: str) -> ResolvedWorkflow:
    resolver = WorkflowResolver(settings)
    matches = [
        resolved
        for project_key in resolver.project_keys()
        if (resolved := resolver.resolve_by_name(project_key, name))
    ]

    if not matches:
        raise ConfigurationError(f"No enabled project has a workflow named {name!r}")

    if len({resolved.checkpoints for resolved in matches}) > 1:
        raise ConfigurationError(f"Workflows named {name!r} have different checkpoints")

    return matches[0]


def _workflow(
    settings: Settings, name: str | None, config: Path | None, dry_run: bool | None
) -> tuple[ResolvedWorkflow, Path, str | None]:
    """The workflow, the root of the worktree and the issue key of the run.
    With a workflow name, the root is the Git worktree of the current
    directory, and there is no issue key."""

    if name is None:
        return _current_workflow(settings, config, dry_run)

    resolved = _named_workflow(settings, name)
    root = top_level(Path.cwd())

    if root is None:
        raise ConfigurationError(f"{Path.cwd()} is not in a Git worktree")

    return resolved, root, None


def _input_values(
    settings: Settings,
    resolved: ResolvedWorkflow,
    root: Path,
    issue_key: str | None,
    names: set[str],
) -> dict[str, str]:
    """The current values of the checkpoint inputs in ``names``."""

    async def values() -> dict[str, str]:
        if "ticket" not in names or issue_key is None:
            return await input_values(names, root, resolved.base_branch, issue_key, None)

        async with _jira_client(settings) as jira:
            return await input_values(names, root, resolved.base_branch, issue_key, jira)

    return asyncio.run(values())


def _jira_client(settings: Settings) -> JiraClient:
    """The JIRA credentials come from the environment, or from the env file
    of the service when the environment does not have them."""

    env_file = settings.service.env_file
    environment = {**(read_env_file(env_file) if env_file.is_file() else {}), **os.environ}

    return JiraClient(
        settings.jira.base_url,
        credentials_from_environment(settings.jira, environment),
        settings.jira.request_timeout,
    )


def _current_workflow(
    settings: Settings, config: Path | None, dry_run: bool | None
) -> tuple[ResolvedWorkflow, Path, str]:
    """The workflow of the run whose worktree holds the current directory,
    the root of that worktree and the issue key of the run."""

    connection = connect(_existing_database(config, dry_run, settings))
    current = Path.cwd().resolve()
    worktree = next(
        (
            worktree
            for worktree in WorktreeRepository(connection).list_by_status(
                WorktreeStatus.ACTIVE, WorktreeStatus.REMOVING, WorktreeStatus.CLEANUP_BLOCKED
            )
            if current.is_relative_to(worktree.path.resolve())
        ),
        None,
    )

    if worktree is None:
        raise ConfigurationError(
            f"{current} is not in the worktree of a run; give the workflow with --workflow"
        )

    automation_run = next(
        (
            automation_run
            for automation_run in RunRepository(connection).list(
                issue_key=worktree.issue_key, include_terminal=True
            )
            if automation_run.worktree_id == worktree.id
        ),
        None,
    )

    if automation_run is None:
        raise ConfigurationError(f"{worktree.issue_key} has no run in {worktree.path}")

    resolved = WorkflowResolver(settings).resolve_for_run(automation_run)

    if resolved is None:
        raise ConfigurationError(
            f"Workflow {automation_run.workflow_name!r} of {automation_run.issue_key} "
            f"is not in the configuration of its project"
        )

    return resolved, worktree.path, automation_run.issue_key


def _load(config: Path | None) -> Settings:
    try:
        return load_settings(config or default_config_file())
    except SpyhopError as error:
        typer.echo(str(error), err=True)

        raise typer.Exit(1) from None


def _setup_logging(verbose: bool) -> None:
    # journald adds its own timestamps.

    timestamp = "" if os.environ.get("INVOCATION_ID") else "%(asctime)s "

    logging.basicConfig(
        format=timestamp + "%(levelname)s %(name)s: %(message)s",
        level=logging.DEBUG if verbose else logging.INFO,
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"

    minutes = round(seconds / 60)

    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _format(value: object) -> str:
    if value is None:
        return "-"

    if hasattr(value, "astimezone"):
        return str(value.astimezone().strftime("%Y-%m-%d %H:%M:%S"))

    return str(value)


def _orca(settings: Settings) -> OrcaClient:
    return orca_client(settings)


def _wake() -> None:
    wake_service()
