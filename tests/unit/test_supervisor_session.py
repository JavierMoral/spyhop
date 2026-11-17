"""The supervisor session: one Claude session for each worktree that gets the
progress of the run and the reasons why it waits for a person."""

from pathlib import Path

from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.exceptions import OrcaCommandError, OrcaUncertainError
from spyhop.domain.models import AutomationRun, Worktree
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


def session_prompts(harness: Harness, terminal: str) -> list[str]:
    return [text for handle, text in harness.orca.prompts if handle == terminal]


async def test_the_session_starts_with_the_first_round_and_gets_the_verdicts_and_the_wait(
    tmp_path: Path,
) -> None:
    harness, run, worktree = await failed_check(tmp_path)

    session, check = harness.orca.terminals

    assert session["title"] == "spyhop"
    assert session["command"].startswith("env CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1 claude ")
    assert "[spyhop] LPD-1: ronda 1 de checkpoints: ticket-info" in session["command"]
    assert session["command"].endswith(" --disallowed-tools Edit Write NotebookEdit")
    assert check["title"] == "checkpoint ticket-info"

    current = harness.worktrees.get(worktree.id)

    assert current is not None
    assert current.supervisor_terminal == "term_check_1"
    assert current.supervisor_pane_key == "pane_term_1"

    await harness.supervisor.supervise()

    verdict, wait = session_prompts(harness, "term_check_1")

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
    assert session_prompts(harness, "term_check_1")[-2:] == [
        "[spyhop] LPD-1: ronda 2 de checkpoints: ticket-info",
        "[spyhop] LPD-1: la run sigue (esperaba: checkpoints_failed)",
    ]


async def test_a_closed_session_starts_again_with_the_message(tmp_path: Path) -> None:
    harness, run, worktree = await failed_check(tmp_path)

    harness.orca.prompt_errors.append(OrcaCommandError("selector_not_found", "terminal gone"))
    await harness.supervisor.supervise()

    session = harness.orca.terminals[2]
    current = harness.worktrees.get(worktree.id)

    assert session["title"] == "spyhop"
    assert "[spyhop] LPD-1: checkpoint ticket-info: FAIL" in session["command"]
    assert current is not None
    assert current.supervisor_terminal == "term_check_3"
    assert session_prompts(harness, "term_check_3")[0].startswith("[spyhop] LPD-1: ESPERA: ")


async def test_an_orca_error_of_the_session_does_not_stop_the_checks(tmp_path: Path) -> None:
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
    assert current.supervisor_terminal is None
