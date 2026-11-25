"""The outcome of a turn of an agent, from its result file."""

from typing import Any

import pytest

from spyhop.application.work_results import WorkResult, validate_work


def payload(**output: Any) -> dict[str, Any]:
    return {"exit_code": 0, "output": {"is_error": False, **output}}


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (
            payload(structured_output={"status": "done", "summary": "Two commits"}),
            WorkResult("done", None, None, "Two commits"),
        ),
        (
            payload(
                structured_output={
                    "status": "question",
                    "question": "Keep the deprecated method?",
                    "summary": "Stopped before the API change",
                }
            ),
            WorkResult(
                "question", None, "Keep the deprecated method?", "Stopped before the API change"
            ),
        ),
        # The object as text, when the agent did not give a structured output.
        (
            payload(result='Done.\n```json\n{"status": "done", "summary": "ok"}\n```'),
            WorkResult("done", None, None, "ok"),
        ),
        (
            payload(structured_output={"status": "question", "summary": "?"}),
            WorkResult("lost", "the result has status question and no question"),
        ),
        (
            payload(structured_output={"verdict": "PASS"}),
            WorkResult("lost", "the result has no status done or question"),
        ),
        (
            {"exit_code": 137, "output": None},
            WorkResult("lost", "claude exited with 137 without a result"),
        ),
        (None, WorkResult("lost", "the result file is not valid JSON")),
        (
            payload(is_error=True, subtype="error_max_turns", num_turns=40),
            WorkResult("lost", "claude ended with an error: error_max_turns"),
        ),
        # `--resume` of a session that does not exist.
        (
            payload(is_error=True, subtype="error_during_execution", num_turns=0),
            WorkResult("resume_failed", "claude ended with error_during_execution"),
        ),
    ],
)
def test_the_outcome_of_a_turn(given: dict[str, Any] | None, expected: WorkResult) -> None:
    assert validate_work(given) == expected
