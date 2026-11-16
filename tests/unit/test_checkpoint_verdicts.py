import pytest

from spyhop.application.checkpoint_verdicts import (
    Problem,
    check_verdicts,
    fingerprint,
    run_reason,
)
from spyhop.domain.enums import WaitingReason

HEAD = "a" * 40
OLD = "b" * 40
BASE = "c" * 40
UPDATED = "2026-11-16T10:21:33.123+0100"
NOW = f"head={HEAD}"


def table(*rows: str, header: str = "| Checkpoint | Verdict | Inputs | Rounds | Report |") -> str:
    return "\n".join(
        ["# Checkpoints", "", header, "| --- | :---: | --- | --- | --- |", *rows, "", "Notes."]
    )


def row(name: str, verdict: str = "PASS", inputs: str = NOW) -> str:
    return f"| {name} | {verdict} | {inputs} | 1 | CLAUDE-CHECKPOINT-{name}.md |"


def test_the_fingerprint_gives_the_inputs_in_a_fixed_order() -> None:
    assert fingerprint({"head": HEAD, "ticket": UPDATED, "base": BASE}) == (
        f"ticket={UPDATED} base={BASE} head={HEAD}"
    )


def test_all_checkpoints_pass_on_their_inputs() -> None:
    reproduce = f"ticket={UPDATED} base={BASE}"
    text = table(row("regressions"), row("reproduce", inputs=reproduce), row("removed", "FAIL"))

    assert check_verdicts(text, {"regressions": NOW, "reproduce": reproduce}) == []


def test_each_checkpoint_gets_its_problem() -> None:
    text = table(row("mutation", "FAIL"), row("security", inputs=f"head={OLD}"))

    problems = check_verdicts(text, {"regressions": NOW, "mutation": NOW, "security": NOW})

    assert problems == [
        Problem("regressions", WaitingReason.CHECKPOINTS_MISSING, "no verdict"),
        Problem("mutation", WaitingReason.CHECKPOINTS_FAILED, "FAIL on head=aaaaaaaaaaaa"),
        Problem(
            "security",
            WaitingReason.CHECKPOINTS_STALE,
            "PASS on head=bbbbbbbbbbbb, now head=aaaaaaaaaaaa",
        ),
    ]
    assert run_reason(problems) is WaitingReason.CHECKPOINTS_MISSING


def test_a_stale_verdict_names_only_the_inputs_that_changed() -> None:
    text = table(row("reproduce", inputs=f"ticket={UPDATED} base={BASE}"))

    [problem] = check_verdicts(
        text, {"reproduce": f"ticket=2026-11-17T08:00:00.000+0100 base={BASE}"}
    )

    assert problem.reason is WaitingReason.CHECKPOINTS_STALE
    assert problem.detail == (f"PASS on ticket={UPDATED}, now ticket=2026-11-17T08:00:00.000+0100")


def test_a_verdict_for_other_inputs_is_stale() -> None:
    [problem] = check_verdicts(table(row("reproduce")), {"reproduce": f"base={BASE} head={HEAD}"})

    assert problem.reason is WaitingReason.CHECKPOINTS_STALE
    assert problem.detail == "PASS on no base, now base=cccccccccccc"


def test_a_failed_checkpoint_comes_before_a_stale_one() -> None:
    problems = check_verdicts(
        table(row("mutation", "FAIL"), row("security", inputs=f"head={OLD}")),
        {"mutation": NOW, "security": NOW},
    )

    assert run_reason(problems) is WaitingReason.CHECKPOINTS_FAILED


def test_columns_are_found_by_their_name() -> None:
    text = table(
        f"| {NOW} | 2 | PASS | regressions |", header="| Inputs | Rounds | Verdict | Checkpoint |"
    )

    assert check_verdicts(text, {"regressions": NOW}) == []


def test_a_workflow_without_checkpoints_needs_no_file() -> None:
    assert check_verdicts(None, {}) == []


def test_a_missing_file_is_a_missing_verdict() -> None:
    assert check_verdicts(None, {"regressions": NOW}) == [
        Problem(None, WaitingReason.CHECKPOINTS_MISSING, "CLAUDE-CHECKPOINTS.md not found")
    ]


@pytest.mark.parametrize(
    ("text", "detail"),
    [
        ("# Checkpoints\n\nNothing yet.", "no table"),
        (table(header="| Checkpoint | Verdict | HEAD | Report | Notes |"), "no Inputs column"),
        ("| Checkpoint | Verdict | Inputs |\n" + row("regressions"), "no separator row"),
        (table(f"| regressions | PASS | {NOW} |"), "row 1 has 3 cells, not 5"),
        (table(row("regressions", "pass")), "row 1: verdict 'pass' is not PASS or FAIL"),
        (table(row("regressions", inputs=f"head={HEAD[:12]}")), "is not a full SHA"),
        (table(row("regressions", inputs=HEAD)), "is not name=value"),
        (table(row("regressions", inputs=f"plan={HEAD}")), "is not name=value"),
        (table(row("regressions", inputs="")), "no inputs"),
        (table(row("regressions", inputs=f"{NOW} {NOW}")), "head is more than once"),
        (table(row("regressions", inputs=f"{NOW} base={BASE}")), "does not give the inputs"),
        (table(row("regressions"), row("regressions")), "row 2: regressions has more than one"),
    ],
)
def test_a_wrong_table_is_a_missing_verdict(text: str, detail: str) -> None:
    [problem] = check_verdicts(text, {"regressions": NOW})

    assert problem.checkpoint is None
    assert problem.reason is WaitingReason.CHECKPOINTS_MISSING
    assert detail in problem.detail
