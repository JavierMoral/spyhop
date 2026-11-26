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
from spyhop.domain.models import OrcaTerminal
from spyhop.infrastructure.orca.client import OrcaClient
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


async def test_set_display_name_sets_the_orca_name_of_the_worktree() -> None:
    runner = FakeRunner('{"ok": true, "result": {}}')

    await client(runner).set_display_name(WORKTREE_ID, "PROJ-123 Fix the import")

    assert runner.calls[0][0][1:] == [
        "worktree",
        "set",
        "--worktree",
        f"id:{WORKTREE_ID}",
        "--display-name",
        "PROJ-123 Fix the import",
        "--json",
    ]


async def test_create_worktree_inherits_the_setup_policy_by_default() -> None:
    runner = FakeRunner(fixture("worktree_create.json"))

    await client(runner).create_worktree(Path("/r"), "PROJ-123", "master", "PROJ-123", "inherit")

    assert "--setup" not in runner.calls[0][0]


@pytest.mark.parametrize(
    "answer",
    [
        CommandTimeoutError(120),
        CommandResult(exit_code=1, stdout="", stderr="socket hang up"),
        json.dumps({"ok": False, "error": {"code": "x", "data": {"operationId": "1-a"}}}),
    ],
)
async def test_an_unreadable_prompt_outcome_is_uncertain(answer: object) -> None:
    runner = FakeRunner(answer)  # type: ignore[arg-type]

    with pytest.raises(OrcaUncertainError):
        await client(runner).send_prompt("term_1", "hello")


@pytest.mark.parametrize("code", ["terminal_handle_stale", "terminal_not_writable"])
async def test_a_stale_or_closed_terminal_is_a_command_error(code: str) -> None:
    error = {"code": code, "data": {"orchestrationRequestId": "1-a"}}
    runner = FakeRunner(json.dumps({"ok": False, "error": error}))

    with pytest.raises(OrcaCommandError) as raised:
        await client(runner).send_prompt("term_1", "hello")

    assert raised.value.code == code


async def test_read_command_timeout_means_unavailable() -> None:
    with pytest.raises(OrcaUnavailableError):
        await client(FakeRunner(CommandTimeoutError(120))).show_worktree(WORKTREE_ID)


async def test_unknown_selector_is_reported_with_its_code() -> None:
    runner = FakeRunner(fixture("selector_not_found.json"))

    with pytest.raises(OrcaCommandError) as raised:
        await client(runner).show_worktree("nope::/x")

    assert raised.value.code == "selector_not_found"


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


async def test_set_card_status_moves_the_card() -> None:
    runner = FakeRunner(fixture("worktree_set.json"))

    await client(runner).set_card_status(WORKTREE_ID, "in-progress")

    assert runner.calls[0][0][1:] == [
        "worktree",
        "set",
        "--worktree",
        f"id:{WORKTREE_ID}",
        "--workspace-status",
        "in-progress",
        "--json",
    ]


async def test_create_terminal_runs_the_command_in_the_worktree() -> None:
    runner = FakeRunner(
        json.dumps({"ok": True, "result": {"terminal": {"handle": "term_9", "paneKey": "tab:p1"}}})
    )

    terminal = await client(runner).create_terminal("repo::/wt/LPD-1", "checkpoint x", "spyhop x")

    assert terminal == OrcaTerminal("term_9", "tab:p1")
    assert runner.calls[0][0] == [
        str(ORCA),
        "terminal",
        "create",
        "--worktree",
        "id:repo::/wt/LPD-1",
        "--title",
        "checkpoint x",
        "--command",
        "spyhop x",
        "--json",
    ]


async def test_create_terminal_without_a_handle_is_not_an_error() -> None:
    runner = FakeRunner(json.dumps({"ok": True, "result": {}}))

    assert await client(runner).create_terminal("repo::/wt", "t", "c") == OrcaTerminal(None, None)


async def test_send_prompt_submits_the_text_to_the_terminal() -> None:
    runner = FakeRunner(json.dumps({"ok": True, "result": {}}))

    await client(runner).send_prompt("term_1", "Fix it.")

    assert runner.calls[0][0] == [
        str(ORCA),
        "terminal",
        "send",
        "--terminal",
        "term_1",
        "--text",
        "Fix it.",
        "--enter",
        "--json",
    ]


async def test_close_terminal_closes_one_terminal() -> None:
    runner = FakeRunner(json.dumps({"ok": True, "result": {}}))

    await client(runner).close_terminal("term_1")

    assert runner.calls[0][0] == [str(ORCA), "terminal", "close", "--terminal", "term_1", "--json"]


async def test_warnings_are_logged_as_objects_or_strings(caplog: pytest.LogCaptureFixture) -> None:
    answer = json.dumps({"ok": True, "result": {"warnings": [{"message": "first"}, "second"]}})

    await client(FakeRunner(answer)).send_prompt("term_1", "hello")

    assert "orca terminal send: first" in caplog.text
    assert "orca terminal send: second" in caplog.text


async def test_the_warning_of_a_prompt_without_a_wait_is_only_a_debug_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    warning = "input was accepted, but no turn start was observed yet"
    answer = json.dumps({"ok": True, "result": {"warnings": [warning]}})

    caplog.set_level("DEBUG")

    await client(FakeRunner(answer)).send_prompt("term_1", "hello")

    [record] = [record for record in caplog.records if warning in record.getMessage()]

    assert record.levelname == "DEBUG"
