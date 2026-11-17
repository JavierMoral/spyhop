"""Runs the checkpoints of a pipeline phase and records their verdicts.

Each checkpoint runs in an Orca terminal of the run's worktree, with
`spyhop checkpoints exec`. That command runs the subagent with
``claude -p --agent checkpoint-<name>`` and writes its result to a file. The
supervisor calls advance() every cycle:

- A round runs the stages of the phase in order (see CheckpointPlanner). A
  stage starts only when the worktree has no uncommitted change. A
  checkpoint that comes after a failed one in the same round is skipped.
- When a result file appears, Spyhop validates the result, adds a section to
  CLAUDE-CHECKPOINT-<name>.md and writes CLAUDE-CHECKPOINTS.md again.
- After an exclusive checkpoint, HEAD must not change and the worktree must
  have no uncommitted change.
- A failed round starts again when someone asks for it (a person, or the
  supervisor after a fix), or when the inputs of a failed checkpoint change
  while no agent works in the worktree.
- The supervisor session of the worktree, when there is one, gets the start
  of each round and each verdict.
- A checkpoint without a result in time is a FAIL. Spyhop closes its terminal
  first, and the phase stays blocked while its process still runs, so that
  it cannot change the worktree under the next stage.

The rows of checkpoint_runs keep the state, so a restart of Spyhop continues
the round. A row is stored before its terminal starts, and the command of the
terminal does nothing when another one runs for the same row or its result
exists, so a lost answer from Orca never runs a checkpoint twice.
"""

import fcntl
import json
import logging
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from spyhop.application.checkpoint_inputs import input_values
from spyhop.application.checkpoint_planner import AGENT_PREFIX, CheckpointPlanner, Stage
from spyhop.application.checkpoint_verdicts import VERDICTS_FILE, fingerprint
from spyhop.application.context_builder import EXCLUDED_PATHS, add_exclusions, git_exclude_file
from spyhop.application.supervisor_session import SupervisorSession
from spyhop.domain.exceptions import OrcaCommandError
from spyhop.domain.models import (
    AutomationRun,
    CheckpointRun,
    ChecksPhase,
    ResolvedWorkflow,
    Worktree,
)
from spyhop.domain.protocols import OrcaGateway, TicketGateway
from spyhop.infrastructure.git import has_changes, head
from spyhop.infrastructure.persistence.repositories import CheckpointRunRepository, utc_now

_log = logging.getLogger(__name__)

REPORT_FILE = "CLAUDE-CHECKPOINT-{name}.md"

# The terminal of a checkpoint when Orca opened it without a handle.
UNKNOWN_TERMINAL = "unknown"

_SEVERITIES = ("high", "medium", "low")

# The JSON object that each checkpoint gives, as `claude --json-schema` takes it.
RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "head", "commands", "findings"],
    "properties": {
        "verdict": {"enum": ["PASS", "FAIL"]},
        "head": {"type": "string"},
        "commands": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["command", "result"],
                "properties": {"command": {"type": "string"}, "result": {"type": "string"}},
            },
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["severity", "summary"],
                "properties": {
                    "severity": {"enum": list(_SEVERITIES)},
                    "file": {"type": ["string", "null"]},
                    "line": {"type": ["integer", "null"]},
                    "summary": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        },
    },
}


def checkpoint_prompt(base: str, head_sha: str, root: Path) -> str:
    return f"BASE={base}\nHEAD={head_sha}\nWorktree root: {root}\nRun the checkpoint."


@dataclass(frozen=True)
class PhaseOutcome:
    """``running``: a stage runs. ``passed``: every checkpoint of the round
    passed. ``failed``: the round ended with a FAIL or a skipped checkpoint.
    ``blocked``: the next stage cannot start, see ``message``."""

    state: Literal["running", "passed", "failed", "blocked"]
    message: str
    round: int = 0
    failed: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Result:
    verdict: Literal["PASS", "FAIL"]
    validation: str | None
    data: dict[str, Any] | None


class CheckRunner:
    def __init__(
        self,
        checkpoint_runs: CheckpointRunRepository,
        orca: OrcaGateway,
        planner: CheckpointPlanner,
        tickets: TicketGateway,
        spyhop: Path,
        results_dir: Path,
        timeout: timedelta,
        now: Callable[[], datetime] = utc_now,
        supervisor_session: SupervisorSession | None = None,
    ) -> None:
        self._checkpoint_runs = checkpoint_runs
        self._supervisor_session = supervisor_session
        self._orca = orca
        self._planner = planner
        self._tickets = tickets
        self._spyhop = spyhop
        self._results_dir = results_dir
        self._timeout = timeout
        self._now = now

    async def advance(
        self,
        run: AutomationRun,
        worktree: Worktree,
        workflow: ResolvedWorkflow,
        agent_busy: bool = False,
    ) -> PhaseOutcome:
        """Moves the checks phase of the run one step. While ``agent_busy`` is
        true, a failed round does not start again."""

        phase = workflow.phases[run.phase]

        assert isinstance(phase, ChecksPhase)

        number = self._checkpoint_runs.last_round(run.id, run.phase)

        if number == 0:
            return await self._start_round(run, worktree, workflow, phase, 1)

        rows = self._checkpoint_runs.list_round(run.id, run.phase, number)

        blocked = [
            message
            for row in rows
            if row.status == "RUNNING"
            and (message := await self._poll(row, run, worktree, workflow))
        ]

        if blocked:
            return PhaseOutcome("blocked", "; ".join(blocked), number)

        rows = self._checkpoint_runs.list_round(run.id, run.phase, number)
        names = [row.name for row in rows if row.status == "RUNNING"]

        if names:
            return PhaseOutcome("running", f"checkpoints running: {', '.join(names)}", number)

        stages = self._planner.plan(phase.checkpoints)
        failed = tuple(row.name for row in rows if row.verdict == "FAIL")
        skipped = self._skipped(phase, failed)
        started = {row.name for row in rows}
        pending = [
            stage
            for stage in stages
            if any(name not in started and name not in skipped for name in stage.checkpoints)
        ]

        if pending:
            return await self._start_stage(
                run, worktree, workflow, number, pending[0], skipped | started
            )

        if not failed:
            return PhaseOutcome("passed", "all checkpoints passed", number)

        if not agent_busy and await self._restart(run, worktree, workflow, rows):
            return await self._start_round(run, worktree, workflow, phase, number + 1)

        detail = f"failed: {', '.join(failed)}"

        if skipped:
            detail += f"; skipped: {', '.join(sorted(skipped))}"

        return PhaseOutcome("failed", detail, number, failed)

    async def _start_round(
        self,
        run: AutomationRun,
        worktree: Worktree,
        workflow: ResolvedWorkflow,
        phase: ChecksPhase,
        number: int,
    ) -> PhaseOutcome:
        stages = self._planner.plan(phase.checkpoints)

        _log.info("%s: checkpoints round %d of phase %d", run.issue_key, number, run.phase)

        if self._supervisor_session is not None:
            await self._supervisor_session.tell(
                worktree, f"ronda {number} de checkpoints: {', '.join(phase.checkpoints)}"
            )

        return await self._start_stage(run, worktree, workflow, number, stages[0], set())

    async def _start_stage(
        self,
        run: AutomationRun,
        worktree: Worktree,
        workflow: ResolvedWorkflow,
        number: int,
        stage: Stage,
        excluded: set[str],
    ) -> PhaseOutcome:
        """Starts the checkpoints of the stage that are not in ``excluded``:
        skipped, or started in an earlier cycle whose answer from Orca was
        lost."""

        # A worktree prepared by an older Spyhop does not exclude the reports.

        add_exclusions(git_exclude_file(worktree.path), EXCLUDED_PATHS)

        if has_changes(worktree.path):
            return PhaseOutcome(
                "blocked",
                f"uncommitted changes in {worktree.path}; checkpoints need a clean worktree",
                number,
            )

        names = [name for name in stage.checkpoints if name not in excluded]
        inputs = {name: self._planner.read(name).inputs for name in names}
        values = await input_values(
            {"base", "head"}.union(*inputs.values()),
            worktree.path,
            workflow.base_branch,
            run.issue_key,
            self._tickets,
        )

        for name in names:
            row = self._checkpoint_runs.create(
                run.id,
                run.phase,
                number,
                name,
                fingerprint({key: values[key] for key in inputs[name]}),
                values["base"],
                values["head"],
                self._results_dir / run.id,
            )

            await self._open_terminal(row, run, worktree, workflow)

        return PhaseOutcome("running", f"checkpoints running: {', '.join(names)}", number)

    async def _open_terminal(
        self,
        row: CheckpointRun,
        run: AutomationRun,
        worktree: Worktree,
        workflow: ResolvedWorkflow,
    ) -> None:
        command = shlex.join(
            [
                str(self._spyhop),
                "checkpoints",
                "exec",
                row.name,
                "--base",
                row.base,
                "--head",
                row.head,
                "--root",
                str(worktree.path),
                "--output",
                str(row.output_file),
            ]
        )

        try:
            terminal = await self._orca.create_terminal(
                worktree.orca_worktree_id, f"checkpoint {row.name}", command
            )
        except OrcaCommandError as error:
            await self._record(
                row,
                run,
                worktree,
                workflow,
                _Result("FAIL", f"Orca refused the terminal: {error}", None),
            )

            return

        self._checkpoint_runs.set_terminal(row.id, terminal.handle or UNKNOWN_TERMINAL)

    async def _poll(
        self, row: CheckpointRun, run: AutomationRun, worktree: Worktree, workflow: ResolvedWorkflow
    ) -> str | None:
        """Records the result of a checkpoint that runs, when there is one.
        Returns why the phase is blocked, or None."""

        if row.output_file.is_file():
            result = self._after_exclusive(
                row, worktree, _validate(_read_output(row.output_file), row.head)
            )

            await self._record(row, run, worktree, workflow, result)
        elif self._now() - row.started_at > self._timeout:
            if row.terminal not in (None, UNKNOWN_TERMINAL):
                try:
                    await self._orca.close_terminal(row.terminal)
                except OrcaCommandError as error:
                    if error.code != "selector_not_found":
                        raise

            if _still_runs(row.output_file):
                return (
                    f"checkpoint {row.name} has no result after {self._timeout} and its "
                    f"process still runs; stop it in its Orca terminal"
                )

            await self._record(
                row,
                run,
                worktree,
                workflow,
                _Result("FAIL", f"no result after {self._timeout}", None),
            )
        elif row.terminal is None:
            # The answer of Orca was lost. The command does nothing when the
            # first terminal runs.

            await self._open_terminal(row, run, worktree, workflow)

        return None

    def _after_exclusive(self, row: CheckpointRun, worktree: Worktree, result: _Result) -> _Result:
        if self._planner.read(row.name).mode != "exclusive":
            return result

        if head(worktree.path) != row.head or has_changes(worktree.path):
            return _Result(
                "FAIL",
                "the checkpoint changed HEAD or left uncommitted changes in the worktree",
                result.data,
            )

        return result

    async def _record(
        self,
        row: CheckpointRun,
        run: AutomationRun,
        worktree: Worktree,
        workflow: ResolvedWorkflow,
        result: _Result,
    ) -> None:
        self._checkpoint_runs.finish(row.id, result.verdict, result.validation)

        _append_report(worktree.path, row, result, self._now())
        _write_verdicts(worktree.path, workflow.checkpoints, self._checkpoint_runs.latest(run.id))

        _log.info(
            "%s: checkpoint %s: %s%s",
            run.issue_key,
            row.name,
            result.verdict,
            f" ({result.validation})" if result.validation else "",
        )

        if self._supervisor_session is not None:
            await self._supervisor_session.tell(
                worktree,
                f"checkpoint {row.name}: {result.verdict}"
                + (f" ({result.validation})" if result.validation else "")
                + f"; informe en {REPORT_FILE.format(name=row.name)}",
            )

    def _skipped(self, phase: ChecksPhase, failed: Sequence[str]) -> set[str]:
        skipped: set[str] = set()

        for name in failed:
            for stage in self._planner.plan(phase.checkpoints, start=name):
                skipped.update(stage.checkpoints)

        return skipped - set(failed)

    async def _restart(
        self,
        run: AutomationRun,
        worktree: Worktree,
        workflow: ResolvedWorkflow,
        rows: Sequence[CheckpointRun],
    ) -> bool:
        """Whether a failed round starts again by itself: a person asked for
        it, or the inputs of a failed checkpoint changed."""

        started = min(row.started_at for row in rows)

        if run.checks_requested_at is not None and run.checks_requested_at > started:
            return True

        failed = [row for row in rows if row.verdict == "FAIL"]
        inputs = {row.name: self._planner.read(row.name).inputs for row in failed}
        values = await input_values(
            set().union(*inputs.values()),
            worktree.path,
            workflow.base_branch,
            run.issue_key,
            self._tickets,
        )

        return any(
            row.inputs != fingerprint({key: values[key] for key in inputs[row.name]})
            for row in failed
        )


def _still_runs(output_file: Path) -> bool:
    """Whether `spyhop checkpoints exec` holds the lock of the result file."""

    lock_file = output_file.parent / f"{output_file.name}.lock"

    if not lock_file.is_file():
        return False

    with lock_file.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True

        fcntl.flock(lock, fcntl.LOCK_UN)

    return False


def _read_output(output_file: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(output_file.read_text())
    except (OSError, ValueError):
        return None

    return payload if isinstance(payload, dict) else None


def _validate(payload: dict[str, Any] | None, expected_head: str) -> _Result:
    """The verdict of a checkpoint. A result that does not follow the
    contract is a FAIL, with the reason in ``validation``."""

    if payload is None:
        return _Result("FAIL", "the result file is not valid JSON", None)

    output = payload.get("output")

    if not isinstance(output, dict):
        return _Result(
            "FAIL", f"claude exited with {payload.get('exit_code')} without a result", None
        )

    if output.get("is_error"):
        return _Result("FAIL", f"claude ended with an error: {output.get('subtype')}", None)

    data = output.get("structured_output")

    if data is None:
        data = _json_object(str(output.get("result") or ""))

    if not isinstance(data, dict):
        return _Result("FAIL", "the result has no JSON object", None)

    problem = _contract_problem(data, expected_head)

    if problem:
        return _Result("FAIL", problem, data)

    return _Result(data["verdict"], None, data)


def _contract_problem(data: dict[str, Any], expected_head: str) -> str | None:
    for key in ("verdict", "head", "commands", "findings"):
        if key not in data:
            return f"the result has no {key}"

    if data["verdict"] not in ("PASS", "FAIL"):
        return f"verdict {data['verdict']!r} is not PASS or FAIL"

    if data["head"] != expected_head:
        return f"head {data['head']!r} is not the HEAD of the stage {expected_head}"

    if not isinstance(data["commands"], list) or not isinstance(data["findings"], list):
        return "commands and findings must be lists"

    if data["verdict"] == "PASS" and not data["commands"]:
        return "a PASS needs commands as evidence"

    if data["verdict"] == "PASS" and any(
        isinstance(finding, dict) and finding.get("severity") in ("high", "medium")
        for finding in data["findings"]
    ):
        return "a PASS cannot have a high or medium finding"

    return None


def _json_object(text: str) -> Any:
    """The JSON object of a text answer, also inside a code fence."""

    text = text.strip()

    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]

    try:
        return json.loads(text)
    except ValueError:
        return None


def _append_report(root: Path, row: CheckpointRun, result: _Result, now: datetime) -> None:
    data = result.data or {}
    lines = [
        f"## {now.astimezone():%Y-%m-%d %H:%M}, phase {row.phase}, round {row.round}: "
        f"{result.verdict}",
        "",
        f"- Agent: {AGENT_PREFIX}{row.name}. Inputs: {row.inputs}.",
        f"- Validation: {result.validation or 'OK'}.",
        "",
        "### Commands",
        "",
    ]

    for command in data.get("commands") or []:
        if isinstance(command, dict):
            lines.append(f"- `{command.get('command', '')}`: {command.get('result', '')}")

    lines += ["", "### Findings", ""]

    for number, finding in enumerate(data.get("findings") or [], 1):
        if not isinstance(finding, dict):
            continue

        location = finding.get("file") or ""

        if finding.get("line") is not None:
            location += f":{finding['line']}"

        lines.append(
            f"{number}. **{finding.get('severity', '')}** `{location}`: "
            f"{finding.get('summary', '')}"
        )

        if finding.get("evidence"):
            lines.append(f"   Evidence: {finding['evidence']}")

    report = root / REPORT_FILE.format(name=row.name)
    existing = report.read_text() if report.is_file() else f"# Checkpoint {row.name}\n"

    report.write_text(existing.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n")


def _write_verdicts(
    root: Path, checkpoints: Sequence[str], latest: dict[str, CheckpointRun]
) -> None:
    lines = [
        "# Checkpoints",
        "",
        "Spyhop writes this file. Do not edit it.",
        "",
        "| Checkpoint | Verdict | Inputs | Round | Report |",
        "| --- | --- | --- | --- | --- |",
    ]

    for name in checkpoints:
        row = latest.get(name)

        if row is not None:
            lines.append(
                f"| {name} | {row.verdict} | {row.inputs} | {row.round} | "
                f"{REPORT_FILE.format(name=name)} |"
            )

    (root / VERDICTS_FILE).write_text("\n".join(lines) + "\n")
