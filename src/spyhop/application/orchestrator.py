"""Registers detected entries as automation runs.

Phase 1 stops at DISCOVERED: preparing worktrees and launching agents come in
later phases.
"""

import logging

from spyhop.domain.models import AutomationRun, IssueEntry
from spyhop.infrastructure.persistence.repositories import RunRepository

_log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, runs: RunRepository, dry_run: bool) -> None:
        self._runs = runs
        self._dry_run = dry_run

    def process_entry(self, entry: IssueEntry) -> AutomationRun | None:
        issue = entry.issue

        if self._runs.has_transition(issue.key, entry.history_id):
            return None

        run = self._runs.create_discovered(entry)

        if run is None:
            _log.info(
                "%s entered %r at %s but already has an active run; ignoring",
                issue.key,
                entry.to_status,
                entry.entered_at.isoformat(),
            )

            return None

        _log.info(
            "%s%s entered %r at %s: workflow %s, repository %s (%s), base %s, plan review %s",
            "[dry run] " if self._dry_run else "",
            issue.key,
            entry.to_status,
            entry.entered_at.isoformat(),
            entry.workflow.name,
            entry.workflow.repository_alias,
            entry.workflow.repository_path,
            entry.workflow.base_branch,
            "yes" if entry.workflow.plan_review else "no",
        )

        return run
