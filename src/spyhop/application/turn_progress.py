"""The progress of a headless turn that runs, from its log.

`spyhop steps exec` writes one line `> <Tool>: <input>` for each tool call of
the agent, and the text of the agent as it is. A summary reads only the part
of the log after ``offset``, so each summary tells what is new since the one
before it.
"""

from dataclasses import dataclass
from pathlib import Path

TOOL_PREFIX = "> "

LINE_LIMIT = 160


@dataclass(frozen=True)
class Progress:
    calls: int  # the tool calls since the offset
    last_call: str | None  # the last tool call, shortened
    last_text: str | None  # the last line of text of the agent, shortened
    offset: int  # where the next summary starts


def read_progress(log: Path, offset: int) -> Progress:
    try:
        with log.open("rb") as file:
            file.seek(offset)
            data = file.read()
    except OSError:
        return Progress(0, None, None, offset)

    # A line that the unit still writes is read by the next summary.

    end = data.rfind(b"\n") + 1
    lines = data[:end].decode(errors="replace").splitlines()
    calls = [line for line in lines if line.startswith(TOOL_PREFIX)]
    texts = [line for line in lines if line.strip() and not line.startswith(TOOL_PREFIX)]

    return Progress(
        len(calls),
        _short(calls[-1].removeprefix(TOOL_PREFIX)) if calls else None,
        _short(texts[-1]) if texts else None,
        offset + end,
    )


def _short(line: str) -> str:
    line = line.strip()

    return line if len(line) <= LINE_LIMIT else line[: LINE_LIMIT - 1] + "…"
