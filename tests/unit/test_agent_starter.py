from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spyhop.application.agent_starter import (
    REPLAY_WINDOW_SECONDS,
    AgentStarter,
    LaunchOutcomeUnknown,
    replay_expired,
)
from spyhop.config.settings import AgentSettings
from spyhop.domain.enums import SessionStatus
from spyhop.domain.exceptions import OrcaCommandError, OrcaUncertainError
from spyhop.domain.models import AutomationRun, Worktree
from spyhop.infrastructure.orca.client import new_operation_id
from tests.fakes import Harness
from tests.unit.test_persistence import orca_worktree


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


@pytest.fixture
def starter(harness: Harness) -> AgentStarter:
    return AgentStarter(harness.sessions, harness.orca, AgentSettings(), harness.clock)


def _run_and_worktree(harness: Harness) -> tuple[AutomationRun, Worktree]:
    run = harness.discover()
    worktree = harness.worktrees.create("LPD-1", orca_worktree(), Path("/repo"), "master")

    return run, worktree


async def test_a_lost_answer_is_replayed_with_the_same_operation_id(
    harness: Harness, starter: AgentStarter
) -> None:
    run, worktree = _run_and_worktree(harness)
    harness.orca.launch_errors.append(OrcaUncertainError("timed out"))

    with pytest.raises(OrcaUncertainError):
        await starter.start(run, worktree, "opus", "goal", "plan")

    session = await starter.start(run, worktree, "opus", "goal", "plan")
    first, second = harness.orca.launches

    assert first["operation_id"] == second["operation_id"] == session.launch_operation_id
    assert session.status is SessionStatus.LAUNCHED
    assert session.agent_handle == "term_2"
    assert len(harness.sessions.list_for_run(run.id)) == 1


async def test_an_old_lost_answer_is_not_replayed(harness: Harness, starter: AgentStarter) -> None:
    run, worktree = _run_and_worktree(harness)
    harness.orca.launch_errors.append(OrcaUncertainError("timed out"))

    with pytest.raises(OrcaUncertainError):
        await starter.start(run, worktree, "opus", "goal", "plan")

    [session] = harness.sessions.list_for_run(run.id)
    harness.clock.advance(timedelta(seconds=REPLAY_WINDOW_SECONDS + 1))

    with pytest.raises(LaunchOutcomeUnknown):
        await starter.start(run, worktree, "opus", "goal", "plan")

    failed = harness.sessions.get(session.id)

    assert failed is not None and failed.status is SessionStatus.FAILED
    assert len(harness.orca.launches) == 1


async def test_a_refused_launch_fails_the_session(harness: Harness, starter: AgentStarter) -> None:
    run, worktree = _run_and_worktree(harness)
    harness.orca.launch_errors.append(OrcaCommandError("agent_not_found", "agent_not_found"))

    with pytest.raises(OrcaCommandError):
        await starter.start(run, worktree, "opus", "goal", "plan")

    [session] = harness.sessions.list_for_run(run.id)

    assert session.status is SessionStatus.FAILED


async def test_a_label_error_only_goes_to_the_log(
    harness: Harness, starter: AgentStarter, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, worktree = _run_and_worktree(harness)
    session = await starter.start(run, worktree, "opus", "goal", "plan")

    async def refuse(terminal: str, title: str) -> None:
        raise OrcaCommandError("terminal_not_found", "terminal_not_found")

    monkeypatch.setattr(harness.orca, "rename_terminal", refuse)

    await starter.label(run, session)

    assert harness.orca.names == {}


def test_a_replay_expires_after_the_window(harness: Harness) -> None:
    run = harness.discover()
    issued = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    window = timedelta(seconds=REPLAY_WINDOW_SECONDS)
    session = harness.sessions.create_launching(
        run.id, "claude", "opus", new_operation_id(issued), "plan"
    )
    broken = harness.sessions.create_launching(run.id, "claude", "opus", "broken", "plan")

    assert not replay_expired(session, issued + window)
    assert replay_expired(session, issued + window + timedelta(seconds=1))
    assert replay_expired(broken, issued)
