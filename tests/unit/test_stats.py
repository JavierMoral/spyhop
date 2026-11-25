import dataclasses
from datetime import timedelta
from pathlib import Path

from spyhop.application.stats import run_stats
from spyhop.domain.enums import RunStatus
from spyhop.domain.models import CheckpointRun, RunEvent
from tests.unit.test_supervisor import NOW, run_in


def checkpoint_run(
    name: str, verdict: str, minutes: int, detail: str | None = None
) -> CheckpointRun:
    return CheckpointRun(
        id=f"c-{name}-{minutes}",
        run_id="r1",
        phase=0,
        round=1,
        name=name,
        inputs="head=abc",
        base="base",
        head="abc",
        output_file=Path("/nowhere.json"),
        unit=None,
        status="DONE",
        verdict=verdict,  # type: ignore[arg-type]
        detail=detail,
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=minutes),
    )


def test_stats_count_the_runs_and_their_times() -> None:
    completed = dataclasses.replace(
        run_in(RunStatus.COMPLETED), finished_at=NOW + timedelta(hours=2), restarts=1
    )
    failed = dataclasses.replace(run_in(RunStatus.FAILED), id="r2")

    stats = run_stats(
        [completed, failed],
        {
            completed.id: [
                RunEvent(NOW + timedelta(minutes=5), "status", "RUNNING"),
                RunEvent(NOW + timedelta(minutes=40), "status", "WAITING_FOR_HUMAN plan_review"),
                RunEvent(NOW + timedelta(minutes=90), "status", "WAITING_FOR_HUMAN plan_review"),
            ]
        },
    )

    assert stats.runs_by_status == {"COMPLETED": 1, "FAILED": 1}
    assert stats.median_plan_seconds == 40 * 60
    assert stats.median_completed_seconds == 7200
    assert stats.restarts == 1
