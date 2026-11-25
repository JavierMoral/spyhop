"""Symbolic links from the Claude Code directory to the subagents, skills,
and scripts of Spyhop."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Link:
    target: Path
    source: Path
    # False: a file or directory that is not a link is at the target.
    linked: bool


def claude_files(bundled: Path, claude: Path) -> list[tuple[Path, Path]]:
    """The source and the target of each agents/*.md file, each
    skills/<name> directory, and each scripts/<name> directory of
    ``bundled``: the same place in ``claude``."""

    sources = (
        sorted((bundled / "agents").glob("*.md"))
        + sorted((bundled / "skills").glob("*/"))
        + sorted((bundled / "scripts").glob("*/"))
    )

    return [(source, claude / source.parent.name / source.name) for source in sources]


def is_linked(source: Path, target: Path) -> bool:
    return target.is_symlink() and target.resolve() == source.resolve()


def link_claude_files(bundled: Path, claude: Path) -> list[Link]:
    """Links each file of ``claude_files``. A link at the target is replaced;
    a file or directory that is not a link is kept."""

    links = []

    for source, target in claude_files(bundled, claude):
        if target.exists() and not target.is_symlink():
            links.append(Link(target, source, False))

            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        target.symlink_to(source)

        links.append(Link(target, source, True))

    return links
