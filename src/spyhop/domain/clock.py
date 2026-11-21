"""The current time. A component that reads the time takes a Clock, so that
the tests can move the time."""

from collections.abc import Callable
from datetime import UTC, datetime

# What gives the current time. The daemon uses utc_now, and the tests a clock
# that they can move.
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)
