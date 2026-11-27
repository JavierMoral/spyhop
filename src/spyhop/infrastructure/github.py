"""Read-only GitHub client. It runs `gh api`, which keeps the token: Spyhop
never sees it. Every call is a GET, and each one goes through `_get`."""

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from spyhop.domain.exceptions import (
    GitHubError,
    GitHubRateLimitedError,
    GitHubUnavailableError,
)
from spyhop.domain.models import (
    CommitStatus,
    ContextStatus,
    PullComment,
    PullRequest,
    PullRequestRef,
)
from spyhop.infrastructure.process.command_runner import (
    CommandRunner,
    CommandTimeoutError,
    SubprocessRunner,
)

_API = "https://api.github.com/repos/"


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _login(user: dict[str, Any] | None) -> str:
    return (user or {}).get("login") or ""


class GitHubClient:
    def __init__(self, runner: CommandRunner | None = None, timeout: float = 30) -> None:
        self._runner = runner or SubprocessRunner()
        self._timeout = timeout

    async def login(self) -> str:
        data = await self._get("user")

        return str(data["login"])

    async def search(self, query: str) -> list[PullRequestRef]:
        data = await self._get("search/issues", {"q": query, "per_page": "100"})

        return [
            PullRequestRef(
                repository=item["repository_url"].removeprefix(_API),
                number=item["number"],
                author=_login(item.get("user")),
                created_at=_time(item["created_at"]),  # type: ignore[arg-type]
            )
            for item in data["items"]
        ]

    async def pull_request(self, repository: str, number: int) -> PullRequest:
        data = await self._get(f"repos/{repository}/pulls/{number}")
        closed_by = None

        if data["state"] == "closed":
            issue = await self._get(f"repos/{repository}/issues/{number}")
            closed_by = _login(issue.get("closed_by")) or None

        return PullRequest(
            repository=repository,
            number=number,
            author=_login(data.get("user")),
            head_branch=data["head"]["ref"],
            head_sha=data["head"]["sha"],
            created_at=_time(data["created_at"]),  # type: ignore[arg-type]
            state=data["state"],
            mergeable_state=data.get("mergeable_state"),
            closed_by=closed_by,
        )

    async def comments(self, repository: str, number: int) -> list[PullComment]:
        base = f"repos/{repository}"
        comments = [
            PullComment(
                f"issue:{item['id']}",
                _login(item.get("user")),
                item.get("body") or "",
                _time(item["created_at"]),  # type: ignore[arg-type]
            )
            for item in await self._pages(f"{base}/issues/{number}/comments")
        ]

        comments.extend(
            PullComment(
                f"review:{item['id']}",
                _login(item.get("user")),
                item.get("body") or "",
                _time(item.get("submitted_at") or item.get("created_at")),  # type: ignore[arg-type]
                state=item.get("state"),
            )
            for item in await self._pages(f"{base}/pulls/{number}/reviews")
        )
        comments.extend(
            PullComment(
                f"inline:{item['id']}",
                _login(item.get("user")),
                item.get("body") or "",
                _time(item["created_at"]),  # type: ignore[arg-type]
                location=f"{item['path']}:{item.get('line') or item.get('original_line') or '?'}",
            )
            for item in await self._pages(f"{base}/pulls/{number}/comments")
        )

        return sorted(comments, key=lambda comment: comment.created_at)

    async def commit_status(self, repository: str, sha: str) -> CommitStatus:
        data = await self._get(f"repos/{repository}/commits/{sha}/status", {"per_page": "100"})

        return CommitStatus(
            state=data["state"],
            contexts=tuple(
                ContextStatus(item["context"], item["state"], _time(item.get("updated_at")))
                for item in data.get("statuses", [])
            ),
        )

    async def _pages(self, path: str) -> list[dict[str, Any]]:
        pages = await self._get(path, {"per_page": "100"}, paginate=True)

        return [item for page in pages for item in page]

    async def _get(
        self, path: str, parameters: dict[str, str] | None = None, paginate: bool = False
    ) -> Any:
        return self._parse(await self._run(self._command(path, parameters, paginate)))

    @staticmethod
    def _command(path: str, parameters: dict[str, str] | None, paginate: bool) -> list[str]:
        command = ["gh", "api", "--method", "GET"]

        if paginate:
            command += ["--paginate", "--slurp"]

        for name, value in (parameters or {}).items():
            command += ["--raw-field", f"{name}={value}"]

        return [*command, path]

    async def _run(self, command: Sequence[str]) -> str:
        try:
            result = await self._runner.run(command, self._timeout)
        except CommandTimeoutError as error:
            raise GitHubError(f"gh did not answer: {error}") from None
        except OSError as error:
            raise GitHubUnavailableError(f"gh cannot run: {error}") from None

        if result.exit_code == 0:
            return result.stdout

        message = result.stderr.strip()[:500] or f"gh exited with {result.exit_code}"
        lowered = message.lower()

        if "rate limit" in lowered:
            raise GitHubRateLimitedError(message)

        if "gh auth login" in lowered or "not logged in" in lowered or result.exit_code == 4:
            raise GitHubUnavailableError(f"gh is not signed in: {message}")

        raise GitHubError(message)

    @staticmethod
    def _parse(text: str) -> Any:
        try:
            return json.loads(text)
        except ValueError:
            raise GitHubError("gh gave an answer that is not JSON") from None
