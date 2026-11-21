import asyncio
import fcntl
import inspect
import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Coroutine
from datetime import timedelta
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any

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
from spyhop.application.run_commands import CommandResult, RunCommandError, RunCommands
from spyhop.application.stats import run_stats
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
    state_dir,
    systemd_user_unit_file,
)
from spyhop.config.settings import Settings, load_settings
from spyhop.daemon import orca_client, run_daemon
from spyhop.doctor import Level, run_checks
from spyhop.domain.enums import WorktreeStatus
from spyhop.domain.exceptions import ConfigurationError, OrcaCommandError, SpyhopError
from spyhop.domain.models import ChecksPhase, ResolvedWorkflow
from spyhop.infrastructure.agent_state import AGENT_STATES, pane_key, write_state
from spyhop.infrastructure.git import top_level
from spyhop.infrastructure.jira.client import JiraClient
from spyhop.infrastructure.jira.mapping import ticket_text
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import (
    CheckpointRunRepository,
    PollerStateRepository,
    RunRepository,
    SessionRepository,
    WorktreeRepository,
    utc_now,
)
from spyhop.infrastructure.process.units import SystemdUnits
from spyhop.infrastructure.service.systemd import (
    install_unit,
    linger_enabled,
    render_unit,
    uninstall_unit,
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
    timeline: Annotated[
        bool, typer.Option("--timeline", help="Show what happened to each run, in order")
    ] = False,
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
    sessions = SessionRepository(connection)
    run_repository = RunRepository(connection)
    runs = run_repository.list(issue_key=issue_key, include_terminal=show_all)

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

        for session in sessions.list_for_run(automation_run.id):
            typer.echo(
                f"{'':<24}agent: {session.role or '-'} {session.model or '-'} "
                f"{session.agent_handle or '-'} {session.status}"
            )

        if timeline:
            for event in run_repository.events(automation_run.id):
                typer.echo(
                    f"{'':<24}{_format(event.at)}  {event.kind}"
                    f"{f': {event.detail}' if event.detail else ''}"
                )


@app.command()
def stats(
    days: Annotated[int, typer.Option(min=1, help="Only the runs of the last days")] = 30,
    config: ConfigOption = None,
    dry_run: Annotated[
        bool | None, typer.Option("--dry-run/--no-dry-run", help="Which database to read")
    ] = None,
) -> None:
    """Show what the runs of the last days took.

    The cost is only the cost of the checkpoints, from their result files.
    Orca does not give the cost of the plan and implementation agents."""

    connection = connect(_existing_database(config, dry_run))
    since = utc_now() - timedelta(days=days)
    runs = [
        run
        for run in RunRepository(connection).list(include_terminal=True)
        if run.created_at >= since
    ]
    sessions = SessionRepository(connection)
    checkpoint_runs = CheckpointRunRepository(connection)
    run_repository = RunRepository(connection)
    result = run_stats(
        runs,
        [session for run in runs for session in sessions.list_for_run(run.id)],
        [row for run in runs for row in checkpoint_runs.list_for_run(run.id)],
        {run.id: run_repository.events(run.id) for run in runs},
    )
    statuses = ", ".join(f"{status} {count}" for status, count in result.runs_by_status.items())

    typer.echo(f"Runs of the last {days} days: {len(runs)}{f' ({statuses})' if statuses else ''}")
    typer.echo(f"Median time to the plan: {_duration(result.median_plan_seconds)}")
    typer.echo(f"Median time to complete: {_duration(result.median_completed_seconds)}")
    typer.echo(f"Restarts of agents: {result.restarts}")

    if result.agents:
        typer.echo(f"\n{'AGENT':<14}{'COUNT':>7}{'MEAN TIME':>12}")

        for agent in result.agents:
            typer.echo(f"{agent.role:<14}{agent.count:>7}{_duration(agent.mean_seconds):>12}")

    if result.checkpoints:
        typer.echo(f"\n{'CHECKPOINT':<18}{'RUNS':>6}{'PASS':>7}{'MEAN TIME':>12}{'COST':>10}")

        for checkpoint in result.checkpoints:
            typer.echo(
                f"{checkpoint.name:<18}{checkpoint.count:>6}"
                f"{checkpoint.passed / checkpoint.count:>7.0%}"
                f"{_duration(checkpoint.mean_seconds):>12}{f'${checkpoint.cost_usd:.2f}':>10}"
            )


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

    _finish(lambda: _run_commands(config, dry_run).fail(issue_key, reason))


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

    _finish(lambda: _run_commands(config, dry_run).retry(target))


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
        orca = orca_client(settings)
        remover = WorktreeRemover(
            RunRepository(connection),
            worktrees,
            worktree_host=orca,
            terminal_host=orca,
            board=orca,
            units=SystemdUnits(),
            repositories=settings.repositories,
        )

        async def remove() -> Cleanup:
            await remover.ensure_ready()

            return await remover.clean_up(worktree, "cleaned up by a person")

        outcome = asyncio.run(remove())
    except SpyhopError as error:
        typer.echo(f"{worktree.issue_key}: removal not finished: {error}", err=True)

        raise typer.Exit(1) from None
    finally:
        # The run can be cancelled also when the worktree stays.

        wake_service()

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
def approve(
    issue_key: Annotated[str, typer.Argument(help="Ticket whose plan is approved")],
    config: ConfigOption = None,
    dry_run: Annotated[
        bool | None, typer.Option("--dry-run/--no-dry-run", help="Which database to use")
    ] = None,
) -> None:
    """Approve the plan of a run that waits for its review.

    The running daemon starts the next phase on its next cycle. The card of
    the worktree also moves to orca.plan_approved_status, to show it on the
    board."""

    _finish(lambda: _run_commands(config, dry_run).approve(issue_key))


@app.command()
def declare(
    issue_key: Annotated[str, typer.Argument(help="Ticket of the agent")],
    what: Annotated[
        str, typer.Argument(help="plan: CLAUDE-PLAN.md is ready. done: CLAUDE-RESULT.md is ready")
    ],
    config: ConfigOption = None,
    dry_run: Annotated[
        bool | None, typer.Option("--dry-run/--no-dry-run", help="Which database to use")
    ] = None,
) -> None:
    """Declare that the plan or the work of the run is ready.

    The agent of the run runs this command at the end of its /goal. The card
    of the worktree also moves, to show it on the board."""

    _finish(lambda: _run_commands(config, dry_run).declare(issue_key, what))


@app.command()
def replan(
    issue_key: Annotated[str, typer.Argument(help="Ticket whose plan changes")],
    changes: Annotated[str, typer.Argument(help="The changes to the plan")],
    config: ConfigOption = None,
    dry_run: Annotated[
        bool | None, typer.Option("--dry-run/--no-dry-run", help="Which database to use")
    ] = None,
) -> None:
    """Start a new plan agent that changes CLAUDE-PLAN.md.

    For a plan that waits for its review when its agent is closed. The running
    daemon starts the agent on its next cycle, and the plan waits for the
    review again."""

    _finish(lambda: _run_commands(config, dry_run).replan(issue_key, changes))


@app.command()
def tell(
    issue_key: Annotated[str, typer.Argument(help="Ticket whose agent gets the message")],
    text: Annotated[str, typer.Argument(help="The message for the agent")],
    config: ConfigOption = None,
    dry_run: Annotated[
        bool | None, typer.Option("--dry-run/--no-dry-run", help="Which database to use")
    ] = None,
) -> None:
    """Send a message to the agent of a ticket's active run.

    Orca types the message into the terminal of the agent and submits it."""

    settings = _load(config)
    connection = connect(_existing_database(config, dry_run, settings))
    active = RunRepository(connection).list(issue_key=issue_key.upper())

    if not active:
        typer.echo(f"{issue_key.upper()} has no active run.", err=True)

        raise typer.Exit(1)

    automation_run = active[0]
    session = SessionRepository(connection).find_launched(automation_run.id)

    if session is None or session.agent_handle is None:
        typer.echo(f"{automation_run.issue_key} has no agent.", err=True)

        raise typer.Exit(1)

    agent = f"{session.role or 'run'} agent {session.agent_handle}"

    try:
        asyncio.run(orca_client(settings).send_prompt(session.agent_handle, text))
    except OrcaCommandError as error:
        typer.echo(
            f"{automation_run.issue_key}: Orca refused the message for the {agent}, "
            f"which can be closed: {error}",
            err=True,
        )

        raise typer.Exit(1) from None
    except SpyhopError as error:
        typer.echo(
            f"{automation_run.issue_key}: the message may not have reached the {agent}: {error}",
            err=True,
        )

        raise typer.Exit(1) from None

    typer.echo(f"{automation_run.issue_key}: message sent to the {agent}.")


@app.command()
def ticket(
    issue_key: Annotated[str, typer.Argument(help="Ticket to read")],
    config: ConfigOption = None,
) -> None:
    """Show a JIRA ticket with all its fields and comments.

    Rich text becomes plain text. Fields without a value are left out."""

    settings = _load(config)

    async def read() -> dict[str, Any]:
        async with _jira_client(settings) as jira:
            return await jira.issue(issue_key.upper(), ["*all"], expand="names")

    try:
        issue = asyncio.run(read())
    except SpyhopError as error:
        typer.echo(f"{issue_key.upper()}: cannot read the ticket: {error}", err=True)

        raise typer.Exit(1) from None

    typer.echo(ticket_text(issue), nl=False)


@app.command()
def doctor(config: ConfigOption = None) -> None:
    """Check the configuration and everything that Spyhop needs to run."""

    checks = asyncio.run(run_checks(config or default_config_file()))
    symbols = {Level.OK: "✓", Level.WARNING: "!", Level.ERROR: "✗"}

    for check in checks:
        typer.echo(f"{symbols[check.level]} {check.name}: {check.detail}")

    if any(check.level is Level.ERROR for check in checks):
        raise typer.Exit(1)


@app.command(hidden=True)
def hook() -> None:
    """Record the state of the Claude agent of this terminal. The Claude Code
    hooks of each worktree run this command with the event on stdin."""

    # A hook must never stop Claude: any problem here is ignored.

    try:
        pane = pane_key(dict(os.environ))

        if pane is not None and write_state(
            state_dir() / AGENT_STATES, pane, json.loads(sys.stdin.read() or "{}")
        ):
            wake_service()
    except Exception as error:  # noqa: BLE001
        logging.getLogger("spyhop").debug("hook ignored: %s", error)


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
    failed: Annotated[
        bool, typer.Option("--failed", help="Run only the failed and stale checkpoints")
    ] = False,
    config: ConfigOption = None,
    dry_run: Annotated[
        bool | None, typer.Option("--dry-run/--no-dry-run", help="Which database to use")
    ] = None,
) -> None:
    """Run the checkpoints of the current phase again.

    For a run that waits with checkpoints_failed when the inputs did not
    change: for example, after you repaired the bundle of the worktree. The
    running daemon starts the new round on its next cycle. With --failed,
    the round keeps each PASS whose inputs did not change."""

    settings = _load(config)
    runs = RunRepository(connect(_existing_database(config, dry_run, settings)))
    active = runs.list(issue_key=issue_key.upper())

    if not active:
        typer.echo(f"{issue_key.upper()} has no active run.", err=True)

        raise typer.Exit(1)

    automation_run = active[0]
    resolved = WorkflowResolver(settings).resolve_for_run(automation_run)

    if (
        resolved is None
        or automation_run.phase >= len(resolved.phases)
        or not isinstance(resolved.phases[automation_run.phase], ChecksPhase)
    ):
        typer.echo(f"{automation_run.issue_key} does not run checkpoints now.", err=True)

        raise typer.Exit(1)

    runs.request_checks(automation_run.id, failed)
    wake_service()

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

        # Always a result file: without one, Spyhop waits for the timeout.

        try:
            exit_code, result = _run_checkpoint(name, base, head_sha, root)
        except Exception as error:
            typer.echo(f"Checkpoint {name} stopped: {error!r}", err=True)

            exit_code, result = 1, None

        partial = output.parent / f"{output.name}.partial"

        partial.write_text(json.dumps({"exit_code": exit_code, "output": result}))
        partial.replace(output)

    wake_service()

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
                if not isinstance(content, dict):
                    continue

                if content.get("type") == "tool_use" and content.get("name") != "StructuredOutput":
                    detail = json.dumps(content.get("input"))[:200]

                    typer.echo(f"> {content.get('name')}: {detail}")
                elif content.get("type") == "text":
                    typer.echo(content.get("text", ""))

    return process.wait(), result


def _run_commands(config: Path | None, dry_run: bool | None) -> RunCommands:
    settings = _load(config)
    connection = connect(_existing_database(config, dry_run, settings))

    return RunCommands(
        RunRepository(connection),
        WorktreeRepository(connection),
        lambda: orca_client(settings),
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

    wake_service()
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
