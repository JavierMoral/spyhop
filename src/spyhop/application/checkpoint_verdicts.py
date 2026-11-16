"""Reads CLAUDE-CHECKPOINTS.md and tells whether the checkpoints of a workflow
pass on their current inputs.

A verdict is valid only for the inputs that the checkpoint read. Each
checkpoint declares its inputs, and a fingerprint records their values:

- ``ticket``: the ``updated`` field of the JIRA issue of the run.
- ``base``: the SHA where the branch starts, the merge base with the base
  branch.
- ``head``: the SHA of HEAD.

A fingerprint is ``name=value`` for each input, in the order above, separated
by spaces, for example ``base=<sha> head=<sha>``. When a value changes, the
verdict is stale.

/checkpoints writes the file again after each run. Its first table has one row
per checkpoint, with the columns Checkpoint, Verdict (PASS or FAIL) and Inputs
(the fingerprint that the verdict is for). Other columns are for people. Rows
of checkpoints that the workflow does not have are ignored.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from spyhop.domain.enums import WaitingReason

VERDICTS_FILE = "CLAUDE-CHECKPOINTS.md"

INPUTS = ("ticket", "base", "head")

_COLUMNS = ("Checkpoint", "Verdict", "Inputs")
_SEPARATOR = re.compile(r":?-+:?")
_SHA = re.compile(r"[0-9a-f]{40}")
INPUT_VALUE = re.compile(r"[^\s|=]+")
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
    fingerprint: dict[str, str]


class _TableError(Exception):
    pass


def fingerprint(values: Mapping[str, str]) -> str:
    """The fingerprint of the input values, in the order of INPUTS."""

    return " ".join(f"{name}={values[name]}" for name in INPUTS if name in values)


def check_verdicts(text: str | None, fingerprints: Mapping[str, str]) -> list[Problem]:
    """The problems that keep the checkpoints from passing. ``fingerprints``
    gives the current fingerprint of each checkpoint of the workflow, and
    ``text`` is the content of the file, or None when it does not exist."""

    if not fingerprints:
        return []

    if text is None:
        return [Problem(None, WaitingReason.CHECKPOINTS_MISSING, f"{VERDICTS_FILE} not found")]

    try:
        verdicts = _verdicts(text)
    except _TableError as error:
        return [Problem(None, WaitingReason.CHECKPOINTS_MISSING, f"{VERDICTS_FILE}: {error}")]

    problems = []

    for name, current_text in fingerprints.items():
        verdict = verdicts.get(name)
        current = _parse(current_text)

        if verdict is None:
            problems.append(Problem(name, WaitingReason.CHECKPOINTS_MISSING, "no verdict"))
        elif verdict.verdict == "FAIL":
            problems.append(
                Problem(
                    name,
                    WaitingReason.CHECKPOINTS_FAILED,
                    f"FAIL on {_short(verdict.fingerprint)}",
                )
            )
        elif verdict.fingerprint != current:
            changed = [
                input_name
                for input_name in INPUTS
                if verdict.fingerprint.get(input_name) != current.get(input_name)
            ]
            problems.append(
                Problem(
                    name,
                    WaitingReason.CHECKPOINTS_STALE,
                    f"PASS on {_short(verdict.fingerprint, changed)}, "
                    f"now {_short(current, changed)}",
                )
            )

    return problems


def run_reason(problems: Sequence[Problem]) -> WaitingReason | None:
    reasons = {problem.reason for problem in problems}

    return next((reason for reason in _PRIORITY if reason in reasons), None)


def _parse(text: str) -> dict[str, str]:
    """The values of a fingerprint. Raises _TableError when it is not valid."""

    values: dict[str, str] = {}

    for part in text.split():
        name, _, value = part.partition("=")

        if name not in INPUTS or not INPUT_VALUE.fullmatch(value):
            raise _TableError(f"{part!r} is not name=value with a name from {', '.join(INPUTS)}")

        if name in values:
            raise _TableError(f"{name} is more than once in {text!r}")

        if name in ("base", "head") and not _SHA.fullmatch(value):
            raise _TableError(f"{name} {value!r} is not a full SHA")

        values[name] = value

    if not values:
        raise _TableError("no inputs")

    if fingerprint(values) != text:
        raise _TableError(f"{text!r} does not give the inputs in the order {', '.join(INPUTS)}")

    return values


def _short(values: Mapping[str, str], names: Sequence[str] | None = None) -> str:
    """The values of ``names`` (all values by default), with short SHAs."""

    shown = [name for name in INPUTS if name in values] if names is None else names
    parts = []

    for name in shown:
        value = values.get(name)

        if value is None:
            parts.append(f"no {name}")
        else:
            parts.append(f"{name}={value[:12] if name in ('base', 'head') else value}")

    return " ".join(parts)


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

        name, verdict, inputs = (cells[positions[column]] for column in _COLUMNS)

        if verdict not in _VERDICTS:
            raise _TableError(f"row {number}: verdict {verdict!r} is not PASS or FAIL")

        try:
            values = _parse(inputs)
        except _TableError as error:
            raise _TableError(f"row {number}: {error}") from None

        if name in verdicts:
            raise _TableError(f"row {number}: {name} has more than one row")

        verdicts[name] = _Verdict(verdict, values)

    return verdicts
