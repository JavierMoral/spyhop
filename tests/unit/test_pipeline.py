"""Pipelines: checks phases run by Spyhop before and after the agent steps."""

import fcntl
import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spyhop.application import checkpoint_runner
from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.exceptions import OrcaUnavailableError, UnitError
from spyhop.domain.models import AutomationRun, CheckpointRun, ChecksPhase, Worktree
from tests.factories import settings
from tests.fakes import PLAN_APPROVED, PLAN_REVIEW, SPYHOP, Harness, git
from tests.unit.test_checkpoint_planner import checkpoint

BUG = {
    "name": "bug_fix",
    "trigger_statuses": ["In Progress"],
    "repository": "portal",
    "prompt": "bug_fix.md",
    "pipeline": "bug",
}


def harness_with(tmp_path: Path, *pipeline: Any, with_supervisor_session: bool = False) -> Harness:
    config = settings(
        tmp_path,
        projects={"LPD": {"workflows": {"Bug": BUG}}},
        pipelines={"bug": list(pipeline)},
    )
    harness = Harness(tmp_path, config, with_supervisor_session)

    harness.orca.real_git = True

    checkpoint(harness.agents_dir, "ticket-info", mode="read", inputs=["ticket"])
    checkpoint(harness.agents_dir, "regressions")
    checkpoint(harness.agents_dir, "security", mode="read")
    checkpoint(harness.agents_dir, "mutation", after=["regressions"])

    return harness


async def started(harness: Harness) -> tuple[AutomationRun, Worktree]:
    run = harness.discover()

    await harness.launcher.launch_pending()

    worktree = harness.worktrees.find_current("LPD-1")

    assert worktree is not None

    return harness.run(run.id), worktree


def running(harness: Harness, run: AutomationRun) -> list[CheckpointRun]:
    run = harness.run(run.id)
    rows = harness.checkpoint_runs.list_round(
        run.id, run.phase, harness.checkpoint_runs.last_round(run.id, run.phase)
    )

    return [row for row in rows if row.status == "RUNNING"]


def answer(
    harness: Harness,
    run: AutomationRun,
    name: str,
    verdict: str = "PASS",
    findings: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> None:
    """Writes the result file that `spyhop checkpoints exec` would write."""

    [row] = [row for row in running(harness, run) if row.name == name]
    data = {
        "verdict": verdict,
        "head": row.head,
        "commands": [{"command": "./gradlew test", "result": "0"}],
        "findings": findings or [],
        **overrides,
    }

    row.output_file.parent.mkdir(parents=True, exist_ok=True)
    row.output_file.write_text(
        json.dumps({"exit_code": 0, "output": {"is_error": False, "structured_output": data}})
    )


def pending_commands(harness: Harness, run: AutomationRun) -> list[str]:
    return [
        row["kind"]
        for row in harness.connection.execute(
            "SELECT kind FROM run_commands WHERE run_id = ? AND consumed_at IS NULL", (run.id,)
        )
    ]


def written(path: Path, text: str, ahead: float = 0) -> None:
    """Writes the file with the exact current time, plus ``ahead`` seconds, as
    its mtime. Linux sets mtime from a coarse clock, which can be a few
    milliseconds before a time that Spyhop took just before."""

    path.write_text(text)

    mtime = time.time() + ahead

    os.utime(path, (mtime, mtime))


HIGH = [{"severity": "high", "file": "App.java", "line": 1, "summary": "Broken."}]


async def test_checks_before_the_plan_run_without_an_agent_and_then_start_it(
    tmp_path: Path,
) -> None:
    harness = harness_with(
        tmp_path, {"checks": ["ticket-info"]}, "plan", "plan_review", "implement"
    )
    run, worktree = await started(harness)

    assert run.status is RunStatus.RUNNING
    assert harness.orca.launches == []

    await harness.supervisor.supervise()

    [unit] = harness.units.started
    [row] = running(harness, run)

    assert unit["unit"] == f"spyhop-checkpoint-{run.id}-{row.id}"
    assert unit["command"][:5] == [str(SPYHOP), "checkpoints", "exec", "ticket-info", "--base"]
    assert unit["cwd"] == worktree.path
    assert unit["log"] == row.output_file.with_suffix(".log")
    assert row.unit == unit["unit"]
    assert row.inputs == "ticket=2026-11-01T09:00:00.000+0000"

    answer(harness, run, "ticket-info")
    await harness.supervisor.supervise()

    assert harness.run(run.id).phase == 1
    assert len(harness.orca.launches) == 1
    assert harness.orca.launches[0]["model"] == "opus"
    assert "escribe CLAUDE-PLAN.md" in harness.orca.launches[0]["prompt"]
    assert "| ticket-info | PASS | ticket=2026-11-01T09:00:00.000+0000 | 1 |" in (
        (worktree.path / "CLAUDE-CHECKPOINTS.md").read_text()
    )
    assert (
        ", phase 0, round 1: PASS"
        in (worktree.path / "CLAUDE-CHECKPOINT-ticket-info.md").read_text()
    )
    assert git(worktree.path, "status", "--porcelain") == ""


async def test_a_failed_check_waits_and_runs_again_when_the_ticket_changes(
    tmp_path: Path,
) -> None:
    harness = harness_with(tmp_path, {"checks": ["ticket-info"]}, "implement")
    run, worktree = await started(harness)

    await harness.supervisor.supervise()
    answer(harness, run, "ticket-info", "FAIL", HIGH)
    await harness.supervisor.supervise()

    waiting = harness.run(run.id)

    assert waiting.status is RunStatus.WAITING_FOR_HUMAN
    assert waiting.waiting_reason is WaitingReason.CHECKPOINTS_FAILED
    assert waiting.error_message is not None
    assert waiting.error_message.startswith("checkpoints failed: ticket-info; see ")

    await harness.supervisor.supervise()

    assert len(harness.units.started) == 1

    harness.tickets.updated["LPD-1"] = "2026-11-02T09:00:00.000+0000"
    await harness.supervisor.supervise()

    assert len(harness.units.started) == 2
    assert harness.run(run.id).status is RunStatus.RUNNING
    assert harness.checkpoint_runs.last_round(run.id, 0) == 2


async def test_rerun_on_request_without_a_change_of_inputs(tmp_path: Path) -> None:
    harness = harness_with(tmp_path, {"checks": ["ticket-info"]}, "implement")
    run, _ = await started(harness)

    await harness.supervisor.supervise()
    answer(harness, run, "ticket-info", "FAIL", HIGH)
    await harness.supervisor.supervise()
    harness.runs.request_checks(run.id)

    assert pending_commands(harness, run) == ["checks"]

    await harness.supervisor.supervise()

    assert harness.checkpoint_runs.last_round(run.id, 0) == 2
    assert pending_commands(harness, run) == []


async def test_rerun_of_the_failed_checkpoints_keeps_each_pass_with_the_same_inputs(
    tmp_path: Path,
) -> None:
    harness = harness_with(tmp_path, {"checks": ["ticket-info", "security"]}, "implement")
    run, worktree = await started(harness)

    await harness.supervisor.supervise()
    answer(harness, run, "ticket-info", "FAIL", HIGH)
    answer(harness, run, "security")
    await harness.supervisor.supervise()
    harness.runs.request_checks(run.id, failed_only=True)
    await harness.supervisor.supervise()

    assert [row.name for row in running(harness, run)] == ["ticket-info"]

    [kept] = [
        row for row in harness.checkpoint_runs.list_round(run.id, 0, 2) if row.name == "security"
    ]

    assert kept.verdict == "PASS"
    assert kept.detail == "kept from round 1"

    answer(harness, run, "ticket-info")
    await harness.supervisor.supervise()

    assert harness.run(run.id).phase == 1
    assert len(harness.units.started) == 3
    assert "| security | PASS | " in (worktree.path / "CLAUDE-CHECKPOINTS.md").read_text()


async def test_rerun_of_the_failed_checkpoints_runs_a_pass_with_changed_inputs(
    tmp_path: Path,
) -> None:
    harness = harness_with(tmp_path, {"checks": ["ticket-info", "security"]}, "implement")
    run, worktree = await started(harness)

    await harness.supervisor.supervise()
    answer(harness, run, "ticket-info", "FAIL", HIGH)
    answer(harness, run, "security")
    await harness.supervisor.supervise()
    git(worktree.path, "commit", "--quiet", "--allow-empty", "-m", "LPD-1 Change")
    harness.runs.request_checks(run.id, failed_only=True)
    await harness.supervisor.supervise()

    assert sorted(row.name for row in running(harness, run)) == ["security", "ticket-info"]


async def implemented(harness: Harness) -> tuple[AutomationRun, Worktree, str]:
    """The implementation agent declared the end; the checks start."""

    run, worktree = await started(harness)
    session = harness.sessions.find_launched(run.id)

    assert session is not None and session.pane_key is not None

    git(worktree.path, "checkout", "--quiet", "-b", "LPD-1")
    (worktree.path / "App.java").write_text("class App { int fixed; }\n")
    git(worktree.path, "commit", "--quiet", "-am", "LPD-1 Fix")
    written(worktree.path / "CLAUDE-RESULT.md", "done")
    harness.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
    harness.orca.set_agent(worktree.orca_worktree_id, "done", session.pane_key)

    await harness.supervisor.supervise()

    return harness.run(run.id), worktree, session.pane_key


async def test_checks_after_the_implementation_run_in_stages_and_complete_the_run(
    tmp_path: Path,
) -> None:
    harness = harness_with(
        tmp_path, "implement", {"checks": ["regressions", "security", "mutation"], "on_fail": "fix"}
    )
    run, worktree, _ = await implemented(harness)

    assert run.status is RunStatus.RUNNING
    assert run.phase == 1
    assert [row.name for row in running(harness, run)] == ["security"]

    answer(harness, run, "security")
    await harness.supervisor.supervise()

    assert [row.name for row in running(harness, run)] == ["regressions"]

    answer(harness, run, "regressions")
    await harness.supervisor.supervise()
    answer(harness, run, "mutation")
    await harness.supervisor.supervise()

    completed = harness.run(run.id)
    table = (worktree.path / "CLAUDE-CHECKPOINTS.md").read_text()

    assert completed.status is RunStatus.COMPLETED
    assert table.index("| regressions | PASS") < table.index("| security | PASS")
    assert table.index("| security | PASS") < table.index("| mutation | PASS")


async def test_a_run_in_progress_keeps_its_pipeline_when_the_configuration_changes(
    tmp_path: Path,
) -> None:
    harness = harness_with(tmp_path, "implement", {"checks": ["regressions"], "on_fail": "fix"})
    run, _, _ = await implemented(harness)

    # A checks phase before implement moves implement to index 1.

    harness.config.pipelines["bug"] = settings(
        tmp_path,
        pipelines={"bug": [{"checks": ["ticket-info"]}, "implement", {"checks": ["regressions"]}]},
    ).pipelines["bug"]

    answer(harness, run, "regressions")
    await harness.supervisor.supervise()

    assert harness.run(run.id).status is RunStatus.COMPLETED


async def test_a_run_from_before_the_stored_pipeline_gets_it_on_supervision(
    tmp_path: Path,
) -> None:
    harness = harness_with(tmp_path, "implement", {"checks": ["regressions"], "on_fail": "fix"})
    run, _, _ = await implemented(harness)

    harness.connection.execute("UPDATE automation_runs SET pipeline = NULL")

    await harness.supervisor.supervise()

    assert harness.run(run.id).pipeline == ("implement", ChecksPhase(("regressions",), "fix"))


async def test_a_fix_prompt_that_orca_cannot_take_is_asked_again(tmp_path: Path) -> None:
    harness = harness_with(tmp_path, "implement", {"checks": ["regressions"], "on_fail": "fix"})
    run, _, _ = await implemented(harness)
    send_prompt = harness.orca.send_prompt
    closed = [OrcaUnavailableError("Orca is closed")]

    async def closed_once(terminal: str, prompt: str) -> None:
        if closed:
            raise closed.pop()

        await send_prompt(terminal, prompt)

    harness.orca.send_prompt = closed_once  # type: ignore[method-assign]
    answer(harness, run, "regressions", "FAIL", HIGH)
    await harness.supervisor.supervise()

    assert harness.run(run.id).fix_requested_at is None

    await harness.supervisor.supervise()

    assert harness.run(run.id).fix_requested_at is not None


async def test_a_failed_check_asks_the_agent_to_fix_it_and_runs_a_new_round(
    tmp_path: Path,
) -> None:
    harness = harness_with(
        tmp_path, "implement", {"checks": ["regressions", "mutation"], "on_fail": "fix"}
    )
    run, worktree, pane = await implemented(harness)
    session = harness.sessions.find_launched(run.id)

    assert session is not None

    answer(harness, run, "regressions", "FAIL", HIGH)
    await harness.supervisor.supervise()

    # mutation comes after regressions, so it is skipped in this round.

    [(terminal, prompt)] = harness.orca.prompts

    assert terminal == session.agent_handle
    assert "han fallado: regressions." in prompt
    assert harness.run(run.id).fix_requested_at is not None
    assert "**high** `App.java:1`: Broken." in (
        (worktree.path / "CLAUDE-CHECKPOINT-regressions.md").read_text()
    )

    # The old CLAUDE-RESULT.md does not end the fix.

    harness.orca.set_agent(worktree.orca_worktree_id, "working", pane)
    await harness.supervisor.supervise()

    assert harness.checkpoint_runs.last_round(run.id, 1) == 1

    git(worktree.path, "commit", "--quiet", "--allow-empty", "-m", "LPD-1 Fix the caller")
    written(worktree.path / "CLAUDE-RESULT.md", "fixed", ahead=1)
    harness.runs.declare(run.id, "done")
    harness.orca.set_agent(worktree.orca_worktree_id, "done", pane)
    await harness.supervisor.supervise()

    assert harness.run(run.id).fix_requested_at is None
    assert harness.checkpoint_runs.last_round(run.id, 1) == 2
    assert [row.name for row in running(harness, run)] == ["regressions"]


async def test_the_last_round_does_not_fix(tmp_path: Path) -> None:
    harness = harness_with(
        tmp_path, "implement", {"checks": ["regressions"], "on_fail": "fix", "rounds": 1}
    )
    run, _, _ = await implemented(harness)

    answer(harness, run, "regressions", "FAIL", HIGH)
    await harness.supervisor.supervise()

    waiting = harness.run(run.id)

    assert harness.orca.prompts == []
    assert waiting.waiting_reason is WaitingReason.CHECKPOINTS_FAILED
    assert waiting.error_message is not None
    assert "after 1 rounds" in waiting.error_message


@pytest.mark.parametrize(
    ("overrides", "validation"),
    [
        ({"head": "0" * 40}, "is not the HEAD of the stage"),
        ({"commands": []}, "a PASS needs commands as evidence"),
        (
            {"findings": [{"severity": "medium", "summary": "Risk."}]},
            "a PASS cannot have a high or medium finding",
        ),
    ],
)
async def test_a_result_that_breaks_the_contract_is_a_fail(
    tmp_path: Path, overrides: dict[str, Any], validation: str
) -> None:
    harness = harness_with(tmp_path, "implement", {"checks": ["regressions"]})
    run, worktree, _ = await implemented(harness)
    [row] = running(harness, run)

    answer(harness, run, "regressions", **overrides)
    await harness.supervisor.supervise()

    finished = harness.checkpoint_runs.get(row.id)

    assert finished is not None and finished.verdict == "FAIL"
    assert finished.detail is not None and validation in finished.detail
    assert harness.run(run.id).waiting_reason is WaitingReason.CHECKPOINTS_FAILED


def git_threads(monkeypatch: pytest.MonkeyPatch) -> list[threading.Thread]:
    """The threads in which the checkpoint runner asks Git."""

    threads: list[threading.Thread] = []

    for name in ("has_changes", "head"):
        real = getattr(checkpoint_runner, name)

        def recorded(path: Path, real: Any = real) -> Any:
            threads.append(threading.current_thread())

            return real(path)

        monkeypatch.setattr(checkpoint_runner, name, recorded)

    return threads


async def test_an_exclusive_check_that_leaves_changes_fails_and_blocks_the_next_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = harness_with(tmp_path, "implement", {"checks": ["regressions", "mutation"]})
    run, worktree, _ = await implemented(harness)
    [row] = running(harness, run)
    threads = git_threads(monkeypatch)

    (worktree.path / "App.java").write_text("class App {}\n")
    answer(harness, run, "regressions")
    await harness.supervisor.supervise()

    finished = harness.checkpoint_runs.get(row.id)

    assert finished is not None and finished.verdict == "FAIL"
    assert finished.detail is not None and "left uncommitted changes" in finished.detail
    assert threads and threading.main_thread() not in threads


async def test_a_dirty_worktree_blocks_the_start_of_a_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = harness_with(tmp_path, {"checks": ["ticket-info"]}, "implement")
    run, worktree = await started(harness)
    threads = git_threads(monkeypatch)

    (worktree.path / "Notes.java").write_text("class Notes {}\n")
    await harness.supervisor.supervise()

    waiting = harness.run(run.id)

    assert harness.units.started == []
    assert waiting.waiting_reason is WaitingReason.UNKNOWN_STATE
    assert waiting.error_message is not None
    assert "checkpoints need a clean worktree" in waiting.error_message
    assert threads and threading.main_thread() not in threads


async def test_a_checkpoint_whose_unit_did_not_start_starts_again(tmp_path: Path) -> None:
    harness = harness_with(tmp_path, {"checks": ["ticket-info"]}, "implement")
    run, _ = await started(harness)

    await harness.supervisor.supervise()

    # Spyhop stopped after it stored the row and before it started the
    # unit, for longer than the timeout.

    [row] = running(harness, run)
    harness.clock.advance(timedelta(hours=4))
    harness.connection.execute("UPDATE checkpoint_runs SET unit = NULL")
    await harness.supervisor.supervise()

    first, second = harness.units.started
    [restarted] = running(harness, run)

    assert first == second
    assert restarted.started_at >= row.started_at + timedelta(hours=4)

    # The timeout counts from the start of the unit.

    await harness.supervisor.supervise()

    assert harness.units.stopped == []
    assert len(running(harness, run)) == 1


async def test_a_check_without_a_result_in_time_is_a_fail_after_its_unit_stops(
    tmp_path: Path,
) -> None:
    harness = harness_with(tmp_path, {"checks": ["ticket-info"]}, "implement")
    run, _ = await started(harness)

    await harness.supervisor.supervise()

    [row] = running(harness, run)
    harness.clock.advance(timedelta(hours=4))
    await harness.supervisor.supervise()

    assert harness.units.stopped == [row.unit]
    assert harness.run(run.id).waiting_reason is WaitingReason.CHECKPOINTS_FAILED


async def test_a_late_check_whose_process_still_runs_blocks_the_phase(tmp_path: Path) -> None:
    harness = harness_with(tmp_path, {"checks": ["ticket-info"]}, "implement")
    run, _ = await started(harness)

    await harness.supervisor.supervise()

    [row] = running(harness, run)
    harness.clock.advance(timedelta(hours=4))
    lock_file = row.output_file.parent / f"{row.output_file.name}.lock"

    lock_file.parent.mkdir(parents=True, exist_ok=True)

    with lock_file.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        await harness.supervisor.supervise()

    waiting = harness.run(run.id)

    assert waiting.waiting_reason is WaitingReason.UNKNOWN_STATE
    assert waiting.error_message is not None
    assert "its process still runs" in waiting.error_message
    assert len(running(harness, run)) == 1

    await harness.supervisor.supervise()

    assert harness.run(run.id).waiting_reason is WaitingReason.CHECKPOINTS_FAILED


async def test_a_checkpoint_whose_unit_did_not_start_gets_no_second_row(
    tmp_path: Path,
) -> None:
    harness = harness_with(tmp_path, {"checks": ["ticket-info", "security"]}, "implement")
    run, _ = await started(harness)

    await harness.supervisor.supervise()

    # Spyhop stopped before the unit of ticket-info, the second of the read
    # stage, started.

    harness.connection.execute("UPDATE checkpoint_runs SET unit = NULL WHERE name = 'ticket-info'")
    await harness.supervisor.supervise()
    answer(harness, run, "security")
    await harness.supervisor.supervise()
    answer(harness, run, "ticket-info")
    await harness.supervisor.supervise()

    rows = harness.checkpoint_runs.list_round(run.id, 0, 1)

    assert sorted(row.name for row in rows) == ["security", "ticket-info"]
    assert harness.run(run.id).phase == 1


async def test_a_unit_that_does_not_start_gets_a_report(tmp_path: Path) -> None:
    harness = harness_with(tmp_path, {"checks": ["ticket-info"]}, "implement")
    run, worktree = await started(harness)
    harness.units.start_errors.append(UnitError("systemd-run x: no user bus"))

    await harness.supervisor.supervise()

    assert "Validation: the checkpoint did not start: systemd-run x: no user bus." in (
        (worktree.path / "CLAUDE-CHECKPOINT-ticket-info.md").read_text()
    )
    assert "| ticket-info | FAIL |" in (worktree.path / "CLAUDE-CHECKPOINTS.md").read_text()

    await harness.supervisor.supervise()

    assert harness.run(run.id).waiting_reason is WaitingReason.CHECKPOINTS_FAILED


async def test_an_approved_plan_without_plan_review_starts_a_new_agent(tmp_path: Path) -> None:
    harness = harness_with(tmp_path, "implement")
    run, worktree = await started(harness)
    session = harness.sessions.find_launched(run.id)

    assert session is not None and session.pane_key is not None

    written(worktree.path / "CLAUDE-PLAN.md", "plan")
    harness.set_card(worktree.orca_worktree_id, PLAN_REVIEW)
    harness.orca.set_agent(worktree.orca_worktree_id, "done", session.pane_key)
    await harness.supervisor.supervise()

    assert harness.run(run.id).waiting_reason is WaitingReason.PLAN_REVIEW

    harness.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
    await harness.supervisor.supervise()

    assert len(harness.orca.launches) == 2
    assert harness.orca.launches[1]["prompt"] == harness.context.goal("LPD-1", "implement")
    assert harness.run(run.id).status is RunStatus.RUNNING


async def test_a_fix_whose_next_round_is_blocked_runs_the_round_once_unblocked(
    tmp_path: Path,
) -> None:
    harness = harness_with(tmp_path, "implement", {"checks": ["regressions"], "on_fail": "fix"})
    run, worktree, pane = await implemented(harness)

    answer(harness, run, "regressions", "FAIL", HIGH)
    await harness.supervisor.supervise()
    (worktree.path / "Notes.java").write_text("class Notes {}\n")
    written(worktree.path / "CLAUDE-RESULT.md", "fixed", ahead=1)
    harness.runs.declare(run.id, "done")
    harness.orca.set_agent(worktree.orca_worktree_id, "done", pane)
    await harness.supervisor.supervise()

    assert harness.run(run.id).waiting_reason is WaitingReason.UNKNOWN_STATE

    (worktree.path / "Notes.java").unlink()
    await harness.supervisor.supervise()

    assert len(harness.orca.prompts) == 1
    assert harness.checkpoint_runs.last_round(run.id, 1) == 2


async def test_a_worktree_without_the_report_exclusions_gets_them(tmp_path: Path) -> None:
    harness = harness_with(tmp_path, {"checks": ["ticket-info", "regressions"]}, "implement")
    run, worktree = await started(harness)

    (worktree.path / ".git" / "info" / "exclude").write_text("/CLAUDE-TASK.md\n/.claude/\n")
    await harness.supervisor.supervise()
    answer(harness, run, "ticket-info")
    await harness.supervisor.supervise()

    assert [row.name for row in running(harness, run)] == ["regressions"]


async def test_each_agent_has_the_title_the_name_and_the_color_of_its_role(tmp_path: Path) -> None:
    harness = harness_with(tmp_path, "plan", "plan_review", "implement")
    run, worktree = await started(harness)
    agents = harness.config.agents

    assert harness.orca.titles == {"term_1": f"plan {agents.plan_model}"}

    session = harness.sessions.find_launched(run.id)

    assert session is not None and session.pane_key is not None

    written(worktree.path / "CLAUDE-PLAN.md", "plan")
    harness.set_card(worktree.orca_worktree_id, PLAN_REVIEW)
    harness.orca.set_agent(worktree.orca_worktree_id, "done", session.pane_key)
    await harness.supervisor.supervise()
    harness.set_card(worktree.orca_worktree_id, PLAN_APPROVED)
    await harness.supervisor.supervise()

    assert harness.orca.titles == {
        "term_1": f"plan {agents.plan_model}",
        "term_2": f"implement {agents.model}",
    }
    assert harness.orca.names == harness.orca.titles
    assert harness.orca.colors == {"term_1": "blue", "term_2": "orange"}


async def test_replan_starts_a_new_plan_agent_and_the_plan_waits_again(tmp_path: Path) -> None:
    harness = harness_with(tmp_path, "plan", "plan_review", "implement")
    run, worktree = await started(harness)
    session = harness.sessions.find_launched(run.id)

    assert session is not None and session.pane_key is not None

    written(worktree.path / "CLAUDE-PLAN.md", "plan")
    harness.set_card(worktree.orca_worktree_id, PLAN_REVIEW)
    harness.orca.set_agent(worktree.orca_worktree_id, "done", session.pane_key)
    await harness.supervisor.supervise()
    harness.orca.agents.clear()
    harness.runs.set_plan_agent_closed_told(run.id, True)
    harness.runs.request_plan_changes(run.id, "Add a test\nfor the empty list.")
    await harness.supervisor.supervise()

    replanned = harness.run(run.id)
    new_session = harness.sessions.find_launched(run.id)

    assert pending_commands(harness, run) == []

    assert len(harness.orca.launches) == 2
    assert "cambia CLAUDE-PLAN.md con estos cambios" in harness.orca.launches[1]["prompt"]
    assert "Add a test for the empty list." in harness.orca.launches[1]["prompt"]
    assert replanned.status is RunStatus.RUNNING
    assert replanned.plan_changes is None
    # The close of the new plan agent is news to the person again.
    assert not replanned.plan_agent_closed_told
    assert new_session is not None and new_session.pane_key is not None
    assert new_session.role == "plan"

    written(worktree.path / "CLAUDE-PLAN.md", "new plan", ahead=1)
    harness.runs.declare(run.id, "plan")
    harness.orca.set_agent(
        worktree.orca_worktree_id,
        "done",
        new_session.pane_key,
        datetime.now(UTC) + timedelta(seconds=1),
    )
    await harness.supervisor.supervise()

    assert harness.run(run.id).waiting_reason is WaitingReason.PLAN_REVIEW
