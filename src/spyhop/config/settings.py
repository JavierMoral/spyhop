"""Spyhop configuration.

Precedence, lowest first: model defaults < config.yaml < SPYHOP_* environment
variables (nested with "__", for example SPYHOP_APPLICATION__DRY_RUN=true) <
command line options, which callers apply with ``model_copy``.
"""

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from spyhop.config.paths import default_env_file, expand_path
from spyhop.domain.exceptions import ConfigurationError


def _expand(value: Any) -> Any:
    if isinstance(value, str | Path):
        return expand_path(value)

    return value


ExpandedPath = Annotated[Path, BeforeValidator(_expand)]


class ApplicationSettings(BaseModel):
    polling_interval: int = Field(default=45, ge=10)
    max_concurrent_agents: int = Field(default=1, ge=1)
    dry_run: bool = True
    timezone: str | None = None


class JiraSettings(BaseModel):
    base_url: str
    user_env: str = "JIRA_API_USER"
    token_env: str = "JIRA_API_TOKEN"
    request_timeout: float = Field(default=20, gt=0)
    transition_margin_minutes: int = Field(default=5, ge=1)


class RepositorySettings(BaseModel):
    path: ExpandedPath
    default_base_branch: str = "master"


class WorkflowSettings(BaseModel):
    name: str
    trigger_statuses: list[str] = Field(min_length=1)
    repository: str
    base_branch: str | None = None
    prompt: str
    plan_review: bool = True


class ProjectSettings(BaseModel):
    enabled: bool = True
    workflows: dict[str, WorkflowSettings] = Field(default_factory=dict)


class OrcaSettings(BaseModel):
    executable: ExpandedPath | None = None
    startup_timeout: float = Field(default=30, gt=0)
    operation_timeout: float = Field(default=120, gt=0)


class AgentSettings(BaseModel):
    provider: str = "claude"
    model: str = "sonnet"
    effort: str = "high"
    max_retries: int = Field(default=0, ge=0)


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

        return self

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
