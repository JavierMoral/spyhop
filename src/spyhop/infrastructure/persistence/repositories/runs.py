"""The runs, their requests and their events."""

import builtins
import json
import sqlite3
import uuid
from datetime import datetime

from spyhop.domain.clock import Clock, utc_now
from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.models import (
    AutomationRun,
    IssueEntry,
    Phase,
    RunEvent,
)
from spyhop.infrastructure.persistence.database import transaction
from spyhop.infrastructure.persistence.repositories._mapping import (
    _phase_name,
    _phase_to_json,
    _record_event,
    _to_run,
    _to_text,
)


class RunRepository:
    def __init__(self, connection: sqlite3.Connection, now: Clock = utc_now) -> None:
        self._connection = connection
        self._now = now

    def create_discovered(self, entry: IssueEntry) -> AutomationRun | None:
        """Returns None when the transition is already recorded or the issue
        already has an active run."""

        now = self._now()
        run_id = str(uuid.uuid4())

        with transaction(self._connection):
            try:
                self._connection.execute(
                    """
                    INSERT INTO automation_runs (
                        id, issue_key, workflow_name, trigger_status, trigger_history_id,
                        triggered_at, status, created_at, status_changed_at, signals
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'declare')
                    """,
                    (
                        run_id,
                        entry.issue.key,
                        entry.workflow.name,
                        entry.to_status,
                        entry.history_id,
                        _to_text(entry.entered_at),
                        RunStatus.DISCOVERED,
                        _to_text(now),
                        _to_text(now),
                    ),
                )
            except sqlite3.IntegrityError:
                return None

            self._event(run_id, "created", f"{entry.to_status} ({entry.workflow.name})")

        return self.get(run_id)

    def get(self, run_id: str) -> AutomationRun | None:
        row = self._connection.execute("SELECT * FROM run_view WHERE id = ?", (run_id,)).fetchone()

        return _to_run(row) if row else None

    def has_transition(self, issue_key: str, history_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM automation_runs WHERE issue_key = ? AND trigger_history_id = ?",
            (issue_key, history_id),
        ).fetchone()

        return row is not None

    def list(
        self, issue_key: str | None = None, include_terminal: bool = False
    ) -> list[AutomationRun]:
        query = "SELECT * FROM run_view"
        conditions = []
        parameters: list[str] = []

        if issue_key:
            conditions.append("issue_key = ?")
            parameters.append(issue_key)

        if not include_terminal:
            conditions.append("status NOT IN (?, ?, ?)")
            parameters.extend(status for status in RunStatus if status.terminal)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY created_at DESC"

        return [_to_run(row) for row in self._connection.execute(query, parameters)]

    def list_by_status(self, *statuses: RunStatus) -> builtins.list[AutomationRun]:
        """Oldest first, the order in which pending runs are launched."""

        placeholders = ", ".join("?" for _ in statuses)
        rows = self._connection.execute(
            f"SELECT * FROM run_view WHERE status IN ({placeholders}) "
            "ORDER BY triggered_at, created_at",
            statuses,
        )

        return [_to_run(row) for row in rows]

    def count_by_status(self, *statuses: RunStatus) -> int:
        placeholders = ", ".join("?" for _ in statuses)
        row = self._connection.execute(
            f"SELECT COUNT(*) FROM automation_runs WHERE status IN ({placeholders})", statuses
        ).fetchone()

        return int(row[0])

    def mark_preparing(self, run_id: str) -> None:
        self._update(run_id, RunStatus.PREPARING, waiting_reason=None, error_message=None)

    def note_wait(self, run_id: str, reason: WaitingReason, message: str) -> None:
        """Records why a pending run does not advance, without changing its
        status: it resumes by itself once the reason is gone."""

        with transaction(self._connection):
            run = self.get(run_id)

            self._connection.execute(
                "UPDATE automation_runs SET waiting_reason = ?, error_message = ? WHERE id = ?",
                (reason, message, run_id),
            )

            # The launcher clears the reason and notes it again in each cycle of
            # the same wait: only the first time is an event.

            last = self._last_status(run_id)

            if run is not None and not (last or "").startswith(f"{run.status} {reason}"):
                self._event(run_id, "status", f"{run.status} {reason}: {message}")

    def mark_ready(self, run_id: str, worktree_id: str) -> None:
        self._update(run_id, RunStatus.READY, worktree_id=worktree_id)

    def mark_running(self, run_id: str) -> None:
        self._update(run_id, RunStatus.RUNNING, started_at=_to_text(self._now()))

    def mark_resumed(self, run_id: str) -> None:
        """Back to RUNNING after waiting for a person."""

        self._update(
            run_id,
            RunStatus.RUNNING,
            waiting_reason=None,
            error_message=None,
        )

    def mark_waiting(self, run_id: str, reason: WaitingReason, message: str | None = None) -> None:
        self._update(
            run_id, RunStatus.WAITING_FOR_HUMAN, waiting_reason=reason, error_message=message
        )

    def mark_completed(self, run_id: str) -> None:
        self._update(
            run_id,
            RunStatus.COMPLETED,
            waiting_reason=None,
            error_message=None,
            finished_at=_to_text(self._now()),
        )

    def mark_cancelled(self, run_id: str, reason: str) -> None:
        self._update(
            run_id,
            RunStatus.CANCELLED,
            waiting_reason=None,
            error_message=reason,
            finished_at=_to_text(self._now()),
        )

    def create_retry(
        self, original: AutomationRun, change: str | None = None
    ) -> AutomationRun | None:
        """A new run of the same ticket and transition, pending launch. None
        when the ticket already has an active run. With ``change``, a new
        implementation agent makes that change (see AgentLauncher)."""

        now = self._now()
        run_id = str(uuid.uuid4())

        with transaction(self._connection):
            try:
                self._connection.execute(
                    """
                    INSERT INTO automation_runs (
                        id, issue_key, workflow_name, trigger_status, trigger_history_id,
                        triggered_at, status, created_at, status_changed_at, retry_of, signals
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'declare')
                    """,
                    (
                        run_id,
                        original.issue_key,
                        original.workflow_name,
                        original.trigger_status,
                        original.trigger_history_id,
                        _to_text(original.triggered_at),
                        RunStatus.DISCOVERED,
                        _to_text(now),
                        _to_text(now),
                        original.id,
                    ),
                )
            except sqlite3.IntegrityError:
                return None

            self._event(run_id, "created", f"retry of {original.id[:8]}")

            if change is not None:
                self._command(run_id, "change", change)

        return self.get(run_id)

    def find(self, target: str) -> builtins.list[AutomationRun]:
        """Runs matching a run id or id prefix, or the latest run of a ticket
        key."""

        rows = self._connection.execute(
            "SELECT * FROM run_view WHERE id LIKE ? ORDER BY created_at DESC",
            (f"{target.lower()}%",),
        ).fetchall()

        if not rows:
            rows = self._connection.execute(
                "SELECT * FROM run_view WHERE issue_key = ? ORDER BY created_at DESC LIMIT 1",
                (target.upper(),),
            ).fetchall()

        return [_to_run(row) for row in rows]

    def set_phase(self, run_id: str, phase: int) -> None:
        with transaction(self._connection):
            self._connection.execute(
                "UPDATE automation_runs SET phase = ? WHERE id = ?",
                (phase, run_id),
            )

            run = self.get(run_id)
            phases = run.pipeline if run and run.pipeline else ()

            self._event(
                run_id,
                "phase",
                _phase_name(phases[phase]) if phase < len(phases) else f"phase {phase}",
            )

    def request_checks(self, run_id: str, failed_only: bool = False) -> None:
        """Asks for a new round of the checks phase. The next round of the run
        consumes the request."""

        self._command(run_id, "checks", "failed" if failed_only else "all")

    def add_notice(self, run_id: str, text: str) -> None:
        self._command(run_id, "notice", text)

    def add_message(self, run_id: str, text: str) -> None:
        """A message of a person for the headless agent of the run. It goes
        to the agent as its next turn."""

        self._command(run_id, "message", text)

    def pending_messages(self, run_id: str) -> builtins.list[tuple[int, str]]:
        """The messages that did not reach the agent, oldest first, with the
        id that `consume_command` takes."""

        return [
            (row["id"], row["payload"])
            for row in self._connection.execute(
                "SELECT id, payload FROM run_commands "
                "WHERE run_id = ? AND kind = 'message' AND consumed_at IS NULL ORDER BY id",
                (run_id,),
            )
        ]

    def pending_notices(self, run_id: str) -> builtins.list[tuple[int, str]]:
        """The notices of the run that did not reach the session, oldest first,
        with the id that `consume_command` takes."""

        return [
            (row["id"], row["payload"])
            for row in self._connection.execute(
                "SELECT id, payload FROM run_commands "
                "WHERE run_id = ? AND kind = 'notice' AND consumed_at IS NULL ORDER BY id",
                (run_id,),
            )
        ]

    def take_plan_changes(self, run_id: str) -> str | None:
        """The changes of the last `spyhop replan`. Consumes that request and
        the ones before it, but not one that arrives after the read."""

        with transaction(self._connection):
            row = self._connection.execute(
                "SELECT id, payload FROM run_commands "
                "WHERE run_id = ? AND kind = 'replan' AND consumed_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (run_id,),
            ).fetchone()

            if row is None:
                return None

            self._connection.execute(
                "UPDATE run_commands SET consumed_at = ? "
                "WHERE run_id = ? AND kind = 'replan' AND consumed_at IS NULL AND id <= ?",
                (_to_text(self._now()), run_id, row["id"]),
            )

            return str(row["payload"])

    def pending_change(self, run_id: str) -> str | None:
        """The change that the next implementation agent of the run makes."""

        row = self._connection.execute(
            "SELECT payload FROM run_commands "
            "WHERE run_id = ? AND kind = 'change' AND consumed_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()

        return str(row["payload"]) if row else None

    def consume_command(self, command_id: int) -> None:
        self._connection.execute(
            "UPDATE run_commands SET consumed_at = ? WHERE id = ?",
            (_to_text(self._now()), command_id),
        )

    def approve(self, run_id: str) -> None:
        """A person approves the plan. The supervisor consumes the request when
        it starts the next phase."""

        self._command(run_id, "approve")

    def set_pipeline(self, run_id: str, phases: tuple[Phase, ...]) -> None:
        """Keeps the pipeline of the run, so that a change of the configuration
        does not change what run.phase points to."""

        self._connection.execute(
            "UPDATE automation_runs SET pipeline = ? WHERE id = ?",
            (json.dumps([_phase_to_json(phase) for phase in phases]), run_id),
        )

    def request_plan_changes(self, run_id: str, changes: str) -> None:
        """A new plan agent makes the changes. The supervisor consumes the
        request when it starts that agent."""

        self._command(run_id, "replan", changes)

    def consume(self, run_id: str, kind: str) -> None:
        """Marks the pending requests of this kind as done."""

        self._connection.execute(
            "UPDATE run_commands SET consumed_at = ? "
            "WHERE run_id = ? AND kind = ? AND consumed_at IS NULL",
            (_to_text(self._now()), run_id, kind),
        )

    def events(self, run_id: str) -> builtins.list[RunEvent]:
        """What happened to the run, oldest first."""

        return [
            RunEvent(datetime.fromisoformat(row["at"]), row["kind"], row["detail"])
            for row in self._connection.execute(
                "SELECT at, kind, detail FROM run_events WHERE run_id = ? ORDER BY id", (run_id,)
            )
        ]

    def _command(self, run_id: str, kind: str, payload: str | None = None) -> None:
        with transaction(self._connection):
            self._connection.execute(
                "INSERT INTO run_commands (run_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (run_id, kind, payload, _to_text(self._now())),
            )
            self._event(run_id, "request", f"{kind}: {payload}" if payload else kind)

    def _event(self, run_id: str, kind: str, detail: str | None = None) -> None:
        _record_event(self._connection, run_id, kind, detail, self._now())

    def set_alert_pending(self, run_id: str, pending: bool) -> None:
        self._connection.execute(
            "UPDATE automation_runs SET alert_pending = ? WHERE id = ?", (int(pending), run_id)
        )

    def count_restart(self, run_id: str) -> None:
        with transaction(self._connection):
            self._connection.execute(
                "UPDATE automation_runs SET restarts = restarts + 1 WHERE id = ?", (run_id,)
            )
            self._event(run_id, "restart", "the agent starts again")

    def mark_failed(self, run_id: str, error_message: str) -> None:
        self._update(
            run_id,
            RunStatus.FAILED,
            waiting_reason=None,
            error_message=error_message[:2000],
            finished_at=_to_text(self._now()),
        )

    def _update(self, run_id: str, status: RunStatus, **columns: str | None) -> None:
        with transaction(self._connection):
            run = self.get(run_id)
            reason = columns.get("waiting_reason")
            message = columns.get("error_message")

            # An event for a new status or a new waiting reason. A caller that does
            # not give a reason leaves it as it is, and a clear of the reason with
            # the same status is not an event.

            changed = (
                run is None
                or run.status is not status
                or (reason is not None and run.waiting_reason != reason)
            )

            columns["status_changed_at"] = _to_text(self._now())
            assignments = ", ".join(f"{column} = ?" for column in ["status", *columns])

            self._connection.execute(
                f"UPDATE automation_runs SET {assignments} WHERE id = ?",
                (status, *columns.values(), run_id),
            )

            if changed:
                self._event(
                    run_id,
                    "status",
                    f"{status}{f' {reason}' if reason else ''}{f': {message}' if message else ''}",
                )

    def _last_status(self, run_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT detail FROM run_events WHERE run_id = ? AND kind = 'status' "
            "ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()

        return row["detail"] if row else None
