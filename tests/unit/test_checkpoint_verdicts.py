import pytest

from spyhop.application.checkpoint_verdicts import Problem, check_verdicts, run_reason
from spyhop.domain.enums import WaitingReason

HEAD = "a" * 40
OLD = "b" * 40


def table(*rows: str, header: str = "| Checkpoint | Verdict | HEAD | Rounds | Report |") -> str:
    return "\n".join(
        ["# Checkpoints", "", header, "| --- | :---: | --- | --- | --- |", *rows, "", "Notes."]
    )


def row(name: str, verdict: str = "PASS", head: str = HEAD) -> str:
    return f"| {name} | {verdict} | {head} | 1 | CLAUDE-CHECKPOINT-{name}.md |"


def test_all_checkpoints_pass_on_head() -> None:
    text = table(row("regressions"), row("security"), row("removed", "FAIL"))

    assert check_verdicts(text, ["regressions", "security"], HEAD) == []


def test_each_checkpoint_gets_its_problem() -> None:
    text = table(row("mutation", "FAIL"), row("security", head=OLD))

    problems = check_verdicts(text, ["regressions", "mutation", "security"], HEAD)

    assert problems == [
        Problem("regressions", WaitingReason.CHECKPOINTS_MISSING, "no verdict"),
        Problem("mutation", WaitingReason.CHECKPOINTS_FAILED, "FAIL on aaaaaaaaaaaa"),
        Problem(
            "security",
            WaitingReason.CHECKPOINTS_STALE,
            "PASS on bbbbbbbbbbbb, HEAD is aaaaaaaaaaaa",
        ),
    ]
    assert run_reason(problems) is WaitingReason.CHECKPOINTS_MISSING


def test_a_failed_checkpoint_comes_before_a_stale_one() -> None:
    problems = check_verdicts(
        table(row("mutation", "FAIL"), row("security", head=OLD)), ["mutation", "security"], HEAD
    )

    assert run_reason(problems) is WaitingReason.CHECKPOINTS_FAILED


def test_columns_are_found_by_their_name() -> None:
    text = table(
        f"| {HEAD} | 2 | PASS | regressions |", header="| HEAD | Rounds | Verdict | Checkpoint |"
    )

    assert check_verdicts(text, ["regressions"], HEAD) == []


def test_a_workflow_without_checkpoints_needs_no_file() -> None:
    assert check_verdicts(None, [], HEAD) == []


def test_a_missing_file_is_a_missing_verdict() -> None:
    assert check_verdicts(None, ["regressions"], HEAD) == [
        Problem(None, WaitingReason.CHECKPOINTS_MISSING, "CLAUDE-CHECKPOINTS.md not found")
    ]


@pytest.mark.parametrize(
    ("text", "detail"),
    [
        ("# Checkpoints\n\nNothing yet.", "no table"),
        (table(header="| Checkpoint | Verdict | Rounds | Report | Notes |"), "no HEAD column"),
        ("| Checkpoint | Verdict | HEAD |\n" + row("regressions"), "no separator row"),
        (table(f"| regressions | PASS | {HEAD} |"), "row 1 has 3 cells, not 5"),
        (table(row("regressions", "pass")), "row 1: verdict 'pass' is not PASS or FAIL"),
        (table(row("regressions", head=HEAD[:12])), "is not a full SHA"),
        (table(row("regressions"), row("regressions")), "row 2: regressions has more than one"),
    ],
)
def test_a_wrong_table_is_a_missing_verdict(text: str, detail: str) -> None:
    [problem] = check_verdicts(text, ["regressions"], HEAD)

    assert problem.checkpoint is None
    assert problem.reason is WaitingReason.CHECKPOINTS_MISSING
    assert detail in problem.detail
