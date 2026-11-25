"""The `spyhop eval` and `spyhop eval-case` commands."""

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, cast

import typer

from spyhop.application.checkpoint_planner import AGENT_PREFIX
from spyhop.application.evals import (
    EVAL_TICKETS_ENV_VAR,
    REVIEW_SCHEMA,
    EvalCase,
    Grade,
    build_repository,
    case_from_commit,
    grade,
    grade_review,
    load_cases,
    review_prompt,
    run_metrics,
)
from spyhop.application.executions import result_execution
from spyhop.cli.checkpoints import _run_agent, _run_checkpoint
from spyhop.cli.common import _load, app
from spyhop.config.paths import claude_agents_dir, database_file, evals_dir
from spyhop.domain.exceptions import SpyhopError
from spyhop.infrastructure.git import has_changes
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import ExecutionRepository


@app.command("eval")
def eval_checkpoint(
    checkpoint: Annotated[
        str,
        typer.Argument(
            help="A checkpoint, for example security, or a review agent, for example "
            "custom-pr-reviewer"
        ),
    ],
    case: Annotated[str, typer.Option(help="Run only the cases that match this glob")] = "*",
    runs: Annotated[int, typer.Option(min=1, help="The runs of each case")] = 1,
) -> None:
    """Run an agent on its eval cases and compare each result with the
    expected result.

    The cases are in <evals dir>/<agent>/<case>. Each run creates a
    temporary repository with a commit for base/ and one for head/, and runs
    the agent there. The agent is checkpoint-<agent>.md when it exists, and
    its cases give the expected verdict. Otherwise the agent is <agent>.md,
    a review agent, and its cases give the expected findings. `spyhop
    ticket` reads the tickets of <evals dir>/tickets, not JIRA. Each run adds
    a line with its consumption and its result to <evals dir>/metrics.jsonl.
    Exits with 1 when a run fails."""

    try:
        cases = load_cases(evals_dir() / checkpoint, case)
    except SpyhopError as error:
        typer.echo(str(error), err=True)

        raise typer.Exit(1) from None

    if not cases:
        typer.echo(f"No eval case of {checkpoint} matches {case!r}.", err=True)

        raise typer.Exit(1)

    agent_file = claude_agents_dir() / f"{AGENT_PREFIX}{checkpoint}.md"
    review = not agent_file.exists()

    if review:
        agent_file = claude_agents_dir() / f"{checkpoint}.md"

    wrong = [case.name for case in cases if (case.findings is not None) != review]

    if wrong:
        kind = "expect.findings" if review else "expect.verdict"

        typer.echo(f"These cases have no {kind}: {', '.join(wrong)}", err=True)

        raise typer.Exit(1)

    try:
        agent = agent_file.read_bytes()
    except OSError as error:
        typer.echo(f"Cannot read the agent {agent_file}: {error}", err=True)

        raise typer.Exit(1) from None

    agent_sha = hashlib.sha256(agent).hexdigest()
    agent_copy = evals_dir() / "agents" / f"{agent_sha}.md"
    eval_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    tickets_dir = evals_dir() / "tickets"
    results_dir = evals_dir() / checkpoint / "results" / eval_id

    results_dir.mkdir(parents=True)
    agent_copy.parent.mkdir(exist_ok=True)

    if not agent_copy.exists():
        agent_copy.write_bytes(agent)

    common = {
        "eval_id": eval_id,
        "checkpoint": checkpoint,
        "agent_sha": agent_sha,
        "claude_version": _claude_version(),
    }

    os.environ[EVAL_TICKETS_ENV_VAR] = str(tickets_dir)
    executions = ExecutionRepository(connect(database_file(_load(None).application.dry_run)))

    summary: list[dict[str, object]] = []
    cost = 0.0

    try:
        for eval_case in cases:
            task = (
                (tickets_dir / f"{eval_case.ticket}.task.md").read_text()
                if eval_case.ticket
                else None
            )
            grades: list[Grade] = []
            obtained: list[str] = []

            for number in range(1, runs + 1):
                typer.echo(f"== {eval_case.name}: run {number} of {runs}")

                with tempfile.TemporaryDirectory(prefix="spyhop-eval-") as temporary:
                    root = Path(temporary) / "portal"
                    base, head = build_repository(eval_case, root, task)
                    transcript = results_dir / f"{eval_case.name}-{number}.jsonl"
                    exit_code, result = (
                        _run_agent(
                            checkpoint,
                            review_prompt(base, head),
                            REVIEW_SCHEMA,
                            root,
                            transcript,
                        )
                        if review
                        else _run_checkpoint(checkpoint, base, head, root, transcript)
                    )
                    payload = {"exit_code": exit_code, "output": result}

                    if has_changes(root):
                        run_grade = Grade(False, "the agent changed the repository")
                    elif review:
                        run_grade = grade_review(eval_case, payload)
                    else:
                        run_grade = grade(eval_case, payload, head)

                (results_dir / f"{eval_case.name}-{number}.json").write_text(json.dumps(payload))

                metrics = {
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                    **common,
                    "run": number,
                    **run_metrics(eval_case, run_grade, payload, transcript),
                }

                with (evals_dir() / "metrics.jsonl").open("a") as metrics_file:
                    metrics_file.write(json.dumps(metrics) + "\n")

                # The outcome of an eval is its grade, not the verdict of the agent.

                execution = result_execution(
                    result or {},
                    agent_file.stem,
                    "eval",
                    datetime.now(UTC),
                    agent_sha=agent_sha,
                    outcome="PASS" if run_grade.passed else "FAIL",
                    transcript=transcript,
                )

                if execution is not None:
                    executions.save(execution)

                run_cost = (result or {}).get("total_cost_usd")

                if isinstance(run_cost, int | float):
                    cost += run_cost

                typer.echo(f"== {eval_case.name}: {run_grade.reason}")
                grades.append(run_grade)
                obtained.append(_obtained(eval_case, run_grade, metrics))

            summary.append(
                {
                    "case": eval_case.name,
                    "expected": _expected(eval_case),
                    "passed": sum(run_grade.passed for run_grade in grades),
                    "runs": runs,
                    "obtained": obtained,
                    "reasons": [run_grade.reason for run_grade in grades],
                }
            )
    except (OSError, subprocess.CalledProcessError) as error:
        typer.echo(f"The eval stopped: {error}", err=True)

        raise typer.Exit(1) from None

    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    typer.echo("")

    typer.echo("RUNS  EXPECTED  OBTAINED  CASE")

    for row in summary:
        results = " ".join(cast(list[str], row["obtained"]))
        marker = "" if row["passed"] == row["runs"] else "  <-"

        typer.echo(
            f"{str(row['passed']) + '/' + str(row['runs']):<5} {row['expected']:<9} "
            f"{results:<9} {row['case']}{marker}"
        )

    passed = sum(int(str(row["passed"])) for row in summary)
    total = runs * len(summary)

    typer.echo(f"\n{passed}/{total} runs passed. Cost: {cost:.2f} USD. Results: {results_dir}")

    if passed < total:
        raise typer.Exit(1)


@app.command("eval-case")
def eval_case(
    agent: Annotated[str, typer.Argument(help="The review agent, for example custom-pr-reviewer")],
    repository: Annotated[Path, typer.Option("--repo", help="The repository of the commit")],
    commit: Annotated[str, typer.Option(help="A commit that fixes a violation")],
    rule: Annotated[str | None, typer.Option(help="The rule that the commit fixes")] = None,
    check: Annotated[str | None, typer.Option(help="The check of the agent")] = None,
    name: Annotated[str | None, typer.Option(help="The case name")] = None,
    one: Annotated[
        bool,
        typer.Option(
            "--one", help="Give one expected finding that any hunk matches, as for a rename"
        ),
    ] = False,
) -> None:
    """Create an eval case of a review agent from a commit that fixes a
    violation.

    The head of the case holds the changed files before the commit, and its
    base the same files without the lines that the commit changes. So the
    case only adds the code with the violation. Each hunk gives one expected
    finding, or with --one, all hunks give one
    expected finding that any of them matches. Review the case.yaml file
    before you use the case."""

    case_dir = evals_dir() / agent / (name or f"{rule or check}-{commit[:8]}".lower())

    try:
        count = case_from_commit(repository, commit, case_dir, rule, check, one)
    except SpyhopError as error:
        typer.echo(str(error), err=True)

        raise typer.Exit(1) from None
    except subprocess.CalledProcessError as error:
        typer.echo(f"git failed: {(error.stderr or '').strip() or error}", err=True)

        raise typer.Exit(1) from None

    typer.echo(f"Created {case_dir} with {count} expected findings. Review its case.yaml.")


def _expected(case: EvalCase) -> str:
    """The verdict of a checkpoint case, or the number of findings of a review
    case."""

    if case.findings is None:
        return str(case.verdict)

    return f"{len(case.findings)}/{len(case.findings)}"


def _obtained(case: EvalCase, run_grade: Grade, metrics: dict[str, object]) -> str:
    """The verdict of a checkpoint run, or the expected findings that a review
    run gave, with ``+Nfp`` for its false positives."""

    if case.findings is None:
        return str(metrics["verdict"] or "none")

    false_positives = len(run_grade.false_positives)

    return f"{run_grade.found}/{len(case.findings)}" + (
        f"+{false_positives}fp" if false_positives else ""
    )


def _claude_version() -> str | None:
    try:
        completed = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    return completed.stdout.strip() or None
