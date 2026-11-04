from pathlib import Path
from typing import Any, Protocol

from spyhop.domain.models import (
    AgentRow,
    JiraUser,
    LaunchResult,
    OrcaWorktree,
    PromptDelivery,
    RemoveResult,
    TerminalRow,
)


class JiraGateway(Protocol):
    async def myself(self) -> JiraUser: ...

    async def search(
        self, jql: str, fields: list[str], changelog: bool = True
    ) -> list[dict[str, Any]]: ...

    async def changelog(self, issue_key: str) -> list[dict[str, Any]]: ...


class TicketGateway(Protocol):
    async def issue(self, issue_key: str, fields: list[str]) -> dict[str, Any]: ...


class OrcaGateway(Protocol):
    @property
    def executable(self) -> Path: ...

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

    async def launch_agent(
        self,
        orca_worktree_id: str,
        prompt: str,
        operation_id: str,
        model: str | None,
        effort: str | None,
    ) -> LaunchResult: ...

    async def worktree_agents(self) -> dict[str, list[AgentRow]]: ...

    async def terminals(self, orca_worktree_id: str) -> list[TerminalRow]: ...

    async def send_prompt(
        self, terminal_handle: str, text: str, request_id: str, wait_submit: float = 15
    ) -> PromptDelivery: ...

    async def close_terminals(self, orca_worktree_id: str) -> int: ...

    async def remove_worktree(self, orca_worktree_id: str) -> RemoveResult: ...

    async def mark_card(self, orca_worktree_id: str, comment: str) -> None: ...
