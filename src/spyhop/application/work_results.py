"""The result of a turn of an agent of the run (a work row).

A turn ends with a JSON object of WORK_SCHEMA in the result event of
`claude -p`. The agent cannot wait for an answer in that mode, so a turn
that needs a decision ends with ``status: question``.
"""

from dataclasses import dataclass
from typing import Any, Literal

from spyhop.application.checkpoint_reports import structured_data

WORK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "question"]},
        "question": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["status", "summary"],
}


@dataclass(frozen=True)
class WorkResult:
    """``done`` and ``question``: what the agent gave. ``lost``: no valid
    result, see ``detail``. ``resume_failed``: `--resume` found no session,
    so the turn did not start."""

    outcome: Literal["done", "question", "lost", "resume_failed"]
    detail: str | None = None
    question: str | None = None
    summary: str | None = None


def validate_work(payload: dict[str, Any] | None) -> WorkResult:
    """The outcome of a turn, from the payload that `spyhop steps exec`
    writes: the exit code of claude and its last result event."""

    if payload is None:
        return WorkResult("lost", "the result file is not valid JSON")

    output = payload.get("output")

    if not isinstance(output, dict):
        return WorkResult("lost", f"claude exited with {payload.get('exit_code')} without a result")

    if output.get("is_error"):
        # Without a session, `--resume` stops before the first turn.

        if output.get("num_turns") == 0:
            return WorkResult("resume_failed", f"claude ended with {output.get('subtype')}")

        return WorkResult("lost", f"claude ended with an error: {output.get('subtype')}")

    data = structured_data(output)

    if not isinstance(data, dict) or data.get("status") not in ("done", "question"):
        return WorkResult("lost", "the result has no status done or question")

    summary = data.get("summary") if isinstance(data.get("summary"), str) else None

    if data["status"] == "question":
        question = data.get("question")

        if not isinstance(question, str) or not question.strip():
            return WorkResult("lost", "the result has status question and no question")

        return WorkResult("question", None, question, summary)

    return WorkResult("done", None, None, summary)
