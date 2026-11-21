"""The checkpoints that ran for a run."""

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from spyhop.domain.clock import Clock, utc_now
from spyhop.domain.models import (
    CheckpointRun,
)
from spyhop.infrastructure.persistence.database import transaction
from spyhop.infrastructure.persistence.repositories._mapping import (
    _record_event,
    _to_checkpoint_run,
    _to_text,
)


class CheckpointRunRepository:
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
    ) -> CheckpointRun:
        """Stored before the unit starts, so a restart of Spyhop does not start
        the checkpoint twice. The result goes to a file named after the row in
        ``results_dir``."""

        with transaction(self._connection):
            checkpoint_run_id = str(uuid.uuid4())
            output_file = results_dir / f"{checkpoint_run_id}.json"
            new_round = round_number > self.last_round(run_id, phase)

            self._connection.execute(
                """
                INSERT INTO checkpoint_runs (
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

    def get(self, checkpoint_run_id: str) -> CheckpointRun | None:
        row = self._connection.execute(
            "SELECT * FROM checkpoint_runs WHERE id = ?", (checkpoint_run_id,)
        ).fetchone()

        return _to_checkpoint_run(row) if row else None

    def last_round(self, run_id: str, phase: int) -> int:
        """The number of the last round of the phase, or 0 before the first."""

        row = self._connection.execute(
            "SELECT MAX(round) FROM checkpoint_runs WHERE run_id = ? AND phase = ?",
            (run_id, phase),
        ).fetchone()

        return int(row[0] or 0)

    def list_round(self, run_id: str, phase: int, round_number: int) -> list[CheckpointRun]:
        rows = self._connection.execute(
            "SELECT * FROM checkpoint_runs WHERE run_id = ? AND phase = ? AND round = ? "
            "ORDER BY rowid",
            (run_id, phase, round_number),
        )

        return [_to_checkpoint_run(row) for row in rows]

    def list_for_run(self, run_id: str) -> list[CheckpointRun]:
        rows = self._connection.execute(
            "SELECT * FROM checkpoint_runs WHERE run_id = ? ORDER BY rowid", (run_id,)
        )

        return [_to_checkpoint_run(row) for row in rows]

    def latest(self, run_id: str, phase: int | None = None) -> dict[str, CheckpointRun]:
        """The last finished run of each checkpoint of the run, or of one phase
        of the run."""

        rows = self._connection.execute(
            "SELECT * FROM checkpoint_runs WHERE run_id = ? AND status = 'DONE' "
            "AND (? IS NULL OR phase = ?) ORDER BY rowid",
            (run_id, phase, phase),
        )

        return {row["name"]: _to_checkpoint_run(row) for row in rows}

    def set_unit(self, checkpoint_run_id: str, unit: str, started_at: datetime) -> None:
        """The timeout of the checkpoint counts from ``started_at``, the start
        of its unit."""

        self._connection.execute(
            "UPDATE checkpoint_runs SET unit = ?, started_at = ? WHERE id = ?",
            (unit, _to_text(started_at), checkpoint_run_id),
        )

    def finish(self, checkpoint_run_id: str, verdict: str, detail: str | None) -> None:
        with transaction(self._connection):
            self._connection.execute(
                "UPDATE checkpoint_runs SET status = 'DONE', verdict = ?, detail = ?, "
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
