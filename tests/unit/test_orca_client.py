"""OrcaClient against answers captured from Orca 1.4.245 (tests/fixtures/orca)."""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from spyhop.domain.exceptions import (
    OrcaCommandError,
    OrcaUnavailableError,
    OrcaUncertainError,
)
from spyhop.infrastructure.orca.client import OrcaClient, new_operation_id
from spyhop.infrastructure.process.command_runner import CommandResult, CommandTimeoutError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "orca"
ORCA = Path("/opt/orca/orca-ide")
WORKTREE_ID = "bc8cbd3b-6629-4670-8f86-e580f76aaae4::/home/user/git/project/wt/PROJ-123"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class FakeRunner:
    def __init__(self, *answers: str | CommandResult | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[list[str], float, Path | None]] = []

    async def run(
        self, args: Sequence[str], time_limit: float, cwd: Path | None = None
    ) -> CommandResult:
        self.calls.append((list(args), time_limit, cwd))

        answer = self.answers.pop(0)

        if isinstance(answer, Exception):
            raise answer

        if isinstance(answer, CommandResult):
            return answer

        ok = json.loads(answer).get("ok", False)

        return CommandResult(exit_code=0 if ok else 1, stdout=answer, stderr="")


def client(runner: FakeRunner) -> OrcaClient:
    return OrcaClient(ORCA, Path("/state"), 30, 120, runner=runner)


def status(reachable: bool) -> str:
    return json.dumps(
        {"ok": True, "result": {"runtime": {"state": "ready", "reachable": reachable}}}
    )


async def test_ensure_ready_checks_the_runtime() -> None:
    runner = FakeRunner(status(True), status(False))

    await client(runner).ensure_ready()

    with pytest.raises(OrcaUnavailableError):
        await client(runner).ensure_ready()

    args, time_limit, cwd = runner.calls[0]

    assert args == [str(ORCA), "status", "--json"]
    assert time_limit == 30
    assert cwd == Path("/state")


async def test_find_worktree_by_reference_shows_the_linked_worktree() -> None:
    runner = FakeRunner(fixture("reference_find.json"), fixture("worktree_show.json"))

    worktree = await client(runner).find_worktree_by_reference("PROJ-123")

    assert worktree is not None
    assert worktree.id == WORKTREE_ID
    assert worktree.path == Path("/home/user/git/project/wt/PROJ-123")
    assert worktree.branch == "jdoe/PROJ-123"
    assert worktree.workspace_status == "in-progress"
    assert runner.calls[0][0][1:] == ["reference", "find", "PROJ-123", "--json"]
    assert runner.calls[1][0][1:] == [
        "worktree",
        "show",
        "--worktree",
        f"id:{WORKTREE_ID}",
        "--json",
    ]


async def test_find_worktree_by_reference_without_matches() -> None:
    runner = FakeRunner(fixture("reference_find_empty.json"))

    assert await client(runner).find_worktree_by_reference("PROJ-999") is None


async def test_find_worktree_by_reference_refuses_ambiguous_links() -> None:
    answer = json.loads(fixture("reference_find.json"))
    match = answer["result"]["matches"][0]
    other = json.loads(json.dumps(match))

    other["workspace"]["id"] = "repo::/elsewhere"
    answer["result"]["matches"].append(other)

    with pytest.raises(OrcaCommandError, match="2 worktrees"):
        await client(FakeRunner(json.dumps(answer))).find_worktree_by_reference("PROJ-123")


async def test_create_worktree_links_the_ticket_without_an_agent() -> None:
    runner = FakeRunner(fixture("worktree_create.json"))

    worktree = await client(runner).create_worktree(
        Path("/home/user/git/project/portal"), "PROJ-123", "master", "PROJ-123", "skip"
    )

    assert worktree.id == WORKTREE_ID
    assert worktree.branch == "jdoe/PROJ-123"
    assert worktree.base_ref == "master"
    assert runner.calls[0][0][1:] == [
        "worktree",
        "create",
        "--repo",
        "path:/home/user/git/project/portal",
        "--name",
        "PROJ-123",
        "--no-parent",
        "--base-branch",
        "master",
        "--reference",
        "PROJ-123",
        "--setup",
        "skip",
        "--json",
    ]


async def test_link_worktree_adds_the_reference_and_shows_the_worktree() -> None:
    runner = FakeRunner('{"ok": true, "result": {}}', fixture("worktree_show.json"))
    path = Path("/home/user/git/project/wt/PROJ-123")

    worktree = await client(runner).link_worktree(path, "PROJ-123")

    assert worktree.id == WORKTREE_ID
    assert [call[0][1:] for call in runner.calls] == [
        ["reference", "add", "PROJ-123", "--worktree", f"path:{path}", "--json"],
        ["worktree", "show", "--worktree", f"path:{path}", "--json"],
    ]


async def test_create_worktree_inherits_the_setup_policy_by_default() -> None:
    runner = FakeRunner(fixture("worktree_create.json"))

    await client(runner).create_worktree(Path("/r"), "PROJ-123", "master", "PROJ-123", "inherit")

    assert "--setup" not in runner.calls[0][0]


async def test_launch_agent_always_passes_its_operation_id() -> None:
    runner = FakeRunner(fixture("agent_launch.json"))
    operation_id = "1793555543449-a813c226561c4e8691e50c39dbf32c98"

    result = await client(runner).launch_agent(
        WORKTREE_ID, "/goal do it", operation_id, "sonnet", "high"
    )

    assert result.agent_handle == "term_bbd3213b-b48b-40d9-92c4-f3140c82d6ae"
    assert result.operation_id == operation_id
    assert result.surface == "terminal"
    assert result.prompt_outcome == "handed-to-terminal"
    assert runner.calls[0][0][1:] == [
        "agent",
        "launch",
        "--agent",
        "claude",
        "--worktree",
        f"id:{WORKTREE_ID}",
        "--surface",
        "terminal",
        "--model",
        "sonnet",
        "--effort",
        "high",
        "--prompt",
        "/goal do it",
        "--retry-launch",
        operation_id,
        "--json",
    ]


async def test_launch_agent_reads_the_launch_nested_in_worktree_create() -> None:
    runner = FakeRunner(fixture("worktree_create.json"))

    result = await client(runner).launch_agent(WORKTREE_ID, "/goal", "1-a", None, None)

    assert result.agent_handle == "term_7630b42d-cd7b-43fa-bf26-0095e1dc414e"
    assert "--model" not in runner.calls[0][0]


async def test_refused_launch_is_a_command_error() -> None:
    runner = FakeRunner(fixture("agent_launch_conflict.json"))

    with pytest.raises(OrcaCommandError) as raised:
        await client(runner).launch_agent(WORKTREE_ID, "/goal", "1-a", None, None)

    assert raised.value.code == "agent_session_operation_conflict"


@pytest.mark.parametrize(
    "answer",
    [
        CommandTimeoutError(120),
        CommandResult(exit_code=1, stdout="", stderr="socket hang up"),
        json.dumps({"ok": False, "error": {"code": "x", "data": {"operationId": "1-a"}}}),
    ],
)
async def test_unreadable_launch_outcome_is_uncertain(answer: object) -> None:
    runner = FakeRunner(answer)  # type: ignore[arg-type]

    with pytest.raises(OrcaUncertainError):
        await client(runner).launch_agent(WORKTREE_ID, "/goal", "1-a", None, None)


async def test_read_command_timeout_means_unavailable() -> None:
    with pytest.raises(OrcaUnavailableError):
        await client(FakeRunner(CommandTimeoutError(120))).worktree_agents()


async def test_unknown_selector_is_reported_with_its_code() -> None:
    runner = FakeRunner(fixture("selector_not_found.json"))

    with pytest.raises(OrcaCommandError) as raised:
        await client(runner).show_worktree("nope::/x")

    assert raised.value.code == "selector_not_found"


async def test_worktree_agents_groups_agents_by_worktree() -> None:
    agents = await client(FakeRunner(fixture("worktree_ps.json"))).worktree_agents()

    [agent] = agents[WORKTREE_ID]

    assert agent.agent_type == "claude"
    assert agent.state == "done"
    assert agent.has_live_pty
    assert agent.prompt is not None and agent.prompt.startswith("/goal ")
    assert agents["bc8cbd3b-6629-4670-8f86-e580f76aaae4::/home/user/git/project/portal"] == []


def test_operation_ids_follow_orca_format() -> None:
    first, second = new_operation_id(), new_operation_id()

    milliseconds, random_part = first.split("-")

    assert milliseconds.isdigit() and len(milliseconds) == 13
    assert len(random_part) == 32 and int(random_part, 16) >= 0
    assert first != second


async def test_launch_agent_returns_the_agents_pane() -> None:
    runner = FakeRunner(fixture("agent_launch.json"))

    result = await client(runner).launch_agent(WORKTREE_ID, "/goal x", "1-a", None, None)

    assert result.pane_key is not None and ":" in result.pane_key


async def test_agent_rows_carry_when_their_state_started() -> None:
    runner = FakeRunner(fixture("worktree_ps_waiting.json"))

    [agent] = (await client(runner).worktree_agents())[WORKTREE_ID]

    assert agent.state == "waiting"
    assert agent.state_started_at is not None
    assert agent.state_started_at.year == 2026


async def test_close_terminals_stops_every_agent_of_the_worktree() -> None:
    runner = FakeRunner(fixture("terminal_close.json"))

    assert await client(runner).close_terminals(WORKTREE_ID) == 4
    assert runner.calls[0][0][1:] == [
        "terminal",
        "close",
        "--worktree",
        f"id:{WORKTREE_ID}",
        "--all",
        "--json",
    ]


async def test_remove_worktree_is_never_forced() -> None:
    runner = FakeRunner(fixture("worktree_rm.json"), fixture("worktree_rm_unmerged.json"))
    orca = client(runner)

    merged = await orca.remove_worktree(WORKTREE_ID)
    unmerged = await orca.remove_worktree(WORKTREE_ID)

    assert runner.calls[0][0][1:] == ["worktree", "rm", "--worktree", f"id:{WORKTREE_ID}", "--json"]
    assert merged.preserved_branch is None
    assert unmerged.preserved_branch == "jdoe/PROJ-123"


async def test_worktree_with_changes_is_refused() -> None:
    runner = FakeRunner(fixture("worktree_rm_dirty.json"))

    with pytest.raises(OrcaCommandError) as raised:
        await client(runner).remove_worktree(WORKTREE_ID)

    assert raised.value.code == "runtime_error"
    assert "??" in raised.value.message


async def test_mark_card_comments_and_marks_unread() -> None:
    runner = FakeRunner(fixture("worktree_set.json"))

    await client(runner).mark_card(WORKTREE_ID, "PROJ-123: no se pudo borrar")

    assert runner.calls[0][0][1:] == [
        "worktree",
        "set",
        "--worktree",
        f"id:{WORKTREE_ID}",
        "--comment",
        "PROJ-123: no se pudo borrar",
        "--unread",
        "--json",
    ]
