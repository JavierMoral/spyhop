"""The `spyhop steps` commands, which the units of the steps of a run start."""

import fcntl
import json
from pathlib import Path
from typing import Annotated

import typer

from spyhop.application.checkpoint_planner import AGENT_PREFIX
from spyhop.application.checkpoint_reports import RESULT_SCHEMA
from spyhop.application.checkpoint_runner import checkpoint_prompt
from spyhop.cli.common import _wake, steps_app
from spyhop.infrastructure.claude_cli import run_agent


@steps_app.command("exec", hidden=True)
def steps_exec(
    name: Annotated[str, typer.Argument(help="The checkpoint")],
    base: Annotated[str, typer.Option(help="The SHA where the branch starts")],
    head_sha: Annotated[str, typer.Option("--head", help="The SHA to check")],
    root: Annotated[Path, typer.Option(help="The root of the worktree")],
    output: Annotated[Path, typer.Option(help="The file for the result")],
) -> None:
    """Run one step and write its result to --output. Spyhop starts this
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


def _run_checkpoint(
    name: str, base: str, head_sha: str, root: Path, transcript: Path | None = None
) -> tuple[int, dict[str, object] | None]:
    """Runs the subagent and shows its steps. Returns the exit code of claude
    and its result event. With ``transcript``, writes each event of the
    stream to that file."""

    return run_agent(
        f"{AGENT_PREFIX}{name}",
        checkpoint_prompt(name, base, head_sha, root),
        RESULT_SCHEMA,
        root,
        transcript,
    )
