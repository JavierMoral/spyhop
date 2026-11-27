"""The daemon loop: a wake supervises and launches now, and JIRA keeps its interval."""

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from spyhop import daemon
from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.domain.exceptions import JiraRateLimitedError
from spyhop.domain.models import JiraUser
from spyhop.infrastructure.persistence.database import connect
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
        None,
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


class Watcher:
    """Records its call. It can fail, as a bug of the watcher would."""

    def __init__(self, calls: list[str], error: Exception | None = None) -> None:
        self._calls = calls
        self._error = error

    async def watch(self) -> None:
        self._calls.append("watch")

        if self._error is not None:
            raise self._error


async def run_with_watcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    watcher: Watcher | None,
    wakes: int = 1,
    dry_run: bool = False,
    watch_interval: int = 60,
) -> list[str]:
    calls: list[str] = []
    cycles: asyncio.Queue[str] = asyncio.Queue()

    class Monitor:
        def __init__(self, **_: Any) -> None:
            self.poll = Step("poll", calls, cycles)

        def record_error(self, message: str) -> None:
            pass

    class Agent:
        def __init__(self, method: str, name: str) -> None:
            setattr(self, method, Step(name, calls, cycles))

    agents = (
        Agent("sweep", "sweep"),
        Agent("supervise", "supervise"),
        Agent("launch_pending", "launch"),
        watcher,
    )

    def build(*_: Any) -> Any:
        calls.append("agents")

        return agents

    if watcher is not None:
        watcher._calls = calls

    monkeypatch.setenv("INVOCATION_ID", "test")
    monkeypatch.setattr(daemon, "JiraClient", Jira)
    monkeypatch.setattr(daemon, "credentials_from_environment", lambda _: None)
    monkeypatch.setattr(daemon, "JiraMonitor", Monitor)
    monkeypatch.setattr(daemon, "_agents", build)

    task = asyncio.create_task(
        daemon.run_daemon(
            settings(
                tmp_path,
                application={"dry_run": dry_run, "polling_interval": 60},
                pull_requests={"enabled": True, "polling_interval": watch_interval},
            ),
            database=tmp_path / "spyhop.db",
            once=dry_run,
        )
    )

    if dry_run:
        await asyncio.wait_for(task, 5)

        return calls

    await asyncio.wait_for(cycles.get(), 5)

    for _ in range(wakes):
        os.kill(os.getpid(), WAKE_SIGNAL)

        await asyncio.wait_for(cycles.get(), 5)

    os.kill(os.getpid(), signal.SIGTERM)

    await asyncio.wait_for(task, 5)

    return calls


async def test_the_watcher_runs_once_for_each_interval_also_when_a_wake_comes_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = await run_with_watcher(tmp_path, monkeypatch, Watcher([]), wakes=2)

    assert calls.count("watch") == 1
    assert calls.count("supervise") == 3


async def test_a_failure_of_the_watcher_does_not_stop_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = await run_with_watcher(tmp_path, monkeypatch, Watcher([], RuntimeError("bug")))

    assert calls.count("supervise") == 2
    assert "Pull request watch failed" in caplog.text


async def test_without_a_watcher_nothing_watches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "watch" not in await run_with_watcher(tmp_path, monkeypatch, None)


async def test_in_dry_run_the_agents_and_the_watcher_do_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = await run_with_watcher(tmp_path, monkeypatch, Watcher([]), dry_run=True)

    assert "agents" not in calls and "watch" not in calls


def test_the_daemon_builds_the_watcher_only_when_it_is_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Orca:
        executable = Path("/opt/orca")

    monkeypatch.setattr(daemon, "orca_client", lambda _: Orca())

    for enabled in (False, True):
        config = settings(tmp_path, pull_requests={"enabled": enabled})
        built = daemon._agents(
            config,
            connect(tmp_path / f"{enabled}.db"),
            WorkflowResolver(config),
            None,  # type: ignore[arg-type]
        )

        assert (built[3] is not None) is enabled
