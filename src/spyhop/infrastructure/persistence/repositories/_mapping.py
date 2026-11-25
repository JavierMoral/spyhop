"""The row mappers of the repositories and the JSON of a pipeline."""

import dataclasses
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from spyhop.domain.enums import RunStatus, SessionStatus, WaitingReason, WorktreeStatus
from spyhop.domain.models import (
    AgentSession,
    AutomationRun,
    ChecksPhase,
    Phase,
    StepRun,
    Worktree,
    validate_pipeline,
)

_log = logging.getLogger(__name__)


def _to_text(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _to_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


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
        runner=row["runner"],
    )


def _record_event(
    connection: sqlite3.Connection, run_id: str, kind: str, detail: str | None, at: datetime
) -> None:
    connection.execute(
        "INSERT INTO run_events (run_id, at, kind, detail) VALUES (?, ?, ?, ?)",
        (run_id, _to_text(at), kind, detail),
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


def _to_step_run(row: sqlite3.Row) -> StepRun:
    return StepRun(
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
        kind=row["kind"],
        session_id=row["session_id"],
        turn=row["turn"],
        outcome=row["outcome"],
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
