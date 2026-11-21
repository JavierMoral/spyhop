"""The `spyhop checkpoints` commands."""

import fcntl
import json
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from spyhop.application.checkpoint_planner import AGENT_PREFIX, CheckpointPlanner
from spyhop.application.checkpoint_runner import RESULT_SCHEMA, checkpoint_prompt
from spyhop.application.checkpoint_verdicts import (
    VERDICTS_FILE,
    check_verdicts,
    fingerprint,
    run_reason,
)
from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.cli.common import (
    ConfigOption,
    _existing_database,
    _input_values,
    _load,
    _wake,
    _workflow,
    checkpoints_app,
)
from spyhop.config.paths import (
    claude_agents_dir,
)
from spyhop.domain.exceptions import SpyhopError
from spyhop.domain.models import ChecksPhase
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import (
    RunRepository,
)


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
    _wake()

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

    _wake()

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
