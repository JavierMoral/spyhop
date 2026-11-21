"""Spyhop configuration.

Precedence, lowest first: model defaults < config.yaml < SPYHOP_* environment
variables (nested with "__", for example SPYHOP_APPLICATION__DRY_RUN=true) <
command line options, which callers apply with ``model_copy``.
"""

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from spyhop.config.paths import default_env_file, expand_path
from spyhop.domain.exceptions import ConfigurationError
from spyhop.domain.models import ChecksPhase, Phase, validate_pipeline


def _expand(value: Any) -> Any:
    if isinstance(value, str | Path):
        return expand_path(value)

    return value


ExpandedPath = Annotated[Path, BeforeValidator(_expand)]


class ApplicationSettings(BaseModel):
    polling_interval: int = Field(default=45, ge=10)
    max_concurrent_agents: int = Field(default=1, ge=1)
    # No new worktree is created while the disk that would hold it has less
    # free space than this. 0 disables the check.
    min_free_disk_gb: float = Field(default=20, ge=0)
    dry_run: bool = True


class JiraSettings(BaseModel):
    base_url: str
    user_env: str = "JIRA_API_USER"
    token_env: str = "JIRA_API_TOKEN"
    request_timeout: float = Field(default=20, gt=0)
    transition_margin_minutes: int = Field(default=5, ge=1)


class RepositorySettings(BaseModel):
    path: ExpandedPath
    default_base_branch: str = "master"
    setup: Literal["inherit", "run", "skip"] = "inherit"
    build_commands: list[str] = Field(default_factory=list)
    # Executable that creates new worktrees instead of Orca; see the README for
    # its contract. Orca's setup hooks do not run for the worktrees it creates.
    worktree_script: ExpandedPath | None = None
    worktree_script_timeout: float = Field(default=600, gt=0)
    # Executable run once Orca has removed a worktree, to remove what lives
    # next to it, such as a bundle of its own.
    worktree_cleanup_script: ExpandedPath | None = None
    worktree_cleanup_script_timeout: float = Field(default=600, gt=0)


class ChecksPhaseSettings(BaseModel):
    """A pipeline phase that runs checkpoints. Each checkpoint is a Claude
    Code subagent in checkpoint-<name>.md."""

    model_config = ConfigDict(extra="forbid")

    checks: list[str] = Field(min_length=1)
    on_fail: Literal["human", "fix"] = "human"
    rounds: int = Field(default=3, ge=1)


PipelineItem = Literal["plan", "plan_review", "implement"] | ChecksPhaseSettings


class WorkflowSettings(BaseModel):
    name: str
    trigger_statuses: list[str] = Field(min_length=1)
    repository: str
    base_branch: str | None = None
    prompt: str
    # Without a pipeline: plan, plan_review and implement, or only
    # implement. With a pipeline, the pipeline decides.
    plan_review: bool = True
    # The name of a pipeline in the top-level pipelines.
    pipeline: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_checkpoints_key(cls, data: Any) -> Any:
        if isinstance(data, dict) and "checkpoints" in data:
            raise ValueError("checkpoints is replaced by pipeline; see pipelines in the README")

        return data

    @model_validator(mode="after")
    def _plan_review_or_pipeline(self) -> "WorkflowSettings":
        if self.pipeline is not None and "plan_review" in self.model_fields_set:
            raise ValueError("plan_review comes from the pipeline; remove it")

        return self


class ProjectSettings(BaseModel):
    enabled: bool = True
    workflows: dict[str, WorkflowSettings] = Field(default_factory=dict)


class OrcaSettings(BaseModel):
    executable: ExpandedPath | None = None
    startup_timeout: float = Field(default=30, gt=0)
    operation_timeout: float = Field(default=120, gt=0)
    # Board column id for cards whose plan awaits review. Orca assigns ids to
    # custom columns itself (status-N), so the default is a built-in column.

    plan_review_status: str = "in-review"

    # Board column that approves a plan: moving the card there makes Spyhop
    # send the implementation /goal to the agent. The card stays there when the
    # work is finished: with CLAUDE-RESULT.md written, it is the completion signal.

    plan_approved_status: str = "in-progress"

    @model_validator(mode="after")
    def _distinct_statuses(self) -> "OrcaSettings":
        if self.plan_approved_status == self.plan_review_status:
            raise ValueError("orca.plan_approved_status must differ from orca.plan_review_status")

        return self


class AgentSettings(BaseModel):
    provider: str = "claude"
    # The plan of a workflow with plan_review comes from an agent on
    # plan_model. The implementation always comes from an agent on model.
    plan_model: str = "opus"
    model: str = "sonnet"
    effort: str = "high"
    # How many times a run relaunches an agent that Orca no longer lists, or
    # tells an agent that stopped without declaring the plan or the end to
    # continue, before the run waits for a person.
    max_restarts: int = Field(default=1, ge=0)
    # A command, usually a slash command, the agent runs over its commits once
    # the implementation is done, before writing CLAUDE-RESULT.md.
    review_command: str | None = None
    # A checkpoint that runs longer than this counts as a FAIL.
    checkpoint_timeout_minutes: float = Field(default=180, gt=0)

    @field_validator("review_command")
    @classmethod
    def _blank_review_command_is_none(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class CleanupSettings(BaseModel):
    enabled: bool = True
    close_status_categories: list[str] = Field(default_factory=lambda: ["done"])
    close_statuses: list[str] = Field(default_factory=list)


class ServiceSettings(BaseModel):
    env_source: Literal["env_file", "login_shell"] = "env_file"
    env_file: ExpandedPath = Field(default_factory=default_env_file)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_nested_delimiter="__", env_prefix="SPYHOP_", extra="forbid"
    )

    application: ApplicationSettings = Field(default_factory=ApplicationSettings)
    jira: JiraSettings
    repositories: dict[str, RepositorySettings] = Field(default_factory=dict)
    projects: dict[str, ProjectSettings] = Field(default_factory=dict)
    pipelines: dict[str, list[PipelineItem]] = Field(default_factory=dict)
    orca: OrcaSettings = Field(default_factory=OrcaSettings)
    agents: AgentSettings = Field(default_factory=AgentSettings)
    cleanup: CleanupSettings = Field(default_factory=CleanupSettings)
    service: ServiceSettings = Field(default_factory=ServiceSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # The YAML file arrives as init kwargs; environment variables must win.

        return env_settings, init_settings

    @model_validator(mode="after")
    def _check_repository_aliases(self) -> "Settings":
        for project_key, project in self.projects.items():
            for issue_type, workflow in project.workflows.items():
                if workflow.repository not in self.repositories:
                    raise ValueError(
                        f"projects.{project_key}.workflows.{issue_type}.repository "
                        f"refers to unknown repository alias {workflow.repository!r}"
                    )

                if workflow.pipeline is not None and workflow.pipeline not in self.pipelines:
                    raise ValueError(
                        f"projects.{project_key}.workflows.{issue_type}.pipeline "
                        f"refers to unknown pipeline {workflow.pipeline!r}"
                    )

        return self

    @model_validator(mode="after")
    def _check_pipelines(self) -> "Settings":
        for name in self.pipelines:
            try:
                validate_pipeline(self.pipeline(name))
            except ValueError as error:
                raise ValueError(f"pipelines.{name}: {error}") from None

        return self

    def pipeline(self, name: str) -> tuple[Phase, ...]:
        return tuple(
            item
            if isinstance(item, str)
            else ChecksPhase(tuple(item.checks), item.on_fail, item.rounds)
            for item in self.pipelines[name]
        )

    def enabled_projects(self) -> dict[str, ProjectSettings]:
        return {key: project for key, project in self.projects.items() if project.enabled}


def load_settings(config_file: Path) -> Settings:
    if not config_file.is_file():
        raise ConfigurationError(
            f"Configuration file not found: {config_file}. Run `spyhop config init`."
        )

    try:
        data = yaml.safe_load(config_file.read_text()) or {}
    except yaml.YAMLError as error:
        raise ConfigurationError(f"Invalid YAML in {config_file}: {error}") from error

    if not isinstance(data, dict):
        raise ConfigurationError(f"{config_file} must contain a mapping at the top level")

    try:
        return Settings(**data)
    except ValueError as error:
        raise ConfigurationError(f"Invalid configuration in {config_file}:\n{error}") from error
