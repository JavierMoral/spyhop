import threading
from pathlib import Path

import pytest

from spyhop.application import checkpoint_inputs
from spyhop.application.checkpoint_inputs import input_values


async def test_git_runs_out_of_the_event_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: list[threading.Thread] = []

    def answer(*_: object) -> str:
        threads.append(threading.current_thread())

        return "abc"

    monkeypatch.setattr(checkpoint_inputs, "head", answer)
    monkeypatch.setattr(checkpoint_inputs, "merge_base", answer)

    values = await input_values({"head", "base"}, tmp_path, "master", None, None)

    assert values == {"head": "abc", "base": "abc"}
    assert len(threads) == 2
    assert threading.main_thread() not in threads
