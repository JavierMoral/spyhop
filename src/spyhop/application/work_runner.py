"""Runs the turns of a headless agent of a run: `claude -p` in a systemd
unit, through StepRunner, one work row for each turn.

The first turn of an agent starts a Claude session with a known id. Each
later turn resumes that session, so the agent keeps its context. A turn
ends with a result: done, a question for a person, or nothing valid (lost).
After a done turn, Spyhop checks the worktree: a turn that left
uncommitted changes or did not write CLAUDE-RESULT.md is unfinished.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal

from spyhop.application.checkpoint_reports import read_output
from spyhop.application.context_builder import RESULT_FILE
from spyhop.application.step_runner import StepRunner
from spyhop.application.work_results import validate_work
from spyhop.config.settings import AgentSettings
from spyhop.domain.clock import Clock, utc_now
from spyhop.domain.exceptions import UnitError
from spyhop.domain.models import AutomationRun, StepRun, Worktree
from spyhop.domain.protocols import UnitGateway
from spyhop.infrastructure.git import has_changes, head
from spyhop.infrastructure.persistence.repositories import StepRunRepository

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnState:
    """The state of the last turn of the current phase. ``none``: no turn.
    ``running``: the turn runs. ``blocked``: the turn has no result in time
    and its process still runs. The others are the outcome of a finished
    turn: ``done``, ``question``, ``lost`` (no valid result),
    ``unfinished`` (done, but the worktree is not ready), and
    ``resume_failed`` (the session of the agent is gone)."""

    state: Literal[
        "none", "running", "blocked", "done", "question", "lost", "unfinished", "resume_failed"
    ]
    detail: str | None = None
    row: StepRun | None = None


def prompt_file(row: StepRun) -> Path:
    return row.output_file.with_suffix(".prompt.md")


class WorkRunner:
    def __init__(
        self,
        rows: StepRunRepository,
        units: UnitGateway,
        spyhop: Path,
        results_dir: Path,
        agents: AgentSettings,
        now: Clock = utc_now,
    ) -> None:
        self._rows = rows
        self._results_dir = results_dir
        self._agents = agents
        self._steps = StepRunner(
            rows, units, spyhop, timedelta(minutes=agents.implement_timeout_minutes), now
        )

    async def turn(
        self,
        run: AutomationRun,
        worktree: Worktree,
        name: str,
        turn: str,
        prompt: str,
        session_id: str | None = None,
    ) -> StepRun:
        """Starts a turn of the agent ``name`` in the current phase of the
        run. Without ``session_id``, the turn starts a new session."""

        sha = await asyncio.to_thread(head, worktree.path)
        row = self._rows.create_work(
            run.id,
            run.phase,
            name,
            turn,
            session_id or str(uuid.uuid4()),
            sha or "",
            self._results_dir / run.id,
        )

        prompt_file(row).parent.mkdir(parents=True, exist_ok=True)
        prompt_file(row).write_text(prompt)

        await self._start(row, worktree)

        return row

    def last_session(self, run_id: str, name: str) -> str | None:
        """The Claude session of the last turn of the agent ``name`` in the
        run ``run_id``."""

        return self._rows.last_session(run_id, name)

    def turn_after_checks(self, run: AutomationRun) -> bool:
        """Whether the last turn of the current checks phase came after the
        start of its last round of checkpoints: a fix or a message for that
        round, which runs or still has to be acted on."""

        row = self._rows.last_work(run.id, run.phase)

        if row is None:
            return False

        checks = self._rows.list_round(run.id, run.phase, self._rows.last_round(run.id, run.phase))

        return not checks or row.started_at >= min(check.started_at for check in checks)

    async def advance(self, run: AutomationRun, worktree: Worktree) -> TurnState:
        """Records the result of the last turn of the current phase, when it
        has one, and gives its state."""

        row = self._rows.last_work(run.id, run.phase)

        if row is None:
            return TurnState("none")

        if row.status == "DONE":
            state = row.outcome if row.outcome in _OUTCOMES else "lost"

            return TurnState(state, row.detail, row)  # type: ignore[arg-type]

        poll = await self._steps.poll(row, self._limit(row))

        if poll.state == "unstarted":
            # Spyhop stopped between the row and the start of its unit.

            await self._start(row, worktree)

            return TurnState("running", None, row)

        if poll.state == "running":
            return TurnState("running", None, row)

        if poll.state == "blocked":
            return TurnState("blocked", poll.message, row)

        outcome: str
        detail: str | None

        if poll.state == "timeout":
            outcome, detail = "lost", poll.message
        else:
            result = validate_work(read_output(row.output_file))
            outcome, detail = result.outcome, result.question or result.detail

            if outcome == "done":
                problem = await self._unfinished(row, worktree)

                if problem is not None:
                    outcome, detail = "unfinished", problem

        self._rows.finish_work(row.id, outcome, detail)

        _log.info("%s: turn %s of %s: %s", run.issue_key, row.turn, row.name, outcome)

        return TurnState(outcome, detail, self._rows.get(row.id))  # type: ignore[arg-type]

    async def _start(self, row: StepRun, worktree: Worktree) -> None:
        arguments = [
            row.name,
            "--root",
            str(worktree.path),
            "--output",
            str(row.output_file),
            "--kind",
            "work",
            "--session",
            str(row.session_id),
            *(["--resume"] if self._rows.resumes(row) else []),
            "--prompt-file",
            str(prompt_file(row)),
            "--model",
            self._agents.model,
            "--effort",
            self._agents.effort,
        ]

        try:
            await self._steps.start(row, arguments, worktree.path)
        except UnitError as error:
            self._rows.finish_work(row.id, "lost", f"the turn did not start: {error}")

    def _limit(self, row: StepRun) -> timedelta:
        minutes = (
            self._agents.code_review_timeout_minutes
            if row.name == "code_review"
            else self._agents.implement_timeout_minutes
        )

        return timedelta(minutes=minutes)

    async def _unfinished(self, row: StepRun, worktree: Worktree) -> str | None:
        """Why a done turn did not leave the worktree ready, or None."""

        if await asyncio.to_thread(has_changes, worktree.path):
            return f"the worktree {worktree.path} has uncommitted changes"

        result = worktree.path / RESULT_FILE

        if not result.is_file() or result.stat().st_mtime < row.started_at.timestamp():
            return f"{RESULT_FILE} was not written in this turn"

        return None


_OUTCOMES = ("done", "question", "lost", "unfinished", "resume_failed")
