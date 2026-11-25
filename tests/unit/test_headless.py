"""Headless runs: the implementation agent and the code review run as
`claude -p` turns in units, one work row for each turn."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from spyhop.application.work_runner import prompt_file
from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.models import AutomationRun, ChecksPhase, CodeReviewPhase, StepRun, Worktree
from tests.fakes import SPYHOP, Harness, git
from tests.unit.test_pipeline import answer, harness_with, running, started, written

REVIEW = {"code_review": "/code-review-loop --rounds 1"}


def headless(tmp_path: Path, *pipeline: Any) -> Harness:
    harness = harness_with(tmp_path, *pipeline)

    harness.config.agents.runner = "headless"

    return harness


def turns(harness: Harness) -> list[StepRun]:
    ids = harness.connection.execute(
        "SELECT id FROM step_runs WHERE kind = 'work' ORDER BY rowid"
    ).fetchall()

    return [row for (step_id,) in ids if (row := harness.checkpoint_runs.get(step_id))]


def unit_of(harness: Harness, row: StepRun) -> list[str]:
    [unit] = [unit for unit in harness.units.started if unit["unit"].endswith(row.id)]

    return [str(part) for part in unit["command"]]


def commit(worktree: Worktree, text: str = "class App { int fixed; }\n") -> None:
    (worktree.path / "App.java").write_text(text)
    git(worktree.path, "commit", "--quiet", "-am", "LPD-1 Fix")


def end_turn(row: StepRun, output: dict[str, Any] | None) -> None:
    """Writes the result file that `spyhop steps exec --kind work` would
    write."""

    row.output_file.parent.mkdir(parents=True, exist_ok=True)
    row.output_file.write_text(json.dumps({"exit_code": 0, "output": output}))


def done(row: StepRun, worktree: Worktree) -> None:
    written(worktree.path / "CLAUDE-RESULT.md", f"done in {row.turn}", ahead=1)
    end_turn(row, {"is_error": False, "structured_output": {"status": "done", "summary": "Done"}})


async def implementing(tmp_path: Path, *pipeline: Any) -> tuple[Harness, AutomationRun, Worktree]:
    harness = headless(tmp_path, *pipeline)
    run, worktree = await started(harness)

    git(worktree.path, "checkout", "--quiet", "-b", "LPD-1")

    return harness, run, worktree


async def test_a_headless_run_implements_reviews_and_checks_in_one_session(
    tmp_path: Path,
) -> None:
    harness, run, worktree = await implementing(
        tmp_path, "implement", REVIEW, {"checks": ["regressions"]}
    )
    [first] = turns(harness)
    command = unit_of(harness, first)

    assert harness.orca.launches == []
    assert harness.run(run.id).status is RunStatus.RUNNING
    assert harness.run(run.id).runner == "headless"
    assert command[:4] == [str(SPYHOP), "steps", "exec", "implement"]
    assert command[command.index("--session") + 1] == first.session_id
    assert "--kind" in command and "--resume" not in command
    assert prompt_file(first).read_text().startswith("/goal Lee CLAUDE-TASK.md")
    assert f"El worktree es {worktree.path}" in prompt_file(first).read_text()
    assert "## Review" not in (worktree.path / "CLAUDE-TASK.md").read_text()

    await harness.supervisor.supervise()

    assert len(turns(harness)) == 1  # the turn still runs

    commit(worktree)
    done(first, worktree)
    await harness.supervisor.supervise()

    [_, review] = turns(harness)

    assert harness.run(run.id).phase == 1
    assert (review.name, review.turn, review.session_id) == (
        "code_review",
        "code_review",
        first.session_id,
    )
    assert "--resume" in unit_of(harness, review)
    assert "Ejecuta `/code-review-loop --rounds 1`" in prompt_file(review).read_text()

    done(review, worktree)
    await harness.supervisor.supervise()

    assert harness.run(run.id).phase == 2
    assert [row.name for row in running(harness, run)] == ["regressions"]

    answer(harness, run, "regressions")
    await harness.supervisor.supervise()

    assert harness.run(run.id).status is RunStatus.COMPLETED


async def test_a_question_waits_for_a_person(tmp_path: Path) -> None:
    harness, run, _ = await implementing(tmp_path, "implement")
    [first] = turns(harness)

    end_turn(
        first,
        {
            "is_error": False,
            "structured_output": {
                "status": "question",
                "question": "Keep the deprecated method?",
                "summary": "Stopped before the API change",
            },
        },
    )
    await harness.supervisor.supervise()
    await harness.supervisor.supervise()

    waiting = harness.run(run.id)

    assert waiting.status is RunStatus.WAITING_FOR_HUMAN
    assert waiting.waiting_reason is WaitingReason.AGENT_QUESTION
    assert waiting.error_message == "Keep the deprecated method?"
    assert len(turns(harness)) == 1


async def test_a_lost_turn_continues_once_in_the_same_session(tmp_path: Path) -> None:
    harness, run, _ = await implementing(tmp_path, "implement")
    [first] = turns(harness)

    end_turn(first, None)
    await harness.supervisor.supervise()

    [_, again] = turns(harness)

    assert (again.turn, again.session_id) == ("continue", first.session_id)
    assert "--resume" in unit_of(harness, again)
    assert "claude exited with 0 without a result" in prompt_file(again).read_text()
    assert harness.run(run.id).restarts == 1

    end_turn(again, None)
    await harness.supervisor.supervise()

    assert len(turns(harness)) == 2
    assert harness.run(run.id).waiting_reason is WaitingReason.UNKNOWN_STATE


async def test_a_done_turn_with_uncommitted_changes_is_unfinished(tmp_path: Path) -> None:
    harness, _, worktree = await implementing(tmp_path, "implement")
    [first] = turns(harness)

    (worktree.path / "App.java").write_text("class App { int half; }\n")
    done(first, worktree)
    await harness.supervisor.supervise()

    [first, again] = turns(harness)

    assert first.outcome == "unfinished"
    assert again.turn == "continue"
    assert "has uncommitted changes" in prompt_file(again).read_text()


async def test_a_review_without_the_session_starts_a_new_one(tmp_path: Path) -> None:
    harness, _, worktree = await implementing(tmp_path, "implement", REVIEW)
    [first] = turns(harness)

    commit(worktree)
    done(first, worktree)
    await harness.supervisor.supervise()

    [_, review] = turns(harness)

    # `--resume` found no session: the turn did not start.

    end_turn(review, {"is_error": True, "subtype": "error_during_execution", "num_turns": 0})
    await harness.supervisor.supervise()

    [_, review, again] = turns(harness)

    assert review.outcome == "resume_failed"
    assert again.session_id != first.session_id
    assert "--resume" not in unit_of(harness, again)
    assert "su sesión ya no existe" in prompt_file(again).read_text()


async def test_a_turn_without_a_result_in_time_stops_and_continues(tmp_path: Path) -> None:
    harness, run, _ = await implementing(tmp_path, "implement")
    [first] = turns(harness)

    harness.clock.advance(timedelta(minutes=harness.config.agents.implement_timeout_minutes + 1))
    await harness.supervisor.supervise()

    [first, again] = turns(harness)

    assert first.outcome == "lost"
    assert first.detail is not None and first.detail.startswith("no result after 6:")
    assert harness.units.stopped == [first.unit]
    assert again.turn == "continue"


async def test_a_turn_whose_unit_did_not_start_starts_it_again(tmp_path: Path) -> None:
    harness, _, _ = await implementing(tmp_path, "implement")
    [first] = turns(harness)

    harness.connection.execute("UPDATE step_runs SET unit = NULL")
    await harness.supervisor.supervise()

    assert len(turns(harness)) == 1
    assert len([unit for unit in harness.units.started if unit["unit"].endswith(first.id)]) == 2


async def test_checks_before_the_implementation_start_the_first_turn(tmp_path: Path) -> None:
    harness = headless(tmp_path, {"checks": ["ticket-info"]}, "implement")
    run, _ = await started(harness)

    assert turns(harness) == []

    await harness.supervisor.supervise()
    answer(harness, run, "ticket-info")
    await harness.supervisor.supervise()

    [first] = turns(harness)

    assert (first.phase, first.turn) == (1, "launch")
    assert harness.orca.launches == []


async def test_a_failed_check_of_a_headless_run_waits_for_a_person(tmp_path: Path) -> None:
    harness, run, worktree = await implementing(
        tmp_path, "implement", {"checks": ["regressions"], "on_fail": "fix"}
    )
    [first] = turns(harness)

    commit(worktree)
    done(first, worktree)
    await harness.supervisor.supervise()
    answer(harness, run, "regressions", "FAIL")
    await harness.supervisor.supervise()

    assert harness.run(run.id).waiting_reason is WaitingReason.CHECKPOINTS_FAILED
    assert harness.orca.launches == []


async def test_the_review_command_becomes_a_code_review_phase(tmp_path: Path) -> None:
    harness = headless(tmp_path, "implement", {"checks": ["regressions"]})

    harness.config.agents.review_command = "/code-review-loop --rounds 2"

    run, worktree = await started(harness)

    assert harness.run(run.id).pipeline == (
        "implement",
        CodeReviewPhase("/code-review-loop --rounds 2"),
        ChecksPhase(("regressions",)),
    )
    assert "## Review" not in (worktree.path / "CLAUDE-TASK.md").read_text()
