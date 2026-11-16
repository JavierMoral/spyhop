from pathlib import Path

import pytest

from spyhop.application.checkpoint_planner import CheckpointPlanner, Stage
from spyhop.domain.exceptions import ConfigurationError


def checkpoint(
    agents_dir: Path, name: str, mode: str = "exclusive", after: list[str] | None = None
) -> None:
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"checkpoint-{name}.md").write_text(
        f"---\nname: checkpoint-{name}\nmodel: sonnet\nmode: {mode}\nafter: {after or []}\n"
        f"---\nCheck {name}.\n"
    )


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "agents"

    checkpoint(directory, "regressions")
    checkpoint(directory, "mutation", after=["regressions"])
    checkpoint(directory, "security", mode="read")
    checkpoint(directory, "naming", mode="read")
    checkpoint(directory, "report", mode="read", after=["mutation"])

    return directory


def test_read_checkpoints_share_a_stage_and_exclusive_ones_run_alone(agents_dir: Path) -> None:
    stages = CheckpointPlanner(agents_dir).plan(
        ["report", "mutation", "regressions", "security", "naming"]
    )

    assert stages == [
        Stage("read", ("naming", "security")),
        Stage("exclusive", ("regressions",)),
        Stage("exclusive", ("mutation",)),
        Stage("read", ("report",)),
    ]


def test_the_order_goes_through_checkpoints_the_workflow_does_not_have(agents_dir: Path) -> None:
    stages = CheckpointPlanner(agents_dir).plan(["report", "regressions"])

    assert stages == [Stage("exclusive", ("regressions",)), Stage("read", ("report",))]


def test_names_select_only_those_checkpoints(agents_dir: Path) -> None:
    stages = CheckpointPlanner(agents_dir).plan(
        ["regressions", "mutation", "security"], names=["mutation", "security"]
    )

    assert stages == [Stage("read", ("security",)), Stage("exclusive", ("mutation",))]


def test_start_selects_the_checkpoint_and_the_ones_after_it(agents_dir: Path) -> None:
    stages = CheckpointPlanner(agents_dir).plan(
        ["regressions", "mutation", "report", "security"], start="regressions"
    )

    assert stages == [
        Stage("exclusive", ("regressions",)),
        Stage("exclusive", ("mutation",)),
        Stage("read", ("report",)),
    ]


def test_a_workflow_without_checkpoints_has_no_stages(agents_dir: Path) -> None:
    assert CheckpointPlanner(agents_dir).plan([]) == []


def test_names_and_start_together_are_rejected(agents_dir: Path) -> None:
    with pytest.raises(ConfigurationError, match="not both"):
        CheckpointPlanner(agents_dir).plan(["mutation"], names=["mutation"], start="mutation")


def test_a_name_outside_the_workflow_is_rejected(agents_dir: Path) -> None:
    with pytest.raises(ConfigurationError, match="'security' is not in this workflow"):
        CheckpointPlanner(agents_dir).plan(["mutation"], start="security")


def test_a_checkpoint_without_file_is_rejected(agents_dir: Path) -> None:
    with pytest.raises(ConfigurationError, match="'missing' has no file"):
        CheckpointPlanner(agents_dir).plan(["missing"])


def test_an_unknown_checkpoint_in_after_is_rejected(agents_dir: Path) -> None:
    checkpoint(agents_dir, "typo", after=["regresions"])

    with pytest.raises(ConfigurationError, match="'regresions' has no file"):
        CheckpointPlanner(agents_dir).plan(["typo"])


def test_a_cycle_is_rejected(agents_dir: Path) -> None:
    checkpoint(agents_dir, "first", after=["second"])
    checkpoint(agents_dir, "second", after=["first"])

    with pytest.raises(ConfigurationError, match="first -> second -> first"):
        CheckpointPlanner(agents_dir).plan(["first"])


def test_a_name_with_a_path_is_rejected(agents_dir: Path) -> None:
    with pytest.raises(ConfigurationError, match="Invalid checkpoint name"):
        CheckpointPlanner(agents_dir).plan(["../secret"])


def test_an_unknown_mode_is_rejected(agents_dir: Path) -> None:
    checkpoint(agents_dir, "odd", mode="parallel")

    with pytest.raises(ConfigurationError, match="mode must be one of read, exclusive"):
        CheckpointPlanner(agents_dir).plan(["odd"])


def test_the_agent_name_must_match_the_file(agents_dir: Path) -> None:
    (agents_dir / "checkpoint-renamed.md").write_text("---\nname: other\n---\n")

    with pytest.raises(ConfigurationError, match="must have name: checkpoint-renamed"):
        CheckpointPlanner(agents_dir).plan(["renamed"])


def test_a_file_without_frontmatter_is_rejected(agents_dir: Path) -> None:
    (agents_dir / "checkpoint-plain.md").write_text("Check something.\n")

    with pytest.raises(ConfigurationError, match="no frontmatter"):
        CheckpointPlanner(agents_dir).plan(["plain"])


def test_without_mode_a_checkpoint_is_exclusive(agents_dir: Path) -> None:
    (agents_dir / "checkpoint-bare.md").write_text("---\nname: checkpoint-bare\n---\n")

    assert CheckpointPlanner(agents_dir).plan(["bare"]) == [Stage("exclusive", ("bare",))]
