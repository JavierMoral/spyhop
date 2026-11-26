import threading
from pathlib import Path

import pytest

from spyhop.application import checkpoint_inputs
from spyhop.application.checkpoint_inputs import input_values
from tests.fakes import git


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


async def test_code_changes_only_with_a_file_that_is_not_a_test(tmp_path: Path) -> None:
    git(tmp_path, "init", "--quiet", "--initial-branch", "master")
    (tmp_path / "App.java").write_text("class App {}\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "--quiet", "-m", "Start")
    git(tmp_path, "checkout", "--quiet", "-b", "LPD-1")

    async def code() -> str:
        values = await input_values({"code"}, tmp_path, "master", None, None)

        return values["code"]

    start = await code()

    for test_file in (
        "src/test/AppTest.java",
        "src/testIntegration/AppTest.java",
        "tests/test_app.py",
        "app-test/src/AppTest.java",
    ):
        (tmp_path / test_file).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / test_file).write_text("class AppTest {}\n")
        git(tmp_path, "add", ".")
        git(tmp_path, "commit", "--quiet", "-m", f"Add {test_file}")

    assert await code() == start

    (tmp_path / "App.java").write_text("class App { int fixed; }\n")
    git(tmp_path, "commit", "--quiet", "-am", "Fix")

    assert await code() != start
