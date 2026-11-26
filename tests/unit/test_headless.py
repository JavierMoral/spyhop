"""Headless runs: the implementation agent and the code review run as
`claude -p` turns in units, one work row for each turn."""

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

from spyhop.application.run_commands import RunCommands
from spyhop.application.work_runner import prompt_file
from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.models import AutomationRun, ChecksPhase, CodeReviewPhase, StepRun, Worktree
from tests.fakes import PLAN_APPROVED, PLAN_REVIEW, SPYHOP, Harness, git
from tests.unit.test_pipeline import answer, harness_with, running, started, written

REVIEW = {"code_review": "/code-review-loop --rounds 1"}


def headless(tmp_path: Path, *pipeline: Any) -> Harness:
    return harness_with(tmp_path, *pipeline)


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

    assert harness.run(run.id).status is RunStatus.RUNNING
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


async def test_a_failed_check_gets_a_fix_turn_and_a_new_round(tmp_path: Path) -> None:
    harness, run, worktree = await implementing(
        tmp_path, "implement", {"checks": ["regressions"], "on_fail": "fix"}
    )
    [first] = turns(harness)

    commit(worktree)
    done(first, worktree)
    await harness.supervisor.supervise()
    answer(harness, run, "regressions", "FAIL")
    await harness.supervisor.supervise()

    [_, fix] = turns(harness)

    assert (fix.phase, fix.turn, fix.session_id) == (1, "fix", first.session_id)
    assert "--resume" in unit_of(harness, fix)
    assert "han fallado: regressions" in prompt_file(fix).read_text()
    assert "declare" not in prompt_file(fix).read_text()

    # No new round while the fix runs, also after a commit of the fix.

    commit(worktree, "class App { int fixed = 1; }\n")
    await harness.supervisor.supervise()

    assert running(harness, run) == []

    done(fix, worktree)
    await harness.supervisor.supervise()

    assert harness.checkpoint_runs.get(fix.id).outcome == "done"  # type: ignore[union-attr]
    assert [(row.name, row.round) for row in running(harness, run)] == [("regressions", 2)]

    answer(harness, run, "regressions")
    await harness.supervisor.supervise()

    assert harness.run(run.id).status is RunStatus.COMPLETED


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


def tell(harness: Harness, text: str) -> str:
    return (
        RunCommands(
            harness.runs, harness.worktrees, lambda: harness.orca, PLAN_REVIEW, PLAN_APPROVED
        )
        .message("LPD-1", text)
        .message
    )


async def test_the_answer_to_a_question_resumes_the_session(tmp_path: Path) -> None:
    harness, run, _ = await implementing(tmp_path, "implement")
    [first] = turns(harness)

    end_turn(
        first,
        {
            "is_error": False,
            "structured_output": {"status": "question", "question": "Keep it?", "summary": "-"},
        },
    )
    await harness.supervisor.supervise()

    assert tell(harness, "Keep it") == "LPD-1: the agent gets the message on the next cycle."

    await harness.supervisor.supervise()

    [_, answer_turn] = turns(harness)

    assert (answer_turn.turn, answer_turn.session_id) == ("answer", first.session_id)
    assert "responde a tu pregunta: Keep it" in prompt_file(answer_turn).read_text()
    assert harness.run(run.id).status is RunStatus.RUNNING
    assert harness.runs.pending_messages(run.id) == []


async def test_a_message_waits_for_the_end_of_the_turn_and_goes_first(tmp_path: Path) -> None:
    harness, run, worktree = await implementing(tmp_path, "implement", REVIEW)
    [first] = turns(harness)

    assert "when the turn that runs ends" in tell(harness, "Use the new API")

    await harness.supervisor.supervise()

    assert len(turns(harness)) == 1

    commit(worktree)
    done(first, worktree)
    await harness.supervisor.supervise()

    [_, message] = turns(harness)

    assert (message.phase, message.turn, message.session_id) == (0, "message", first.session_id)
    assert "La persona te dice: Use the new API" in prompt_file(message).read_text()

    done(message, worktree)
    await harness.supervisor.supervise()

    assert turns(harness)[-1].name == "code_review"


async def test_a_message_to_a_run_that_waits_after_its_checks_starts_a_turn(
    tmp_path: Path,
) -> None:
    harness, run, worktree = await implementing(tmp_path, "implement", {"checks": ["regressions"]})
    [first] = turns(harness)

    commit(worktree)
    done(first, worktree)
    await harness.supervisor.supervise()
    answer(harness, run, "regressions", "FAIL")
    await harness.supervisor.supervise()

    assert harness.run(run.id).waiting_reason is WaitingReason.CHECKPOINTS_FAILED

    tell(harness, "The finding is right, fix it")
    await harness.supervisor.supervise()

    [_, message] = turns(harness)

    assert (message.phase, message.turn) == (1, "message")

    commit(worktree, "class App { int fixed = 2; }\n")
    done(message, worktree)
    await harness.supervisor.supervise()

    assert [(row.name, row.round) for row in running(harness, run)] == [("regressions", 2)]


async def test_a_change_after_a_completed_run_resumes_its_session(tmp_path: Path) -> None:
    harness, run, worktree = await implementing(tmp_path, "implement")
    [first] = turns(harness)

    commit(worktree)
    done(first, worktree)
    await harness.supervisor.supervise()

    assert harness.run(run.id).status is RunStatus.COMPLETED

    RunCommands(
        harness.runs, harness.worktrees, lambda: harness.orca, PLAN_REVIEW, PLAN_APPROVED
    ).change("LPD-1", "Keep the acting user")
    await harness.launcher.launch_pending()

    [_, change] = turns(harness)

    assert change.run_id != run.id
    assert (change.turn, change.session_id) == ("change", first.session_id)
    assert "--resume" in unit_of(harness, change)
    assert "Haz este cambio que pide la persona en LPD-1: Keep the acting user" in (
        prompt_file(change).read_text()
    )
    assert "su sesión ya no existe" not in prompt_file(change).read_text()


def commands(harness: Harness) -> RunCommands:
    return RunCommands(
        harness.runs, harness.worktrees, lambda: harness.orca, PLAN_REVIEW, PLAN_APPROVED
    )


def planned(row: StepRun, worktree: Worktree) -> None:
    written(worktree.path / "CLAUDE-PLAN.md", f"plan of {row.turn}", ahead=1)
    end_turn(row, {"is_error": False, "structured_output": {"status": "done", "summary": "Plan"}})


async def test_a_headless_plan_waits_for_its_review_and_a_new_session_implements_it(
    tmp_path: Path,
) -> None:
    harness, run, worktree = await implementing(tmp_path, "plan", "plan_review", "implement")
    [plan] = turns(harness)
    command = unit_of(harness, plan)

    assert (plan.name, plan.turn) == ("plan", "launch")
    assert command[command.index("--model") + 1] == "opus"
    assert "escribe CLAUDE-PLAN.md" in prompt_file(plan).read_text()
    assert "No modifiques código" in prompt_file(plan).read_text()
    assert "terminar el turno" in (worktree.path / "CLAUDE-TASK.md").read_text()

    planned(plan, worktree)
    await harness.supervisor.supervise()
    await harness.supervisor.supervise()

    assert harness.run(run.id).waiting_reason is WaitingReason.PLAN_REVIEW
    assert len(turns(harness)) == 1
    assert harness.orca.worktrees[worktree.orca_worktree_id].workspace_status == PLAN_REVIEW

    # The changes of the person go to the plan session.

    commands(harness).replan("LPD-1", "Add a test for the tablet viewport")
    await harness.supervisor.supervise()

    [_, replan] = turns(harness)

    assert (replan.turn, replan.session_id) == ("replan", plan.session_id)
    assert "Add a test for the tablet viewport" in prompt_file(replan).read_text()

    planned(replan, worktree)
    await harness.supervisor.supervise()

    # A question about the plan changes nothing, and the plan waits again.

    os.utime(worktree.path / "CLAUDE-PLAN.md", (1, 1))
    tell(harness, "Why a new class?")
    await harness.supervisor.supervise()

    [*_, question] = turns(harness)

    assert (question.name, question.turn, question.session_id) == (
        "plan",
        "message",
        plan.session_id,
    )
    assert "No modifiques código" in prompt_file(question).read_text()

    end_turn(
        question,
        {"is_error": False, "structured_output": {"status": "done", "summary": "Because"}},
    )
    await harness.supervisor.supervise()

    assert harness.run(run.id).waiting_reason is WaitingReason.PLAN_REVIEW
    assert len(turns(harness)) == 3

    await commands(harness).approve("LPD-1")
    await harness.supervisor.supervise()

    [*_, implement] = turns(harness)
    command = unit_of(harness, implement)

    assert harness.run(run.id).phase == 2
    assert (implement.name, implement.turn) == ("implement", "launch")
    assert implement.session_id != plan.session_id
    assert "--resume" not in command
    assert command[command.index("--model") + 1] == "sonnet"
    assert "implementa CLAUDE-PLAN.md" in prompt_file(implement).read_text()


async def test_the_session_gets_the_progress_of_a_long_turn(tmp_path: Path) -> None:
    harness = harness_with(tmp_path, "implement", with_supervisor_session=True)
    run, _ = await started(harness)
    [first] = turns(harness)
    log = first.output_file.with_suffix(".log")

    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('> Bash: {"command": "./gradlew test"}\nTest 1 en verde.\n')
    await harness.supervisor.supervise()

    def progress() -> list[str]:
        return [text for _, text in harness.orca.prompts if "en marcha desde hace" in text]

    assert progress() == []

    harness.clock.advance(timedelta(minutes=16))
    await harness.supervisor.supervise()
    harness.clock.advance(timedelta(minutes=5))
    await harness.supervisor.supervise()
    harness.clock.advance(timedelta(minutes=11))
    await harness.supervisor.supervise()

    [one, two] = progress()

    assert "turno launch de implement en marcha desde hace 16 min: 1 llamadas" in one
    assert 'la última: Bash: {"command": "./gradlew test"}' in one
    assert "el agente dice: «Test 1 en verde.»" in one
    assert "en marcha desde hace 32 min: ninguna llamada nueva" in two
    assert harness.run(run.id).status is RunStatus.RUNNING
