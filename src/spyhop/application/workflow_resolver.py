from spyhop.config.settings import Settings, WorkflowSettings
from spyhop.domain.models import ResolvedWorkflow


class WorkflowResolver:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(self, project_key: str, issue_type: str) -> ResolvedWorkflow | None:
        project = self._settings.enabled_projects().get(project_key)

        if project is None:
            return None

        for workflow_issue_type, workflow in project.workflows.items():
            if workflow_issue_type.casefold() == issue_type.casefold():
                return self._resolved(workflow)

        return None

    def resolve_by_name(self, project_key: str, workflow_name: str) -> ResolvedWorkflow | None:
        """The workflow a run was created with, looked up again when launching."""

        project = self._settings.enabled_projects().get(project_key)

        if project is None:
            return None

        for workflow in project.workflows.values():
            if workflow.name == workflow_name:
                return self._resolved(workflow)

        return None

    def project_keys(self) -> list[str]:
        return sorted(
            key for key, project in self._settings.enabled_projects().items() if project.workflows
        )

    def trigger_statuses(self) -> list[str]:
        return sorted(
            {
                status
                for project in self._settings.enabled_projects().values()
                for workflow in project.workflows.values()
                for status in workflow.trigger_statuses
            }
        )

    def _resolved(self, workflow: WorkflowSettings) -> ResolvedWorkflow:
        repository = self._settings.repositories[workflow.repository]

        return ResolvedWorkflow(
            name=workflow.name,
            trigger_statuses=tuple(workflow.trigger_statuses),
            repository_alias=workflow.repository,
            repository_path=repository.path,
            base_branch=workflow.base_branch or repository.default_base_branch,
            prompt=workflow.prompt,
            plan_review=workflow.plan_review,
            setup=repository.setup,
            build_commands=tuple(repository.build_commands),
        )
