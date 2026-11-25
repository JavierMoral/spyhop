import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from spyhop.application.evals import (
    EVAL_TICKETS_ENV_VAR,
    REVIEW_SCHEMA,
    EvalCase,
    ExpectedFinding,
    Lines,
    _programs,
    base_content,
    build_repository,
    case_from_commit,
    grade,
    grade_review,
    load_cases,
)
from spyhop.cli import app
from spyhop.config.paths import database_file
from spyhop.domain.exceptions import ConfigurationError
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import ExecutionRepository

HEAD = "a" * 40

BASH_EVENT = {
    "type": "assistant",
    "message": {
        "content": [
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "f=a.java; git diff && sed -n 1,9p $f | git grep x"},
            }
        ]
    },
}

TASK = (
    "# Task: LPD-1\n\n## Eval Repository\n\nThis repository contains only these files. "
    "Do not search for other files.\n\n- `src/FooImpl.java`\n"
)

PASS_YAML = "ticket: LPD-1\nmessage: LPD-1 Fix\nexpect:\n  verdict: PASS\n"
FAIL_YAML = "ticket: LPD-1\nmessage: LPD-1 Fix\nexpect:\n  verdict: FAIL\n  files: [FooImpl.java]\n"


def write_case(checkpoint_dir: Path, name: str, content: str) -> Path:
    directory = checkpoint_dir / name

    for side, text in (("base", "old\n"), ("head", "new\n")):
        (directory / side / "src").mkdir(parents=True)
        (directory / side / "src" / "FooImpl.java").write_text(text)

    (directory / "case.yaml").write_text(content)

    return directory


def payload(verdict: str, findings: list[dict[str, Any]], head: str = HEAD) -> dict[str, Any]:
    return {
        "exit_code": 0,
        "output": {
            "structured_output": {
                "verdict": verdict,
                "head": head,
                "commands": [{"command": "git diff", "result": "0"}],
                "findings": findings,
            }
        },
    }


REVIEW_YAML = """message: LPD-1 Add Foo
expect:
  findings:
    - {rule: "202", check: ORD-01, file: FooImpl.java, lines: [10, 12]}
    - {check: NAM-04, file: FooImpl.java, lines: 30}
  clean:
    - {file: FooImpl.java, lines: [40, 50]}
"""

REVIEW_CASE = EvalCase(
    "review",
    Path(),
    None,
    "LPD-1 Add Foo",
    None,
    findings=(
        ExpectedFinding((Lines("FooImpl.java", 10, 12),), ("202",), ("ORD-01",)),
        ExpectedFinding((Lines("FooImpl.java", 30, 30),), (), ("NAM-04",)),
    ),
    clean=(Lines("FooImpl.java", 40, 50),),
)

ANY_CASE = EvalCase(
    "rename",
    Path(),
    None,
    "LPD-1 Add Foo",
    None,
    findings=(
        ExpectedFinding(
            (Lines("FooImpl.java", 10, 10), Lines("BarImpl.java", 80, 80)), ("101",), ()
        ),
    ),
)


def review_payload(*findings: dict[str, Any]) -> dict[str, Any]:
    return {
        "exit_code": 0,
        "output": {
            "structured_output": {
                "scope": "1 file",
                "findings": list(findings),
                "dismissed": [],
                "clean": [],
            }
        },
    }


def review_finding(
    line: int, rule: str | None = None, check: str | None = None, file: str = "src/FooImpl.java"
) -> dict[str, Any]:
    return {
        "severity": "WARN",
        "rule": rule,
        "check": check,
        "file": file,
        "line": line,
        "end_line": None,
        "summary": "s",
    }


FAIL_CASE = EvalCase("partial", Path(), "LPD-1", "LPD-1 Fix", "FAIL", ("FooImpl.java",))
PASS_CASE = EvalCase("fix", Path(), "LPD-1", "LPD-1 Fix", "PASS")


def test_load_cases_filters_by_name(tmp_path: Path) -> None:
    write_case(tmp_path, "lpd-1-fix", PASS_YAML)
    directory = write_case(tmp_path, "lpd-1-partial", FAIL_YAML)

    assert load_cases(tmp_path, "*-partial") == [
        EvalCase("lpd-1-partial", directory, "LPD-1", "LPD-1 Fix", "FAIL", ("FooImpl.java",))
    ]


def test_load_cases_rejects_a_fail_case_without_files(tmp_path: Path) -> None:
    write_case(tmp_path, "bad", PASS_YAML.replace("PASS", "FAIL"))

    with pytest.raises(ConfigurationError, match="needs expect.files"):
        load_cases(tmp_path)


def test_load_cases_rejects_a_case_without_head(tmp_path: Path) -> None:
    directory = write_case(tmp_path, "bad", PASS_YAML)

    (directory / "head" / "src" / "FooImpl.java").unlink()
    (directory / "head" / "src").rmdir()
    (directory / "head").rmdir()

    with pytest.raises(ConfigurationError, match="no head/"):
        load_cases(tmp_path)


def test_load_cases_reads_a_review_case_without_a_ticket(tmp_path: Path) -> None:
    directory = write_case(tmp_path, "review", REVIEW_YAML)

    assert load_cases(tmp_path) == [
        EvalCase(
            "review",
            directory,
            None,
            REVIEW_CASE.message,
            None,
            findings=REVIEW_CASE.findings,
            clean=REVIEW_CASE.clean,
        )
    ]


def test_load_cases_rejects_an_expected_finding_without_a_rule_or_a_check(
    tmp_path: Path,
) -> None:
    write_case(tmp_path, "bad", REVIEW_YAML.replace('rule: "202", check: ORD-01, ', ""))

    with pytest.raises(ConfigurationError, match="needs a rule or a check"):
        load_cases(tmp_path)


def test_load_cases_rejects_a_case_with_a_verdict_and_findings(tmp_path: Path) -> None:
    write_case(tmp_path, "bad", REVIEW_YAML.replace("expect:\n", "expect:\n  verdict: PASS\n"))

    with pytest.raises(ConfigurationError, match="expect.verdict or expect.findings"):
        load_cases(tmp_path)


def test_build_repository_adds_the_shared_files_and_origin_master(tmp_path: Path) -> None:
    (tmp_path / "cases" / "shared" / "rules").mkdir(parents=True)
    (tmp_path / "cases" / "shared" / "rules" / "202.md").write_text("sort\n")
    write_case(tmp_path / "cases", "review", REVIEW_YAML)
    (case,) = load_cases(tmp_path / "cases")

    base, head = build_repository(case, tmp_path / "one", None)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path / "one"), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    assert git("rev-parse", "origin/master") == base
    assert git("show", f"{base}:rules/202.md") == "sort"
    assert git("diff", "--name-only", base, head) == "src/FooImpl.java"
    assert not (tmp_path / "one" / "CLAUDE-TASK.md").exists()


def test_grade_review_passes_when_each_expected_finding_is_given() -> None:
    run_grade = grade_review(
        REVIEW_CASE,
        review_payload(
            review_finding(14, rule="202"),
            review_finding(30, check="NAM-04"),
            review_finding(60, rule="101"),
        ),
    )

    assert run_grade.passed
    assert (run_grade.found, run_grade.extra) == (2, 1)


def test_grade_review_misses_a_finding_with_another_id_or_far_lines() -> None:
    run_grade = grade_review(
        REVIEW_CASE,
        review_payload(review_finding(11, rule="203"), review_finding(34, check="NAM-04")),
    )

    assert not run_grade.passed
    assert run_grade.missed == (
        "202|ORD-01 FooImpl.java:10 (cited as 203|None)",
        "NAM-04 FooImpl.java:30",
    )


def test_grade_review_matches_any_place_of_an_expected_finding() -> None:
    assert grade_review(
        ANY_CASE, review_payload(review_finding(81, "101", file="BarImpl.java"))
    ).passed
    assert not grade_review(ANY_CASE, review_payload(review_finding(50, "101"))).passed


def test_load_cases_reads_the_places_of_an_expected_finding(tmp_path: Path) -> None:
    write_case(
        tmp_path,
        "rename",
        "message: LPD-1 Add Foo\nexpect:\n  findings:\n    - {rule: 101, any: ["
        "{file: FooImpl.java, lines: 10}, {file: BarImpl.java, lines: [80, 80]}]}\n",
    )

    (case,) = load_cases(tmp_path)

    assert case.findings == ANY_CASE.findings


def test_grade_review_accepts_each_listed_id(tmp_path: Path) -> None:
    write_case(
        tmp_path,
        "order",
        "message: LPD-1 Add Foo\nexpect:\n  findings:\n"
        '    - {rule: ["202", "907"], check: [ORD-04, CON-03], file: FooImpl.java, lines: 10}\n',
    )

    (case,) = load_cases(tmp_path)

    assert grade_review(case, review_payload(review_finding(10, "907"))).passed
    assert grade_review(case, review_payload(review_finding(10, check="CON-03"))).passed
    assert not grade_review(case, review_payload(review_finding(10, "201"))).passed


def test_grade_review_fails_a_finding_on_clean_lines() -> None:
    run_grade = grade_review(
        REVIEW_CASE,
        review_payload(
            review_finding(10, check="ORD-01"),
            review_finding(30, check="NAM-04"),
            review_finding(45, rule="503"),
        ),
    )

    assert not run_grade.passed
    assert run_grade.false_positives == ("503 src/FooImpl.java:45",)


def test_grade_review_reads_the_json_of_a_text_answer() -> None:
    text = json.dumps(
        {"findings": [review_finding(10, check="ORD-01"), review_finding(30, check="NAM-04")]}
    )
    run_grade = grade_review(
        REVIEW_CASE,
        {"exit_code": 0, "output": {"result": f"Here is the report.\n\n```json\n{text}\n```"}},
    )

    assert run_grade.passed


def test_grade_review_fails_a_result_without_findings() -> None:
    run_grade = grade_review(REVIEW_CASE, {"exit_code": 1, "output": None})

    assert run_grade.reason == "invalid result: claude exited with 1"


def test_build_repository_commits_base_and_head(tmp_path: Path) -> None:
    write_case(tmp_path / "cases", "lpd-1-fix", PASS_YAML)
    (case,) = load_cases(tmp_path / "cases")

    base, head = build_repository(case, tmp_path / "one", "# Task: LPD-1\n")

    diff = subprocess.run(
        ["git", "-C", str(tmp_path / "one"), "diff", base, head],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert "-old\n+new" in diff
    assert (tmp_path / "one" / "CLAUDE-TASK.md").read_text() == TASK
    assert build_repository(case, tmp_path / "two", "") == (base, head)


def test_grade_passes_a_fail_with_a_finding_in_an_expected_file() -> None:
    finding = {"severity": "high", "file": "a/b/FooImpl.java", "summary": "s"}

    assert grade(FAIL_CASE, payload("FAIL", [finding]), HEAD).passed


def test_grade_fails_a_fail_with_only_low_findings_in_the_expected_file() -> None:
    finding = {"severity": "low", "file": "a/b/FooImpl.java", "summary": "s"}

    assert not grade(FAIL_CASE, payload("FAIL", [finding]), HEAD).passed


def test_grade_needs_a_finding_at_each_expected_place(tmp_path: Path) -> None:
    directory = write_case(
        tmp_path,
        "lpd-1-variant",
        "ticket: LPD-1\nmessage: LPD-1 Fix\nexpect:\n  verdict: FAIL\n  places:\n"
        "  - {file: FooImpl.java, lines: [40, 42]}\n  - {file: BarImpl.java, lines: 7}\n",
    )
    (case,) = load_cases(tmp_path)

    def finding(file: str, line: int, severity: str = "high") -> dict[str, Any]:
        return {"severity": severity, "file": f"a/{file}", "line": line, "summary": "s"}

    assert case.places == (
        Lines("FooImpl.java", 40, 42),
        Lines("BarImpl.java", 7, 7),
    )
    assert directory == case.directory
    assert grade(
        case, payload("FAIL", [finding("FooImpl.java", 44), finding("BarImpl.java", 7)]), HEAD
    ).passed

    # A finding at the fixed place of the same file, and a low finding, do
    # not cover the variants.

    result = grade(
        case,
        payload("FAIL", [finding("FooImpl.java", 10), finding("BarImpl.java", 7, "low")]),
        HEAD,
    )

    assert result.reason == (
        "FAIL, but no high or medium finding at FooImpl.java:40, BarImpl.java:7"
    )


def test_grade_fails_a_wrong_verdict() -> None:
    assert grade(FAIL_CASE, payload("PASS", []), HEAD).reason == "PASS, expected FAIL"


def test_grade_fails_a_result_for_another_head() -> None:
    result = grade(PASS_CASE, payload("PASS", [], head="b" * 40), HEAD)

    assert result.reason.startswith("invalid result: head")


def test_ticket_reads_the_eval_ticket(tmp_path: Path) -> None:
    (tmp_path / "LPD-1.txt").write_text("# LPD-1: Title\n")

    result = CliRunner().invoke(app, ["ticket", "lpd-1"], env={EVAL_TICKETS_ENV_VAR: str(tmp_path)})

    assert result.exit_code == 0
    assert result.output == "# LPD-1: Title\n"


def test_ticket_fails_without_the_eval_ticket(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["ticket", "LPD-2"], env={EVAL_TICKETS_ENV_VAR: str(tmp_path)})

    assert result.exit_code == 1


@pytest.fixture
def evals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        "spyhop.cli.evals._load",
        lambda _: SimpleNamespace(application=SimpleNamespace(dry_run=False)),
    )
    # The eval sets the variable in os.environ. A delenv of a missing
    # variable records nothing, so set it first: the teardown removes it.
    monkeypatch.setenv(EVAL_TICKETS_ENV_VAR, "")
    monkeypatch.delenv(EVAL_TICKETS_ENV_VAR)

    (tmp_path / "claude" / "agents").mkdir(parents=True)
    (tmp_path / "claude" / "agents" / "checkpoint-security.md").write_text("prompt")

    directory = tmp_path / "data" / "spyhop" / "evals"

    write_case(directory / "security", "lpd-1-fix", PASS_YAML)
    (directory / "tickets").mkdir()
    (directory / "tickets" / "LPD-1.task.md").write_text("# Task: LPD-1\n")

    return directory


def test_eval_runs_each_case_and_writes_the_summary(
    evals: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def run_checkpoint(
        name: str, base: str, head_sha: str, root: Path, transcript: Path
    ) -> tuple[int, dict[str, object]]:
        calls.append(
            (
                name,
                (root / "CLAUDE-TASK.md").read_text(),
                (root / "src" / "FooImpl.java").read_text(),
            )
        )
        transcript.write_text(json.dumps(BASH_EVENT) + "\n")

        return 0, payload("PASS", [], head_sha)["output"] | {"session_id": f"s{len(calls)}"}

    monkeypatch.setattr("spyhop.cli.evals._run_checkpoint", run_checkpoint)

    result = CliRunner().invoke(app, ["eval", "security", "--runs", "2"])

    assert result.exit_code == 0, result.output
    assert calls == [("security", TASK, "new\n")] * 2

    summary_file = next(evals.glob("security/results/*/summary.json"))

    assert json.loads(summary_file.read_text()) == [
        {
            "case": "lpd-1-fix",
            "expected": "PASS",
            "passed": 2,
            "runs": 2,
            "obtained": ["PASS", "PASS"],
            "reasons": ["PASS", "PASS"],
        }
    ]
    assert "2/2   PASS      PASS PASS lpd-1-fix\n" in result.output
    assert "2/2 runs passed" in result.output

    agent_sha = hashlib.sha256(b"prompt").hexdigest()
    lines = (evals / "metrics.jsonl").read_text().splitlines()

    assert (evals / "agents" / f"{agent_sha}.md").read_text() == "prompt"
    assert [json.loads(line)["run"] for line in lines] == [1, 2]
    assert json.loads(lines[0]) | {"at": None, "eval_id": None, "claude_version": None} == {
        "at": None,
        "eval_id": None,
        "checkpoint": "security",
        "agent_sha": agent_sha,
        "claude_version": None,
        "run": 1,
        "case": "lpd-1-fix",
        "expected": "PASS",
        "verdict": "PASS",
        "passed": True,
        "reason": "PASS",
        "exit_code": 0,
        "is_error": None,
        "models": [],
        "cost_usd": None,
        "duration_ms": None,
        "duration_api_ms": None,
        "num_turns": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_creation_tokens": None,
        "tool_calls": {"Bash": 1},
        "bash_programs": {"git": 2, "sed": 1},
        "findings": {},
        "session_id": "s1",
    }

    executions = ExecutionRepository(connect(database_file(dry_run=False))).list()

    assert [(e.session_id, e.agent, e.source, e.outcome, e.agent_sha) for e in executions] == [
        ("s1", "checkpoint-security", "eval", "PASS", agent_sha),
        ("s2", "checkpoint-security", "eval", "PASS", agent_sha),
    ]
    assert executions[0].profile is not None
    assert executions[0].profile["calls"] == {"Bash": 1}


def test_eval_fails_a_run_that_changes_the_repository(
    evals: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run_checkpoint(
        name: str, base: str, head_sha: str, root: Path, transcript: Path
    ) -> tuple[int, dict[str, object]]:
        (root / "changed.txt").write_text("")

        return 0, payload("PASS", [], head_sha)["output"]

    monkeypatch.setattr("spyhop.cli.evals._run_checkpoint", run_checkpoint)

    result = CliRunner().invoke(app, ["eval", "security"])

    assert result.exit_code == 1
    assert "the agent changed the repository" in result.output


def test_programs_keeps_quoted_pipes_in_one_word() -> None:
    command = 'f=a.java; if grep -q "x\\|y" $f; then sed -n 1p $f | head -1; fi'

    assert _programs(command) == ["grep", "sed", "head"]


def test_eval_grades_a_review_agent_by_its_findings(
    tmp_path: Path, evals: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "claude" / "agents" / "reviewer.md").write_text("review prompt")
    write_case(evals / "reviewer", "review", REVIEW_YAML)

    calls = []

    def run_agent(
        agent: str, prompt: str, schema: dict[str, Any], root: Path, transcript: Path
    ) -> tuple[int, dict[str, object]]:
        calls.append((agent, prompt.startswith("Review the commits"), schema))

        return 0, review_payload(review_finding(10, check="ORD-01"))["output"]

    monkeypatch.setattr("spyhop.cli.evals._run_agent", run_agent)

    result = CliRunner().invoke(app, ["eval", "reviewer"])

    assert result.exit_code == 1, result.output
    assert calls == [("reviewer", True, REVIEW_SCHEMA)]
    assert "0/1   2/2       1/2       review  <-\n" in result.output

    metrics = json.loads((evals / "metrics.jsonl").read_text())

    assert {key: metrics[key] for key in ("found", "missed", "false_positives", "extra")} == {
        "found": 1,
        "missed": ["NAM-04 FooImpl.java:30"],
        "false_positives": [],
        "extra": 0,
    }


def test_eval_rejects_a_case_of_another_kind(evals: Path, tmp_path: Path) -> None:
    (tmp_path / "claude" / "agents" / "reviewer.md").write_text("review prompt")
    write_case(evals / "reviewer", "lpd-1-fix", PASS_YAML)

    result = CliRunner().invoke(app, ["eval", "reviewer"])

    assert result.exit_code == 1
    assert "These cases have no expect.findings: lpd-1-fix" in result.output


def fix_repository(tmp_path: Path) -> tuple[Path, str]:
    """A repository whose last commit fixes the order of two calls, adds one
    file, and deletes another one. Returns the repository and the commit."""

    root = tmp_path / "portal"

    def git(*args: str) -> str:
        return subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test",
                *args,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    (root / "src").mkdir(parents=True)
    git("init", "-q")
    (root / "src" / "Foo.java").write_text("a\nfoo.setB();\nfoo.setA();\nz\n")
    (root / "src" / "Old.java").write_text("old\n")
    git("add", "-A")
    git("commit", "-q", "-m", "LPD-7 Add Foo")
    (root / "src" / "Foo.java").write_text("a\nfoo.setA();\nfoo.setB();\nz\n")
    (root / "src" / "Old.java").unlink()
    (root / "src" / "New.java").write_text("new\n")
    git("add", "-A")
    git("commit", "-q", "-m", "LPD-7 Sort the setters")

    return root, git("rev-parse", "HEAD")


def test_case_from_commit_adds_only_the_lines_before_the_fix(tmp_path: Path) -> None:
    repository, commit = fix_repository(tmp_path)
    case_dir = tmp_path / "evals" / "reviewer" / "202-fix"

    assert case_from_commit(repository, commit[:8], case_dir, "202", "ORD-01") == 2

    assert (case_dir / "base" / "src" / "Foo.java").read_text() == "a\nfoo.setB();\nz\n"
    assert (
        case_dir / "head" / "src" / "Foo.java"
    ).read_text() == "a\nfoo.setB();\nfoo.setA();\nz\n"
    assert not (case_dir / "base" / "src" / "New.java").exists()
    assert not (case_dir / "head" / "src" / "New.java").exists()
    assert (case_dir / "head" / "src" / "Old.java").exists()
    assert not (case_dir / "base" / "src" / "Old.java").exists()

    (case,) = load_cases(case_dir.parent)

    assert case.message == "LPD-7 Update the code"
    assert case.findings == (
        ExpectedFinding((Lines("src/Foo.java", 3, 3),), ("202",), ("ORD-01",)),
        ExpectedFinding((Lines("src/Old.java", 1, 1),), ("202",), ("ORD-01",)),
    )


def test_case_from_commit_gives_one_finding_for_all_hunks(tmp_path: Path) -> None:
    repository, commit = fix_repository(tmp_path)
    case_dir = tmp_path / "evals" / "reviewer" / "101-fix"

    assert case_from_commit(repository, commit, case_dir, "101", None, one=True) == 1

    (case,) = load_cases(case_dir.parent)

    assert case.findings == (
        ExpectedFinding((Lines("src/Foo.java", 3, 3), Lines("src/Old.java", 1, 1)), ("101",), ()),
    )


def test_case_from_commit_needs_a_rule_or_a_check(tmp_path: Path) -> None:
    repository, commit = fix_repository(tmp_path)

    with pytest.raises(ConfigurationError, match="give a rule or a check"):
        case_from_commit(repository, commit, tmp_path / "case", None, None)


def test_eval_case_does_not_overwrite_a_case(evals: Path, tmp_path: Path) -> None:
    repository, commit = fix_repository(tmp_path)
    arguments = ["eval-case", "reviewer", "--repo", str(repository), "--commit", commit]

    first = CliRunner().invoke(app, [*arguments, "--check", "ORD-01", "--name", "one"])
    second = CliRunner().invoke(app, [*arguments, "--check", "ORD-01", "--name", "one"])

    assert first.exit_code == 0, first.output
    assert "with 2 expected findings" in first.output
    assert second.exit_code == 1
    assert "already exists" in second.output


def test_eval_case_reports_an_unknown_commit(evals: Path, tmp_path: Path) -> None:
    repository, _ = fix_repository(tmp_path)

    result = CliRunner().invoke(
        app,
        ["eval-case", "reviewer", "--repo", str(repository), "--commit", "f" * 8, "--rule", "1"],
    )

    assert result.exit_code == 1
    assert result.output.startswith("git failed:")


JAVA_HEAD = (
    "public class Foo {\n"
    "\n"
    "\tpublic int count() {\n"
    "\t\treturn _hasItems() ? 1 : 0;\n"
    "\t}\n"
    "\n"
    "\t/**\n"
    "\t * Returns true.\n"
    "\t */\n"
    "\t@Override\n"
    "\tpublic boolean isFoo() {\n"
    "\t\treturn true;\n"
    "\t}\n"
    "\n"
    "\tprivate boolean _hasItems() {\n"
    "\t\treturn true;\n"
    "\t}\n"
    "\n"
    "\tprivate final String _name;\n"
    "\n"
    "}\n"
)


def test_base_content_removes_the_member_of_each_added_java_line() -> None:
    # Line 4 is in count(), line 11 is the signature of isFoo(), and line 19
    # is a field.

    base = base_content(JAVA_HEAD.encode(), {4, 11, 19}, "src/Foo.java").decode()

    assert base == (
        "public class Foo {\n\n\tprivate boolean _hasItems() {\n\t\treturn true;\n\t}\n\n}\n"
    )


def test_base_content_removes_only_the_added_lines_of_other_files() -> None:
    assert base_content(b"a\nb\nc\n", {2}, "page.jsp") == b"a\nc\n"


def test_base_content_keeps_the_member_before_an_added_annotation() -> None:
    head = (
        "public class Foo {\n\n"
        "\tpublic int getA() {\n\t\treturn 1;\n\t}\n\n"
        "\t@Input\n\tpublic boolean getB() {\n\t\treturn true;\n\t}\n\n"
        "}\n"
    )

    # Lines 7 to 10 are the annotation and the method getB().

    base = base_content(head.encode(), {7, 8, 9, 10}, "src/Foo.java").decode()

    assert base == "public class Foo {\n\n\tpublic int getA() {\n\t\treturn 1;\n\t}\n\n}\n"
