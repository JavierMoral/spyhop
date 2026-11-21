"""The daemon loop: a wake supervises and launches now, and JIRA keeps its interval."""

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from spyhop import daemon
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
    def __init__(self, name: str, calls: list[str], done: asyncio.Queue[str]) -> None:
        self._name = name
        self._calls = calls
        self._done = done

    async def __call__(self, *_: Any) -> None:
        self._calls.append(self._name)

        if self._name == "launch":
            await self._done.put(self._name)


async def test_a_wake_runs_the_agents_now_and_not_the_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    cycles: asyncio.Queue[str] = asyncio.Queue()

    class Monitor:
        def __init__(self, **_: Any) -> None:
            self.poll = Step("poll", calls, cycles)

    class Agent:
        def __init__(self, method: str, name: str) -> None:
            setattr(self, method, Step(name, calls, cycles))

    agents = (
        Agent("sweep", "sweep"),
        Agent("supervise", "supervise"),
        Agent("launch_pending", "launch"),
    )

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

    # The cleanup reads JIRA, so a wake skips it too.

    assert calls == ["poll", "sweep", "supervise", "launch", "supervise", "launch"]
