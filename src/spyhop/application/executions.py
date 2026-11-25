"""The history of the executions of each agent: its tokens and its times.

Each execution is one Claude session. The checkpoints and the evals give
their consumption in the result event of `claude -p`. The agents of a run and
the supervisor session run in Orca terminals, so their consumption comes from
the transcripts that Claude Code writes in ~/.claude/projects. A transcript
holds the title of its session ("plan opus", "spyhop"), which tells the agent.
"""

import hashlib
import json
import logging
import re
import statistics
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from spyhop.application.checkpoint_planner import AGENT_PREFIX
from spyhop.application.checkpoint_reports import read_output
from spyhop.application.supervisor_session import TITLE as SUPERVISOR_TITLE
from spyhop.domain.enums import WorktreeStatus
from spyhop.domain.models import AgentExecution, AutomationRun
from spyhop.infrastructure.persistence.repositories import (
    ExecutionRepository,
    RunRepository,
    SessionRepository,
    StepRunRepository,
    WorktreeRepository,
)

_log = logging.getLogger(__name__)

SUPERVISOR = "supervisor"

_TITLE_TYPES = ("custom-title", "agent-name")


@dataclass(frozen=True)
class Consumption:
    models: str | None
    turns: int | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


@dataclass(frozen=True)
class Transcript:
    title: str | None
    started_at: datetime
    finished_at: datetime
    active_ms: int
    consumption: Consumption
    context_added_tokens: int | None
    profile: dict[str, Any]


def result_consumption(result: dict[str, Any]) -> Consumption:
    """The consumption in the result event of `claude -p`. modelUsage also
    counts the subagents and the other models of the session."""

    model_usage = result.get("modelUsage")

    if isinstance(model_usage, dict) and model_usage:
        usages = [usage for usage in model_usage.values() if isinstance(usage, dict)]

        def total(key: str) -> int:
            return sum(int(usage.get(key) or 0) for usage in usages)

        return Consumption(
            ",".join(sorted(model_usage)),
            result.get("num_turns"),
            total("inputTokens"),
            total("outputTokens"),
            total("cacheReadInputTokens"),
            total("cacheCreationInputTokens"),
        )

    usage = result.get("usage") or {}

    return Consumption(
        None,
        result.get("num_turns"),
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        int(usage.get("cache_read_input_tokens") or 0),
        int(usage.get("cache_creation_input_tokens") or 0),
    )


def result_execution(
    result: dict[str, Any],
    agent: str,
    source: Literal["pipeline", "eval"],
    finished_at: datetime,
    run: AutomationRun | None = None,
    agent_sha: str | None = None,
    outcome: str | None = None,
    transcript: Path | None = None,
) -> AgentExecution | None:
    """The execution of a result event of `claude -p`, or None when the event
    has no session id. The session starts ``duration_ms`` before
    ``finished_at``. The profile comes from ``transcript``, the transcript or
    the stream of the session, when it is there."""

    session_id = result.get("session_id")

    if not isinstance(session_id, str):
        return None

    duration = int(result.get("duration_ms") or 0)
    consumption = result_consumption(result)
    cost = result.get("total_cost_usd")
    context_added, profile = (read_profile(transcript) if transcript else None) or (None, None)

    return AgentExecution(
        session_id=session_id,
        agent=agent,
        source=source,
        run_id=run.id if run else None,
        issue_key=run.issue_key if run else None,
        agent_sha=agent_sha,
        models=consumption.models,
        started_at=datetime.fromtimestamp(finished_at.timestamp() - duration / 1000, UTC),
        finished_at=finished_at,
        wall_ms=duration,
        active_ms=duration,
        turns=consumption.turns,
        input_tokens=consumption.input_tokens,
        output_tokens=consumption.output_tokens,
        cache_read_tokens=consumption.cache_read_tokens,
        cache_creation_tokens=consumption.cache_creation_tokens,
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        outcome=outcome,
        context_added_tokens=context_added,
        profile=profile,
    )


# A Bash call is a repetition when its first characters are the same as the
# first characters of an earlier call: a loop that a script can do in one call.
# A "cd <directory> &&" at the start does not count, because the agents start
# most of their commands with it.

BASH_PREFIX = 40

_LEADING_CD = re.compile(r"^(?:cd\s+\S+\s*(?:&&|;)\s*)+")


class ToolProfile:
    """Where an agent can improve: the calls of each tool, the size of their
    results, the repeated Bash calls, the errors of each kind, and the growth
    of the context. It reads the blocks of the messages, so it reads a Claude
    Code transcript and a stream of `claude -p --output-format stream-json`."""

    def __init__(self) -> None:
        self._names: dict[str, str] = {}
        self._calls: Counter[str] = Counter()
        self._result_bytes: Counter[str] = Counter()
        self._max_result_bytes: dict[str, int] = {}
        self._prefixes: set[str] = set()
        self._repeated_bash = 0
        self._errors: Counter[str] = Counter()
        self._first_context: int | None = None
        self._peak_context = 0

    def add(self, entry: dict[str, Any], context: bool = True) -> None:
        """Adds one entry. ``context`` is False for the entries of a subagent,
        which has its own context."""

        message = entry.get("message")

        if not isinstance(message, dict):
            return

        content = message.get("content")
        blocks = (
            [block for block in content if isinstance(block, dict)]
            if isinstance(content, list)
            else []
        )

        if entry.get("type") == "assistant":
            usage = message.get("usage")

            if context and isinstance(usage, dict):
                size = sum(
                    int(usage.get(key) or 0)
                    for key in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                )

                if self._first_context is None:
                    self._first_context = size

                self._peak_context = max(self._peak_context, size)

            for block in blocks:
                if block.get("type") == "tool_use":
                    self._add_call(block)
        elif entry.get("type") == "user":
            for block in blocks:
                if block.get("type") == "tool_result":
                    self._add_result(block)

    def context_added_tokens(self) -> int | None:
        if self._first_context is None:
            return None

        return self._peak_context - self._first_context

    def result(self) -> dict[str, Any]:
        return {
            "calls": dict(self._calls),
            "result_bytes": dict(self._result_bytes),
            "max_result_bytes": self._max_result_bytes,
            "repeated_bash": self._repeated_bash,
            "errors": dict(self._errors),
        }

    def _add_call(self, block: dict[str, Any]) -> None:
        name = str(block.get("name"))

        self._names[str(block.get("id"))] = name
        self._calls[name] += 1

        if name == "Bash":
            command = str((block.get("input") or {}).get("command") or "").strip()
            prefix = _LEADING_CD.sub("", command)[:BASH_PREFIX]

            if prefix in self._prefixes:
                self._repeated_bash += 1

            self._prefixes.add(prefix)

    def _add_result(self, block: dict[str, Any]) -> None:
        name = self._names.get(str(block.get("tool_use_id")), "unknown")
        text = _text(block.get("content"))
        size = len(text.encode())

        self._result_bytes[name] += size
        self._max_result_bytes[name] = max(self._max_result_bytes.get(name, 0), size)

        if block.get("is_error"):
            self._errors[error_kind(text)] += 1


def error_kind(text: str) -> str:
    """exit: a command with an exit code that is not zero. hook: a hook
    blocked the call. denied: the permissions or the person refused the call.
    tool: the input of the call is not valid."""

    if text.startswith("Exit code"):
        return "exit"

    if "hook error" in text[:200]:
        return "hook"

    if text.startswith("Permission to use") or "doesn't want to proceed" in text[:200]:
        return "denied"

    if text.startswith("<tool_use_error>"):
        return "tool"

    return "other"


def read_profile(path: Path) -> tuple[int | None, dict[str, Any]] | None:
    """The growth of the context and the profile of a transcript or of a
    stream, or None when the file is not there."""

    if not path.is_file():
        return None

    profile = ToolProfile()

    for entry in _entries(path):
        profile.add(entry)

    return profile.context_added_tokens(), profile.result()


def read_transcript(path: Path) -> Transcript | None:
    """The title, the times and the consumption of a Claude Code transcript,
    with the tokens of its subagents. None when it has no message.

    Claude Code writes one line for each block of a message, each with the
    usage of the message, so the usage counts once for each message id. The
    active time adds the gaps between the messages, except the gap before
    each prompt of a person: that gap is a wait."""

    title = None
    times: list[datetime] = []
    active = 0.0
    previous: datetime | None = None
    turns = 0
    models: set[str] = set()
    usages: dict[str, dict[str, Any]] = {}
    profile = ToolProfile()

    for entry in _entries(path):
        profile.add(entry)

        if entry.get("type") in _TITLE_TYPES:
            title = entry.get("customTitle") or entry.get("agentName") or title

            continue

        if entry.get("type") not in ("user", "assistant") or "timestamp" not in entry:
            continue

        at = datetime.fromisoformat(entry["timestamp"]).astimezone(UTC)
        prompt = _is_prompt(entry)

        if previous is not None and not prompt:
            active += max((at - previous).total_seconds(), 0)

        previous = at
        turns += prompt

        times.append(at)
        _add_usage(entry, models, usages)

    if not usages or not times:
        return None

    for subagent in sorted(path.with_suffix("").glob("subagents/*.jsonl")):
        for entry in _entries(subagent):
            profile.add(entry, context=False)
            _add_usage(entry, models, usages)

    def total(key: str) -> int:
        return sum(int(usage.get(key) or 0) for usage in usages.values())

    return Transcript(
        title,
        times[0],
        times[-1],
        int(active * 1000),
        Consumption(
            ",".join(sorted(models)) or None,
            turns,
            total("input_tokens"),
            total("output_tokens"),
            total("cache_read_input_tokens"),
            total("cache_creation_input_tokens"),
        ),
        profile.context_added_tokens(),
        profile.result(),
    )


def transcripts_dir(claude_dir: Path, worktree_path: Path) -> Path:
    """Claude Code keeps the transcripts of a directory in a directory named
    after its path, with each character that is not a letter or a digit
    changed to "-"."""

    return claude_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(worktree_path))


def agent_sha(agent_file: Path, started_at: datetime) -> str | None:
    """The sha256 of the agent file, when the file did not change after the
    start of the execution."""

    try:
        if agent_file.stat().st_mtime > started_at.timestamp():
            return None

        return hashlib.sha256(agent_file.read_bytes()).hexdigest()
    except OSError:
        return None


class ExecutionRecorder:
    """Records the executions of the agents. Each sweep reads only the files
    that changed after the previous sweep, so the first sweep after a start
    of Spyhop also records the sessions from before."""

    def __init__(
        self,
        executions: ExecutionRepository,
        runs: RunRepository,
        worktrees: WorktreeRepository,
        sessions: SessionRepository,
        checkpoint_runs: StepRunRepository,
        claude_dir: Path,
        agents_dir: Path,
    ) -> None:
        self._executions = executions
        self._runs = runs
        self._worktrees = worktrees
        self._sessions = sessions
        self._checkpoint_runs = checkpoint_runs
        self._claude_dir = claude_dir
        self._agents_dir = agents_dir
        self._read: dict[Path, float] = {}

    def sweep(self) -> int:
        """Returns the number of executions that it recorded."""

        runs = self._runs.list(include_terminal=True)
        recorded = 0

        for run in runs:
            for row in self._checkpoint_runs.list_for_run(run.id):
                if (
                    row.status != "DONE"
                    or row.finished_at is None
                    or not self._changed(row.output_file)
                ):
                    continue

                payload = read_output(row.output_file)
                result = payload.get("output") if payload else None

                if not isinstance(result, dict):
                    continue

                agent = f"{AGENT_PREFIX}{row.name}"
                worktree = self._worktrees.get(run.worktree_id) if run.worktree_id else None
                execution = result_execution(
                    result,
                    agent,
                    "pipeline",
                    row.finished_at,
                    run,
                    agent_sha(self._agents_dir / f"{agent}.md", row.started_at),
                    row.verdict,
                    # `claude -p` runs at the root of the worktree.
                    transcripts_dir(self._claude_dir, worktree.path)
                    / f"{result.get('session_id')}.jsonl"
                    if worktree
                    else None,
                )

                if execution is not None:
                    self._executions.save(execution)
                    recorded += 1

        # The headless agents of a run have no title: their turns give the
        # agent of each session.

        work = {
            str(row.session_id): row.name
            for run in runs
            for row in self._checkpoint_runs.list_work(run.id)
        }

        # A reopened ticket can get a new worktree at the same path.

        paths: dict[Path, set[str]] = {}

        for worktree in self._worktrees.list_by_status(*WorktreeStatus):
            paths.setdefault(worktree.path, set()).add(worktree.id)

        for path, worktree_ids in paths.items():
            recorded += self._sweep_directory(
                path, [run for run in runs if run.worktree_id in worktree_ids], work
            )

        return recorded

    def _sweep_directory(
        self, worktree_path: Path, runs: Sequence[AutomationRun], work: dict[str, str]
    ) -> int:
        """Records the agents of ``runs``, the runs of the worktrees at
        ``worktree_path``, newest first. ``work`` gives the agent of each
        session of a headless agent: all the turns of a session are in its
        transcript."""

        directory = transcripts_dir(self._claude_dir, worktree_path)

        if not runs or not directory.is_dir():
            return 0

        roles = {
            session.role
            for run in runs
            for session in self._sessions.list_for_run(run.id)
            if session.role
        }
        recorded = 0

        for path in sorted(directory.glob("*.jsonl")):
            if not self._changed(path):
                continue

            transcript = read_transcript(path)
            agent = work.get(path.stem) or (_agent(transcript.title, roles) if transcript else None)

            if transcript is None or agent is None:
                continue

            # The run of the session: the last run of the worktree that was
            # created before the session started.

            run = next((run for run in runs if run.created_at <= transcript.started_at), runs[-1])
            consumption = transcript.consumption

            self._executions.save(
                AgentExecution(
                    session_id=path.stem,
                    agent=agent,
                    source="pipeline",
                    run_id=run.id,
                    issue_key=run.issue_key,
                    agent_sha=None,
                    models=consumption.models,
                    started_at=transcript.started_at,
                    finished_at=transcript.finished_at,
                    wall_ms=int(
                        (transcript.finished_at - transcript.started_at).total_seconds() * 1000
                    ),
                    active_ms=transcript.active_ms,
                    turns=consumption.turns,
                    input_tokens=consumption.input_tokens,
                    output_tokens=consumption.output_tokens,
                    cache_read_tokens=consumption.cache_read_tokens,
                    cache_creation_tokens=consumption.cache_creation_tokens,
                    cost_usd=None,
                    outcome=None,
                    context_added_tokens=transcript.context_added_tokens,
                    profile=transcript.profile,
                )
            )

            recorded += 1

        return recorded

    def _changed(self, path: Path) -> bool:
        """Whether ``path`` changed after the previous sweep. A file that
        changes is read again on the next sweep."""

        try:
            modified = path.stat().st_mtime
        except OSError:
            return False

        if self._read.get(path) == modified:
            return False

        self._read[path] = modified

        return True


@dataclass(frozen=True)
class PeriodStats:
    period: str
    count: int
    passed: int | None  # None when no execution of the period has a verdict
    median_active_ms: float
    p90_active_ms: float
    median_input_tokens: float  # with the cache
    median_output_tokens: float
    total_tokens: int
    new_versions: list[str]


def total_tokens(execution: AgentExecution) -> int:
    return (
        execution.input_tokens
        + execution.output_tokens
        + execution.cache_read_tokens
        + execution.cache_creation_tokens
    )


def input_tokens(execution: AgentExecution) -> int:
    return execution.input_tokens + execution.cache_read_tokens + execution.cache_creation_tokens


def period_of(at: datetime, by: Literal["day", "week", "month"]) -> str:
    if by == "day":
        return at.date().isoformat()

    if by == "month":
        return at.strftime("%Y-%m")

    year, week, _ = at.isocalendar()

    return f"{year}-W{week:02d}"


def history(
    executions: Sequence[AgentExecution], by: Literal["day", "week", "month"]
) -> list[PeriodStats]:
    """The executions of one agent, period by period, oldest first. A
    version is new in the first period where it occurs."""

    seen: set[str] = set()
    rows = []

    for period, group in _periods(executions, by).items():
        verdicts = [execution.outcome for execution in group if execution.outcome]
        versions = list(
            dict.fromkeys(execution.agent_sha for execution in group if execution.agent_sha)
        )
        active = [execution.active_ms for execution in group]

        rows.append(
            PeriodStats(
                period=period,
                count=len(group),
                passed=sum(verdict == "PASS" for verdict in verdicts) if verdicts else None,
                median_active_ms=statistics.median(active),
                p90_active_ms=_p90(active),
                median_input_tokens=statistics.median(input_tokens(e) for e in group),
                median_output_tokens=statistics.median(e.output_tokens for e in group),
                total_tokens=sum(total_tokens(execution) for execution in group),
                new_versions=[sha[:8] for sha in versions if sha not in seen],
            )
        )
        seen.update(versions)

    return rows


@dataclass(frozen=True)
class PeriodProfile:
    """The medians of the executions of a period that have a profile, and
    the sums of their calls and errors."""

    period: str
    count: int
    median_calls: float
    median_context_added_tokens: float
    median_result_bytes: float
    max_result_bytes: int
    median_repeated_bash: float
    median_errors: float
    calls: dict[str, int]
    errors: dict[str, int]


def profile_history(
    executions: Sequence[AgentExecution], by: Literal["day", "week", "month"]
) -> list[PeriodProfile]:
    """Like ``history``, for the profiles. A period without profiles has no
    row."""

    rows = []

    for period, group in _periods(executions, by).items():
        profiles = [(e.context_added_tokens, e.profile) for e in group if e.profile is not None]

        if not profiles:
            continue

        calls: Counter[str] = Counter()
        errors: Counter[str] = Counter()

        for _, profile in profiles:
            calls.update(profile.get("calls") or {})
            errors.update(profile.get("errors") or {})

        rows.append(
            PeriodProfile(
                period=period,
                count=len(profiles),
                median_calls=statistics.median(
                    sum((profile.get("calls") or {}).values()) for _, profile in profiles
                ),
                median_context_added_tokens=statistics.median(
                    context or 0 for context, _ in profiles
                ),
                median_result_bytes=statistics.median(
                    sum((profile.get("result_bytes") or {}).values()) for _, profile in profiles
                ),
                max_result_bytes=max(
                    max((profile.get("max_result_bytes") or {}).values(), default=0)
                    for _, profile in profiles
                ),
                median_repeated_bash=statistics.median(
                    int(profile.get("repeated_bash") or 0) for _, profile in profiles
                ),
                median_errors=statistics.median(
                    sum((profile.get("errors") or {}).values()) for _, profile in profiles
                ),
                calls=dict(calls.most_common()),
                errors=dict(errors.most_common()),
            )
        )

    return rows


def _periods(
    executions: Sequence[AgentExecution], by: Literal["day", "week", "month"]
) -> dict[str, list[AgentExecution]]:
    periods: dict[str, list[AgentExecution]] = {}

    for execution in sorted(executions, key=lambda execution: execution.started_at):
        periods.setdefault(period_of(execution.started_at, by), []).append(execution)

    return periods


def _p90(values: Sequence[int]) -> float:
    if len(values) < 2:
        return float(values[0])

    return statistics.quantiles(values, n=10, method="inclusive")[-1]


def _agent(title: str | None, roles: set[str]) -> str | None:
    """The agent of a session title: "spyhop" is the supervisor session, and
    "<role> <model>" is the agent of a role of the run."""

    if not title:
        return None

    if title == SUPERVISOR_TITLE:
        return SUPERVISOR

    role = title.split()[0]

    return role if role in roles else None


def _is_prompt(entry: dict[str, Any]) -> bool:
    """A message of a person or of Spyhop, and not the result of a tool."""

    if entry.get("type") != "user" or entry.get("isMeta"):
        return False

    content = (entry.get("message") or {}).get("content")

    return isinstance(content, str) or (
        isinstance(content, list)
        and not any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        )
    )


def _add_usage(entry: dict[str, Any], models: set[str], usages: dict[str, dict[str, Any]]) -> None:
    message = entry.get("message")

    if entry.get("type") != "assistant" or not isinstance(message, dict):
        return

    usage = message.get("usage")

    if not isinstance(usage, dict) or not message.get("id"):
        return

    usages[str(message["id"])] = usage

    if message.get("model") and not str(message["model"]).startswith("<"):
        models.add(str(message["model"]))


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        return "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )

    return ""


def _entries(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open() as lines:
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue

                if isinstance(entry, dict):
                    yield entry
    except OSError as error:
        _log.debug("cannot read %s: %s", path, error)
