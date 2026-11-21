import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from spyhop.cli import app
from spyhop.infrastructure.agent_state import pane_key, read_state, state_of, write_state

QUESTION = {
    "tool_name": "AskUserQuestion",
    "tool_input": {"questions": [{"question": "A or B?"}, {"question": "Now?"}]},
}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"hook_event_name": "UserPromptSubmit", "prompt": "go"}, ("working", None)),
        ({"hook_event_name": "PreToolUse", **QUESTION}, ("waiting", "A or B? / Now?")),
        ({"hook_event_name": "PostToolUse", **QUESTION}, ("working", None)),
        ({"hook_event_name": "PreToolUse", "tool_name": "Bash"}, None),
        (
            {
                "hook_event_name": "Notification",
                "notification_type": "permission_prompt",
                "message": "Claude needs your permission",
            },
            ("blocked", "Claude needs your permission"),
        ),
        ({"hook_event_name": "Notification", "notification_type": "idle_prompt"}, None),
        ({"hook_event_name": "Stop", "last_assistant_message": "Done."}, ("done", None)),
        ({"hook_event_name": "SessionEnd", "reason": "other"}, ("ended", None)),
    ],
)
def test_each_hook_event_gives_a_state(
    payload: dict[str, Any], expected: tuple[str, str | None] | None
) -> None:
    assert state_of(payload) == expected


def test_the_pane_comes_from_spyhop_first_and_then_orca() -> None:
    assert pane_key({"ORCA_PANE_KEY": "tab:leaf"}) == "tab:leaf"
    assert pane_key({"SPYHOP_PANE_KEY": "mine", "ORCA_PANE_KEY": "tab:leaf"}) == "mine"
    assert pane_key({}) is None


def test_a_written_state_is_read_back(tmp_path: Path) -> None:
    now = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)

    write_state(tmp_path, "tab:leaf", {"hook_event_name": "PreToolUse", **QUESTION}, now)
    write_state(tmp_path, "tab:leaf", {"hook_event_name": "PreToolUse", "tool_name": "Bash"}, now)

    state = read_state(tmp_path, "tab:leaf")

    assert state is not None
    assert (state.state, state.question, state.at) == ("waiting", "A or B? / Now?", now)
    assert read_state(tmp_path, "other:pane") is None


def test_the_hook_command_records_the_state_and_never_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, woken: list[bool]
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("ORCA_PANE_KEY", "tab:leaf")

    result = CliRunner().invoke(app, ["hook"], input=json.dumps({"hook_event_name": "Stop"}))
    broken = CliRunner().invoke(app, ["hook"], input="not json")

    state = read_state(tmp_path / "spyhop" / "agents", "tab:leaf")

    assert result.exit_code == 0 and broken.exit_code == 0
    assert state is not None and state.state == "done"
    assert woken == [True]
