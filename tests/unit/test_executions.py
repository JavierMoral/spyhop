import dataclasses
import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from spyhop.application.executions import (
    ExecutionRecorder,
    error_kind,
    history,
    profile_history,
    read_transcript,
    result_execution,
    transcripts_dir,
)
from spyhop.domain.models import AgentExecution, OrcaWorktree
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import (
    CheckpointRunRepository,
    ExecutionRepository,
    RunRepository,
    SessionRepository,
    WorktreeRepository,
)

START = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


def at(seconds: int) -> str:
    return (START + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def prompt(seconds: int, text: str = "Run the plan") -> dict[str, Any]:
    return {"type": "user", "timestamp": at(seconds), "message": {"content": text}}


def tool_result(seconds: int) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": at(seconds),
        "message": {"content": [{"type": "tool_result", "content": "ok"}]},
    }


def reply(seconds: int, message_id: str, output: int, model: str = "claude-opus") -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": at(seconds),
        "message": {
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": 1,
                "output_tokens": output,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 10,
            },
        },
    }


def call(seconds: int, tool_id: str, name: str, command: str = "") -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": at(seconds),
        "message": {
            "id": f"m-{tool_id}",
            "content": [
                {"type": "tool_use", "id": tool_id, "name": name, "input": {"command": command}}
            ],
        },
    }


def result(seconds: int, tool_id: str, text: str, error: bool = False) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": at(seconds),
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": text, "is_error": error}
            ]
        },
    }


def write_jsonl(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))

    return path


def test_a_transcript_counts_each_message_once_and_the_waits_not(tmp_path: Path) -> None:
    path = write_jsonl(
        tmp_path / "s1.jsonl",
        [
            {"type": "custom-title", "customTitle": "implement sonnet"},
            prompt(0),
            # Two blocks of the same message, each with the usage of the message.
            reply(10, "m1", 50),
            reply(10, "m1", 50),
            tool_result(70),
            reply(80, "m2", 20),
            # A person answers one hour later: a wait, not work.
            prompt(3680, "Fix the findings"),
            reply(3700, "m3", 5),
        ],
    )

    write_jsonl(
        tmp_path / "s1" / "subagents" / "agent-a.jsonl",
        [reply(30, "m4", 7, "claude-haiku")],
    )

    transcript = read_transcript(path)

    assert transcript is not None
    assert transcript.title == "implement sonnet"
    assert (transcript.started_at, transcript.finished_at) == (
        START,
        START + timedelta(seconds=3700),
    )
    assert transcript.active_ms == 100_000
    assert transcript.consumption.turns == 2
    assert transcript.consumption.models == "claude-haiku,claude-opus"
    assert transcript.consumption.output_tokens == 82
    assert transcript.consumption.input_tokens == 4
    assert transcript.consumption.cache_read_tokens == 400
    assert transcript.consumption.cache_creation_tokens == 40


def test_a_transcript_gives_the_profile_of_its_tools(tmp_path: Path) -> None:
    usage = {"input_tokens": 1, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0}
    first = reply(1, "m1", 1) | {"message": {"id": "m1", "usage": usage}}
    largest = reply(9, "m9", 1) | {"message": {"id": "m9", "usage": usage | {"input_tokens": 501}}}
    path = write_jsonl(
        tmp_path / "s1.jsonl",
        [
            prompt(0),
            first,
            call(2, "t1", "Bash", "cd /wt && grep -rn foo src/main/java/com/liferay/portal/a"),
            result(3, "t1", "x" * 300),
            call(4, "t2", "Bash", "grep -rn foo src/main/java/com/liferay/portal/b"),
            result(5, "t2", "Exit code 1", error=True),
            call(6, "t3", "Read"),
            result(7, "t3", "PreToolUse:Read hook error: blocked", error=True),
            call(8, "t4", "Bash", "git push"),
            result(8, "t4", "Permission to use Bash with command git push has been denied.", True),
            largest,
        ],
    )

    write_jsonl(
        tmp_path / "s1" / "subagents" / "agent-a.jsonl",
        [
            call(5, "t5", "Grep")
            | {
                "message": {
                    "id": "m5",
                    "usage": usage | {"input_tokens": 9e6},
                    "content": [{"type": "tool_use", "id": "t5", "name": "Grep"}],
                }
            },
            result(6, "t5", [{"type": "text", "text": "abc"}]),  # type: ignore[arg-type]
        ],
    )

    transcript = read_transcript(path)

    assert transcript is not None
    assert transcript.context_added_tokens == 500
    assert transcript.profile == {
        "calls": {"Bash": 3, "Read": 1, "Grep": 1},
        "result_bytes": {"Bash": 372, "Read": 35, "Grep": 3},
        "max_result_bytes": {"Bash": 300, "Read": 35, "Grep": 3},
        "repeated_bash": 1,
        "errors": {"exit": 1, "hook": 1, "denied": 1},
    }


def test_the_kind_of_an_error_comes_from_its_text() -> None:
    assert [
        error_kind(text)
        for text in (
            "Exit code 143",
            "PreToolUse:Bash hook error: [guard.py]: blocked",
            "Permission to use Bash with command rm has been denied.",
            "The user doesn't want to proceed with this tool use.",
            "<tool_use_error>String to replace not found in file.</tool_use_error>",
            "File does not exist.",
        )
    ] == ["exit", "hook", "denied", "denied", "tool", "other"]


def test_a_transcript_without_messages_gives_nothing(tmp_path: Path) -> None:
    path = write_jsonl(tmp_path / "s1.jsonl", [{"type": "custom-title", "customTitle": "x"}])

    assert read_transcript(path) is None


def test_a_result_event_gives_the_tokens_of_all_its_models() -> None:
    result = {
        "session_id": "s1",
        "duration_ms": 90_000,
        "num_turns": 7,
        "total_cost_usd": 0.5,
        "modelUsage": {
            "claude-sonnet": {
                "inputTokens": 3,
                "outputTokens": 100,
                "cacheReadInputTokens": 1000,
                "cacheCreationInputTokens": 50,
            },
            "claude-haiku": {"inputTokens": 1, "outputTokens": 10},
        },
    }

    execution = result_execution(result, "checkpoint-security", "eval", START, outcome="PASS")

    assert execution is not None
    assert execution.started_at == START - timedelta(seconds=90)
    assert (execution.wall_ms, execution.active_ms, execution.turns) == (90_000, 90_000, 7)
    assert execution.models == "claude-haiku,claude-sonnet"
    assert (execution.input_tokens, execution.output_tokens) == (4, 110)
    assert (execution.cache_read_tokens, execution.cache_creation_tokens) == (1000, 50)
    assert (execution.cost_usd, execution.outcome) == (0.5, "PASS")
    assert result_execution({"duration_ms": 1}, "checkpoint-security", "eval", START) is None


def test_the_recorder_records_the_agents_and_the_checkpoints_of_a_worktree(
    tmp_path: Path,
) -> None:
    connection = connect(tmp_path / "spyhop.db")
    run_id = _run(connection)
    worktree_path = tmp_path / "worktrees" / "LPD-1"
    worktree = WorktreeRepository(connection).create(
        "LPD-1",
        OrcaWorktree(f"repo::{worktree_path}", worktree_path, "LPD-1", "master", None, "", False),
        tmp_path / "portal",
        "master",
    )

    RunRepository(connection).mark_ready(run_id, worktree.id)
    SessionRepository(connection).create_launching(run_id, "claude", "sonnet", "op", "implement")

    claude = tmp_path / "claude"
    directory = transcripts_dir(claude, worktree_path)
    title = {"type": "custom-title"}

    write_jsonl(
        directory / "agent.jsonl",
        [title | {"customTitle": "implement sonnet"}, prompt(0), reply(5, "m1", 9)],
    )
    write_jsonl(
        directory / "supervisor.jsonl",
        [title | {"customTitle": "spyhop"}, prompt(0), reply(5, "m2", 3)],
    )
    # A session of a person, and a session of a role that the run does not have.
    write_jsonl(directory / "person.jsonl", [prompt(0), reply(5, "m3", 1)])
    write_jsonl(
        directory / "other.jsonl",
        [title | {"customTitle": "review opus"}, prompt(0), reply(5, "m4", 1)],
    )

    agents = claude / "agents"
    checkpoint_runs = CheckpointRunRepository(connection)
    row = checkpoint_runs.create(run_id, 0, 1, "security", "x", "b", "h", tmp_path / "results")

    agents.mkdir(parents=True)
    (agents / "checkpoint-security.md").write_text("prompt")
    os.utime(agents / "checkpoint-security.md", (0, 0))
    checkpoint_runs.finish(row.id, "PASS", None)
    row.output_file.parent.mkdir(parents=True)
    row.output_file.write_text(
        json.dumps(
            {
                "exit_code": 0,
                "output": {"session_id": "cp", "duration_ms": 1000, "usage": {"output_tokens": 4}},
            }
        )
    )

    write_jsonl(directory / "cp.jsonl", [call(0, "t1", "Bash", "ls"), result(1, "t1", "a")])

    executions = ExecutionRepository(connection)
    recorder = ExecutionRecorder(
        executions,
        RunRepository(connection),
        WorktreeRepository(connection),
        SessionRepository(connection),
        checkpoint_runs,
        claude,
        agents,
    )

    assert recorder.sweep() == 3
    assert recorder.sweep() == 0

    recorded = {execution.session_id: execution for execution in executions.list()}

    assert sorted(recorded) == ["agent", "cp", "supervisor"]
    assert (recorded["agent"].agent, recorded["agent"].output_tokens) == ("implement", 9)
    assert (recorded["agent"].run_id, recorded["agent"].issue_key) == (run_id, "LPD-1")
    assert recorded["supervisor"].agent == "supervisor"
    assert (recorded["cp"].agent, recorded["cp"].outcome) == ("checkpoint-security", "PASS")
    assert recorded["cp"].agent_sha == hashlib.sha256(b"prompt").hexdigest()
    assert recorded["cp"].profile is not None
    assert recorded["cp"].profile["calls"] == {"Bash": 1}
    assert recorded["agent"].profile is not None
    assert recorded["agent"].context_added_tokens == 0
    assert executions.list("implement") == [recorded["agent"]]


def test_the_history_gives_each_period_and_its_new_versions() -> None:
    def execution(day: int, active_ms: int, output: int, sha: str, outcome: str) -> AgentExecution:
        return AgentExecution(
            session_id=f"s{day}-{active_ms}",
            agent="checkpoint-security",
            source="pipeline",
            run_id=None,
            issue_key=None,
            agent_sha=sha,
            models=None,
            started_at=START + timedelta(days=day),
            finished_at=START + timedelta(days=day),
            wall_ms=active_ms,
            active_ms=active_ms,
            turns=None,
            input_tokens=10,
            output_tokens=output,
            cache_read_tokens=100,
            cache_creation_tokens=0,
            cost_usd=None,
            outcome=outcome,
        )

    rows = history(
        [
            execution(1, 1000, 5, "a" * 64, "PASS"),
            execution(0, 3000, 1, "a" * 64, "FAIL"),
            execution(0, 1000, 3, "a" * 64, "PASS"),
            execution(1, 2000, 7, "b" * 64, "PASS"),
        ],
        "day",
    )

    assert [(row.period, row.count, row.passed) for row in rows] == [
        ("2026-11-20", 2, 1),
        ("2026-11-21", 2, 2),
    ]
    assert [row.median_active_ms for row in rows] == [2000, 1500]
    assert [row.median_output_tokens for row in rows] == [2, 6]
    assert rows[0].median_input_tokens == 110
    assert rows[0].total_tokens == 224
    assert [row.new_versions for row in rows] == [["aaaaaaaa"], ["bbbbbbbb"]]
    assert history([execution(0, 1000, 1, "a" * 64, "PASS")], "week")[0].period == "2026-W47"


def test_the_profile_history_gives_the_medians_and_the_sums_of_each_period() -> None:
    def execution(number: int, calls: dict[str, int], errors: dict[str, int]) -> AgentExecution:
        return AgentExecution(
            session_id=f"s{number}",
            agent="implement",
            source="pipeline",
            run_id=None,
            issue_key=None,
            agent_sha=None,
            models=None,
            started_at=START,
            finished_at=START,
            wall_ms=0,
            active_ms=0,
            turns=None,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=None,
            outcome=None,
            context_added_tokens=number * 1000,
            profile={
                "calls": calls,
                "result_bytes": {"Bash": number * 100},
                "max_result_bytes": {"Bash": number * 50},
                "repeated_bash": number,
                "errors": errors,
            },
        )

    without_profile = dataclasses.replace(execution(9, {}, {}), session_id="s9", profile=None)
    [row] = profile_history(
        [
            execution(1, {"Bash": 4, "Read": 1}, {"exit": 1}),
            execution(3, {"Bash": 2}, {"exit": 1, "hook": 1}),
            without_profile,
        ],
        "day",
    )

    assert (row.period, row.count) == ("2026-11-20", 2)
    assert (row.median_calls, row.median_context_added_tokens) == (3.5, 2000)
    assert (row.median_result_bytes, row.max_result_bytes) == (200, 150)
    assert (row.median_repeated_bash, row.median_errors) == (2, 1.5)
    assert row.calls == {"Bash": 6, "Read": 1}
    assert row.errors == {"exit": 2, "hook": 1}


def _run(connection: sqlite3.Connection) -> str:
    connection.execute(
        "INSERT INTO automation_runs (id, issue_key, workflow_name, trigger_status, "
        "trigger_history_id, triggered_at, status, created_at) "
        "VALUES ('r1', 'LPD-1', 'bug_fix', 'In Progress', '7', ?, 'RUNNING', ?)",
        ((START - timedelta(hours=1)).isoformat(), (START - timedelta(hours=1)).isoformat()),
    )

    return "r1"
