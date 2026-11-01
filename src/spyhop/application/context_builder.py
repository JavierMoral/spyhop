"""Prepares a worktree for its agent: TASK.md, Git exclusions, permission
profile, and the /goal prompt that starts the work.

Templates come from ~/.config/spyhop/templates/ when present there, otherwise
from the package.
"""

import json
import re
from pathlib import Path
from typing import Literal

from jinja2 import (
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    PackageLoader,
    StrictUndefined,
    TemplateError,
)

from spyhop.config.settings import REVIEW_STATUS
from spyhop.domain.exceptions import SpyhopError
from spyhop.domain.models import ResolvedWorkflow, TicketDetails

TASK_FILE = "TASK.md"
PLAN_FILE = "PLAN.md"
RESULT_FILE = "RESULT.md"
SETTINGS_FILE = Path(".claude") / "settings.local.json"

# Anchored to the worktree root. info/exclude is shared by every worktree of a
# repository, which is fine: the patterns only name Spyhop's own files.

EXCLUDED_PATHS = (f"/{TASK_FILE}", f"/{PLAN_FILE}", f"/{RESULT_FILE}", f"/{SETTINGS_FILE}")

Phase = Literal["plan", "implement", "full"]


class ContextError(SpyhopError):
    pass


def template_environment(user_templates: Path) -> Environment:
    environment = Environment(
        loader=ChoiceLoader(
            [FileSystemLoader(user_templates), PackageLoader("spyhop", "templates")]
        ),
        autoescape=False,
        extensions=["jinja2.ext.do"],
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )

    environment.filters["fence"] = fence
    environment.filters["oneline"] = oneline

    return environment


class ContextBuilder:
    def __init__(
        self,
        templates: Environment,
        orca_executable: Path,
        plan_review_status: str,
        plan_approved_status: str = "in-progress",
    ) -> None:
        self._templates = templates
        self._orca_executable = orca_executable
        self._plan_review_status = plan_review_status
        self._plan_approved_status = plan_approved_status

    def prepare(
        self, worktree_path: Path, ticket: TicketDetails, workflow: ResolvedWorkflow
    ) -> None:
        if not worktree_path.is_dir():
            raise ContextError(f"Worktree directory not found: {worktree_path}")

        task = self.render_task(ticket, workflow)
        settings = self.render_settings(workflow)

        (worktree_path / TASK_FILE).write_text(task)

        settings_file = worktree_path / SETTINGS_FILE

        settings_file.parent.mkdir(exist_ok=True)
        settings_file.write_text(settings)

        add_exclusions(git_exclude_file(worktree_path), EXCLUDED_PATHS)

    def render_task(self, ticket: TicketDetails, workflow: ResolvedWorkflow) -> str:
        guidance = self._render(workflow.prompt).strip()

        return self._render(
            "task.md",
            ticket=ticket,
            workflow=workflow,
            guidance=guidance,
            implement_goal=self.goal(ticket.key, "implement"),
            plan_review_status=self._plan_review_status,
            plan_approved_status=self._plan_approved_status,
        )

    def render_settings(self, workflow: ResolvedWorkflow) -> str:
        text = self._render(
            "settings.local.json.j2",
            build_commands=list(workflow.build_commands),
            orca_commands=self._orca_commands(),
        )

        try:
            json.loads(text)
        except ValueError as error:
            raise ContextError(
                f"settings.local.json.j2 does not render valid JSON: {error}"
            ) from error

        return text if text.endswith("\n") else text + "\n"

    def launch_goal(self, issue_key: str, workflow: ResolvedWorkflow) -> str:
        return self.goal(issue_key, "plan" if workflow.plan_review else "full")

    def goal(self, issue_key: str, phase: Phase) -> str:
        text = self._render(
            "goal.md",
            phase=phase,
            issue_key=issue_key,
            orca=str(self._orca_executable),
            plan_review_status=self._plan_review_status,
            review_status=REVIEW_STATUS,
        )

        return oneline(text)

    def _orca_commands(self) -> list[str]:
        # The agent may call Orca by name or by the absolute path in its goal;
        # permission rules match the literal command, so both are listed.

        return list(dict.fromkeys([self._orca_executable.name, str(self._orca_executable)]))

    def _render(self, name: str, **variables: object) -> str:
        try:
            return self._templates.get_template(name).render(**variables)
        except TemplateError as error:
            raise ContextError(f"Template {name}: {error}") from error


def fence(text: str) -> str:
    """Wraps untrusted text in a code fence it cannot close."""

    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    marker = "`" * max(3, longest + 1)

    return f"{marker}text\n{text.strip()}\n{marker}"


def oneline(text: str) -> str:
    return " ".join(text.split())


def git_exclude_file(worktree_path: Path) -> Path:
    """info/exclude of the repository, read from the common Git directory as
    Git does, so it also works for linked worktrees."""

    dot_git = worktree_path / ".git"

    if dot_git.is_dir():
        return dot_git / "info" / "exclude"

    try:
        content = dot_git.read_text().strip()
    except OSError as error:
        raise ContextError(f"{worktree_path} is not a Git worktree: {error}") from None

    if not content.startswith("gitdir:"):
        raise ContextError(f"Unexpected {dot_git}: {content[:100]!r}")

    git_dir = Path(content.removeprefix("gitdir:").strip())

    if not git_dir.is_absolute():
        git_dir = (worktree_path / git_dir).resolve()

    common_dir_file = git_dir / "commondir"

    if common_dir_file.is_file():
        common_dir = Path(common_dir_file.read_text().strip())

        if not common_dir.is_absolute():
            common_dir = (git_dir / common_dir).resolve()
    else:
        common_dir = git_dir

    return common_dir / "info" / "exclude"


def add_exclusions(exclude_file: Path, patterns: tuple[str, ...]) -> None:
    existing = exclude_file.read_text().splitlines() if exclude_file.exists() else []
    missing = [pattern for pattern in patterns if pattern not in existing]

    if not missing:
        return

    exclude_file.parent.mkdir(parents=True, exist_ok=True)

    lines = list(existing)

    if lines and lines[-1].strip():
        lines.append("")

    lines.append("# Added by Spyhop: files written for its agents")
    lines.extend(missing)

    exclude_file.write_text("\n".join(lines) + "\n")
