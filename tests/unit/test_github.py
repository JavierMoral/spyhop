"""The read-only GitHub client: every call is a GET through gh."""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from spyhop.domain.exceptions import (
    GitHubError,
    GitHubRateLimitedError,
    GitHubUnavailableError,
)
from spyhop.infrastructure.github import GitHubClient
from spyhop.infrastructure.process.command_runner import CommandResult, CommandTimeoutError

PULL = {
    "state": "closed",
    "created_at": "2026-11-01T10:00:00Z",
    "user": {"login": "me"},
    "head": {"ref": "branch", "sha": "abc1234"},
    "mergeable_state": "dirty",
}


class Runner:
    def __init__(self, answers: list[Any] | None = None) -> None:
        self.commands: list[list[str]] = []
        self.answers = answers or []

    async def run(
        self,
        args: Sequence[str],
        time_limit: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> CommandResult:
        self.commands.append(list(args))

        answer = self.answers.pop(0) if self.answers else {}

        if isinstance(answer, Exception):
            raise answer

        if isinstance(answer, CommandResult):
            return answer

        return CommandResult(0, json.dumps(answer), "")


def answers() -> list[Any]:
    """One answer for each call of the methods that the test below runs."""

    comment = {"id": 1, "user": {"login": "x"}, "body": "b", "created_at": "2026-11-01T10:00:00Z"}

    return [
        {"login": "me"},  # login
        {"items": []},  # search
        PULL,  # pull_request: the pull request
        {"closed_by": {"login": "boss"}},  # pull_request: the issue
        [[comment]],  # comments: issue comments
        [[{**comment, "state": "APPROVED"}]],  # comments: reviews
        [[{**comment, "path": "a.java", "line": 3}]],  # comments: inline comments
        {"state": "failure", "statuses": [{"context": "ci", "state": "failure"}]},
    ]


async def test_every_command_of_the_client_is_a_get_through_gh() -> None:
    runner = Runner(answers())
    client = GitHubClient(runner)

    await client.login()
    await client.search("is:pr head:branch")
    pull_request = await client.pull_request("acme/repo", 7)
    comments = await client.comments("acme/repo", 7)
    status = await client.commit_status("acme/repo", "abc1234")

    assert len(runner.commands) == 8

    for command in runner.commands:
        assert command[:4] == ["gh", "api", "--method", "GET"]
        assert "--method" not in command[4:]
        assert not {"-X", "-f", "-F", "--field", "--input"} & set(command)

    assert (pull_request.closed_by, pull_request.mergeable_state) == ("boss", "dirty")
    assert [(comment.id, comment.state, comment.location) for comment in comments] == [
        ("issue:1", None, None),
        ("review:1", "APPROVED", None),
        ("inline:1", None, "a.java:3"),
    ]
    assert status.contexts[0].context == "ci"


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (CommandResult(1, "", "API rate limit exceeded"), GitHubRateLimitedError),
        (
            CommandResult(4, "", "To get started with GitHub CLI, run: gh auth login"),
            GitHubUnavailableError,
        ),
        (CommandResult(1, "", "HTTP 404"), GitHubError),
        (CommandResult(0, "not json", ""), GitHubError),
        (CommandTimeoutError(30), GitHubError),
        (FileNotFoundError("gh"), GitHubUnavailableError),
    ],
)
async def test_a_failure_of_gh_is_a_github_error(result: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        await GitHubClient(Runner([result])).login()


async def test_the_token_is_never_in_a_command_or_an_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret-token")

    runner = Runner([CommandResult(1, "", "HTTP 401")])

    with pytest.raises(GitHubError) as error:
        await GitHubClient(runner).login()

    assert "secret-token" not in str(error.value) + str(runner.commands) + caplog.text
