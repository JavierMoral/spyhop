from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spyhop.application.monitor import JiraMonitor
from spyhop.application.orchestrator import Orchestrator
from spyhop.application.transition_detector import TransitionDetector
from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.domain.enums import RunStatus
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import (
    ObservationRepository,
    PollerStateRepository,
    RunRepository,
)
from tests.factories import history, issue, settings
from tests.unit.test_transition_detector import FakeJira

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def connection(tmp_path: Path):
    connection = connect(tmp_path / "spyhop.db")

    yield connection

    connection.close()


def monitor(tmp_path: Path, connection, jira: FakeJira) -> JiraMonitor:
    return JiraMonitor(
        detector=TransitionDetector(jira, WorkflowResolver(settings(tmp_path)), "UTC"),
        orchestrator=Orchestrator(RunRepository(connection), dry_run=True),
        poller_state=PollerStateRepository(connection),
        observations=ObservationRepository(connection),
        margin=timedelta(minutes=5),
    )


async def test_first_poll_only_sets_the_watermark(tmp_path: Path, connection) -> None:
    jira = FakeJira(
        [issue(histories=[history("1", "2026-11-01T08:00:00.000+0000", "In Progress")])]
    )

    assert await monitor(tmp_path, connection, jira).poll(now=T0) == []
    assert jira.jql == []
    assert PollerStateRepository(connection).watermark() == T0


async def test_entries_are_registered_once(tmp_path: Path, connection) -> None:
    jira = FakeJira(
        [issue("LPD-1", histories=[history("7", "2026-11-01T09:10:00.000+0000", "In Progress")])]
    )
    subject = monitor(tmp_path, connection, jira)

    await subject.poll(now=T0)

    [run] = await subject.poll(now=T0 + timedelta(minutes=15))

    assert run.issue_key == "LPD-1"
    assert run.status is RunStatus.DISCOVERED
    assert run.trigger_history_id == "7"
    assert '"2026/11/01 08:55"' in jira.jql[0]

    assert await subject.poll(now=T0 + timedelta(minutes=16)) == []
    assert PollerStateRepository(connection).watermark() == T0 + timedelta(minutes=16)


async def test_reentry_while_a_run_is_active_is_ignored(tmp_path: Path, connection) -> None:
    jira = FakeJira(
        [issue("LPD-1", histories=[history("7", "2026-11-01T09:10:00.000+0000", "In Progress")])]
    )
    subject = monitor(tmp_path, connection, jira)

    await subject.poll(now=T0)
    await subject.poll(now=T0 + timedelta(minutes=15))

    jira.issues = [
        issue("LPD-1", histories=[history("8", "2026-11-01T09:20:00.000+0000", "In Progress")])
    ]

    assert await subject.poll(now=T0 + timedelta(minutes=25)) == []
    assert len(RunRepository(connection).list(include_terminal=True)) == 1


async def test_failed_poll_keeps_the_watermark(tmp_path: Path, connection) -> None:
    class FailingJira(FakeJira):
        async def search(self, jql, fields):
            raise RuntimeError("down")

    subject = monitor(tmp_path, connection, FailingJira([]))

    await subject.poll(now=T0)

    with pytest.raises(RuntimeError):
        await subject.poll(now=T0 + timedelta(minutes=1))

    assert PollerStateRepository(connection).watermark() == T0
