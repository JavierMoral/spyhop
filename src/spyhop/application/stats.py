"""`spyhop stats`: what the runs of a period took, from the database. The
executions of the agents are in the history of executions."""

import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.models import AutomationRun, RunEvent


@dataclass(frozen=True)
class Stats:
    runs_by_status: dict[str, int]
    # From the start of the run to the first wait for the review of its plan.
    median_plan_seconds: float | None
    median_completed_seconds: float | None
    restarts: int


def run_stats(
    runs: Sequence[AutomationRun], events: Mapping[str, Sequence[RunEvent]] | None = None
) -> Stats:
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

    return Stats(
        runs_by_status=dict(Counter(str(run.status) for run in runs).most_common()),
        median_plan_seconds=_median(planned),
        median_completed_seconds=statistics.median(completed) if completed else None,
        restarts=sum(run.restarts for run in runs),
    )


def _median(values: Sequence[float | None]) -> float | None:
    known = [value for value in values if value is not None]

    return statistics.median(known) if known else None
