"""Orca CLI client.

Every command runs with ``--json``. Orca answers ``{"ok": true, "result": …}``
or ``{"ok": false, "error": {"code", "message", "data"}}`` on stdout, with exit
code 1 for the latter. Commands are argument lists, never shell strings, and
always run from the same directory, which Orca requires to replay a launch.

Launch idempotency: an operation id has the form ``<epoch ms>-<32 hex>`` and the
caller may choose it. Spyhop generates it, stores it, and passes it as
``--retry-launch`` on the first attempt too. Orca starts a new launch for an id
it has never seen and replays the accepted one for an id it has, so repeating
the same command after a lost answer, a timeout or a Spyhop restart never
starts a second agent.

Prompts sent to a running agent follow the same pattern with ``terminal send
--retry-request <uuid>``: a new id sends, a known id replays the accepted send,
and a known id with other text is refused with ``request_mismatch``.

Verified against Orca 1.4.245; the captured answers are in tests/fixtures/orca.
"""

import json
import logging
import secrets
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spyhop.domain.exceptions import (
    OrcaCommandError,
    OrcaUnavailableError,
    OrcaUncertainError,
)
from spyhop.domain.models import (
    AgentRow,
    LaunchResult,
    OrcaWorktree,
    PromptDelivery,
    RemoveResult,
    TerminalRow,
)
from spyhop.infrastructure.process.command_runner import (
    CommandRunner,
    CommandTimeoutError,
    SubprocessRunner,
)

_log = logging.getLogger(__name__)

AGENT = "claude"

# /goal and the permission profile were verified in a terminal agent; a chat
# surface would depend on the user's Chat UI setting.

SURFACE = "terminal"

_UNAVAILABLE_CODES = {"runtime_unavailable", "runtime_not_running", "app_not_running"}

_DEFINITE_CODES = {
    "terminal_handle_stale",
    "request_mismatch",
    "agent_session_operation_conflict",
    "selector_not_found",
    "invalid_argument",
}


def new_operation_id() -> str:
    return f"{int(time.time() * 1000)}-{secrets.token_hex(16)}"


class OrcaClient:
    def __init__(
        self,
        executable: Path,
        cwd: Path,
        startup_timeout: float,
        operation_timeout: float,
        runner: CommandRunner | None = None,
    ) -> None:
        self._executable = executable
        self._cwd = cwd
        self._startup_timeout = startup_timeout
        self._operation_timeout = operation_timeout
        self._runner = runner or SubprocessRunner()

    @property
    def executable(self) -> Path:
        return self._executable

    async def ensure_ready(self) -> None:
        result = await self._call(["status"], self._startup_timeout, uncertain=False)
        runtime = result.get("runtime") or {}

        if not runtime.get("reachable", runtime.get("state") == "ready"):
            raise OrcaUnavailableError(
                f"Orca runtime is not reachable (state {runtime.get('state')!r})"
            )

    async def find_worktree_by_reference(self, issue_key: str) -> OrcaWorktree | None:
        result = await self._call(
            ["reference", "find", issue_key], self._operation_timeout, uncertain=False
        )
        workspaces = [
            match["workspace"]
            for match in result.get("matches", [])
            if (match.get("workspace") or {}).get("kind") == "worktree"
            and not match["workspace"].get("archived")
        ]
        ids = sorted({workspace["id"] for workspace in workspaces})

        if not ids:
            return None

        if len(ids) > 1:
            raise OrcaCommandError(
                "ambiguous_reference",
                f"{issue_key} is linked to {len(ids)} worktrees: {', '.join(ids)}",
            )

        return await self.show_worktree(ids[0])

    async def worktree_base_path(self, repository_path: Path) -> Path | None:
        """Where Orca creates the repository's worktrees, when configured."""

        result = await self._call(
            ["repo", "show", "--repo", f"path:{repository_path}"],
            self._operation_timeout,
            uncertain=False,
        )
        base = (result.get("repo") or {}).get("worktreeBasePath")

        return Path(base) if base else None

    async def show_worktree(self, orca_worktree_id: str) -> OrcaWorktree:
        result = await self._call(
            ["worktree", "show", "--worktree", f"id:{orca_worktree_id}"],
            self._operation_timeout,
            uncertain=False,
        )

        return to_orca_worktree(result["worktree"])

    async def create_worktree(
        self,
        repository_path: Path,
        name: str,
        base_branch: str,
        reference: str,
        setup: str,
    ) -> OrcaWorktree:
        """Creates the worktree without an agent, so that CLAUDE-TASK.md and the
        permission profile are in place before the agent starts."""

        args = [
            "worktree",
            "create",
            "--repo",
            f"path:{repository_path}",
            "--name",
            name,
            "--no-parent",
            "--base-branch",
            base_branch,
            "--reference",
            reference,
        ]

        if setup != "inherit":
            args += ["--setup", setup]

        result = await self._call(args, self._operation_timeout, uncertain=True)

        return to_orca_worktree(result["worktree"])

    async def link_worktree(self, path: Path, reference: str) -> OrcaWorktree:
        """Links a worktree created outside Orca to the ticket. Orca lists such
        a worktree once it has seen it in Git; until then this is refused with
        ``selector_not_found``. Existing links are kept, so repeating it is
        harmless."""

        selector = f"path:{path}"

        await self._call(
            ["reference", "add", reference, "--worktree", selector],
            self._operation_timeout,
            uncertain=True,
        )
        result = await self._call(
            ["worktree", "show", "--worktree", selector],
            self._operation_timeout,
            uncertain=False,
        )

        return to_orca_worktree(result["worktree"])

    async def launch_agent(
        self,
        orca_worktree_id: str,
        prompt: str,
        operation_id: str,
        model: str | None,
        effort: str | None,
    ) -> LaunchResult:
        args = [
            "agent",
            "launch",
            "--agent",
            AGENT,
            "--worktree",
            f"id:{orca_worktree_id}",
            "--surface",
            SURFACE,
        ]

        if model:
            args += ["--model", model]

            if effort:
                args += ["--effort", effort]

        args += ["--prompt", prompt, "--retry-launch", operation_id]

        result = await self._call(args, self._operation_timeout, uncertain=True)

        return to_launch_result(result, operation_id)

    async def worktree_agents(self) -> dict[str, list[AgentRow]]:
        result = await self._call(["worktree", "ps"], self._operation_timeout, uncertain=False)

        return {
            row["worktreeId"]: [to_agent_row(agent) for agent in row.get("agents", [])]
            for row in result.get("worktrees", [])
        }

    async def terminals(self, orca_worktree_id: str) -> list[TerminalRow]:
        result = await self._call(
            ["terminal", "list", "--worktree", f"id:{orca_worktree_id}"],
            self._operation_timeout,
            uncertain=False,
        )

        return [to_terminal_row(terminal) for terminal in result.get("terminals", [])]

    async def send_prompt(
        self, terminal_handle: str, text: str, request_id: str, wait_submit: float = 15
    ) -> PromptDelivery:
        """Types ``text`` into the agent's terminal and submits it. Repeating
        the call with the same request id never submits twice."""

        result = await self._call(
            [
                "terminal",
                "send",
                "--terminal",
                terminal_handle,
                "--text",
                text,
                "--enter",
                "--retry-request",
                request_id,
                "--wait-submit",
                f"{wait_submit:g}",
            ],
            self._operation_timeout + wait_submit,
            uncertain=True,
        )
        prompt = (result.get("send") or {}).get("prompt") or {}

        return PromptDelivery(
            request_id=prompt.get("requestId") or request_id,
            replayed=bool((result.get("mutation") or {}).get("replayed")),
            stages=tuple(prompt.get("stages") or ()),
        )

    async def close_terminals(self, orca_worktree_id: str) -> int:
        """Stops the worktree's agents and closes its terminals."""

        result = await self._call(
            ["terminal", "close", "--worktree", f"id:{orca_worktree_id}", "--all"],
            self._operation_timeout,
            uncertain=True,
        )

        return int(result.get("closed") or 0)

    async def remove_worktree(self, orca_worktree_id: str) -> RemoveResult:
        """Never forced: a worktree with uncommitted changes is refused with
        ``runtime_error`` and the offending files in the message. Orca deletes
        the branch only when it is merged."""

        result = await self._call(
            ["worktree", "rm", "--worktree", f"id:{orca_worktree_id}"],
            self._operation_timeout,
            uncertain=True,
        )

        return RemoveResult(
            preserved_branch=(result.get("preservedBranch") or {}).get("branchName")
        )

    async def mark_card(self, orca_worktree_id: str, comment: str) -> None:
        """Comments on the worktree's card and marks it unread."""

        await self._call(
            [
                "worktree",
                "set",
                "--worktree",
                f"id:{orca_worktree_id}",
                "--comment",
                comment,
                "--unread",
            ],
            self._operation_timeout,
            uncertain=False,
        )

    async def _call(
        self, args: Sequence[str], time_limit: float, uncertain: bool
    ) -> dict[str, Any]:
        """``uncertain`` marks commands with side effects: when their outcome
        cannot be read, they raise OrcaUncertainError instead of
        OrcaUnavailableError."""

        command = [str(self._executable), *args, "--json"]
        name = " ".join(args[:2])
        failure = OrcaUncertainError if uncertain else OrcaUnavailableError

        try:
            completed = await self._runner.run(command, time_limit, cwd=self._cwd)
        except CommandTimeoutError as error:
            raise failure(f"`orca {name}` timed out: {error}") from None
        except OSError as error:
            raise OrcaUnavailableError(f"cannot run {self._executable}: {error}") from None

        try:
            payload = json.loads(completed.stdout)
        except ValueError:
            detail = (completed.stderr or completed.stdout).strip()[:300]

            raise failure(
                f"`orca {name}` exited with {completed.exit_code} without JSON: {detail}"
            ) from None

        if not isinstance(payload, dict):
            raise failure(f"`orca {name}` returned an unexpected payload")

        if payload.get("ok"):
            result = payload.get("result")

            if not isinstance(result, dict):
                raise failure(f"`orca {name}` returned no result")

            for warning in result.get("warnings", []) or []:
                _log.warning("orca %s: %s", name, warning.get("message", warning))

            return result

        failed = payload.get("error") or {}
        code = str(failed.get("code") or "unknown_error")
        message = str(failed.get("message") or code)

        if code in _UNAVAILABLE_CODES:
            raise OrcaUnavailableError(f"`orca {name}`: {message}")

        # A lost reply comes back as an error carrying the id to replay with.
        # A stale handle or a reused id are definite refusals, not lost replies.

        data = failed.get("data")

        if (
            uncertain
            and code not in _DEFINITE_CODES
            and isinstance(data, dict)
            and (data.get("operationId") or data.get("orchestrationRequestId"))
        ):
            raise OrcaUncertainError(f"`orca {name}`: {message}")

        raise OrcaCommandError(code, message, failed.get("data"))


def to_orca_worktree(data: dict[str, Any]) -> OrcaWorktree:
    return OrcaWorktree(
        id=data["id"],
        path=Path(data["path"]),
        branch=_short_branch(data.get("branch") or ""),
        base_ref=data.get("baseRef"),
        workspace_status=data.get("workspaceStatus"),
        comment=data.get("comment") or "",
        unread=bool(data.get("isUnread", data.get("unread", False))),
    )


def to_launch_result(result: dict[str, Any], operation_id: str) -> LaunchResult:
    # `agent launch` answers with the launch itself; `worktree create --agent`
    # nests it under "launch".

    launch = result.get("launch", result)
    outcome = launch.get("outcome") or {}
    handle = outcome.get("handle") or result.get("agentTerminalHandle")

    if not handle:
        raise OrcaUncertainError("Orca accepted the launch but returned no agent handle")

    return LaunchResult(
        agent_handle=handle,
        operation_id=launch.get("operationId") or operation_id,
        surface=outcome.get("kind") or SURFACE,
        prompt_outcome=(launch.get("prompt") or {}).get("outcome"),
        pane_key=outcome.get("paneKey"),
    )


def to_agent_row(data: dict[str, Any]) -> AgentRow:
    return AgentRow(
        agent_type=data.get("agentType") or "",
        state=data.get("state") or "",
        pane_key=data.get("paneKey"),
        has_live_pty=bool(data.get("hasLivePty")),
        prompt=data.get("prompt"),
        state_started_at=_from_epoch_ms(data.get("stateStartedAt")),
    )


def to_terminal_row(data: dict[str, Any]) -> TerminalRow:
    # An agent's paneKey is "<tabId>:<leafId>" of the terminal it runs in.

    tab, leaf = data.get("tabId"), data.get("leafId")

    return TerminalRow(
        handle=data["handle"],
        pane_key=f"{tab}:{leaf}" if tab and leaf else None,
        title=data.get("title") or "",
    )


def _from_epoch_ms(value: Any) -> datetime | None:
    if not isinstance(value, int | float) or value <= 0:
        return None

    return datetime.fromtimestamp(value / 1000, UTC)


def _short_branch(ref: str) -> str:
    return ref.removeprefix("refs/heads/")
