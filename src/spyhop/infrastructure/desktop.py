"""Desktop notifications, for what Spyhop cannot tell a supervisor session:
a run that has no worktree yet."""

import logging

from spyhop.infrastructure.process.command_runner import (
    CommandRunner,
    CommandTimeoutError,
    SubprocessRunner,
)

_log = logging.getLogger(__name__)

TIMEOUT = 5


async def notify(title: str, body: str, runner: CommandRunner | None = None) -> None:
    """Shows a notification with notify-send. A machine without it, or
    without a desktop session, only gets a debug log."""

    try:
        result = await (runner or SubprocessRunner()).run(
            ["notify-send", "--app-name=Spyhop", title, body], TIMEOUT
        )
    except (CommandTimeoutError, OSError) as error:
        _log.debug("no desktop notification: %s", error)

        return

    if result.exit_code != 0:
        _log.debug("no desktop notification: %s", result.stderr.strip())
