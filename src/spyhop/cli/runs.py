"""The commands that start, follow and change the runs."""

import asyncio
import csv
import dataclasses
import json
import logging
import os
import statistics
import sys
from datetime import timedelta
from typing import Annotated, Any, Literal

import typer

from spyhop.application.executions import history as agent_history
from spyhop.application.executions import profile_history, total_tokens
from spyhop.application.janitor import Cleanup, WorktreeRemover
from spyhop.application.stats import run_stats
from spyhop.cli.common import (
    ConfigOption,
    _duration,
    _existing_database,
    _finish,
    _format,
    _jira_client,
    _load,
    _orca,
    _run_commands,
    _setup_logging,
    _wake,
    app,
)
from spyhop.config.paths import (
    database_file,
    state_dir,
)
from spyhop.daemon import run_daemon
from spyhop.domain.clock import utc_now
from spyhop.domain.exceptions import OrcaCommandError, SpyhopError
from spyhop.domain.models import AgentExecution
from spyhop.infrastructure.agent_state import AGENT_STATES, pane_key, write_state
from spyhop.infrastructure.jira.mapping import ticket_text
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import (
    ExecutionRepository,
    PollerStateRepository,
    RunRepository,
    SessionRepository,
    WorktreeRepository,
)
from spyhop.infrastructure.process.units import SystemdUnits


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
    agent: Annotated[
        str | None,
        typer.Argument(help="The agent, for example implement or checkpoint-security"),
    ] = None,
    days: Annotated[int, typer.Option(min=1, help="Only the last days")] = 30,
    by: Annotated[
        Literal["day", "week", "month"], typer.Option(help="The period of each row of an agent")
    ] = "week",
    source: Annotated[
        Literal["pipeline", "eval", "all"],
        typer.Option(help="The executions of the runs, of the evals, or both"),
    ] = "pipeline",
    as_csv: Annotated[
        bool, typer.Option("--csv", help="Print each execution as CSV, for other tools")
    ] = False,
    profile: Annotated[
        bool, typer.Option("--profile", help="Show where the agent can improve")
    ] = False,
    config: ConfigOption = None,
    dry_run: Annotated[
        bool | None, typer.Option("--dry-run/--no-dry-run", help="Which database to read")
    ] = None,
) -> None:
    """Show what the runs and the agents of the last days took.

    Without an agent, shows the runs and a summary of each agent. With an
    agent, shows how its tokens and its time change, period by period. The
    service records an execution for each Claude session of a checkpoint, of
    an agent of a run, and of the supervisor session. `spyhop eval` records
    its runs with the source eval. TIME is the time that the agent worked,
    without the waits for a person. IN counts the input tokens with the cache.

    With --profile, shows the medians of the tool calls (CALLS), of the
    growth of the context that each later call pays again (CTX+), of the size
    of the tool results (RESULT), of the Bash calls that start like an earlier
    call (REPEAT), and of the errors. MAX is the largest tool result. TOOLS and
    ERRORS add the calls and the errors of the period: exit (an exit code that
    is not zero), hook (a hook blocked the call), denied (the permissions or
    the person refused the call), and tool (the input of the call is not
    valid)."""

    connection = connect(_existing_database(config, dry_run))
    since = utc_now() - timedelta(days=days)
    executions = ExecutionRepository(connection).list(
        agent, since, None if source == "all" else source
    )

    if as_csv:
        writer = csv.writer(sys.stdout)

        writer.writerow(field.name for field in dataclasses.fields(AgentExecution))

        for execution in executions:
            writer.writerow(
                json.dumps(value) if isinstance(value, dict) else value
                for value in (
                    getattr(execution, field.name) for field in dataclasses.fields(execution)
                )
            )

        return

    if agent is None:
        run_repository = RunRepository(connection)
        runs = [
            run for run in run_repository.list(include_terminal=True) if run.created_at >= since
        ]
        result = run_stats(runs, {run.id: run_repository.events(run.id) for run in runs})
        statuses = ", ".join(f"{status} {count}" for status, count in result.runs_by_status.items())

        typer.echo(
            f"Runs of the last {days} days: {len(runs)}{f' ({statuses})' if statuses else ''}"
        )
        typer.echo(f"Median time to the plan: {_duration(result.median_plan_seconds)}")
        typer.echo(f"Median time to complete: {_duration(result.median_completed_seconds)}")
        typer.echo(f"Restarts of agents: {result.restarts}")

        groups: dict[str, list[AgentExecution]] = {}

        for execution in executions:
            groups.setdefault(execution.agent, []).append(execution)

        if groups:
            typer.echo(
                f"\n{'AGENT':<26}{'RUNS':>6}{'PASS':>6}{'MEDIAN TIME':>13}"
                f"{'MEDIAN TOKENS':>15}  LAST"
            )

        for name, group in sorted(groups.items()):
            verdicts = [execution.outcome for execution in group if execution.outcome]
            passed = (
                f"{sum(verdict == 'PASS' for verdict in verdicts) / len(verdicts):.0%}"
                if verdicts
                else "-"
            )

            typer.echo(
                f"{name:<26}{len(group):>6}{passed:>6}"
                f"{_time(statistics.median(e.active_ms for e in group)):>13}"
                f"{_tokens(statistics.median(total_tokens(e) for e in group)):>15}"
                f"  {_format(group[-1].started_at)}"
            )

        return

    if not executions:
        typer.echo(f"No executions of {agent} in the last {days} days.")

        return

    if profile:
        typer.echo(
            f"{'PERIOD':<12}{'RUNS':>6}{'CALLS':>7}{'CTX+':>8}{'RESULT':>9}{'MAX':>9}"
            f"{'REPEAT':>8}{'ERRORS':>8}  TOOLS / ERRORS"
        )

        for item in profile_history(executions, by):
            tools = " ".join(f"{name} {count}" for name, count in list(item.calls.items())[:4])
            errors = " ".join(f"{kind} {count}" for kind, count in item.errors.items())

            typer.echo(
                f"{item.period:<12}{item.count:>6}{item.median_calls:>7g}"
                f"{_tokens(item.median_context_added_tokens):>8}"
                f"{_tokens(item.median_result_bytes) + 'B':>9}"
                f"{_tokens(item.max_result_bytes) + 'B':>9}"
                f"{item.median_repeated_bash:>8g}{item.median_errors:>8g}"
                f"  {tools}{f' / {errors}' if errors else ''}"
            )

        return

    typer.echo(
        f"{'PERIOD':<12}{'RUNS':>6}{'PASS':>6}{'TIME':>9}{'P90':>9}"
        f"{'IN':>9}{'OUT':>8}{'TOTAL':>9}  NEW VERSION"
    )

    for row in agent_history(executions, by):
        passed = f"{row.passed / row.count:.0%}" if row.passed is not None else "-"

        typer.echo(
            f"{row.period:<12}{row.count:>6}{passed:>6}"
            f"{_time(row.median_active_ms):>9}{_time(row.p90_active_ms):>9}"
            f"{_tokens(row.median_input_tokens):>9}{_tokens(row.median_output_tokens):>8}"
            f"{_tokens(row.total_tokens):>9}  {' '.join(row.new_versions)}"
        )


def _time(milliseconds: float) -> str:
    return (
        f"{milliseconds / 1000:.0f}s" if milliseconds < 60_000 else _duration(milliseconds / 1000)
    )


def _tokens(count: float) -> str:
    for limit, unit in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if count >= limit:
            return f"{count / limit:.1f}{unit}"

    return str(round(count))


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
        orca = _orca(settings)
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

        _wake()

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

    Orca types the message into the terminal of the agent and submits it.
    After a completed run, a new run starts where a new implementation agent
    makes the change, and the checkpoints after the implementation run again."""

    settings = _load(config)
    connection = connect(_existing_database(config, dry_run, settings))
    active = RunRepository(connection).list(issue_key=issue_key.upper())

    if not active:
        _finish(lambda: _run_commands(config, dry_run).change(issue_key, text))

        return

    automation_run = active[0]
    session = SessionRepository(connection).find_launched(automation_run.id)

    if session is None or session.agent_handle is None:
        typer.echo(f"{automation_run.issue_key} has no agent.", err=True)

        raise typer.Exit(1)

    agent = f"{session.role or 'run'} agent {session.agent_handle}"

    try:
        asyncio.run(_orca(settings).send_prompt(session.agent_handle, text))
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
            _wake()
    except Exception as error:  # noqa: BLE001
        logging.getLogger("spyhop").debug("hook ignored: %s", error)
