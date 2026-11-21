"""The daemon loop: a wake supervises and launches now, and JIRA keeps its interval."""

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from spyhop import daemon
from spyhop.domain.exceptions import JiraRateLimitedError
from spyhop.domain.models import JiraUser
from spyhop.infrastructure.service.systemd import WAKE_SIGNAL
from tests.factories import settings


class Jira:
    def __init__(self, *_: Any) -> None:
        pass

    async def __aenter__(self) -> "Jira":
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass

    async def myself(self) -> JiraUser:
        return JiraUser("id", "Test", "UTC")


class Step:
    """Records its call. The supervisor ends each cycle of the test: it runs on
    each poll and each wake."""

    def __init__(
        self,
        name: str,
        calls: list[str],
        cycles: asyncio.Queue[str],
        error: Exception | None = None,
    ) -> None:
        self._name = name
        self._calls = calls
        self._cycles = cycles
        self._error = error

    async def __call__(self, *_: Any) -> None:
        self._calls.append(self._name)

        if self._name == "supervise":
            await self._cycles.put(self._name)

        if self._error is not None:
            raise self._error


@pytest.mark.parametrize(
    ("poll_error", "expected"),
    [
        # The cleanup reads JIRA, so a wake skips it.
        (None, ["poll", "sweep", "supervise", "launch", "supervise", "launch"]),
        # While the rate limit lasts, a wake launches nothing: a launch reads JIRA.
        (
            JiraRateLimitedError(600),
            ["poll", "sweep", "supervise", "launch", "supervise"],
        ),
    ],
)
async def test_a_wake_supervises_now_and_leaves_jira_on_the_poll_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    poll_error: Exception | None,
    expected: list[str],
) -> None:
    calls: list[str] = []
    cycles: asyncio.Queue[str] = asyncio.Queue()

    class Monitor:
        def __init__(self, **_: Any) -> None:
            self.poll = Step("poll", calls, cycles, poll_error)

        def record_error(self, message: str) -> None:
            pass

    class Agent:
        def __init__(self, method: str, name: str) -> None:
            setattr(self, method, Step(name, calls, cycles))

    agents = (
        Agent("sweep", "sweep"),
        Agent("supervise", "supervise"),
        Agent("launch_pending", "launch"),
    )

    monkeypatch.setenv("INVOCATION_ID", "test")
    monkeypatch.setattr(daemon, "JiraClient", Jira)
    monkeypatch.setattr(daemon, "credentials_from_environment", lambda _: None)
    monkeypatch.setattr(daemon, "JiraMonitor", Monitor)
    monkeypatch.setattr(daemon, "_agents", lambda *_: agents)

    task = asyncio.create_task(
        daemon.run_daemon(
            settings(tmp_path, application={"dry_run": False, "polling_interval": 60}),
            database=tmp_path / "spyhop.db",
        )
    )

    await asyncio.wait_for(cycles.get(), 5)

    os.kill(os.getpid(), WAKE_SIGNAL)

    await asyncio.wait_for(cycles.get(), 5)

    os.kill(os.getpid(), signal.SIGTERM)

    await asyncio.wait_for(task, 5)

    assert calls == expected
