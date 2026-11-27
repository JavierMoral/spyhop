"""`spyhop status KEY` lists the pull requests of the worktree."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spyhop.cli import app
from spyhop.domain.models import CachedPullRequest, OrcaWorktree, PullRequest
from spyhop.infrastructure.persistence.database import connect
from spyhop.infrastructure.persistence.repositories import (
    PullRequestRepository,
    WorktreeRepository,
)
from tests.factories import settings


def test_status_shows_a_line_for_each_pull_request_and_marks_the_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "spyhop.db"
    connection = connect(database)
    worktree = WorktreeRepository(connection).create(
        "LPD-1",
        OrcaWorktree("repo::wt", tmp_path / "wt", "me/LPD-1", "master", None, "", False),
        tmp_path / "portal",
        "master",
    )
    rows = PullRequestRepository(connection)

    for number, minute, state, verdict, ci in [
        (1, 1, "closed", "rejected", None),
        (2, 2, "open", None, "failure"),
    ]:
        rows.save(
            worktree.id,
            CachedPullRequest(
                PullRequest(
                    "acme/team",
                    number,
                    "me",
                    "me/LPD-1",
                    "abc",
                    datetime(2026, 11, 1, 10, minute, tzinfo=UTC),
                    state,  # type: ignore[arg-type]
                ),
                None,
                ci,
                verdict,
            ),
        )

    monkeypatch.setattr("spyhop.cli.runs._load", lambda _: settings(tmp_path))
    monkeypatch.setattr("spyhop.cli.runs.database_file", lambda _: database)

    result = CliRunner().invoke(app, ["status", "LPD-1"])

    assert result.exit_code == 0
    assert "PR acme/team#1 closed (rejected) CI -" in result.output
    assert "PR acme/team#2 open CI failure vigente" in result.output
