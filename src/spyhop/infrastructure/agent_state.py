"""The state of each Claude agent, written by the Claude Code hooks of the
worktree through `spyhop hook`, one JSON file for each terminal pane.

The pane comes from SPYHOP_PANE_KEY or, in an Orca terminal, ORCA_PANE_KEY:
the same key that Spyhop stores for the agent at its launch.
"""

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The directory of the state files, in the state directory of Spyhop.
AGENT_STATES = "agents"

PANE_VARIABLES = ("SPYHOP_PANE_KEY", "ORCA_PANE_KEY")

# Notifications that mean the agent waits for a person.
BLOCKING_NOTIFICATIONS = ("permission_prompt", "agent_needs_input", "elicitation_dialog")

QUESTION_TOOL = "AskUserQuestion"


@dataclass(frozen=True)
class AgentState:
    state: str  # working, waiting, blocked, done or ended
    question: str | None
    at: datetime
    session_id: str | None


def state_of(payload: dict[str, Any]) -> tuple[str, str | None] | None:
    """The state and the question that a hook event gives, or None for an
    event that does not change the state."""

    event = payload.get("hook_event_name")

    if event == "UserPromptSubmit":
        return "working", None

    if event in ("PreToolUse", "PostToolUse") and payload.get("tool_name") == QUESTION_TOOL:
        if event == "PostToolUse":
            return "working", None

        questions = (payload.get("tool_input") or {}).get("questions") or []
        text = " / ".join(
            str(question.get("question"))
            for question in questions
            if isinstance(question, dict) and question.get("question")
        )

        return "waiting", text or None

    if event == "Notification" and payload.get("notification_type") in BLOCKING_NOTIFICATIONS:
        return "blocked", payload.get("message")

    if event == "Stop":
        return "done", None

    if event == "SessionEnd":
        return "ended", None

    return None


def pane_key(environment: dict[str, str]) -> str | None:
    return next((environment[name] for name in PANE_VARIABLES if environment.get(name)), None)


def state_file(states_dir: Path, pane: str) -> Path:
    return states_dir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', pane)}.json"


def write_state(
    states_dir: Path, pane: str, payload: dict[str, Any], now: datetime | None = None
) -> None:
    found = state_of(payload)

    if found is None:
        return

    state, question = found
    data = {
        "state": state,
        "question": question,
        "at": (now or datetime.now(UTC)).isoformat(),
        "session_id": payload.get("session_id"),
    }

    states_dir.mkdir(parents=True, exist_ok=True)

    # A complete file or none: the supervisor can read it at any moment.

    with tempfile.NamedTemporaryFile("w", dir=states_dir, delete=False) as temporary:
        json.dump(data, temporary)

    os.replace(temporary.name, state_file(states_dir, pane))


def read_state(states_dir: Path, pane: str) -> AgentState | None:
    try:
        data = json.loads(state_file(states_dir, pane).read_text())

        return AgentState(
            state=str(data["state"]),
            question=data.get("question"),
            at=datetime.fromisoformat(data["at"]),
            session_id=data.get("session_id"),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
