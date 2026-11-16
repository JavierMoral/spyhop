"""Reads CLAUDE-CHECKPOINTS.md and tells whether the checkpoints of a workflow
pass on the current HEAD.

/checkpoints writes the file again after each run. Its first table has one row
per checkpoint, with the columns Checkpoint, Verdict (PASS or FAIL) and HEAD
(the full SHA that the verdict is for). Other columns are for people. Rows of
checkpoints that the workflow does not have are ignored.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from spyhop.domain.enums import WaitingReason

VERDICTS_FILE = "CLAUDE-CHECKPOINTS.md"

_COLUMNS = ("Checkpoint", "Verdict", "HEAD")
_SEPARATOR = re.compile(r":?-+:?")
_SHA = re.compile(r"[0-9a-f]{40}")
_VERDICTS = ("PASS", "FAIL")

# The reason of the run is the first one in this order that a problem has.
_PRIORITY = (
    WaitingReason.CHECKPOINTS_MISSING,
    WaitingReason.CHECKPOINTS_FAILED,
    WaitingReason.CHECKPOINTS_STALE,
)


@dataclass(frozen=True)
class Problem:
    checkpoint: str | None  # None when the whole file is wrong
    reason: WaitingReason
    detail: str


@dataclass(frozen=True)
class _Verdict:
    verdict: str
    head: str


class _TableError(Exception):
    pass


def check_verdicts(text: str | None, checkpoints: Sequence[str], head: str) -> list[Problem]:
    """The problems that keep the checkpoints from passing on ``head``.
    ``text`` is the content of the file, or None when it does not exist."""

    if not checkpoints:
        return []

    if text is None:
        return [Problem(None, WaitingReason.CHECKPOINTS_MISSING, f"{VERDICTS_FILE} not found")]

    try:
        verdicts = _verdicts(text)
    except _TableError as error:
        return [Problem(None, WaitingReason.CHECKPOINTS_MISSING, f"{VERDICTS_FILE}: {error}")]

    problems = []

    for name in dict.fromkeys(checkpoints):
        verdict = verdicts.get(name)

        if verdict is None:
            problems.append(Problem(name, WaitingReason.CHECKPOINTS_MISSING, "no verdict"))
        elif verdict.verdict == "FAIL":
            problems.append(
                Problem(name, WaitingReason.CHECKPOINTS_FAILED, f"FAIL on {verdict.head[:12]}")
            )
        elif verdict.head != head:
            problems.append(
                Problem(
                    name,
                    WaitingReason.CHECKPOINTS_STALE,
                    f"PASS on {verdict.head[:12]}, HEAD is {head[:12]}",
                )
            )

    return problems


def run_reason(problems: Sequence[Problem]) -> WaitingReason | None:
    reasons = {problem.reason for problem in problems}

    return next((reason for reason in _PRIORITY if reason in reasons), None)


def _verdicts(text: str) -> dict[str, _Verdict]:
    lines = [line.strip() for line in text.splitlines()]
    start = next((index for index, line in enumerate(lines) if line.startswith("|")), None)

    if start is None:
        raise _TableError("no table")

    table = []

    for line in lines[start:]:
        if not line.startswith("|"):
            break

        table.append([cell.strip() for cell in line.strip("|").split("|")])

    header = table[0]

    if len(table) < 2 or not all(_SEPARATOR.fullmatch(cell) for cell in table[1]):
        raise _TableError("no separator row under the header")

    for column in _COLUMNS:
        if column not in header:
            raise _TableError(f"no {column} column")

    positions = {column: header.index(column) for column in _COLUMNS}
    verdicts: dict[str, _Verdict] = {}

    for number, cells in enumerate(table[2:], 1):
        if len(cells) != len(header):
            raise _TableError(f"row {number} has {len(cells)} cells, not {len(header)}")

        name, verdict, head = (cells[positions[column]] for column in _COLUMNS)

        if verdict not in _VERDICTS:
            raise _TableError(f"row {number}: verdict {verdict!r} is not PASS or FAIL")

        if not _SHA.fullmatch(head):
            raise _TableError(f"row {number}: HEAD {head!r} is not a full SHA")

        if name in verdicts:
            raise _TableError(f"row {number}: {name} has more than one row")

        verdicts[name] = _Verdict(verdict, head)

    return verdicts
