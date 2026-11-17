import asyncio
import fcntl
import json
import logging
import os
import subprocess
from importlib.resources import files
from pathlib import Path
from typing import Annotated

import typer

from spyhop import __version__
from spyhop.application.checkpoint_inputs import input_values
from spyhop.application.checkpoint_planner import AGENT_PREFIX, CheckpointPlanner
from spyhop.application.checkpoint_runner import RESULT_SCHEMA, checkpoint_prompt
from spyhop.application.checkpoint_verdicts import (
    VERDICTS_FILE,
    check_verdicts,
    fingerprint,
    run_reason,
)
from spyhop.application.janitor import Cleanup, WorktreeRemover
from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.config.credentials import (
    credentials_from_environment,
    read_env_file,
    write_env_file,
)
from spyhop.config.paths import (
    claude_agents_dir,
    database_file,
    default_config_file,
    spyhop_executable,
    systemd_user_unit_file,
)
from spyhop.config.settings import Settings, load_settings
from spyhop.daemon import orca_client, run_daemon
from spyhop.doctor import Level, run_checks
from spyhop.domain.enums import WorktreeStatus
from spyhop.domain.exceptions import ConfigurationError, SpyhopError
from spyhop.domain.models import ChecksPhase, ResolvedWorkflow
from spyhop.infrastructure.git import top_level
from spyhop.infrastructure.jira.client import JiraClient
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import (
    PollerStateRepository,
    RunRepository,
    WorktreeRepository,
)
from spyhop.infrastructure.process.units import SystemdUnits
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
checkpoints_app = typer.Typer(help="Plan the checkpoints of a workflow.", no_args_is_help=True)
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
            RunRepository(connection),
            worktrees,
            orca_client(settings),
            SystemdUnits(),
            settings.repositories,
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


@checkpoints_app.command("plan")
def checkpoints_plan(
    workflow: Annotated[
        str | None,
        typer.Option(help="Workflow name, instead of the run of the current worktree"),
    ] = None,
    config: ConfigOption = None,
    dry_run: Annotated[
        bool | None, typer.Option("--dry-run/--no-dry-run", help="Which database to read")
    ] = None,
) -> None:
    """Print as JSON the checks phases of the pipeline and their stages.

    Without --workflow, the workflow is the one of the run whose worktree holds
    the current directory. A stage holds read checkpoints that run in
    parallel, or one exclusive checkpoint. Each checkpoint has the current
    fingerprint of its inputs, or null when it reads the ticket and there is
    no run."""

    settings = _load(config)

    try:
        resolved, root, issue_key = _workflow(settings, workflow, config, dry_run)
        planner = CheckpointPlanner(claude_agents_dir())
        phases = [
            (index, phase, planner.plan(phase.checkpoints))
            for index, phase in enumerate(resolved.phases)
            if isinstance(phase, ChecksPhase)
        ]
        inputs = {name: planner.read(name).inputs for name in resolved.checkpoints}
        wanted = {"base", "head"}.union(*inputs.values())

        if issue_key is None:
            wanted.discard("ticket")

        values = _input_values(settings, resolved, root, issue_key, wanted) if phases else {}
    except SpyhopError as error:
        typer.echo(str(error), err=True)

        raise typer.Exit(1) from None

    def checkpoint_inputs(name: str) -> str | None:
        if not set(inputs[name]) <= values.keys():
            return None

        return fingerprint({key: values[key] for key in inputs[name]})

    plan = {
        "workflow": resolved.name,
        "base_branch": resolved.base_branch,
        "base": values.get("base"),
        "head": values.get("head"),
        "phases": [
            {
                "phase": index,
                "on_fail": phase.on_fail,
                "rounds": phase.rounds,
                "stages": [
                    {
                        "mode": stage.mode,
                        "checkpoints": [
                            {
                                "name": name,
                                "agent": f"{AGENT_PREFIX}{name}",
                                "inputs": checkpoint_inputs(name),
                            }
                            for name in stage.checkpoints
                        ],
                    }
                    for stage in stages
                ],
            }
            for index, phase, stages in phases
        ],
    }

    typer.echo(json.dumps(plan, indent=2))


@checkpoints_app.command("rerun")
def checkpoints_rerun(
    issue_key: Annotated[str, typer.Argument(help="Ticket whose checkpoints run again")],
    config: ConfigOption = None,
    dry_run: Annotated[
        bool | None, typer.Option("--dry-run/--no-dry-run", help="Which database to use")
    ] = None,
) -> None:
    """Run the checkpoints of the current phase again.

    For a run that waits with checkpoints_failed when the inputs did not
    change: for example, after you repaired the bundle of the worktree. The
    running daemon starts the new round on its next cycle."""

    settings = _load(config)
    runs = RunRepository(connect(_existing_database(config, dry_run, settings)))
    active = runs.list(issue_key=issue_key.upper())

    if not active:
        typer.echo(f"{issue_key.upper()} has no active run.", err=True)

        raise typer.Exit(1)

    automation_run = active[0]
    project_key = automation_run.issue_key.rsplit("-", 1)[0]
    resolved = WorkflowResolver(settings).resolve_by_name(project_key, automation_run.workflow_name)

    if (
        resolved is None
        or automation_run.phase >= len(resolved.phases)
        or not isinstance(resolved.phases[automation_run.phase], ChecksPhase)
    ):
        typer.echo(f"{automation_run.issue_key} does not run checkpoints now.", err=True)

        raise typer.Exit(1)

    runs.request_checks(automation_run.id)

    typer.echo(f"{automation_run.issue_key}: the checkpoints run again on the next cycle.")


@checkpoints_app.command("exec", hidden=True)
def checkpoints_exec(
    name: Annotated[str, typer.Argument(help="The checkpoint")],
    base: Annotated[str, typer.Option(help="The SHA where the branch starts")],
    head_sha: Annotated[str, typer.Option("--head", help="The SHA to check")],
    root: Annotated[Path, typer.Option(help="The root of the worktree")],
    output: Annotated[Path, typer.Option(help="The file for the result")],
) -> None:
    """Run one checkpoint and write its result to --output. Spyhop starts this
    command in a systemd user unit. It does nothing when the result exists or
    another instance runs for the same file."""

    output.parent.mkdir(parents=True, exist_ok=True)

    with (output.parent / f"{output.name}.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            typer.echo(f"Checkpoint {name} already runs for {output}.")

            return

        if output.exists():
            typer.echo(f"Checkpoint {name} already has a result in {output}.")

            return

        typer.echo(f"Checkpoint {name} on {head_sha[:12]} in {root}")

        exit_code, result = _run_checkpoint(name, base, head_sha, root)
        partial = output.parent / f"{output.name}.partial"

        partial.write_text(json.dumps({"exit_code": exit_code, "output": result}))
        partial.replace(output)

    data = (result or {}).get("structured_output")
    verdict = data.get("verdict") if isinstance(data, dict) else None

    typer.echo(f"Checkpoint {name}: {verdict or 'no verdict'}. Spyhop reads {output}.")


@checkpoints_app.command("check")
def checkpoints_check(
    workflow: Annotated[
        str | None,
        typer.Option(help="Workflow name, instead of the run of the current worktree"),
    ] = None,
    config: ConfigOption = None,
    dry_run: Annotated[
        bool | None, typer.Option("--dry-run/--no-dry-run", help="Which database to read")
    ] = None,
) -> None:
    """Print as JSON whether all the checkpoints pass on their current inputs.

    Reads CLAUDE-CHECKPOINTS.md at the root of the worktree. Exits with 1 when
    a verdict is missing, is FAIL, or is for older inputs."""

    settings = _load(config)

    try:
        resolved, root, issue_key = _workflow(settings, workflow, config, dry_run)
        planner = CheckpointPlanner(claude_agents_dir())
        inputs = {name: planner.read(name).inputs for name in dict.fromkeys(resolved.checkpoints)}
        values = _input_values(
            settings, resolved, root, issue_key, {"head"}.union(*inputs.values())
        )
    except SpyhopError as error:
        typer.echo(str(error), err=True)

        raise typer.Exit(1) from None

    verdicts_file = root / VERDICTS_FILE
    problems = check_verdicts(
        verdicts_file.read_text() if verdicts_file.is_file() else None,
        {name: fingerprint({key: values[key] for key in keys}) for name, keys in inputs.items()},
    )
    reason = run_reason(problems)
    outcome = {
        "workflow": resolved.name,
        "head": values["head"],
        "accepted": reason is None,
        "reason": reason,
        "problems": [
            {"checkpoint": problem.checkpoint, "reason": problem.reason, "detail": problem.detail}
            for problem in problems
        ],
    }

    typer.echo(json.dumps(outcome, indent=2))

    if reason is not None:
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


def _run_checkpoint(
    name: str, base: str, head_sha: str, root: Path
) -> tuple[int, dict[str, object] | None]:
    """Runs the subagent and shows its steps. Returns the exit code of claude
    and its result event."""

    command = [
        "claude",
        "-p",
        checkpoint_prompt(base, head_sha, root),
        "--agent",
        f"{AGENT_PREFIX}{name}",
        "--verbose",
        "--output-format",
        "stream-json",
        "--json-schema",
        json.dumps(RESULT_SCHEMA),
        "--dangerously-skip-permissions",
    ]
    result: dict[str, object] | None = None

    try:
        process = subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, text=True)
    except OSError as error:
        typer.echo(f"Cannot run claude: {error}", err=True)

        return 127, None

    assert process.stdout is not None

    for line in process.stdout:
        try:
            event = json.loads(line)
        except ValueError:
            continue

        if not isinstance(event, dict):
            continue

        if event.get("type") == "result":
            result = event
        elif event.get("type") == "assistant":
            for content in (event.get("message") or {}).get("content") or []:
                if content.get("type") == "tool_use" and content.get("name") != "StructuredOutput":
                    detail = json.dumps(content.get("input"))[:200]

                    typer.echo(f"> {content.get('name')}: {detail}")
                elif content.get("type") == "text":
                    typer.echo(content.get("text", ""))

    return process.wait(), result


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
    """The current values of the checkpoint inputs in ``names``. The JIRA
    credentials come from the environment, or from the env file of the
    service when the environment does not have them."""

    async def values() -> dict[str, str]:
        if "ticket" not in names or issue_key is None:
            return await input_values(names, root, resolved.base_branch, issue_key, None)

        env_file = settings.service.env_file
        environment = {**(read_env_file(env_file) if env_file.is_file() else {}), **os.environ}

        async with JiraClient(
            settings.jira.base_url,
            credentials_from_environment(settings.jira, environment),
            settings.jira.request_timeout,
        ) as jira:
            return await input_values(names, root, resolved.base_branch, issue_key, jira)

    return asyncio.run(values())


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

    project_key = automation_run.issue_key.rsplit("-", 1)[0]
    resolved = WorkflowResolver(settings).resolve_by_name(project_key, automation_run.workflow_name)

    if resolved is None:
        raise ConfigurationError(
            f"Workflow {automation_run.workflow_name!r} of {automation_run.issue_key} "
            f"is not in the configuration of project {project_key}"
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


def _format(value: object) -> str:
    if value is None:
        return "-"

    if hasattr(value, "astimezone"):
        return str(value.astimezone().strftime("%Y-%m-%d %H:%M:%S"))

    return str(value)
