from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from spyhop.domain.models import (
    AgentRow,
    JiraUser,
    LaunchResult,
    OrcaTerminal,
    OrcaWorktree,
    RemoveResult,
)


class JiraGateway(Protocol):
    async def myself(self) -> JiraUser: ...

    async def search(
        self, jql: str, fields: list[str], changelog: bool = True
    ) -> list[dict[str, Any]]: ...

    async def changelog(self, issue_key: str) -> list[dict[str, Any]]: ...


class TicketGateway(Protocol):
    async def issue(self, issue_key: str, fields: list[str]) -> dict[str, Any]: ...


class WorktreeHost(Protocol):
    """Creates, finds and removes the worktrees of the runs."""

    async def ensure_ready(self) -> None: ...

    async def find_worktree_by_reference(self, issue_key: str) -> OrcaWorktree | None: ...

    async def worktree_base_path(self, repository_path: Path) -> Path | None: ...

    async def show_worktree(self, orca_worktree_id: str) -> OrcaWorktree: ...

    async def create_worktree(
        self,
        repository_path: Path,
        name: str,
        base_branch: str,
        reference: str,
        setup: str,
    ) -> OrcaWorktree: ...

    async def link_worktree(self, path: Path, reference: str) -> OrcaWorktree: ...

    async def remove_worktree(self, orca_worktree_id: str) -> RemoveResult: ...


class TerminalHost(Protocol):
    """Runs the agents and the supervisor sessions in terminals of a worktree."""

    async def ensure_ready(self) -> None: ...

    async def launch_agent(
        self,
        orca_worktree_id: str,
        prompt: str,
        operation_id: str,
        model: str | None,
        effort: str | None,
    ) -> LaunchResult: ...

    async def worktree_agents(self) -> dict[str, list[AgentRow]]: ...

    async def close_terminals(self, orca_worktree_id: str) -> int: ...

    async def create_terminal(
        self, orca_worktree_id: str, title: str, command: str
    ) -> OrcaTerminal: ...

    async def send_prompt(self, terminal: str, prompt: str) -> None: ...

    async def close_terminal(self, terminal: str) -> None: ...

    async def rename_terminal(self, terminal: str, title: str) -> None: ...


class BoardMirror(Protocol):
    """Shows the progress of the runs on a board. Spyhop does not read the
    board: the card of a run from before `spyhop declare` comes from
    WorktreeHost.show_worktree. A failure of the board is only logged, so the
    board needs no readiness check."""

    async def mark_card(self, orca_worktree_id: str, comment: str) -> None: ...

    async def set_card_status(self, orca_worktree_id: str, status: str) -> None: ...


async def ensure_ready(*hosts: WorktreeHost | TerminalHost) -> None:
    """Makes sure that each host is ready. A host that has more than one role,
    such as Orca, is asked once."""

    asked: list[object] = []

    for host in hosts:
        if not any(host is other for other in asked):
            asked.append(host)

            await host.ensure_ready()


class UnitGateway(Protocol):
    """Runs commands outside the Spyhop service, so that a restart of the
    service does not stop them."""

    async def start(self, unit: str, command: Sequence[str], cwd: Path, log: Path) -> None: ...

    async def stop(self, pattern: str) -> None: ...
