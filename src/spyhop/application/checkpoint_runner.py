"""Runs the checkpoints of a pipeline phase and records their verdicts.

Each checkpoint runs in its own systemd user unit, outside the Spyhop
service, with `spyhop checkpoints exec`. That command runs the subagent with
``claude -p --agent checkpoint-<name>`` and writes its result to a file, and
its steps to a log file next to it. The supervisor calls advance() every
cycle:

- A round runs the stages of the phase in order (see CheckpointPlanner). A
  stage starts only when the worktree has no uncommitted change. A
  checkpoint that comes after a failed one in the same round is skipped.
- When a result file appears, Spyhop validates the result, adds a section to
  CLAUDE-CHECKPOINT-<name>.md and writes CLAUDE-CHECKPOINTS.md again (see
  checkpoint_reports).
- After an exclusive checkpoint, HEAD must not change and the worktree must
  have no uncommitted change.
- A failed round starts again when someone asks for it (a person, or the
  supervisor after a fix), or when the inputs of a failed checkpoint change
  while no agent works in the worktree. A person can ask for only the failed
  and stale checkpoints: the new round keeps each PASS whose inputs did not
  change, as a row that does not run.
- The supervisor session of the worktree, when there is one, gets the start
  of each round, the start of each checkpoint with its log, and each verdict.
- A checkpoint without a result in time is a FAIL. Spyhop stops its unit
  first, and the phase stays blocked while its process still runs, so that
  it cannot change the worktree under the next stage.

The rows of checkpoint_runs keep the state, so a restart of Spyhop continues
the round, and a restart does not stop the units. A row is stored before its
unit starts. The unit has a fixed name, and its command does nothing when
another one runs for the same row or its result exists, so a checkpoint never
runs twice.
"""

import asyncio
import fcntl
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from spyhop.application.checkpoint_inputs import input_values
from spyhop.application.checkpoint_planner import CheckpointPlanner, Stage
from spyhop.application.checkpoint_reports import (
    REPORT_FILE,
    CheckpointResult,
    append_report,
    read_output,
    validate,
    write_verdicts,
)
from spyhop.application.checkpoint_verdicts import fingerprint
from spyhop.application.context_builder import EXCLUDED_PATHS, add_exclusions, git_exclude_file
from spyhop.application.supervisor_session import SupervisorSession
from spyhop.domain.exceptions import UnitError
from spyhop.domain.models import (
    AutomationRun,
    CheckpointRun,
    ChecksPhase,
    ResolvedWorkflow,
    Worktree,
)
from spyhop.domain.protocols import TicketGateway, UnitGateway
from spyhop.infrastructure.git import has_changes, head
from spyhop.infrastructure.persistence.repositories import CheckpointRunRepository, utc_now

_log = logging.getLogger(__name__)

UNIT_PREFIX = "spyhop-checkpoint-"


def unit_pattern(run_id: str) -> str:
    """The units of the checkpoints of a run."""

    return f"{UNIT_PREFIX}{run_id}-*"


def log_file(row: CheckpointRun) -> Path:
    return row.output_file.with_suffix(".log")


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


class CheckRunner:
    def __init__(
        self,
        checkpoint_runs: CheckpointRunRepository,
        units: UnitGateway,
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
        self._units = units
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
            kept = (
                await self._kept(run, worktree, workflow, phase)
                if run.checks_failed_only and _requested(run, rows)
                else []
            )

            return await self._start_round(run, worktree, workflow, phase, number + 1, kept)

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
        kept: Sequence[CheckpointRun] = (),
    ) -> PhaseOutcome:
        """Starts the round. Each row in ``kept`` is a PASS that the round
        copies and does not run again."""

        for row in kept:
            copy = self._checkpoint_runs.create(
                run.id,
                run.phase,
                number,
                row.name,
                row.inputs,
                row.base,
                row.head,
                self._results_dir / run.id,
            )

            self._checkpoint_runs.finish(copy.id, "PASS", f"kept from round {row.round}")

        excluded = {row.name for row in kept}
        stage = next(
            stage
            for stage in self._planner.plan(phase.checkpoints)
            if any(name not in excluded for name in stage.checkpoints)
        )

        return await self._start_stage(
            run, worktree, workflow, number, stage, excluded, new_round=True
        )

    async def _start_stage(
        self,
        run: AutomationRun,
        worktree: Worktree,
        workflow: ResolvedWorkflow,
        number: int,
        stage: Stage,
        excluded: set[str],
        new_round: bool = False,
    ) -> PhaseOutcome:
        """Starts the checkpoints of the stage that are not in ``excluded``:
        skipped, or started in an earlier cycle whose answer from Orca was
        lost."""

        # A worktree prepared by an older Spyhop does not exclude the reports.

        add_exclusions(git_exclude_file(worktree.path), EXCLUDED_PATHS)

        if await asyncio.to_thread(has_changes, worktree.path):
            return PhaseOutcome(
                "blocked",
                f"uncommitted changes in {worktree.path}; checkpoints need a clean worktree",
                number,
            )

        if new_round:
            # Only here: a round that cannot start yet is tried on each cycle.

            phase = workflow.phases[run.phase]

            assert isinstance(phase, ChecksPhase)

            _log.info("%s: checkpoints round %d of phase %d", run.issue_key, number, run.phase)

            if self._supervisor_session is not None:
                names = [name for name in phase.checkpoints if name not in excluded]

                await self._supervisor_session.tell(
                    worktree, f"ronda {number} de checkpoints: {', '.join(names)}"
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

            await self._start_unit(row, run, worktree, workflow)

        return PhaseOutcome("running", f"checkpoints running: {', '.join(names)}", number)

    async def _start_unit(
        self,
        row: CheckpointRun,
        run: AutomationRun,
        worktree: Worktree,
        workflow: ResolvedWorkflow,
    ) -> None:
        unit = f"{UNIT_PREFIX}{run.id}-{row.id}"
        command = [
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

        try:
            await self._units.start(unit, command, worktree.path, log_file(row))
        except UnitError as error:
            await self._record(
                row,
                run,
                worktree,
                workflow,
                CheckpointResult("FAIL", f"the checkpoint did not start: {error}", None),
            )

            return

        self._checkpoint_runs.set_unit(row.id, unit, self._now())

        if self._supervisor_session is not None:
            await self._supervisor_session.tell(
                worktree, f"checkpoint {row.name} arrancado; log en {log_file(row)}"
            )

    async def _poll(
        self, row: CheckpointRun, run: AutomationRun, worktree: Worktree, workflow: ResolvedWorkflow
    ) -> str | None:
        """Records the result of a checkpoint that runs, when there is one.
        Returns why the phase is blocked, or None."""

        if row.output_file.is_file():
            result = await self._after_exclusive(
                row, worktree, validate(read_output(row.output_file), row.head)
            )

            await self._record(row, run, worktree, workflow, result)
        elif row.unit is None:
            # Spyhop stopped between the row and the start of its unit. A
            # second start of the unit does nothing. The timeout counts from
            # the start of the unit.

            await self._start_unit(row, run, worktree, workflow)
        elif self._now() - row.started_at > self._timeout:
            try:
                await self._units.stop(row.unit)
            except UnitError as error:
                _log.warning("%s: %s", run.issue_key, error)

            if _still_runs(row.output_file):
                return (
                    f"checkpoint {row.name} has no result after {self._timeout} and its "
                    f"process still runs; stop it with `systemctl --user stop {row.unit}`"
                )

            await self._record(
                row,
                run,
                worktree,
                workflow,
                CheckpointResult("FAIL", f"no result after {self._timeout}", None),
            )

        return None

    async def _after_exclusive(
        self, row: CheckpointRun, worktree: Worktree, result: CheckpointResult
    ) -> CheckpointResult:
        if self._planner.read(row.name).mode != "exclusive":
            return result

        current_head = await asyncio.to_thread(head, worktree.path)

        if current_head != row.head or await asyncio.to_thread(has_changes, worktree.path):
            return CheckpointResult(
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
        result: CheckpointResult,
    ) -> None:
        self._checkpoint_runs.finish(row.id, result.verdict, result.validation)

        append_report(worktree.path, row, result, self._now())
        write_verdicts(worktree.path, workflow.checkpoints, self._checkpoint_runs.latest(run.id))

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

        if _requested(run, rows):
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

    async def _kept(
        self, run: AutomationRun, worktree: Worktree, workflow: ResolvedWorkflow, phase: ChecksPhase
    ) -> list[CheckpointRun]:
        """The last PASS of each checkpoint of the phase whose inputs did not
        change since then."""

        latest = self._checkpoint_runs.latest(run.id, run.phase)
        passed = [
            row
            for name in phase.checkpoints
            if (row := latest.get(name)) is not None and row.verdict == "PASS"
        ]
        inputs = {row.name: self._planner.read(row.name).inputs for row in passed}
        values = await input_values(
            set().union(*inputs.values()),
            worktree.path,
            workflow.base_branch,
            run.issue_key,
            self._tickets,
        )

        return [
            row
            for row in passed
            if row.inputs == fingerprint({key: values[key] for key in inputs[row.name]})
        ]


def _requested(run: AutomationRun, rows: Sequence[CheckpointRun]) -> bool:
    """Whether someone asked for a new round after the start of ``rows``."""

    started = min(row.started_at for row in rows)

    return run.checks_requested_at is not None and run.checks_requested_at > started


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
