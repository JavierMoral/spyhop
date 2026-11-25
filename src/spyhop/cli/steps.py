"""The `spyhop steps` commands, which the units of the steps of a run start."""

import fcntl
import json
from pathlib import Path
from typing import Annotated, Literal

import typer

from spyhop.application.checkpoint_planner import AGENT_PREFIX
from spyhop.application.checkpoint_reports import RESULT_SCHEMA
from spyhop.application.checkpoint_runner import checkpoint_prompt
from spyhop.application.work_results import WORK_SCHEMA
from spyhop.cli.common import _wake, steps_app
from spyhop.infrastructure.claude_cli import run_agent


@steps_app.command("exec", hidden=True)
def steps_exec(
    name: Annotated[str, typer.Argument(help="The checkpoint, or the agent of a work turn")],
    root: Annotated[Path, typer.Option(help="The root of the worktree")],
    output: Annotated[Path, typer.Option(help="The file for the result")],
    base: Annotated[
        str | None, typer.Option(help="The SHA where the branch starts (a check)")
    ] = None,
    head_sha: Annotated[
        str | None, typer.Option("--head", help="The SHA to check (a check)")
    ] = None,
    kind: Annotated[
        Literal["check", "work"], typer.Option(help="A checkpoint, or a turn of an agent")
    ] = "check",
    session: Annotated[str | None, typer.Option(help="The Claude session of a work turn")] = None,
    resume: Annotated[
        bool, typer.Option("--resume", help="Resume --session, not start it (a work turn)")
    ] = False,
    prompt_file: Annotated[
        Path | None, typer.Option(help="The file with the prompt of a work turn")
    ] = None,
    model: Annotated[str | None, typer.Option(help="The model of a work turn")] = None,
    effort: Annotated[str | None, typer.Option(help="The effort of a work turn")] = None,
) -> None:
    """Run one step and write its result to --output. Spyhop starts this
    command in a systemd user unit. It does nothing when the result exists or
    another instance runs for the same file."""

    if kind == "check" and (base is None or head_sha is None):
        raise typer.BadParameter("a check needs --base and --head")

    if kind == "work" and (session is None or prompt_file is None):
        raise typer.BadParameter("a work turn needs --session and --prompt-file")

    label = "Checkpoint" if kind == "check" else "Step"

    output.parent.mkdir(parents=True, exist_ok=True)

    with (output.parent / f"{output.name}.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            typer.echo(f"{label} {name} already runs for {output}.")

            return

        if output.exists():
            typer.echo(f"{label} {name} already has a result in {output}.")

            return

        # Always a result file: without one, Spyhop waits for the timeout.

        try:
            if kind == "check":
                assert base is not None and head_sha is not None

                typer.echo(f"Checkpoint {name} on {head_sha[:12]} in {root}")

                exit_code, result = _run_checkpoint(name, base, head_sha, root)
            else:
                assert session is not None and prompt_file is not None

                typer.echo(f"Step {name} ({'resume' if resume else 'start'} {session}) in {root}")

                exit_code, result = _run_work(
                    prompt_file.read_text(), session, resume, model, effort, root
                )
        except Exception as error:
            typer.echo(f"{label} {name} stopped: {error!r}", err=True)

            exit_code, result = 1, None

        partial = output.parent / f"{output.name}.partial"

        partial.write_text(json.dumps({"exit_code": exit_code, "output": result}))
        partial.replace(output)

    _wake()

    data = (result or {}).get("structured_output")
    key = "verdict" if kind == "check" else "status"
    value = data.get(key) if isinstance(data, dict) else None

    typer.echo(f"{label} {name}: {value or f'no {key}'}. Spyhop reads {output}.")


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


def _run_work(
    prompt: str,
    session: str,
    resume: bool,
    model: str | None,
    effort: str | None,
    root: Path,
) -> tuple[int, dict[str, object] | None]:
    """Runs one turn of the main agent in the Claude session ``session``:
    the first turn starts the session, a later turn resumes it. The agent
    cannot ask with AskUserQuestion: it ends the turn with a question."""

    options = [
        "--resume" if resume else "--session-id",
        session,
        "--disallowed-tools",
        "AskUserQuestion",
        *(["--model", model] if model else []),
        *(["--effort", effort] if effort else []),
    ]

    return run_agent(None, prompt, WORK_SCHEMA, root, options=options)
