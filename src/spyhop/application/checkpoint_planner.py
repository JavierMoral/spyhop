"""Puts the checkpoints of a workflow in the order that /checkpoints runs them.

A checkpoint is a Claude Code subagent in ``checkpoint-<name>.md``. Its
frontmatter gives ``mode`` (``read`` or ``exclusive``) and ``after``, the
checkpoints that run before it. ``after`` only sets the order: a checkpoint
that is not selected does not run, but the order through it stays.

A stage holds the ``read`` checkpoints that can run in parallel, or one
``exclusive`` checkpoint, which runs alone in the worktree.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml

from spyhop.domain.exceptions import ConfigurationError

AGENT_PREFIX = "checkpoint-"

_MODES = ("read", "exclusive")
_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")

Mode = Literal["read", "exclusive"]


@dataclass(frozen=True)
class Checkpoint:
    name: str
    mode: Mode
    after: tuple[str, ...]


@dataclass(frozen=True)
class Stage:
    mode: Mode
    checkpoints: tuple[str, ...]


class CheckpointPlanner:
    def __init__(self, agents_dir: Path) -> None:
        self._agents_dir = agents_dir

    def plan(
        self,
        workflow_checkpoints: Sequence[str],
        names: Sequence[str] = (),
        start: str | None = None,
    ) -> list[Stage]:
        """The stages of the selected checkpoints: ``names`` only, ``start``
        and the checkpoints after it, or all the workflow's checkpoints."""

        if names and start:
            raise ConfigurationError("Select checkpoints by name or with a start, not both")

        members = list(dict.fromkeys(workflow_checkpoints))
        catalog: dict[str, Checkpoint] = {}

        for name in members:
            self._load(name, catalog, ())

        befores = {name: self._befores(name, catalog) for name in members}
        selected = self._select(members, befores, list(dict.fromkeys(names)), start)

        return self._stages(selected, catalog, befores)

    def _befores(self, name: str, catalog: dict[str, Checkpoint]) -> set[str]:
        befores: set[str] = set()
        pending = list(catalog[name].after)

        while pending:
            before = pending.pop()

            if before not in befores:
                befores.add(before)
                pending.extend(catalog[before].after)

        return befores

    def _load(self, name: str, catalog: dict[str, Checkpoint], path: tuple[str, ...]) -> None:
        if name in path:
            raise ConfigurationError(
                f"Checkpoints run after each other in a cycle: {' -> '.join((*path, name))}"
            )

        if name in catalog:
            return

        checkpoint = self._read(name)

        for before in checkpoint.after:
            self._load(before, catalog, (*path, name))

        catalog[name] = checkpoint

    def _read(self, name: str) -> Checkpoint:
        if not _NAME.fullmatch(name):
            raise ConfigurationError(
                f"Invalid checkpoint name {name!r}: use lowercase letters, digits and hyphens"
            )

        agent_file = self._agents_dir / f"{AGENT_PREFIX}{name}.md"

        if not agent_file.is_file():
            raise ConfigurationError(f"Checkpoint {name!r} has no file {agent_file}")

        frontmatter = _frontmatter(agent_file)

        if frontmatter.get("name") != f"{AGENT_PREFIX}{name}":
            raise ConfigurationError(f"{agent_file} must have name: {AGENT_PREFIX}{name}")

        mode = frontmatter.get("mode", "exclusive")

        if mode not in _MODES:
            raise ConfigurationError(f"{agent_file}: mode must be one of {', '.join(_MODES)}")

        after = frontmatter.get("after", [])

        if not isinstance(after, list) or not all(isinstance(before, str) for before in after):
            raise ConfigurationError(f"{agent_file}: after must be a list of checkpoint names")

        return Checkpoint(name, cast(Mode, mode), tuple(after))

    def _select(
        self,
        members: list[str],
        befores: dict[str, set[str]],
        names: list[str],
        start: str | None,
    ) -> list[str]:
        for name in [*names, *([start] if start else [])]:
            if name not in members:
                raise ConfigurationError(f"Checkpoint {name!r} is not in this workflow")

        if names:
            return names

        if start:
            return [name for name in members if name == start or start in befores[name]]

        return members

    def _stages(
        self, selected: list[str], catalog: dict[str, Checkpoint], befores: dict[str, set[str]]
    ) -> list[Stage]:
        stages: list[Stage] = []
        remaining = set(selected)

        while remaining:
            ready = sorted(name for name in remaining if not befores[name] & remaining)
            reads = tuple(name for name in ready if catalog[name].mode == "read")

            if reads:
                stages.append(Stage("read", reads))

            stages.extend(
                Stage("exclusive", (name,)) for name in ready if catalog[name].mode == "exclusive"
            )

            remaining.difference_update(ready)

        return stages


def _frontmatter(agent_file: Path) -> dict[str, object]:
    lines = agent_file.read_text().splitlines()

    if not lines or lines[0].strip() != "---" or "---" not in (line.strip() for line in lines[1:]):
        raise ConfigurationError(f"{agent_file} has no frontmatter between --- lines")

    end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")

    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as error:
        raise ConfigurationError(f"Invalid frontmatter in {agent_file}: {error}") from error

    if not isinstance(frontmatter, dict):
        raise ConfigurationError(f"{agent_file}: the frontmatter must be a mapping")

    return frontmatter
