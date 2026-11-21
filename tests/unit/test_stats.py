import dataclasses
import json
from datetime import timedelta
from pathlib import Path

from spyhop.application.stats import checkpoint_cost, run_stats
from spyhop.domain.enums import RunStatus, SessionStatus
from spyhop.domain.models import AgentSession, CheckpointRun, RunEvent
from tests.unit.test_supervisor import NOW, run_in


def session(role: str | None, minutes: int) -> AgentSession:
    return AgentSession(
        id=f"s-{role}-{minutes}",
        run_id="r1",
        provider="claude",
        model="opus",
        agent_handle="term_1",
        launch_operation_id="op",
        status=SessionStatus.FINISHED,
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=minutes),
        role=role,
    )


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


def test_stats_count_runs_agents_and_checkpoints() -> None:
    completed = dataclasses.replace(
        run_in(RunStatus.COMPLETED), finished_at=NOW + timedelta(hours=2), restarts=1
    )
    failed = dataclasses.replace(run_in(RunStatus.FAILED), id="r2")

    stats = run_stats(
        [completed, failed],
        [session("plan", 30), session("implement", 10), session("implement", 20), session(None, 5)],
        [
            checkpoint_run("security", "PASS", 2),
            checkpoint_run("security", "FAIL", 4),
            checkpoint_run("security", "PASS", 0, "kept from round 1"),
        ],
        {
            completed.id: [
                RunEvent(NOW + timedelta(minutes=5), "status", "RUNNING"),
                RunEvent(NOW + timedelta(minutes=40), "status", "WAITING_FOR_HUMAN plan_review"),
                RunEvent(NOW + timedelta(minutes=90), "status", "WAITING_FOR_HUMAN plan_review"),
            ]
        },
        lambda row: 0.5,
    )

    assert stats.runs_by_status == {"COMPLETED": 1, "FAILED": 1}
    assert stats.median_plan_seconds == 40 * 60
    assert stats.median_completed_seconds == 7200
    assert stats.restarts == 1
    assert [(agent.role, agent.count, agent.mean_seconds) for agent in stats.agents] == [
        ("implement", 2, 900),
        ("plan", 1, 1800),
        ("unknown", 1, 300),
    ]

    [security] = stats.checkpoints

    assert (security.count, security.passed, security.mean_seconds) == (2, 1, 180)
    assert security.cost_usd == 1.0


def test_the_cost_of_a_checkpoint_comes_from_its_result_file(tmp_path: Path) -> None:
    row = dataclasses.replace(
        checkpoint_run("security", "PASS", 1), output_file=tmp_path / "result.json"
    )

    assert checkpoint_cost(row) is None

    row.output_file.write_text(json.dumps({"exit_code": 0, "output": {"total_cost_usd": 0.25}}))

    assert checkpoint_cost(row) == 0.25
