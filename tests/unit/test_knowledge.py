import dataclasses
import json
import os
from pathlib import Path
from typing import Any

from spyhop.application.knowledge import KnowledgeWriter, fixed_findings
from spyhop.domain.enums import RunStatus
from spyhop.domain.models import StepRun
from tests.unit.test_stats import checkpoint_run
from tests.unit.test_supervisor import run_in

RUN = run_in(RunStatus.COMPLETED)

FINDINGS = [
    {
        "severity": "high",
        "file": "App.java",
        "line": 3,
        "summary": "Null check missing.",
        "evidence": "NPE in testX",
    },
    {"severity": "low", "summary": "Style."},
]


def result(tmp_path: Path, row: StepRun, findings: list[dict[str, Any]]) -> StepRun:
    output_file = tmp_path / f"{row.id}.json"
    data = {"verdict": row.verdict, "head": "abc", "commands": [], "findings": findings}

    output_file.write_text(json.dumps({"exit_code": 0, "output": {"structured_output": data}}))

    return dataclasses.replace(row, output_file=output_file)


def test_only_high_and_medium_findings_of_a_fixed_checkpoint_are_stored(tmp_path: Path) -> None:
    rows = [
        result(tmp_path, checkpoint_run("regressions", "FAIL", 1), FINDINGS),
        result(tmp_path, checkpoint_run("security", "FAIL", 2), FINDINGS),
        result(tmp_path, checkpoint_run("regressions", "PASS", 3), []),
    ]

    [entry] = fixed_findings(RUN, rows)

    assert entry["bucket"] == "gotchas"
    assert entry["title"] == "Null check missing."
    assert entry["slug"].startswith("lpd-1-regressions-")
    assert entry["tags"] == ["spyhop", "regressions"]
    assert entry["jira"] == ["LPD-1"]
    assert entry["body"]["evidence"] == "`App.java:3`\nNPE in testX"


class Rows:
    def __init__(self, rows: list[StepRun]) -> None:
        self.rows = rows

    def list_for_run(self, run_id: str) -> list[StepRun]:
        return self.rows


async def test_the_script_gets_each_finding_on_stdin(tmp_path: Path) -> None:
    script = tmp_path / "write.sh"
    received = tmp_path / "received"

    script.write_text(f'#!/bin/sh\necho "$1" > {received}\ncat >> {received}\n')
    os.chmod(script, 0o755)

    rows = Rows(
        [
            result(tmp_path, checkpoint_run("regressions", "FAIL", 1), FINDINGS),
            result(tmp_path, checkpoint_run("regressions", "PASS", 3), []),
        ]
    )

    await KnowledgeWriter(rows, script, 10).record(RUN)  # type: ignore[arg-type]

    source, entry = received.read_text().split("\n", 1)

    assert source == "spyhop-r1"
    assert json.loads(entry)["body"]["finding"] == "Null check missing."
