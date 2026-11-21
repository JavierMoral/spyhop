"""`spyhop stats`: what the runs of a period took, from the database and the
result files of the checkpoints."""

import statistics
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from spyhop.application.checkpoint_reports import read_output
from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.models import AgentSession, AutomationRun, CheckpointRun, RunEvent

KEPT_PREFIX = "kept from round"


@dataclass(frozen=True)
class AgentStats:
    role: str
    count: int
    mean_seconds: float | None


@dataclass(frozen=True)
class CheckpointStats:
    name: str
    count: int
    passed: int
    mean_seconds: float | None
    cost_usd: float


@dataclass(frozen=True)
class Stats:
    runs_by_status: dict[str, int]
    # From the start of the run to the first wait for the review of its plan.
    median_plan_seconds: float | None
    median_completed_seconds: float | None
    restarts: int
    agents: list[AgentStats]
    checkpoints: list[CheckpointStats]


def checkpoint_cost(row: CheckpointRun) -> float | None:
    """The cost that `claude -p` gave for the checkpoint, when its result
    file is still there."""

    payload = read_output(row.output_file) if row.output_file.is_file() else None
    output = payload.get("output") if payload else None
    cost = output.get("total_cost_usd") if isinstance(output, dict) else None

    return float(cost) if isinstance(cost, int | float) else None


def run_stats(
    runs: Sequence[AutomationRun],
    sessions: Sequence[AgentSession],
    checkpoint_runs: Sequence[CheckpointRun],
    events: Mapping[str, Sequence[RunEvent]] | None = None,
    cost: Callable[[CheckpointRun], float | None] = checkpoint_cost,
) -> Stats:
    """Counts each checkpoint that ran, and not the PASS verdicts that a
    rerun with --failed kept."""

    completed = [
        (run.finished_at - run.created_at).total_seconds()
        for run in runs
        if run.status is RunStatus.COMPLETED and run.finished_at is not None
    ]
    plan_review = f"{RunStatus.WAITING_FOR_HUMAN} {WaitingReason.PLAN_REVIEW}"
    planned = [
        next(
            (
                (event.at - run.created_at).total_seconds()
                for event in (events or {}).get(run.id, [])
                if event.kind == "status" and (event.detail or "").startswith(plan_review)
            ),
            None,
        )
        for run in runs
    ]
    roles: dict[str, list[AgentSession]] = {}

    for session in sessions:
        roles.setdefault(session.role or "unknown", []).append(session)

    names: dict[str, list[CheckpointRun]] = {}

    for row in checkpoint_runs:
        if row.status == "DONE" and not (row.detail or "").startswith(KEPT_PREFIX):
            names.setdefault(row.name, []).append(row)

    return Stats(
        runs_by_status=dict(Counter(str(run.status) for run in runs).most_common()),
        median_plan_seconds=_median(planned),
        median_completed_seconds=statistics.median(completed) if completed else None,
        restarts=sum(run.restarts for run in runs),
        agents=[
            AgentStats(
                role,
                len(group),
                _mean([_seconds(session.started_at, session.finished_at) for session in group]),
            )
            for role, group in sorted(roles.items())
        ],
        checkpoints=[
            CheckpointStats(
                name,
                len(rows),
                sum(row.verdict == "PASS" for row in rows),
                _mean([_seconds(row.started_at, row.finished_at) for row in rows]),
                sum(cost(row) or 0 for row in rows),
            )
            for name, rows in sorted(names.items())
        ],
    )


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    return (end - start).total_seconds() if start and end else None


def _median(values: Sequence[float | None]) -> float | None:
    known = [value for value in values if value is not None]

    return statistics.median(known) if known else None


def _mean(values: Sequence[float | None]) -> float | None:
    known = [value for value in values if value is not None]

    return statistics.fmean(known) if known else None
