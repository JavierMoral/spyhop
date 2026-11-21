import dataclasses
from pathlib import Path

from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.domain.enums import RunStatus
from spyhop.domain.models import ChecksPhase
from tests.factories import settings
from tests.unit.test_supervisor import run_in


def test_the_workflow_of_a_run_comes_from_the_project_of_its_issue_key(tmp_path: Path) -> None:
    resolver = WorkflowResolver(settings(tmp_path))
    run = run_in(RunStatus.RUNNING)

    resolved = resolver.resolve_for_run(run)

    assert resolved is not None and resolved.name == "bug_fix"
    assert resolver.resolve_for_run(dataclasses.replace(run, issue_key="OFF-1")) is None
    assert resolver.resolve_for_run(dataclasses.replace(run, workflow_name="gone")) is None


def test_a_started_run_keeps_its_pipeline_when_the_configuration_changes(
    tmp_path: Path,
) -> None:
    resolver = WorkflowResolver(settings(tmp_path))
    run = run_in(RunStatus.RUNNING)
    pipeline = ("implement", ChecksPhase(("regressions",), "fix"))

    resolved = resolver.resolve_for_run(dataclasses.replace(run, pipeline=pipeline))
    configured = resolver.resolve_for_run(run)

    assert resolved is not None and resolved.phases == pipeline
    assert not resolved.plan_review
    assert configured is not None and configured.phases == ("plan", "plan_review", "implement")
