import asyncio
import logging
import os
import shutil
import sys
from importlib.resources import files
from pathlib import Path
from typing import Annotated

import typer

from spyhop import __version__
from spyhop.application.janitor import Cleanup, WorktreeRemover
from spyhop.config.credentials import write_env_file
from spyhop.config.paths import database_file, default_config_file, systemd_user_unit_file
from spyhop.config.settings import Settings, load_settings
from spyhop.daemon import orca_client, run_daemon
from spyhop.doctor import Level, run_checks
from spyhop.domain.exceptions import SpyhopError
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import (
    PollerStateRepository,
    RunRepository,
    WorktreeRepository,
)
from spyhop.infrastructure.service.systemd import (
    install_unit,
    linger_enabled,
    render_unit,
    uninstall_unit,
)

app = typer.Typer(
    help="Watches JIRA and feeds tickets to Claude Code agents running in Orca.",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Manage the configuration.", no_args_is_help=True)
service_app = typer.Typer(help="Manage the systemd user service.", no_args_is_help=True)

app.add_typer(config_app, name="config")
app.add_typer(service_app, name="service")

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config", "-c", help="Configuration file [default: ~/.config/spyhop/config.yaml]"
    ),
]


@app.command()
def run(
    config: ConfigOption = None,
    dry_run: Annotated[
        bool | None, typer.Option("--dry-run/--no-dry-run", help="Override application.dry_run")
    ] = None,
    once: Annotated[bool, typer.Option(help="Poll once and exit")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run the daemon in the foreground."""

    _setup_logging(verbose)

    settings = _load(config)

    if dry_run is not None:
        settings.application.dry_run = dry_run

    try:
        asyncio.run(run_daemon(settings, once=once))
    except SpyhopError as error:
        logging.getLogger("spyhop").error("%s", error)

        raise typer.Exit(1) from None


@app.command()
def status(
    issue_key: Annotated[str | None, typer.Argument(help="Only this ticket")] = None,
    config: ConfigOption = None,
    show_all: Annotated[bool, typer.Option("--all", help="Include finished runs")] = False,
    dry_run: Annotated[
        bool | None, typer.Option("--dry-run/--no-dry-run", help="Which database to read")
    ] = None,
) -> None:
    """Show the last poll and the automation runs."""

    settings = _load(config)
    use_dry_run = settings.application.dry_run if dry_run is None else dry_run
    database = database_file(use_dry_run)

    if not database.exists():
        typer.echo(f"No database yet at {database}; has `spyhop run` been started?")

        return

    connection = connect(database)
    poller_state = PollerStateRepository(connection)
    runs = RunRepository(connection).list(issue_key=issue_key, include_terminal=show_all)

    typer.echo(f"Database:       {database}{' (dry run)' if use_dry_run else ''}")
    typer.echo(f"Last good poll: {_format(poller_state.last_success_at())}")
    typer.echo(f"Watermark:      {_format(poller_state.watermark())}")

    last_error = poller_state.last_error()

    if last_error:
        typer.echo(f"Last error:     {last_error}")

    typer.echo("")

    if not runs:
        typer.echo("No runs." if show_all else "No active runs (use --all to include finished).")

        return

    typer.echo(f"{'RUN':<10}{'TICKET':<14}{'STATUS':<20}{'WORKFLOW':<18}{'ENTERED':<22}TRIGGER")

    for automation_run in runs:
        typer.echo(
            f"{automation_run.id[:8]:<10}{automation_run.issue_key:<14}"
            f"{automation_run.status:<20}{automation_run.workflow_name:<18}"
            f"{_format(automation_run.triggered_at):<22}{automation_run.trigger_status}"
        )

        if automation_run.retry_of:
            typer.echo(f"{'':<24}retry of {automation_run.retry_of[:8]}")

        if automation_run.waiting_reason:
            typer.echo(f"{'':<24}waiting: {automation_run.waiting_reason}")

        if automation_run.error_message:
            typer.echo(f"{'':<24}{automation_run.error_message}")


@app.command()
def fail(
    issue_key: Annotated[str, typer.Argument(help="Ticket whose active run is given up")],
    reason: Annotated[str, typer.Option(help="Recorded as the run's error")] = (
        "marked as failed by a person"
    ),
    config: ConfigOption = None,
    dry_run: Annotated[
        bool | None, typer.Option("--dry-run/--no-dry-run", help="Which database to use")
    ] = None,
) -> None:
    """Mark a ticket's active run as FAILED, freeing its agent slot.

    For runs Spyhop cannot conclude by itself, such as one waiting with
    unknown_state. The agent and the worktree in Orca are left untouched."""

    runs = RunRepository(connect(_existing_database(config, dry_run)))
    active = runs.list(issue_key=issue_key.upper())

    if not active:
        typer.echo(f"{issue_key.upper()} has no active run.", err=True)

        raise typer.Exit(1)

    for automation_run in active:
        runs.mark_failed(automation_run.id, reason)

        typer.echo(f"{automation_run.issue_key}: {automation_run.status} -> FAILED ({reason})")


@app.command()
def retry(
    target: Annotated[str, typer.Argument(help="Run id (or its first characters) or ticket key")],
    config: ConfigOption = None,
    dry_run: Annotated[
        bool | None, typer.Option("--dry-run/--no-dry-run", help="Which database to use")
    ] = None,
) -> None:
    """Start a finished run again as a new run on the same worktree.

    With a ticket key, retries the ticket's latest run. The running daemon
    picks the new run up on its next cycle."""

    runs = RunRepository(connect(_existing_database(config, dry_run)))
    matches = runs.find(target)

    if len(matches) != 1:
        typer.echo(
            f"No run matches {target!r}." if not matches else f"{target!r} matches several runs.",
            err=True,
        )

        raise typer.Exit(1)

    [original] = matches

    if not original.status.terminal:
        typer.echo(f"{original.issue_key}: run {original.id[:8]} is still {original.status}.")

        raise typer.Exit(1)

    retried = runs.create_retry(original)

    if retried is None:
        typer.echo(f"{original.issue_key} already has an active run.", err=True)

        raise typer.Exit(1)

    typer.echo(
        f"{retried.issue_key}: run {retried.id[:8]} created as a retry of {original.id[:8]} "
        f"({original.status})"
    )


@app.command()
def cleanup(
    issue_key: Annotated[str, typer.Argument(help="Ticket whose worktree is removed")],
    config: ConfigOption = None,
    dry_run: Annotated[
        bool | None, typer.Option("--dry-run/--no-dry-run", help="Which database to use")
    ] = None,
) -> None:
    """Remove a ticket's worktree now, whatever its JIRA status.

    Cancels the ticket's active run and stops its agents. Removal is never
    forced: with uncommitted changes the worktree stays, marked
    CLEANUP_BLOCKED, until they are committed or discarded. Orca keeps the
    branch unless it is merged."""

    settings = _load(config)
    connection = connect(_existing_database(config, dry_run, settings))
    worktrees = WorktreeRepository(connection)
    worktree = worktrees.find_current(issue_key.upper())

    if worktree is None:
        typer.echo(f"{issue_key.upper()} has no worktree to remove.", err=True)

        raise typer.Exit(1)

    try:
        remover = WorktreeRemover(
            RunRepository(connection), worktrees, orca_client(settings), settings.repositories
        )
        outcome = asyncio.run(remover.clean_up(worktree, "cleaned up by a person"))
    except SpyhopError as error:
        typer.echo(f"{worktree.issue_key}: removal not finished: {error}", err=True)

        raise typer.Exit(1) from None

    if outcome is Cleanup.BLOCKED:
        blocked = worktrees.get(worktree.id)

        typer.echo(
            f"{worktree.issue_key}: {worktree.path} not removed: "
            f"{blocked.cleanup_error if blocked else 'refused by Orca'}",
            err=True,
        )

        raise typer.Exit(1)

    typer.echo(f"{worktree.issue_key}: {worktree.path} removed")


@app.command()
def doctor(config: ConfigOption = None) -> None:
    """Check configuration, credentials, JIRA, repositories, Orca and the service."""

    checks = asyncio.run(run_checks(config or default_config_file()))
    symbols = {Level.OK: "✓", Level.WARNING: "!", Level.ERROR: "✗"}

    for check in checks:
        typer.echo(f"{symbols[check.level]} {check.name}: {check.detail}")

    if any(check.level is Level.ERROR for check in checks):
        raise typer.Exit(1)


@app.command()
def version() -> None:
    """Print the Spyhop version."""

    typer.echo(__version__)


@config_app.command("init")
def config_init(config: ConfigOption = None) -> None:
    """Create the configuration file and the credentials file if they are missing."""

    config_file = config or default_config_file()

    if config_file.exists():
        typer.echo(f"Configuration already exists: {config_file}")
    else:
        config_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        config_file.write_text((files("spyhop") / "templates" / "config.example.yaml").read_text())

        typer.echo(f"Created {config_file}; edit it before running `spyhop doctor`.")

    settings = _load(config_file)
    env_file = settings.service.env_file

    if settings.service.env_source != "env_file":
        return

    if env_file.exists():
        typer.echo(f"Credentials file already exists: {env_file}")

        return

    names = (settings.jira.user_env, settings.jira.token_env)
    missing = [name for name in names if not os.environ.get(name)]

    if missing:
        typer.echo(
            f"Cannot create {env_file}: {', '.join(missing)} not set in this session.", err=True
        )

        raise typer.Exit(1)

    write_env_file(env_file, {name: os.environ[name] for name in names})

    typer.echo(f"Created {env_file} (mode 600) from {', '.join(names)}.")


@service_app.command("install")
def service_install(
    config: ConfigOption = None,
    start: Annotated[bool, typer.Option(help="Start or restart the service now")] = True,
) -> None:
    """Install and enable the systemd user service with this machine's paths."""

    config_file = (config or default_config_file()).resolve()
    settings = _load(config_file)
    unit_file = systemd_user_unit_file()

    install_unit(unit_file, render_unit(_spyhop_executable(), config_file, settings.service), start)

    typer.echo(f"Installed {unit_file}{' and started it' if start else ''}.")

    if linger_enabled() is False:
        typer.echo(
            "The service starts when you log in. To start it at boot instead, "
            'run once: loginctl enable-linger "$USER"'
        )


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Stop, disable and remove the systemd user service."""

    unit_file = systemd_user_unit_file()

    uninstall_unit(unit_file)

    typer.echo(f"Removed {unit_file}.")


def _existing_database(
    config: Path | None, dry_run: bool | None, settings: Settings | None = None
) -> Path:
    settings = settings or _load(config)
    database = database_file(settings.application.dry_run if dry_run is None else dry_run)

    if not database.exists():
        typer.echo(f"No database yet at {database}.", err=True)

        raise typer.Exit(1)

    return database


def _load(config: Path | None) -> Settings:
    try:
        return load_settings(config or default_config_file())
    except SpyhopError as error:
        typer.echo(str(error), err=True)

        raise typer.Exit(1) from None


def _spyhop_executable() -> Path:
    found = shutil.which("spyhop")

    if found:
        return Path(found).resolve()

    return Path(sys.argv[0]).resolve()


def _setup_logging(verbose: bool) -> None:
    # journald adds its own timestamps.

    timestamp = "" if os.environ.get("INVOCATION_ID") else "%(asctime)s "

    logging.basicConfig(
        format=timestamp + "%(levelname)s %(name)s: %(message)s",
        level=logging.DEBUG if verbose else logging.INFO,
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)


def _format(value: object) -> str:
    if value is None:
        return "-"

    if hasattr(value, "astimezone"):
        return str(value.astimezone().strftime("%Y-%m-%d %H:%M:%S"))

    return str(value)
