"""The commands for the configuration, the service and the installation."""

import asyncio
import os
from importlib.resources import files
from typing import Annotated

import typer

from spyhop import __version__
from spyhop.cli.common import (
    ConfigOption,
    _load,
    app,
    claude_app,
    config_app,
    service_app,
)
from spyhop.config.credentials import (
    write_env_file,
)
from spyhop.config.paths import (
    bundled_claude_dir,
    claude_dir,
    default_config_file,
    spyhop_executable,
    systemd_user_unit_file,
)
from spyhop.doctor import Level, run_checks
from spyhop.infrastructure.claude_links import link_claude_files
from spyhop.infrastructure.service.systemd import (
    install_unit,
    linger_enabled,
    render_unit,
    uninstall_unit,
)


@app.command()
def doctor(config: ConfigOption = None) -> None:
    """Check the configuration and everything that Spyhop needs to run."""

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


@claude_app.command("install")
def claude_install() -> None:
    """Link the subagents, skills, and scripts of Spyhop in ~/.claude, so that Claude Code
    uses them. Run it again after a new installation of Spyhop."""

    links = link_claude_files(bundled_claude_dir(), claude_dir())

    for link in links:
        if link.linked:
            typer.echo(f"Linked {link.target} -> {link.source}")
        else:
            typer.echo(f"Not linked: {link.target} is not a link; remove it first.", err=True)

    if not all(link.linked for link in links):
        raise typer.Exit(1)


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

    install_unit(unit_file, render_unit(spyhop_executable(), config_file, settings.service), start)

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
