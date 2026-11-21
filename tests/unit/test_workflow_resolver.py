import dataclasses
from pathlib import Path

from spyhop.application.workflow_resolver import WorkflowResolver
from spyhop.domain.enums import RunStatus
from tests.factories import settings
from tests.unit.test_supervisor import run_in


def test_the_workflow_of_a_run_comes_from_the_project_of_its_issue_key(tmp_path: Path) -> None:
    resolver = WorkflowResolver(settings(tmp_path))
    run = run_in(RunStatus.RUNNING)

    resolved = resolver.resolve_for_run(run)

    assert resolved is not None and resolved.name == "bug_fix"
    assert resolver.resolve_for_run(dataclasses.replace(run, issue_key="OFF-1")) is None
    assert resolver.resolve_for_run(dataclasses.replace(run, workflow_name="gone")) is None
