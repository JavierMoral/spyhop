"""Stores the findings that a run fixed, so that the agents of later tickets
can find them with knowledge.recall_command.

A finding counts as fixed when its checkpoint failed in the run and passed
at the end. Each finding with a high or medium severity goes to
knowledge.write_script: the finding as JSON on stdin, and the source,
``spyhop-<run id>``, as the argument. A failure only goes to the log.
"""

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from spyhop.application.checkpoint_runner import read_output
from spyhop.domain.models import AutomationRun, CheckpointRun
from spyhop.infrastructure.persistence.repositories import CheckpointRunRepository
from spyhop.infrastructure.process.command_runner import (
    CommandRunner,
    CommandTimeoutError,
    SubprocessRunner,
)

_log = logging.getLogger(__name__)

SEVERITIES = ("high", "medium")


class KnowledgeWriter:
    def __init__(
        self,
        checkpoint_runs: CheckpointRunRepository,
        script: Path,
        timeout: float,
        runner: CommandRunner | None = None,
    ) -> None:
        self._checkpoint_runs = checkpoint_runs
        self._script = script
        self._timeout = timeout
        self._runner = runner or SubprocessRunner()

    async def record(self, run: AutomationRun) -> None:
        for entry in fixed_findings(run, self._checkpoint_runs.list_for_run(run.id)):
            try:
                result = await self._runner.run(
                    [str(self._script), f"spyhop-{run.id}"],
                    self._timeout,
                    stdin=json.dumps(entry),
                )
            except (CommandTimeoutError, OSError) as error:
                _log.warning("%s: finding not stored: %s", run.issue_key, error)

                return

            if result.exit_code != 0:
                _log.warning(
                    "%s: finding not stored: %s exited with %d: %s",
                    run.issue_key,
                    self._script,
                    result.exit_code,
                    result.stderr.strip()[-500:],
                )


def fixed_findings(run: AutomationRun, rows: list[CheckpointRun]) -> list[dict[str, Any]]:
    """The entries for the findings of the failed checkpoints whose last
    verdict is PASS."""

    last: dict[str, CheckpointRun] = {}

    for row in rows:
        if row.status == "DONE":
            last[row.name] = row

    entries: dict[str, dict[str, Any]] = {}

    for row in rows:
        if row.verdict != "FAIL" or last[row.name].verdict != "PASS":
            continue

        for finding in _findings(row):
            entry = _entry(run, row, finding)

            entries.setdefault(entry["slug"], entry)

    return list(entries.values())


def _findings(row: CheckpointRun) -> list[dict[str, Any]]:
    payload = read_output(row.output_file) if row.output_file.is_file() else None
    output = payload.get("output") if payload else None
    data = output.get("structured_output") if isinstance(output, dict) else None
    findings = data.get("findings") if isinstance(data, dict) else None

    return [
        finding
        for finding in findings or []
        if isinstance(finding, dict)
        and finding.get("severity") in SEVERITIES
        and finding.get("summary")
    ]


def _entry(run: AutomationRun, row: CheckpointRun, finding: dict[str, Any]) -> dict[str, Any]:
    summary = str(finding["summary"]).strip()
    location = str(finding.get("file") or "")

    if finding.get("line") is not None:
        location += f":{finding['line']}"

    digest = hashlib.sha1(summary.encode()).hexdigest()[:8]
    evidence = "\n".join(
        part for part in (f"`{location}`" if location else "", finding.get("evidence")) if part
    )

    return {
        "bucket": "gotchas",
        "title": summary.splitlines()[0][:120],
        "slug": re.sub(r"[^a-z0-9]+", "-", f"{run.issue_key}-{row.name}-{digest}".lower()),
        "tags": ["spyhop", row.name],
        "modules": [],
        "jira": [run.issue_key],
        "body": {
            "context": f"Checkpoint {row.name} of {run.issue_key}, round {row.round}.",
            "finding": summary,
            "evidence": evidence,
            "why": f"The checkpoint {row.name} failed on this, and the agent fixed it.",
        },
        "links": [],
    }
