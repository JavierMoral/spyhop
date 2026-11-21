from typing import Any, cast

from spyhop.domain.protocols import TerminalHost, WorktreeHost, ensure_ready


class Host:
    def __init__(self) -> None:
        self.asked = 0

    async def ensure_ready(self) -> None:
        self.asked += 1


async def test_each_host_is_asked_once_also_with_more_than_one_role() -> None:
    orca, tmux = Host(), Host()

    await ensure_ready(cast(Any, orca), cast(Any, orca))
    await ensure_ready(cast(WorktreeHost, orca), cast(TerminalHost, tmux))

    assert (orca.asked, tmux.asked) == (2, 1)
