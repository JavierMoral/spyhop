from importlib.resources import files
from pathlib import Path

from spyhop.config.paths import bundled_claude_dir
from spyhop.config.settings import load_settings
from spyhop.doctor import Level, _check_checkpoints
from spyhop.infrastructure.claude_links import Link, link_claude_files


def bundled(tmp_path: Path) -> Path:
    root = tmp_path / "bundled"

    (root / "agents").mkdir(parents=True)
    (root / "agents" / "checkpoint-security.md").write_text("agent")
    (root / "skills" / "recall").mkdir(parents=True)
    (root / "scripts" / "reviewer").mkdir(parents=True)

    return root


def test_agents_skills_and_scripts_are_linked(tmp_path: Path) -> None:
    root = bundled(tmp_path)
    claude = tmp_path / "claude"

    links = link_claude_files(root, claude)

    assert links == [
        Link(
            claude / "agents" / "checkpoint-security.md",
            root / "agents" / "checkpoint-security.md",
            True,
        ),
        Link(claude / "skills" / "recall", root / "skills" / "recall", True),
        Link(claude / "scripts" / "reviewer", root / "scripts" / "reviewer", True),
    ]
    assert (claude / "agents" / "checkpoint-security.md").read_text() == "agent"
    assert (claude / "skills" / "recall").readlink() == root / "skills" / "recall"


def test_an_old_link_is_replaced(tmp_path: Path) -> None:
    root = bundled(tmp_path)
    target = tmp_path / "claude" / "agents" / "checkpoint-security.md"

    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path / "removed.md")

    link_claude_files(root, tmp_path / "claude")

    assert target.readlink() == root / "agents" / "checkpoint-security.md"


def test_a_file_that_is_not_a_link_is_kept(tmp_path: Path) -> None:
    root = bundled(tmp_path)
    target = tmp_path / "claude" / "agents" / "checkpoint-security.md"

    target.parent.mkdir(parents=True)
    target.write_text("mine")

    [link, *_] = link_claude_files(root, tmp_path / "claude")

    assert link.linked is False
    assert target.read_text() == "mine"


def test_the_bundled_checkpoints_give_the_example_pipelines(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"

    config_file.write_text((files("spyhop") / "templates" / "config.example.yaml").read_text())

    checks = _check_checkpoints(load_settings(config_file), bundled_claude_dir() / "agents")

    assert [check.level for check in checks] == [Level.OK, Level.OK]
