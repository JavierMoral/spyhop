import builtins
import dataclasses
import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from spyhop.domain.enums import RunStatus, SessionStatus, WaitingReason, WorktreeStatus
from spyhop.domain.models import (
    AgentSession,
    AutomationRun,
    CheckpointRun,
    ChecksPhase,
    IssueEntry,
    JiraIssue,
    OrcaWorktree,
    Phase,
    RunEvent,
    Worktree,
    validate_pipeline,
)

_log = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _to_text(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _to_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class RunRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create_discovered(self, entry: IssueEntry) -> AutomationRun | None:
        """Returns None when the transition is already recorded or the issue
        already has an active run."""

        now = utc_now()
        run_id = str(uuid.uuid4())

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

        run = self.get(run_id)

        if run is not None and run.waiting_reason is not reason:
            self._event(run_id, "status", f"{run.status} {reason}: {message}")

        self._connection.execute(
            "UPDATE automation_runs SET waiting_reason = ?, error_message = ? WHERE id = ?",
            (reason, message, run_id),
        )

    def mark_ready(self, run_id: str, worktree_id: str) -> None:
        self._update(run_id, RunStatus.READY, worktree_id=worktree_id)

    def mark_running(self, run_id: str) -> None:
        self._update(run_id, RunStatus.RUNNING, started_at=_to_text(utc_now()))

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
            finished_at=_to_text(utc_now()),
        )

    def mark_cancelled(self, run_id: str, reason: str) -> None:
        self._update(
            run_id,
            RunStatus.CANCELLED,
            waiting_reason=None,
            error_message=reason,
            finished_at=_to_text(utc_now()),
        )

    def create_retry(self, original: AutomationRun) -> AutomationRun | None:
        """A new run of the same ticket and transition, pending launch. None
        when the ticket already has an active run."""

        now = utc_now()
        run_id = str(uuid.uuid4())

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
        self._connection.execute(
            "UPDATE automation_runs SET phase = ?, fix_requested_at = NULL WHERE id = ?",
            (phase, run_id),
        )

        run = self.get(run_id)
        phases = run.pipeline if run and run.pipeline else ()

        self._event(run_id, "phase", _phase_name(phases[phase]) if phase < len(phases) else "end")

    def mark_fix_requested(self, run_id: str) -> None:
        """Back to RUNNING while the agent fixes the findings."""

        self._connection.execute(
            "UPDATE automation_runs SET fix_requested_at = ? WHERE id = ?",
            (_to_text(utc_now()), run_id),
        )
        self._event(run_id, "fix", "the agent fixes the findings of the checkpoints")
        self.mark_resumed(run_id)

    def clear_fix_request(self, run_id: str) -> None:
        self._connection.execute(
            "UPDATE automation_runs SET fix_requested_at = NULL WHERE id = ?", (run_id,)
        )

    def request_checks(self, run_id: str, failed_only: bool = False) -> None:
        """Asks for a new round of the checks phase. The next round of the run
        consumes the request."""

        self._command(run_id, "checks", "failed" if failed_only else "all")

    def add_notice(self, run_id: str, text: str) -> None:
        self._command(run_id, "notice", text)

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
            (_to_text(utc_now()), run_id, row["id"]),
        )

        return str(row["payload"])

    def consume_command(self, command_id: int) -> None:
        self._connection.execute(
            "UPDATE run_commands SET consumed_at = ? WHERE id = ?",
            (_to_text(utc_now()), command_id),
        )

    def declare(self, run_id: str, what: str) -> None:
        """The agent declares that the plan is ready (``plan``) or that the
        work is done (``done``)."""

        self._connection.execute(
            "UPDATE automation_runs SET declared = ?, declared_at = ? WHERE id = ?",
            (what, _to_text(utc_now()), run_id),
        )
        self._event(run_id, "declared", what)

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
            (_to_text(utc_now()), run_id, kind),
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
        self._connection.execute(
            "INSERT INTO run_commands (run_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (run_id, kind, payload, _to_text(utc_now())),
        )
        self._event(run_id, "request", f"{kind}: {payload}" if payload else kind)

    def _event(self, run_id: str, kind: str, detail: str | None = None) -> None:
        _record_event(self._connection, run_id, kind, detail)

    def note_missing(self, run_id: str) -> AutomationRun:
        """Counts one more cycle without the agent of the run."""

        self._connection.execute(
            """
            UPDATE automation_runs
            SET missing_cycles = missing_cycles + 1,
                missing_since = COALESCE(missing_since, ?)
            WHERE id = ?
            """,
            (_to_text(utc_now()), run_id),
        )

        run = self.get(run_id)

        assert run is not None

        return run

    def clear_missing(self, run_id: str) -> None:
        self._connection.execute(
            "UPDATE automation_runs SET missing_cycles = 0, missing_since = NULL WHERE id = ?",
            (run_id,),
        )

    def set_plan_agent_closed_told(self, run_id: str, told: bool) -> None:
        self._connection.execute(
            "UPDATE automation_runs SET plan_agent_closed_told = ? WHERE id = ?",
            (int(told), run_id),
        )

    def set_alert_pending(self, run_id: str, pending: bool) -> None:
        self._connection.execute(
            "UPDATE automation_runs SET alert_pending = ? WHERE id = ?", (int(pending), run_id)
        )

    def count_restart(self, run_id: str) -> None:
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
            finished_at=_to_text(utc_now()),
        )

    def _update(self, run_id: str, status: RunStatus, **columns: str | None) -> None:
        run = self.get(run_id)
        reason = columns.get("waiting_reason")

        # A caller that does not give a waiting reason leaves it as it is.

        if (
            run is None
            or run.status is not status
            or ("waiting_reason" in columns and run.waiting_reason != reason)
        ):
            message = columns.get("error_message")

            self._event(
                run_id,
                "status",
                f"{status}{f' {reason}' if reason else ''}{f': {message}' if message else ''}",
            )

        columns["status_changed_at"] = _to_text(utc_now())
        assignments = ", ".join(f"{column} = ?" for column in ["status", *columns])

        self._connection.execute(
            f"UPDATE automation_runs SET {assignments} WHERE id = ?",
            (status, *columns.values(), run_id),
        )


class WorktreeRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, worktree_id: str) -> Worktree | None:
        row = self._connection.execute(
            "SELECT * FROM worktrees WHERE id = ?", (worktree_id,)
        ).fetchone()

        return _to_worktree(row) if row else None

    def find_current(self, issue_key: str) -> Worktree | None:
        """The ticket's worktree unless it was removed."""

        row = self._connection.execute(
            "SELECT * FROM worktrees WHERE issue_key = ? AND status != ?",
            (issue_key, WorktreeStatus.REMOVED),
        ).fetchone()

        return _to_worktree(row) if row else None

    def create(
        self,
        issue_key: str,
        orca_worktree: OrcaWorktree,
        repository_path: Path,
        base_branch: str,
    ) -> Worktree:
        worktree_id = str(uuid.uuid4())

        self._connection.execute(
            """
            INSERT INTO worktrees (
                id, issue_key, orca_worktree_id, repository_path, path, branch_name,
                base_branch, created_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                worktree_id,
                issue_key,
                orca_worktree.id,
                str(repository_path),
                str(orca_worktree.path),
                orca_worktree.branch,
                base_branch,
                _to_text(utc_now()),
                WorktreeStatus.ACTIVE,
            ),
        )

        worktree = self.get(worktree_id)

        assert worktree is not None

        return worktree

    def find_last_removed(self, issue_key: str) -> Worktree | None:
        row = self._connection.execute(
            "SELECT * FROM worktrees WHERE issue_key = ? AND status = ? "
            "ORDER BY removed_at DESC, rowid DESC LIMIT 1",
            (issue_key, WorktreeStatus.REMOVED),
        ).fetchone()

        return _to_worktree(row) if row else None

    def list_by_status(self, *statuses: WorktreeStatus) -> list[Worktree]:
        placeholders = ", ".join("?" for _ in statuses)
        rows = self._connection.execute(
            f"SELECT * FROM worktrees WHERE status IN ({placeholders}) ORDER BY created_at",
            statuses,
        )

        return [_to_worktree(row) for row in rows]

    def mark_removing(self, worktree_id: str) -> None:
        self._set_status(worktree_id, WorktreeStatus.REMOVING, None)

    def mark_cleanup_blocked(self, worktree_id: str, reason: str) -> None:
        self._set_status(worktree_id, WorktreeStatus.CLEANUP_BLOCKED, reason[:2000])

    def mark_active(self, worktree_id: str) -> None:
        self._set_status(worktree_id, WorktreeStatus.ACTIVE, None)

    def mark_removed(self, worktree_id: str, reason: str | None = None) -> None:
        self._connection.execute(
            "UPDATE worktrees SET status = ?, cleanup_error = ?, removed_at = ? WHERE id = ?",
            (WorktreeStatus.REMOVED, reason, _to_text(utc_now()), worktree_id),
        )

    def set_supervisor(self, worktree_id: str, terminal: str | None, pane_key: str | None) -> None:
        self._connection.execute(
            "UPDATE worktrees SET supervisor_terminal = ?, supervisor_pane_key = ? WHERE id = ?",
            (terminal, pane_key, worktree_id),
        )

    def _set_status(self, worktree_id: str, status: WorktreeStatus, error: str | None) -> None:
        self._connection.execute(
            "UPDATE worktrees SET status = ?, cleanup_error = ? WHERE id = ?",
            (status, error, worktree_id),
        )


class CheckpointRunRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

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
                _to_text(utc_now()),
            ),
        )

        # A new round consumes the requests for a round of the run. A later
        # stage of the same round does not.

        if new_round:
            self._connection.execute(
                "UPDATE run_commands SET consumed_at = ? "
                "WHERE run_id = ? AND kind = 'checks' AND consumed_at IS NULL",
                (_to_text(utc_now()), run_id),
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
        self._connection.execute(
            "UPDATE checkpoint_runs SET status = 'DONE', verdict = ?, detail = ?, finished_at = ? "
            "WHERE id = ?",
            (verdict, detail, _to_text(utc_now()), checkpoint_run_id),
        )

        row = self.get(checkpoint_run_id)

        if row is not None:
            _record_event(
                self._connection,
                row.run_id,
                "checkpoint",
                f"{row.name} round {row.round}: {verdict}",
            )


class SessionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def find_launching(self, run_id: str) -> AgentSession | None:
        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE run_id = ? AND status = ?",
            (run_id, SessionStatus.LAUNCHING),
        ).fetchone()

        return _to_session(row) if row else None

    def create_launching(
        self, run_id: str, provider: str, model: str | None, operation_id: str, role: str
    ) -> AgentSession:
        """Stored before calling Orca, so a launch interrupted at any point is
        retried with the same operation id."""

        session_id = str(uuid.uuid4())

        self._connection.execute(
            """
            INSERT INTO agent_sessions (
                id, run_id, provider, model, launch_operation_id, status, role
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, run_id, provider, model, operation_id, SessionStatus.LAUNCHING, role),
        )

        session = self.get(session_id)

        assert session is not None

        return session

    def mark_launched(self, session_id: str, agent_handle: str, pane_key: str | None) -> None:
        self._connection.execute(
            """
            UPDATE agent_sessions SET status = ?, agent_handle = ?, pane_key = ?, started_at = ?
            WHERE id = ?
            """,
            (SessionStatus.LAUNCHED, agent_handle, pane_key, _to_text(utc_now()), session_id),
        )

    def mark_finished(self, session_id: str) -> None:
        self._connection.execute(
            "UPDATE agent_sessions SET status = ?, finished_at = ? WHERE id = ?",
            (SessionStatus.FINISHED, _to_text(utc_now()), session_id),
        )

    def find_launched(self, run_id: str) -> AgentSession | None:
        """The run's agent: its latest launched session."""

        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE run_id = ? AND status = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (run_id, SessionStatus.LAUNCHED),
        ).fetchone()

        return _to_session(row) if row else None

    def mark_failed(self, session_id: str) -> None:
        self._connection.execute(
            "UPDATE agent_sessions SET status = ?, finished_at = ? WHERE id = ?",
            (SessionStatus.FAILED, _to_text(utc_now()), session_id),
        )

    def get(self, session_id: str) -> AgentSession | None:
        row = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
        ).fetchone()

        return _to_session(row) if row else None

    def list_for_run(self, run_id: str) -> list[AgentSession]:
        rows = self._connection.execute(
            "SELECT * FROM agent_sessions WHERE run_id = ? ORDER BY rowid", (run_id,)
        )

        return [_to_session(row) for row in rows]


class PollerStateRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def watermark(self) -> datetime | None:
        row = self._connection.execute("SELECT watermark FROM poller_state WHERE id = 1").fetchone()

        return _to_datetime(row["watermark"]) if row else None

    def last_success_at(self) -> datetime | None:
        row = self._connection.execute(
            "SELECT last_success_at FROM poller_state WHERE id = 1"
        ).fetchone()

        return _to_datetime(row["last_success_at"]) if row else None

    def last_error(self) -> str | None:
        row = self._connection.execute(
            "SELECT last_error FROM poller_state WHERE id = 1"
        ).fetchone()

        return row["last_error"] if row else None

    def record_success(self, watermark: datetime) -> None:
        self._connection.execute(
            """
            INSERT INTO poller_state (id, watermark, last_success_at, last_error)
            VALUES (1, ?, ?, NULL)
            ON CONFLICT (id) DO UPDATE SET
                watermark = excluded.watermark,
                last_success_at = excluded.last_success_at,
                last_error = NULL
            """,
            (_to_text(watermark), _to_text(utc_now())),
        )

    def record_error(self, message: str) -> None:
        self._connection.execute(
            "UPDATE poller_state SET last_error = ? WHERE id = 1", (message[:1000],)
        )


class ObservationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def record(self, issue: JiraIssue) -> None:
        self._connection.execute(
            """
            INSERT INTO issue_observations (
                issue_key, last_status, last_assignee, last_updated_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (issue_key) DO UPDATE SET
                last_status = excluded.last_status,
                last_assignee = excluded.last_assignee,
                last_updated_at = excluded.last_updated_at,
                last_seen_at = excluded.last_seen_at
            """,
            (
                issue.key,
                issue.status,
                issue.assignee,
                _to_text(issue.updated_at),
                _to_text(utc_now()),
            ),
        )


def _to_run(row: sqlite3.Row) -> AutomationRun:
    return AutomationRun(
        id=row["id"],
        issue_key=row["issue_key"],
        worktree_id=row["worktree_id"],
        workflow_name=row["workflow_name"],
        trigger_status=row["trigger_status"],
        trigger_history_id=row["trigger_history_id"],
        triggered_at=datetime.fromisoformat(row["triggered_at"]),
        status=RunStatus(row["status"]),
        waiting_reason=WaitingReason(row["waiting_reason"]) if row["waiting_reason"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=_to_datetime(row["started_at"]),
        finished_at=_to_datetime(row["finished_at"]),
        error_message=row["error_message"],
        retry_of=row["retry_of"],
        status_changed_at=_to_datetime(row["status_changed_at"]),
        phase=row["phase"],
        fix_requested_at=_to_datetime(row["fix_requested_at"]),
        checks_requested_at=_to_datetime(row["checks_requested_at"]),
        checks_failed_only=bool(row["checks_failed_only"]),
        restarts=row["restarts"],
        plan_changes=row["plan_changes"],
        signals=row["signals"],
        declared=row["declared"],
        declared_at=_to_datetime(row["declared_at"]),
        approved_at=_to_datetime(row["approved_at"]),
        pipeline=_pipeline_from_json(row["pipeline"]),
        missing_cycles=row["missing_cycles"],
        missing_since=_to_datetime(row["missing_since"]),
        plan_agent_closed_told=bool(row["plan_agent_closed_told"]),
        alert_pending=bool(row["alert_pending"]),
    )


def _record_event(
    connection: sqlite3.Connection, run_id: str, kind: str, detail: str | None
) -> None:
    connection.execute(
        "INSERT INTO run_events (run_id, at, kind, detail) VALUES (?, ?, ?, ?)",
        (run_id, _to_text(utc_now()), kind, detail),
    )


def _phase_name(phase: Phase) -> str:
    return phase if isinstance(phase, str) else f"checks ({', '.join(phase.checkpoints)})"


def _phase_to_json(phase: Phase) -> object:
    return phase if isinstance(phase, str) else dataclasses.asdict(phase)


def _pipeline_from_json(text: str | None) -> tuple[Phase, ...] | None:
    """None when the run has no pipeline, or when the stored one is not a
    valid pipeline: the run then uses the pipeline of the configuration."""

    if not text:
        return None

    try:
        phases = tuple(
            phase
            if isinstance(phase, str)
            else ChecksPhase(**{**phase, "checkpoints": tuple(phase["checkpoints"])})
            for phase in json.loads(text)
        )

        for phase in phases:
            if isinstance(phase, ChecksPhase) and (
                phase.on_fail not in ("human", "fix")
                or not all(isinstance(name, str) for name in phase.checkpoints)
            ):
                raise ValueError(f"not a checks phase: {phase}")

        validate_pipeline(phases)
    except (ValueError, TypeError, KeyError) as error:
        _log.warning("Stored pipeline ignored, the configuration applies: %s", error)

        return None

    return phases


def _to_checkpoint_run(row: sqlite3.Row) -> CheckpointRun:
    return CheckpointRun(
        id=row["id"],
        run_id=row["run_id"],
        phase=row["phase"],
        round=row["round"],
        name=row["name"],
        inputs=row["inputs"],
        base=row["base"],
        head=row["head"],
        output_file=Path(row["output_file"]),
        unit=row["unit"],
        status=row["status"],
        verdict=row["verdict"],
        detail=row["detail"],
        started_at=datetime.fromisoformat(row["started_at"]),
        finished_at=_to_datetime(row["finished_at"]),
    )


def _to_worktree(row: sqlite3.Row) -> Worktree:
    return Worktree(
        id=row["id"],
        issue_key=row["issue_key"],
        orca_worktree_id=row["orca_worktree_id"],
        repository_path=Path(row["repository_path"]),
        path=Path(row["path"]),
        branch_name=row["branch_name"],
        base_branch=row["base_branch"],
        created_at=datetime.fromisoformat(row["created_at"]),
        status=WorktreeStatus(row["status"]),
        cleanup_error=row["cleanup_error"],
        removed_at=_to_datetime(row["removed_at"]),
        supervisor_terminal=row["supervisor_terminal"],
        supervisor_pane_key=row["supervisor_pane_key"],
    )


def _to_session(row: sqlite3.Row) -> AgentSession:
    return AgentSession(
        id=row["id"],
        run_id=row["run_id"],
        provider=row["provider"],
        model=row["model"],
        agent_handle=row["agent_handle"],
        launch_operation_id=row["launch_operation_id"],
        status=SessionStatus(row["status"]),
        started_at=_to_datetime(row["started_at"]),
        finished_at=_to_datetime(row["finished_at"]),
        pane_key=row["pane_key"],
        role=row["role"],
    )
