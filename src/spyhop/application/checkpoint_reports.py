"""The result of a checkpoint and the files that Spyhop writes from it: the
JSON contract that `claude -p` follows, the report of each checkpoint and the
table of the verdicts in the worktree."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from spyhop.application.checkpoint_planner import AGENT_PREFIX
from spyhop.application.checkpoint_verdicts import VERDICTS_FILE
from spyhop.domain.models import StepRun

REPORT_FILE = "CLAUDE-CHECKPOINT-{name}.md"

_SEVERITIES = ("high", "medium", "low")

# The JSON object that each checkpoint gives, as `claude --json-schema` takes it.
RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "head", "commands", "findings"],
    "properties": {
        "verdict": {"enum": ["PASS", "FAIL"]},
        "head": {"type": "string"},
        "commands": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["command", "result"],
                "properties": {"command": {"type": "string"}, "result": {"type": "string"}},
            },
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["severity", "summary"],
                "properties": {
                    "severity": {"enum": list(_SEVERITIES)},
                    "file": {"type": ["string", "null"]},
                    "line": {"type": ["integer", "null"]},
                    "summary": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class CheckpointResult:
    verdict: Literal["PASS", "FAIL"]
    validation: str | None
    data: dict[str, Any] | None


def read_output(output_file: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(output_file.read_text())
    except (OSError, ValueError):
        return None

    return payload if isinstance(payload, dict) else None


def validate(payload: dict[str, Any] | None, expected_head: str) -> CheckpointResult:
    """The verdict of a checkpoint. A result that does not follow the
    contract is a FAIL, with the reason in ``validation``."""

    if payload is None:
        return CheckpointResult("FAIL", "the result file is not valid JSON", None)

    output = payload.get("output")

    if not isinstance(output, dict):
        return CheckpointResult(
            "FAIL", f"claude exited with {payload.get('exit_code')} without a result", None
        )

    if output.get("is_error"):
        return CheckpointResult(
            "FAIL", f"claude ended with an error: {output.get('subtype')}", None
        )

    data = structured_data(output)

    if not isinstance(data, dict):
        return CheckpointResult("FAIL", "the result has no JSON object", None)

    problem = _contract_problem(data, expected_head)

    if problem:
        return CheckpointResult("FAIL", problem, data)

    return CheckpointResult(data["verdict"], None, data)


def _contract_problem(data: dict[str, Any], expected_head: str) -> str | None:
    for key in ("verdict", "head", "commands", "findings"):
        if key not in data:
            return f"the result has no {key}"

    if data["verdict"] not in ("PASS", "FAIL"):
        return f"verdict {data['verdict']!r} is not PASS or FAIL"

    if data["head"] != expected_head:
        return f"head {data['head']!r} is not the HEAD of the stage {expected_head}"

    if not isinstance(data["commands"], list) or not isinstance(data["findings"], list):
        return "commands and findings must be lists"

    if data["verdict"] == "PASS" and not data["commands"]:
        return "a PASS needs commands as evidence"

    if data["verdict"] == "PASS" and any(
        isinstance(finding, dict) and finding.get("severity") in ("high", "medium")
        for finding in data["findings"]
    ):
        return "a PASS cannot have a high or medium finding"

    return None


def structured_data(output: dict[str, Any]) -> Any:
    """The JSON object of the result event of claude: its structured output,
    or the JSON of its text answer when the agent wrote the object as text."""

    data = output.get("structured_output")

    if data is None:
        data = _json_object(str(output.get("result") or ""))

    return data


def _json_object(text: str) -> Any:
    """The JSON object of a text answer, also inside a code fence after
    other text."""

    text = text.strip()
    fence = text.rfind("```json")

    if fence >= 0:
        text = text[fence:]

    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]

    try:
        return json.loads(text)
    except ValueError:
        return None


def append_report(root: Path, row: StepRun, result: CheckpointResult, now: datetime) -> None:
    data = result.data or {}
    lines = [
        f"## {now.astimezone():%Y-%m-%d %H:%M}, phase {row.phase}, round {row.round}: "
        f"{result.verdict}",
        "",
        f"- Agent: {AGENT_PREFIX}{row.name}. Inputs: {row.inputs}.",
        f"- Validation: {result.validation or 'OK'}.",
        "",
        "### Commands",
        "",
    ]

    for command in data.get("commands") or []:
        if isinstance(command, dict):
            lines.append(f"- `{command.get('command', '')}`: {command.get('result', '')}")

    lines += ["", "### Findings", ""]

    for number, finding in enumerate(data.get("findings") or [], 1):
        if not isinstance(finding, dict):
            continue

        location = finding.get("file") or ""

        if finding.get("line") is not None:
            location += f":{finding['line']}"

        lines.append(
            f"{number}. **{finding.get('severity', '')}** `{location}`: "
            f"{finding.get('summary', '')}"
        )

        if finding.get("evidence"):
            lines.append(f"   Evidence: {finding['evidence']}")

    report = root / REPORT_FILE.format(name=row.name)
    existing = report.read_text() if report.is_file() else f"# Checkpoint {row.name}\n"

    report.write_text(existing.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n")


def write_verdicts(root: Path, checkpoints: Sequence[str], latest: dict[str, StepRun]) -> None:
    lines = [
        "# Checkpoints",
        "",
        "Spyhop writes this file. Do not edit it.",
        "",
        "| Checkpoint | Verdict | Inputs | Round | Report |",
        "| --- | --- | --- | --- | --- |",
    ]

    for name in checkpoints:
        row = latest.get(name)

        if row is not None:
            lines.append(
                f"| {name} | {row.verdict} | {row.inputs} | {row.round} | "
                f"{REPORT_FILE.format(name=name)} |"
            )

    (root / VERDICTS_FILE).write_text("\n".join(lines) + "\n")
