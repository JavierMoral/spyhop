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

Verified against Orca 1.4.245; the captured answers are in tests/fixtures/orca.
"""

import json
import logging
import secrets
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from spyhop.domain.exceptions import (
    OrcaCommandError,
    OrcaUnavailableError,
    OrcaUncertainError,
)
from spyhop.domain.models import AgentRow, LaunchResult, OrcaWorktree
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
        """Creates the worktree without an agent, so that TASK.md and the
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

        # A lost launch reply comes back as an error carrying the operation id
        # to replay with.

        if uncertain and isinstance(failed.get("data"), dict) and failed["data"].get("operationId"):
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
    )


def to_agent_row(data: dict[str, Any]) -> AgentRow:
    return AgentRow(
        agent_type=data.get("agentType") or "",
        state=data.get("state") or "",
        pane_key=data.get("paneKey"),
        has_live_pty=bool(data.get("hasLivePty")),
        prompt=data.get("prompt"),
    )


def _short_branch(ref: str) -> str:
    return ref.removeprefix("refs/heads/")
