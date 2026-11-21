"""The current values of the checkpoint inputs (see checkpoint_verdicts)."""

import asyncio
from collections.abc import Iterable
from pathlib import Path

from spyhop.application.checkpoint_verdicts import INPUT_VALUE
from spyhop.domain.exceptions import ConfigurationError
from spyhop.domain.protocols import TicketGateway
from spyhop.infrastructure.git import head, merge_base


async def input_values(
    names: Iterable[str],
    root: Path,
    base_branch: str,
    issue_key: str | None,
    tickets: TicketGateway | None,
) -> dict[str, str]:
    """The values of the inputs in ``names``. Raises ConfigurationError when
    one cannot be read."""

    wanted = set(names)
    values = {}

    if "head" in wanted:
        current_head = await asyncio.to_thread(head, root)

        if current_head is None:
            raise ConfigurationError(f"Cannot read the HEAD of {root}")

        values["head"] = current_head

    if "base" in wanted:
        base = await asyncio.to_thread(merge_base, root, base_branch)

        if base is None:
            raise ConfigurationError(f"Cannot find where HEAD leaves {base_branch} in {root}")

        values["base"] = base

    if "ticket" in wanted:
        if issue_key is None or tickets is None:
            raise ConfigurationError(
                "A checkpoint reads the ticket, so it needs the run of the current "
                "worktree, not --workflow"
            )

        issue = await tickets.issue(issue_key, ["updated"])
        updated = issue.get("fields", {}).get("updated")

        if not isinstance(updated, str) or not INPUT_VALUE.fullmatch(updated):
            raise ConfigurationError(f"{issue_key} has no valid updated field in JIRA")

        values["ticket"] = updated

    return values
