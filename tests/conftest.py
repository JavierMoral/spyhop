import pytest


@pytest.fixture(autouse=True)
def woken(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """The commands wake the Spyhop service of the machine. Tests record the
    wake instead."""

    calls: list[bool] = []

    monkeypatch.setattr("spyhop.cli.wake_service", lambda: calls.append(True))

    return calls
