"""The eval cases of a checkpoint and the grade of each run.

An eval runs an agent on a small repository with a known change and
compares its result with the expected result. A checkpoint agent gives a
verdict. A review agent gives a list of findings, and the case names the
findings that it must give and the lines where it must give none. Each case
holds a copy of the files, so it does not depend on the revisions of a real
repository. The cases live outside any repository, because the tickets of
security cases are private:

    <evals dir>/tickets/<KEY>.txt        the ticket, as `spyhop ticket` shows it
    <evals dir>/tickets/<KEY>.task.md    the CLAUDE-TASK.md of the run
    <evals dir>/<agent>/shared/          files that each base gets, for example
                                         the rule catalog of a review agent
    <evals dir>/<agent>/<case>/case.yaml
    <evals dir>/<agent>/<case>/base/     the files before the change
    <evals dir>/<agent>/<case>/head/     the same files after the change

Each run adds one line to <evals dir>/metrics.jsonl. Each version of an agent
is kept in <evals dir>/agents/<sha256>.md, so a metric can be compared with
the prompt that gave it.
"""

import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from spyhop.application.checkpoint_reports import structured_data, validate
from spyhop.domain.exceptions import ConfigurationError

_SHELL_KEYWORDS = {
    "!",
    "do",
    "done",
    "elif",
    "else",
    "fi",
    "if",
    "then",
    "time",
    "until",
    "while",
    "{",
    "}",
}

# `spyhop ticket` reads the ticket from this directory, not from JIRA.
EVAL_TICKETS_ENV_VAR = "SPYHOP_EVAL_TICKETS"

TASK_FILE = "CLAUDE-TASK.md"

SHARED_DIR = "shared"

# A finding of a review agent matches an expected finding this many lines
# before or after the expected lines.
LINE_TOLERANCE = 3

_SEVERITIES = ("BLOCK", "WARN", "NIT")

# The JSON object that a review agent gives, as `claude --json-schema` takes it.
REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["scope", "findings", "dismissed", "clean"],
    "properties": {
        "scope": {"type": "string"},
        "risk": {"enum": ["LOW", "MEDIUM", "HIGH"]},
        "numbered_rules_verified": {"type": "boolean"},
        "sampled": {"type": "boolean"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["severity", "rule", "check", "file", "line", "summary"],
                "properties": {
                    "severity": {"enum": list(_SEVERITIES)},
                    "rule": {"type": ["string", "null"]},
                    "check": {"type": ["string", "null"]},
                    "file": {"type": "string"},
                    "line": {"type": ["integer", "null"]},
                    "end_line": {"type": ["integer", "null"]},
                    "summary": {"type": "string"},
                    "fix": {"type": "string"},
                    "verify": {"type": "boolean"},
                },
            },
        },
        "dismissed": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["candidate", "reason"],
                "properties": {
                    "candidate": {"type": "string"},
                    "file": {"type": ["string", "null"]},
                    "line": {"type": ["integer", "null"]},
                    "check": {"type": ["string", "null"]},
                    "reason": {"type": "string"},
                },
            },
        },
        "clean": {"type": "array", "items": {"type": "string"}},
    },
}

# A fixed author and date give the same SHAs on each run.
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Eval",
    "GIT_AUTHOR_EMAIL": "eval@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_NAME": "Eval",
    "GIT_COMMITTER_EMAIL": "eval@example.com",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


@dataclass(frozen=True)
class Lines:
    """Lines ``start`` to ``end`` of a file whose path ends with ``file``."""

    file: str
    start: int
    end: int


@dataclass(frozen=True)
class ExpectedFinding:
    """A finding that a review agent must give: it cites one of ``rules``
    or one of ``checks`` on one of ``places``. A renamed identifier, for
    example, has a place for each of its uses, and the agent can report any
    of them. A problem that more than one rule describes accepts each of
    them."""

    places: tuple[Lines, ...]
    rules: tuple[str, ...]
    checks: tuple[str, ...]


@dataclass(frozen=True)
class EvalCase:
    name: str
    directory: Path
    ticket: str | None
    # The message of the commit with the change.
    message: str
    # The verdict of a checkpoint agent; None for a review agent.
    verdict: Literal["PASS", "FAIL"] | None
    # For a FAIL: a high or medium finding must name a file that ends with
    # one of these paths.
    files: tuple[str, ...] = ()
    # For a FAIL: each of these places needs a high or medium finding, for
    # example each variant of a defect, also in the file that the change
    # fixes.
    places: tuple[Lines, ...] = ()
    # The findings of a review agent; None for a checkpoint agent.
    findings: tuple[ExpectedFinding, ...] | None = None
    # A finding of a review agent on these lines is a false positive.
    clean: tuple[Lines, ...] = ()


@dataclass(frozen=True)
class Grade:
    passed: bool
    reason: str
    # For a review agent: the expected findings that the agent gave, the ones
    # that it did not give, its findings on clean lines, and its other
    # findings.
    found: int = 0
    missed: tuple[str, ...] = ()
    false_positives: tuple[str, ...] = ()
    extra: int = 0


def load_cases(checkpoint_dir: Path, pattern: str = "*") -> list[EvalCase]:
    cases = []

    for case_file in sorted(checkpoint_dir.glob("*/case.yaml")):
        name = case_file.parent.name

        if fnmatch.fnmatch(name, pattern):
            cases.append(_read_case(name, case_file))

    return cases


def build_repository(case: EvalCase, root: Path, task: str | None) -> tuple[str, str]:
    """Creates in ``root`` a repository with one commit for base/ and one for
    head/. Both commits also hold the shared/ files of the agent, and
    origin/master points to the base commit. With ``task``, writes the task
    file with the list of the files. Returns the SHAs of the two commits."""

    shared = case.directory.parent / SHARED_DIR

    root.mkdir(parents=True)

    _git(root, "init", "-q")
    (root / ".git" / "info" / "exclude").write_text(f"/{TASK_FILE}\n")

    _commit(root, shared, case.directory / "base", "Base")

    base = _git(root, "rev-parse", "HEAD")

    _git(root, "update-ref", "refs/remotes/origin/master", base)
    _git(root, "rm", "-rq", ".")
    _commit(root, shared, case.directory / "head", case.message)

    if task is not None:
        files = "".join(f"- `{path}`\n" for path in _git(root, "ls-files").splitlines())

        (root / TASK_FILE).write_text(
            f"{task}\n## Eval Repository\n\nThis repository contains only these files. "
            f"Do not search for other files.\n\n{files}"
        )

    return base, _git(root, "rev-parse", "HEAD")


def grade(case: EvalCase, payload: dict[str, Any] | None, head_sha: str) -> Grade:
    """Compares the result file of one run with the case. A result that does
    not follow the contract of the checkpoints fails, also when the verdict
    is the expected one."""

    result = validate(payload, head_sha)

    if result.validation is not None:
        return Grade(False, f"invalid result: {result.validation}")

    if result.verdict != case.verdict:
        return Grade(False, f"{result.verdict}, expected {case.verdict}")

    if case.verdict == "PASS":
        return Grade(True, "PASS")

    findings = (result.data or {}).get("findings") or []

    if case.places:
        serious = [
            finding
            for finding in findings
            if isinstance(finding, dict) and finding.get("severity") in ("high", "medium")
        ]
        missed = [
            f"{place.file}:{place.start}"
            for place in case.places
            if not any(_overlaps(finding, place, LINE_TOLERANCE) for finding in serious)
        ]

        if missed:
            return Grade(False, f"FAIL, but no high or medium finding at {', '.join(missed)}")

        return Grade(True, "FAIL at each expected place")

    files = [
        str(finding.get("file") or "")
        for finding in findings
        if isinstance(finding, dict) and finding.get("severity") in ("high", "medium")
    ]

    if any(file.endswith(expected) for file in files for expected in case.files):
        return Grade(True, "FAIL in an expected file")

    return Grade(False, f"FAIL, but no high or medium finding in {', '.join(case.files)}")


def case_from_commit(
    repository: Path,
    commit: str,
    case_dir: Path,
    rule: str | None,
    check: str | None,
    one: bool = False,
) -> int:
    """Writes in ``case_dir`` a review case from a commit that fixes a
    violation: head/ holds the changed files before the commit, and base/
    the same files without the lines that the commit changes. So the change
    of the case only adds the code with the violation, and does not show the
    fixed form. Each hunk that adds lines gives one expected finding with
    ``rule`` and ``check``. With ``one``, the hunks give one expected
    finding that any of them matches. Returns the number of expected
    findings."""

    if rule is None and check is None:
        raise ConfigurationError("give a rule or a check")

    if case_dir.exists():
        raise ConfigurationError(f"{case_dir} already exists")

    fixed = _git(repository, "rev-parse", "--verify", f"{commit}^{{commit}}")
    broken = _git(repository, "rev-parse", "--verify", f"{fixed}^")
    subject = _git(repository, "log", "-1", "--format=%s", fixed)
    ticket = re.match(r"[A-Z][A-Z0-9]*-\d+", subject)

    changed = {}

    for line in _git(
        repository, "diff", "--no-renames", "--name-status", broken, fixed
    ).splitlines():
        status, path = line.split("\t", 1)

        if status != "A":
            _write_blob(repository, broken, path, case_dir / "head" / path)

            changed[path] = status

    for side in ("base", "head"):
        (case_dir / side).mkdir(parents=True, exist_ok=True)

    places = []
    added: dict[str, set[int]] = {}
    path = ""

    for line in _git(repository, "diff", "--no-renames", "--unified=0", fixed, broken).splitlines():
        if line.startswith("+++ "):
            path = line.removeprefix("+++ b/")

            continue

        hunk = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", line)

        if hunk is None or hunk.group(2) == "0":
            continue

        start = int(hunk.group(1))
        end = start + int(hunk.group(2) or 1) - 1

        added.setdefault(path, set()).update(range(start, end + 1))
        places.append(f"{{file: {json.dumps(path)}, lines: [{start}, {end}]}}")

    for path, status in changed.items():
        if status == "D":
            continue

        target = case_dir / "base" / path

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            base_content((case_dir / "head" / path).read_bytes(), added.get(path, set()), path)
        )

    ids = "".join(
        f"{key}: {json.dumps(value)}, "
        for key, value in (("rule", rule), ("check", check))
        if value is not None
    )

    if one and places:
        findings = [
            f"    - {{{ids}any: [\n"
            + "".join(f"        {place},\n" for place in places)
            + "      ]}\n"
        ]
    else:
        findings = [f"    - {{{ids}{place[1:]}\n" for place in places]

    (case_dir / "case.yaml").write_text(
        f"# Generated from {fixed[:12]} {subject}\n"
        "# Review the expected findings: the commit can fix more than one problem.\n"
        f"message: {json.dumps((ticket.group(0) if ticket else 'LPD-1') + ' Update the code')}\n"
        "expect:\n"
        "  findings:\n"
        f"{''.join(findings)}"
    )

    return len(findings)


def base_content(head: bytes, added: set[int], path: str) -> bytes:
    """The content of a file at the base of a case: ``head`` without the
    lines in ``added``. In a Java file, each of these lines takes its whole
    class member with it, so that the change of the case adds complete
    members, as a real branch does, and not single lines in old code."""

    lines = head.splitlines(keepends=True)
    removed = set(added)

    if path.endswith(".java"):
        for number in sorted(added):
            removed.update(_member_lines(lines, number))

    return b"".join(line for number, line in enumerate(lines, 1) if number not in removed)


def _member_lines(lines: list[bytes], number: int) -> range:
    """The lines of the class member that holds line ``number``, with its
    annotations, its Javadoc, and the blank line after it. A member starts
    with one tab and ends with a tab and a brace, or with a semicolon when it
    has no body."""

    def text(index: int) -> str:
        return lines[index - 1].decode(errors="replace").rstrip("\r\n")

    def is_comment_or_annotation(index: int) -> bool:
        return re.match(r"\t(@| ?\*|/\*)", text(index)) is not None

    def is_member_start(index: int) -> bool:
        return re.match(r"\t[^\t\s}]", text(index)) is not None

    # An annotation or a comment belongs to the member after it.

    while number < len(lines) and is_comment_or_annotation(number):
        number += 1

    start = next(
        (
            index
            for index in range(number, 0, -1)
            if is_member_start(index) and not is_comment_or_annotation(index)
        ),
        None,
    )

    if start is None:
        return range(number, number + 1)

    while start > 1 and is_comment_or_annotation(start - 1):
        start -= 1

    end = None

    for index in range(number, len(lines) + 1):
        if text(index) == "\t}":
            end = index

            break

        if index > number and is_member_start(index):
            break

    if end is None:
        end = next(
            (index for index in range(number, len(lines) + 1) if text(index).endswith(";")),
            number,
        )

    if end < len(lines) and not text(end + 1).strip():
        end += 1

    return range(start, end + 1)


def _write_blob(repository: Path, revision: str, path: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        subprocess.run(
            ["git", "-C", str(repository), "show", f"{revision}:{path}"],
            capture_output=True,
            check=True,
        ).stdout
    )


def review_prompt(base: str, head_sha: str) -> str:
    return (
        f"Review the commits from BASE={base} to HEAD={head_sha}. origin/master is BASE. "
        "Return the JSON output."
    )


def _commit(root: Path, shared: Path, side: Path, message: str) -> None:
    if shared.is_dir():
        shutil.copytree(shared, root, dirs_exist_ok=True)

    shutil.copytree(side, root, dirs_exist_ok=True)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", message)


def grade_review(case: EvalCase, payload: dict[str, Any]) -> Grade:
    """Compares the findings of a review agent with the case. The run passes
    when the agent gives each expected finding and no finding on clean
    lines."""

    output = payload.get("output")

    if not isinstance(output, dict):
        return Grade(False, f"invalid result: claude exited with {payload.get('exit_code')}")

    if output.get("is_error"):
        return Grade(False, f"invalid result: claude ended with an error: {output.get('subtype')}")

    data = structured_data(output)
    findings = data.get("findings") if isinstance(data, dict) else None

    if not isinstance(findings, list):
        return Grade(False, "invalid result: the result has no findings list")

    findings = [finding for finding in findings if isinstance(finding, dict)]
    expected = case.findings or ()
    matched: set[int] = set()
    missed = []

    for expected_finding in expected:
        index = next(
            (
                index
                for index, finding in enumerate(findings)
                if _cites(finding, expected_finding)
                and any(
                    _overlaps(finding, place, LINE_TOLERANCE) for place in expected_finding.places
                )
            ),
            None,
        )

        if index is None:
            missed.append(_describe(expected_finding, findings))
        else:
            matched.add(index)

    false_positives = tuple(
        f"{finding.get('rule') or finding.get('check')} {finding.get('file')}:{finding.get('line')}"
        for finding in findings
        if any(_overlaps(finding, lines, 0) for lines in case.clean)
    )
    found = len(expected) - len(missed)
    reason = f"{found}/{len(expected)} expected findings"

    if missed:
        reason += f"; missed {', '.join(missed)}"

    if false_positives:
        reason += f"; false positives {', '.join(false_positives)}"

    return Grade(
        not missed and not false_positives,
        reason,
        found,
        tuple(missed),
        false_positives,
        len(findings) - len(matched) - len(false_positives),
    )


def _cites(finding: dict[str, Any], expected: ExpectedFinding) -> bool:
    rule = finding.get("rule")

    return (rule is not None and str(rule) in expected.rules) or (
        finding.get("check") in expected.checks
    )


def _describe(expected: ExpectedFinding, findings: list[dict[str, Any]]) -> str:
    """The ids and the first place of a missed finding, and the ids of the
    findings on its places, which cite another rule or check."""

    place = expected.places[0]
    description = f"{'|'.join(expected.rules + expected.checks)} {place.file}:{place.start}"
    other = [
        f"{finding.get('rule')}|{finding.get('check')}"
        for finding in findings
        if any(_overlaps(finding, place, LINE_TOLERANCE) for place in expected.places)
    ]

    if other:
        description += f" (cited as {', '.join(other)})"

    return description


def _overlaps(finding: dict[str, Any], lines: Lines, tolerance: int) -> bool:
    start = finding.get("line")

    if not isinstance(start, int) or not str(finding.get("file") or "").endswith(lines.file):
        return False

    end = finding.get("end_line")

    if not isinstance(end, int) or end < start:
        end = start

    return start <= lines.end + tolerance and end >= lines.start - tolerance


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_GIT_ENV},
    )

    return completed.stdout.strip()


def _read_case(name: str, case_file: Path) -> EvalCase:
    try:
        data = yaml.safe_load(case_file.read_text())
        expect = data["expect"]
        review = "findings" in expect
        case = EvalCase(
            name=name,
            directory=case_file.parent,
            ticket=str(data["ticket"]) if not review or "ticket" in data else None,
            message=str(data["message"]),
            verdict=None if review else expect["verdict"],
            files=tuple(str(file) for file in expect.get("files") or ()),
            places=tuple(_lines(item) for item in expect.get("places") or ()),
            findings=(
                tuple(_expected_finding(item) for item in expect["findings"] or ())
                if review
                else None
            ),
            clean=tuple(_lines(item) for item in expect.get("clean") or ()),
        )
    except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError) as error:
        raise ConfigurationError(f"{case_file}: {error!r}") from None

    if case.findings is not None:
        if "verdict" in expect:
            raise ConfigurationError(f"{case_file}: give expect.verdict or expect.findings")
    elif case.verdict not in ("PASS", "FAIL"):
        raise ConfigurationError(f"{case_file}: expect.verdict must be PASS or FAIL")

    if case.verdict == "FAIL" and not case.files and not case.places:
        raise ConfigurationError(f"{case_file}: a FAIL case needs expect.files or expect.places")

    for side in ("base", "head"):
        if not (case.directory / side).is_dir():
            raise ConfigurationError(f"{case_file}: the case has no {side}/ directory")

    return case


def _expected_finding(item: dict[str, Any]) -> ExpectedFinding:
    rules = _ids(item.get("rule"))
    checks = _ids(item.get("check"))

    if not rules and not checks:
        raise ValueError("an expected finding needs a rule or a check")

    places = tuple(_lines(place) for place in item["any"]) if "any" in item else (_lines(item),)

    if not places:
        raise ValueError("an expected finding needs a place")

    return ExpectedFinding(places, rules, checks)


def _ids(value: Any) -> tuple[str, ...]:
    """``rule`` and ``check`` are one id or a list of accepted ids."""

    if value is None:
        return ()

    return tuple(str(id_) for id_ in (value if isinstance(value, list) else [value]))


def _lines(item: dict[str, Any]) -> Lines:
    """``lines`` is one line or a [start, end] list."""

    lines = item["lines"]
    start, end = (lines, lines) if isinstance(lines, int) else lines

    if not isinstance(start, int) or not isinstance(end, int) or end < start:
        raise ValueError(f"lines {lines!r} must be a line or a [start, end] list")

    return Lines(str(item["file"]), start, end)


def run_metrics(
    case: EvalCase, run_grade: Grade, payload: dict[str, Any], transcript: Path
) -> dict[str, Any]:
    """The consumption and the performance of one run, from the result event
    of claude and from the stream of the run."""

    output = payload.get("output")
    result = output if isinstance(output, dict) else {}
    data = structured_data(result)
    findings = data.get("findings") if isinstance(data, dict) else None
    usage = result.get("usage") or {}
    tools: Counter[str] = Counter()
    programs: Counter[str] = Counter()

    for tool_use in _tool_uses(transcript):
        tools[str(tool_use.get("name"))] += 1

        if tool_use.get("name") == "Bash":
            programs.update(_programs(str((tool_use.get("input") or {}).get("command") or "")))

    return {
        "case": case.name,
        "expected": case.verdict,
        "verdict": data.get("verdict") if isinstance(data, dict) else None,
        "passed": run_grade.passed,
        "reason": run_grade.reason,
        "exit_code": payload.get("exit_code"),
        "is_error": result.get("is_error"),
        "models": sorted(result.get("modelUsage") or {}),
        "cost_usd": result.get("total_cost_usd"),
        "duration_ms": result.get("duration_ms"),
        "duration_api_ms": result.get("duration_api_ms"),
        "num_turns": result.get("num_turns"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
        "tool_calls": dict(tools),
        "bash_programs": dict(programs),
        "findings": dict(
            Counter(
                str(finding.get("severity"))
                for finding in findings or []
                if isinstance(finding, dict)
            )
        ),
        "session_id": result.get("session_id"),
        **(
            {}
            if case.findings is None
            else {
                "expected_findings": len(case.findings),
                "found": run_grade.found,
                "missed": list(run_grade.missed),
                "false_positives": list(run_grade.false_positives),
                "extra": run_grade.extra,
            }
        ),
    }


def _programs(command: str) -> list[str]:
    """The program of each command in a shell line: the first word after a
    command separator, without variable assignments and shell keywords.
    Quoted text stays one word."""

    lexer = shlex.shlex(command.replace("\n", ";"), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True

    try:
        tokens = list(lexer)
    except ValueError:
        return []

    programs = []
    start = True

    for token in tokens:
        if set(token) <= set(";&|()"):
            start = True
        elif start and token not in _SHELL_KEYWORDS and not re.match(r"^\w+=", token):
            programs.append(token)
            start = False

    return programs


def _tool_uses(transcript: Path) -> list[dict[str, Any]]:
    tool_uses = []

    try:
        lines = transcript.read_text().splitlines()
    except OSError:
        return []

    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue

        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue

        for content in (event.get("message") or {}).get("content") or []:
            if isinstance(content, dict) and content.get("type") == "tool_use":
                tool_uses.append(content)

    return tool_uses
