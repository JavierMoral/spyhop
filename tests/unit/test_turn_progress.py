"""The progress of a headless turn, from its log."""

from pathlib import Path

from spyhop.application.turn_progress import read_progress


def test_each_summary_reads_what_is_new_and_leaves_a_line_in_progress(tmp_path: Path) -> None:
    log = tmp_path / "turn.log"

    log.write_text(
        "Step implement (start s-1) in /wt\n"
        "Empiezo leyendo el plan.\n"
        '> Read: {"file_path": "CLAUDE-PLAN.md"}\n'
        '> Bash: {"command": "./gradlew test"}\n'
        "Test 1 en verde.\n"
        '> Bash: {"command": "ls -l'
    )

    first = read_progress(log, 0)

    assert (first.calls, first.last_call, first.last_text) == (
        2,
        'Bash: {"command": "./gradlew test"}',
        "Test 1 en verde.",
    )

    with log.open("a") as file:
        file.write('a build"}\n')

    second = read_progress(log, first.offset)

    assert (second.calls, second.last_call, second.last_text) == (
        1,
        'Bash: {"command": "ls -la build"}',
        None,
    )
    assert read_progress(log, second.offset).calls == 0
    assert read_progress(tmp_path / "missing.log", 0).calls == 0
