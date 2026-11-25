"""The steps that ran for a run: its checkpoints and the turns of its agents."""

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from spyhop.domain.clock import Clock, utc_now
from spyhop.domain.models import (
    StepRun,
)
from spyhop.infrastructure.persistence.database import transaction
from spyhop.infrastructure.persistence.repositories._mapping import (
    _record_event,
    _to_step_run,
    _to_text,
)


class StepRunRepository:
    def __init__(self, connection: sqlite3.Connection, now: Clock = utc_now) -> None:
        self._connection = connection
        self._now = now

    def create(
        self,
        run_id: str,
        phase: int,
        round_number: int,
        name: str,
        inputs: str,
        base: str,
        head: str,
        results_dir: Path,
    ) -> StepRun:
        """Stored before the unit starts, so a restart of Spyhop does not start
        the checkpoint twice. The result goes to a file named after the row in
        ``results_dir``."""

        with transaction(self._connection):
            checkpoint_run_id = str(uuid.uuid4())
            output_file = results_dir / f"{checkpoint_run_id}.json"
            new_round = round_number > self.last_round(run_id, phase)

            self._connection.execute(
                """
                INSERT INTO step_runs (
                    id, run_id, phase, round, name, inputs, base, head, output_file,
                    status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?)
                """,
                (
                    checkpoint_run_id,
                    run_id,
                    phase,
                    round_number,
                    name,
                    inputs,
                    base,
                    head,
                    str(output_file),
                    _to_text(self._now()),
                ),
            )

            # A new round consumes the requests for a round of the run. A later
            # stage of the same round does not.

            if new_round:
                self._connection.execute(
                    "UPDATE run_commands SET consumed_at = ? "
                    "WHERE run_id = ? AND kind = 'checks' AND consumed_at IS NULL",
                    (_to_text(self._now()), run_id),
                )

            checkpoint_run = self.get(checkpoint_run_id)

            assert checkpoint_run is not None

            return checkpoint_run

    def create_work(
        self,
        run_id: str,
        phase: int,
        name: str,
        turn: str,
        session_id: str,
        head: str,
        results_dir: Path,
    ) -> StepRun:
        """A turn of an agent of the run, stored before its unit starts. Its
        round counts the turns of the phase."""

        with transaction(self._connection):
            step_run_id = str(uuid.uuid4())
            [last] = self._connection.execute(
                "SELECT MAX(round) FROM step_runs WHERE run_id = ? AND phase = ? AND kind = 'work'",
                (run_id, phase),
            ).fetchone()

            self._connection.execute(
                """
                INSERT INTO step_runs (
                    id, run_id, phase, round, name, inputs, base, head, output_file,
                    status, started_at, kind, session_id, turn
                ) VALUES (?, ?, ?, ?, ?, '', '', ?, ?, 'RUNNING', ?, 'work', ?, ?)
                """,
                (
                    step_run_id,
                    run_id,
                    phase,
                    (last or 0) + 1,
                    name,
                    head,
                    str(results_dir / f"{step_run_id}.json"),
                    _to_text(self._now()),
                    session_id,
                    turn,
                ),
            )

            row = self.get(step_run_id)

            assert row is not None

            return row

    def last_work(self, run_id: str, phase: int | None = None) -> StepRun | None:
        """The last turn of the run, or of one phase of the run."""

        row = self._connection.execute(
            "SELECT * FROM step_runs WHERE run_id = ? AND kind = 'work' "
            "AND (? IS NULL OR phase = ?) ORDER BY rowid DESC LIMIT 1",
            (run_id, phase, phase),
        ).fetchone()

        return _to_step_run(row) if row else None

    def last_session(self, run_id: str, name: str) -> str | None:
        """The Claude session of the last turn of the agent ``name``."""

        row = self._connection.execute(
            "SELECT session_id FROM step_runs WHERE run_id = ? AND kind = 'work' AND name = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (run_id, name),
        ).fetchone()

        return row["session_id"] if row else None

    def resumes(self, row: StepRun) -> bool:
        """Whether the turn resumes its session: an earlier turn of the run
        started it."""

        found = self._connection.execute(
            "SELECT 1 FROM step_runs WHERE run_id = ? AND kind = 'work' AND session_id = ? "
            "AND rowid < (SELECT rowid FROM step_runs WHERE id = ?) LIMIT 1",
            (row.run_id, row.session_id, row.id),
        ).fetchone()

        return found is not None

    def finish_work(self, step_run_id: str, outcome: str, detail: str | None) -> None:
        with transaction(self._connection):
            self._connection.execute(
                "UPDATE step_runs SET status = 'DONE', outcome = ?, detail = ?, finished_at = ? "
                "WHERE id = ?",
                (outcome, detail, _to_text(self._now()), step_run_id),
            )

            row = self.get(step_run_id)

            if row is not None:
                _record_event(
                    self._connection,
                    row.run_id,
                    "turn",
                    f"{row.name} {row.turn}: {outcome}{f' ({detail})' if detail else ''}",
                    self._now(),
                )

    def get(self, checkpoint_run_id: str) -> StepRun | None:
        row = self._connection.execute(
            "SELECT * FROM step_runs WHERE id = ?", (checkpoint_run_id,)
        ).fetchone()

        return _to_step_run(row) if row else None

    def last_round(self, run_id: str, phase: int) -> int:
        """The number of the last round of the phase, or 0 before the first."""

        row = self._connection.execute(
            "SELECT MAX(round) FROM step_runs WHERE run_id = ? AND phase = ? AND kind = 'check'",
            (run_id, phase),
        ).fetchone()

        return int(row[0] or 0)

    def list_round(self, run_id: str, phase: int, round_number: int) -> list[StepRun]:
        rows = self._connection.execute(
            "SELECT * FROM step_runs "
            "WHERE run_id = ? AND phase = ? AND round = ? AND kind = 'check' ORDER BY rowid",
            (run_id, phase, round_number),
        )

        return [_to_step_run(row) for row in rows]

    def list_for_run(self, run_id: str) -> list[StepRun]:
        """The checkpoints of the run, oldest first."""

        rows = self._connection.execute(
            "SELECT * FROM step_runs WHERE run_id = ? AND kind = 'check' ORDER BY rowid",
            (run_id,),
        )

        return [_to_step_run(row) for row in rows]

    def latest(self, run_id: str, phase: int | None = None) -> dict[str, StepRun]:
        """The last finished run of each checkpoint of the run, or of one phase
        of the run."""

        rows = self._connection.execute(
            "SELECT * FROM step_runs WHERE run_id = ? AND status = 'DONE' "
            "AND kind = 'check' AND (? IS NULL OR phase = ?) ORDER BY rowid",
            (run_id, phase, phase),
        )

        return {row["name"]: _to_step_run(row) for row in rows}

    def set_unit(self, checkpoint_run_id: str, unit: str, started_at: datetime) -> None:
        """The timeout of the checkpoint counts from ``started_at``, the start
        of its unit."""

        self._connection.execute(
            "UPDATE step_runs SET unit = ?, started_at = ? WHERE id = ?",
            (unit, _to_text(started_at), checkpoint_run_id),
        )

    def finish(self, checkpoint_run_id: str, verdict: str, detail: str | None) -> None:
        with transaction(self._connection):
            self._connection.execute(
                "UPDATE step_runs SET status = 'DONE', verdict = ?, detail = ?, "
                "finished_at = ? WHERE id = ?",
                (verdict, detail, _to_text(self._now()), checkpoint_run_id),
            )

            row = self.get(checkpoint_run_id)

            if row is not None:
                _record_event(
                    self._connection,
                    row.run_id,
                    "checkpoint",
                    f"{row.name} round {row.round}: {verdict}",
                    self._now(),
                )
