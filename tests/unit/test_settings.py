from importlib.resources import files
from pathlib import Path

import pytest

from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.config.settings import AgentSettings, load_settings
from spyhop.domain.exceptions import ConfigurationError
from spyhop.domain.models import ChecksPhase
from tests.factories import settings


def test_example_configuration_is_valid(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"

    config_file.write_text((files("spyhop") / "templates" / "config.example.yaml").read_text())

    loaded = load_settings(config_file)

    assert loaded.application.dry_run is True
    assert loaded.repositories["main"].path == Path.home() / "git" / "your-repository"
    assert loaded.service.env_source == "env_file"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("  ", None),
        (" /code-review-loop --rounds 2 ", "/code-review-loop --rounds 2"),
    ],
)
def test_blank_review_command_means_no_review(value: str | None, expected: str | None) -> None:
    assert AgentSettings(review_command=value).review_command == expected


def test_environment_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.yaml"

    config_file.write_text(
        "application:\n  dry_run: true\n  polling_interval: 60\n"
        "jira:\n  base_url: https://example.atlassian.net\n"
    )
    monkeypatch.setenv("SPYHOP_APPLICATION__DRY_RUN", "false")

    loaded = load_settings(config_file)

    assert loaded.application.dry_run is False
    assert loaded.application.polling_interval == 60


def test_paths_expand_home_and_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOS", str(tmp_path))

    loaded = settings(tmp_path, repositories={"portal": {"path": "${REPOS}/portal"}})

    assert loaded.repositories["portal"].path == tmp_path / "portal"


def test_unknown_repository_alias_is_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"

    config_file.write_text(
        "jira:\n  base_url: https://example.atlassian.net\n"
        "projects:\n  LPD:\n    workflows:\n      Bug:\n        name: bug_fix\n"
        "        trigger_statuses: [In Progress]\n        repository: missing\n"
        "        prompt: bug_fix.md\n"
    )

    with pytest.raises(ConfigurationError, match="unknown repository alias 'missing'"):
        load_settings(config_file)


def test_missing_configuration_file_suggests_init(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="spyhop config init"):
        load_settings(tmp_path / "absent.yaml")


BUG = {
    "name": "bug_fix",
    "trigger_statuses": ["In Progress"],
    "repository": "portal",
    "prompt": "bug_fix.md",
}


def test_a_pipeline_gives_the_phases_and_the_plan_review(tmp_path: Path) -> None:
    loaded = settings(
        tmp_path,
        projects={"LPD": {"workflows": {"Bug": {**BUG, "pipeline": "bug"}}}},
        pipelines={
            "bug": [
                {"checks": ["ticket-info"]},
                "plan",
                "plan_review",
                "implement",
                {"checks": ["regressions"], "on_fail": "fix", "rounds": 2},
            ]
        },
    )
    workflow = WorkflowResolver(loaded).resolve("LPD", "Bug")

    assert workflow is not None
    assert workflow.plan_review is True
    assert workflow.phases == (
        ChecksPhase(("ticket-info",)),
        "plan",
        "plan_review",
        "implement",
        ChecksPhase(("regressions",), "fix", 2),
    )
    assert workflow.checkpoints_around_implement() == (("ticket-info",), ("regressions",))


def test_without_a_pipeline_plan_review_gives_the_phases(tmp_path: Path) -> None:
    loaded = settings(
        tmp_path, projects={"LPD": {"workflows": {"Bug": {**BUG, "plan_review": False}}}}
    )
    workflow = WorkflowResolver(loaded).resolve("LPD", "Bug")

    assert workflow is not None
    assert workflow.phases == ("implement",)
    assert workflow.checkpoints == ()


@pytest.mark.parametrize(
    ("pipeline", "error"),
    [
        (["plan", "plan_review"], "no implement step"),
        (["plan", "implement"], "plan and plan_review go together"),
        (["implement", "plan", "plan_review"], "in the order plan, plan_review, implement"),
        (["implement", "implement"], "implement is in the pipeline more than once"),
        (["plan", {"checks": ["a"]}, "plan_review", "implement"], "between plan and plan_review"),
        ([{"checks": ["a"], "on_fail": "fix"}, "implement"], "needs checks after the implement"),
        (["implement", {"checks": ["a"]}, {"checks": ["a"]}], "'a' is in the pipeline more"),
        (["review", "implement"], "Input should be"),
        (["implement", {"checks": []}], "at least 1 item"),
        (["implement", {"checks": ["a"], "rounds": 0}], "greater than or equal to 1"),
    ],
)
def test_a_wrong_pipeline_is_rejected(tmp_path: Path, pipeline: list[object], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        settings(tmp_path, pipelines={"bug": pipeline})


@pytest.mark.parametrize(
    ("workflow", "error"),
    [
        ({**BUG, "pipeline": "missing"}, "unknown pipeline 'missing'"),
        ({**BUG, "pipeline": "bug", "plan_review": False}, "plan_review comes from the pipeline"),
        ({**BUG, "checkpoints": ["regressions"]}, "checkpoints is replaced by pipeline"),
    ],
)
def test_a_workflow_with_a_wrong_pipeline_key_is_rejected(
    tmp_path: Path, workflow: dict[str, object], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        settings(
            tmp_path,
            projects={"LPD": {"workflows": {"Bug": workflow}}},
            pipelines={"bug": ["implement"]},
        )
