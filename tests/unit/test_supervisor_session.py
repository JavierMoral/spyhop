"""The supervisor session: one Claude session for each worktree that gets the
progress of the run and the reasons why it waits for a person."""

from pathlib import Path

from spyhop.application.supervisor_session import UNUSABLE
from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.exceptions import OrcaCommandError, OrcaUncertainError
from spyhop.domain.models import AutomationRun, CheckpointRun, OrcaTerminal, Worktree
from tests.fakes import Harness
from tests.unit.test_pipeline import HIGH, answer, harness_with, running, started


async def failed_check(tmp_path: Path) -> tuple[Harness, AutomationRun, Worktree]:
    harness = harness_with(
        tmp_path, {"checks": ["ticket-info"]}, "implement", with_supervisor_session=True
    )
    run, worktree = await started(harness)

    await harness.supervisor.supervise()
    answer(harness, run, "ticket-info", "FAIL", HIGH)

    return harness, run, worktree


def running_or_done(harness: Harness, run: AutomationRun) -> CheckpointRun:
    [row] = harness.checkpoint_runs.list_round(run.id, 0, 1)

    return row


def session_prompts(harness: Harness, terminal: str) -> list[str]:
    return [text for handle, text in harness.orca.prompts if handle == terminal]


async def test_the_session_starts_with_the_first_round_and_gets_the_verdicts_and_the_wait(
    tmp_path: Path,
) -> None:
    harness, run, worktree = await failed_check(tmp_path)

    [session] = harness.orca.terminals

    assert session["title"] == "spyhop"
    assert session["command"].startswith("env CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1 claude ")
    assert "[spyhop] LPD-1: ronda 1 de checkpoints: ticket-info" in session["command"]
    assert session["command"].endswith(" --disallowed-tools Edit Write NotebookEdit")

    current = harness.worktrees.get(worktree.id)

    assert current is not None
    assert current.supervisor_terminal == "term_check_1"
    assert current.supervisor_pane_key == "pane_term_1"

    await harness.supervisor.supervise()

    start, verdict, wait = session_prompts(harness, "term_check_1")

    assert start == (
        "[spyhop] LPD-1: checkpoint ticket-info arrancado; log en "
        f"{running_or_done(harness, run).output_file.with_suffix('.log')}"
    )

    assert verdict == (
        "[spyhop] LPD-1: checkpoint ticket-info: FAIL; informe en CLAUDE-CHECKPOINT-ticket-info.md"
    )
    assert wait.startswith(
        "[spyhop] LPD-1: ESPERA: checkpoints_failed: checkpoints failed: ticket-info; see "
    )
    assert harness.orca.cards[worktree.orca_worktree_id].startswith(
        "LPD-1: checkpoints_failed: checkpoints failed: ticket-info; see "
    )


async def test_the_session_pane_is_not_an_agent_of_the_run(tmp_path: Path) -> None:
    harness, run, worktree = await failed_check(tmp_path)

    await harness.supervisor.supervise()

    assert harness.run(run.id).waiting_reason is WaitingReason.CHECKPOINTS_FAILED

    # The session works on the message about the wait.

    harness.orca.set_agent(worktree.orca_worktree_id, "working", "pane_term_1")
    harness.tickets.updated["LPD-1"] = "2026-11-02T09:00:00.000+0000"
    await harness.supervisor.supervise()

    assert harness.run(run.id).status is RunStatus.RUNNING
    assert harness.checkpoint_runs.last_round(run.id, 0) == 2
    round_start, checkpoint_start, resumed = session_prompts(harness, "term_check_1")[-3:]

    assert round_start == "[spyhop] LPD-1: ronda 2 de checkpoints: ticket-info"
    assert checkpoint_start.startswith("[spyhop] LPD-1: checkpoint ticket-info arrancado")
    assert resumed == "[spyhop] LPD-1: la run sigue (esperaba: checkpoints_failed)"


async def test_a_closed_session_starts_again_with_the_message(tmp_path: Path) -> None:
    harness, run, worktree = await failed_check(tmp_path)

    harness.orca.prompt_errors.append(OrcaCommandError("selector_not_found", "terminal gone"))
    await harness.supervisor.supervise()

    session = harness.orca.terminals[1]
    current = harness.worktrees.get(worktree.id)

    assert session["title"] == "spyhop"
    assert "[spyhop] LPD-1: checkpoint ticket-info: FAIL" in session["command"]
    assert current is not None
    assert current.supervisor_terminal == "term_check_2"
    assert session_prompts(harness, "term_check_2")[0].startswith("[spyhop] LPD-1: ESPERA: ")


async def test_an_orca_error_of_the_session_does_not_stop_the_checks_and_the_next_message_starts_it(
    tmp_path: Path,
) -> None:
    harness = harness_with(
        tmp_path, {"checks": ["ticket-info"]}, "implement", with_supervisor_session=True
    )
    run, worktree = await started(harness)

    harness.orca.terminal_errors.append(OrcaUncertainError("timed out"))
    await harness.supervisor.supervise()

    [row] = running(harness, run)
    current = harness.worktrees.get(worktree.id)

    assert row.name == "ticket-info"
    assert current is not None
    assert current.supervisor_terminal == "term_check_2"


def session_harness(tmp_path: Path) -> Harness:
    return harness_with(
        tmp_path, {"checks": ["ticket-info"]}, "implement", with_supervisor_session=True
    )


async def test_a_round_that_cannot_start_is_told_once_it_starts(tmp_path: Path) -> None:
    harness = session_harness(tmp_path)
    _, worktree = await started(harness)
    notes = worktree.path / "Notes.java"

    notes.write_text("class Notes {}\n")
    await harness.supervisor.supervise()
    await harness.supervisor.supervise()

    notes.unlink()
    await harness.supervisor.supervise()

    messages = [terminal["command"] for terminal in harness.orca.terminals]
    messages += [text for _, text in harness.orca.prompts]

    assert sum("ronda 1 de checkpoints" in message for message in messages) == 1


async def test_a_session_without_a_pane_is_closed_and_not_started_again(tmp_path: Path) -> None:
    harness = session_harness(tmp_path)
    harness.orca.terminal_answers.append(OrcaTerminal("term_no_pane", None))
    run, worktree = await started(harness)

    await harness.supervisor.supervise()
    answer(harness, run, "ticket-info", "FAIL", HIGH)
    await harness.supervisor.supervise()

    current = harness.worktrees.get(worktree.id)

    assert harness.orca.closed == ["term_no_pane"]
    assert len(harness.orca.terminals) == 1
    assert harness.orca.prompts == []
    assert current is not None
    assert current.supervisor_terminal == UNUSABLE
    assert harness.run(run.id).waiting_reason is WaitingReason.CHECKPOINTS_FAILED
    assert harness.orca.cards[worktree.orca_worktree_id].startswith("LPD-1: checkpoints_failed")


async def test_a_session_without_a_handle_is_not_started_again(tmp_path: Path) -> None:
    harness = session_harness(tmp_path)
    harness.orca.terminal_answers.append(OrcaTerminal(None, "pane_x"))
    run, _ = await started(harness)

    await harness.supervisor.supervise()
    answer(harness, run, "ticket-info", "FAIL", HIGH)
    await harness.supervisor.supervise()

    assert harness.orca.closed == []
    assert len(harness.orca.terminals) == 1
