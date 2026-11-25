"""Runs Claude Code in print mode (`claude -p`) and reads its stream."""

import json
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any


def run_agent(
    agent: str,
    prompt: str,
    schema: dict[str, Any],
    root: Path,
    transcript: Path | None = None,
) -> tuple[int, dict[str, object] | None]:
    """Runs ``agent`` with ``prompt`` in ``root`` and shows its steps. Returns
    the exit code of claude and its result event, which holds the JSON object
    of ``schema``. With ``transcript``, writes each event of the stream to
    that file."""

    command = [
        "claude",
        "-p",
        prompt,
        "--agent",
        agent,
        "--verbose",
        "--output-format",
        "stream-json",
        "--json-schema",
        json.dumps(schema),
        "--dangerously-skip-permissions",
    ]
    result: dict[str, object] | None = None

    try:
        process = subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, text=True)
    except OSError as error:
        print(f"Cannot run claude: {error}", file=sys.stderr, flush=True)

        return 127, None

    assert process.stdout is not None

    with transcript.open("w") if transcript else nullcontext() as log:
        for line in process.stdout:
            if log is not None:
                log.write(line)

            try:
                event = json.loads(line)
            except ValueError:
                continue

            if not isinstance(event, dict):
                continue

            if event.get("type") == "result":
                result = event
            elif event.get("type") == "assistant":
                for content in (event.get("message") or {}).get("content") or []:
                    if not isinstance(content, dict):
                        continue

                    if (
                        content.get("type") == "tool_use"
                        and content.get("name") != "StructuredOutput"
                    ):
                        detail = json.dumps(content.get("input"))[:200]

                        print(f"> {content.get('name')}: {detail}", flush=True)
                    elif content.get("type") == "text":
                        print(content.get("text", ""), flush=True)

    return process.wait(), result
