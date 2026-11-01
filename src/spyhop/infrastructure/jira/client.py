"""Read-only JIRA Cloud REST v3 client.

The API token only ever travels in the Authorization header built by httpx; it
is never logged, stored or included in an exception message.
"""

from types import TracebackType
from typing import Any, Self

import httpx

from spyhop.config.credentials import JiraCredentials
from spyhop.domain.exceptions import (
    JiraAuthenticationError,
    JiraError,
    JiraRateLimitedError,
)
from spyhop.domain.models import JiraUser

_PAGE_SIZE = 100


class JiraClient:
    def __init__(
        self,
        base_url: str,
        credentials: JiraCredentials,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            auth=httpx.BasicAuth(credentials.user, credentials.token),
            base_url=base_url.rstrip("/"),
            headers={"Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def myself(self) -> JiraUser:
        data = await self._request("GET", "/rest/api/3/myself")

        return JiraUser(
            account_id=data["accountId"],
            display_name=data.get("displayName", ""),
            time_zone=data.get("timeZone") or "UTC",
        )

    async def project_statuses(self, project_key: str) -> dict[str, list[str]] | None:
        """Statuses per issue type, or None when the project does not exist or
        is not visible. Search silently returns nothing for unknown projects, so
        this is how a misconfigured project is detected."""

        try:
            data: Any = await self._request("GET", f"/rest/api/3/project/{project_key}/statuses")
        except JiraError as error:
            if error.status_code == 404:
                return None

            raise

        return {
            issue_type["name"]: [status["name"] for status in issue_type.get("statuses", [])]
            for issue_type in data
        }

    async def search(self, jql: str, fields: list[str]) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        body: dict[str, Any] = {
            "expand": "changelog",
            "fields": fields,
            "jql": jql,
            "maxResults": _PAGE_SIZE,
        }

        while True:
            data = await self._request("POST", "/rest/api/3/search/jql", json=body)

            issues.extend(data.get("issues", []))

            next_page_token = data.get("nextPageToken")

            if data.get("isLast", True) or not next_page_token:
                return issues

            body["nextPageToken"] = next_page_token

    async def changelog(self, issue_key: str) -> list[dict[str, Any]]:
        histories: list[dict[str, Any]] = []
        start_at = 0

        while True:
            data = await self._request(
                "GET",
                f"/rest/api/3/issue/{issue_key}/changelog",
                params={"maxResults": _PAGE_SIZE, "startAt": start_at},
            )

            values = data.get("values", [])

            histories.extend(values)

            if data.get("isLast", True) or not values:
                return histories

            start_at += len(values)

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, url, **kwargs)
        except httpx.HTTPError as error:
            raise JiraError(f"{method} {url} failed: {type(error).__name__}") from None

        if response.status_code in (401, 403):
            raise JiraAuthenticationError(
                f"JIRA rejected the credentials ({response.status_code})",
                status_code=response.status_code,
            )

        if response.status_code == 429:
            raise JiraRateLimitedError(_retry_after(response))

        if response.is_error:
            raise JiraError(
                f"{method} {url} returned {response.status_code}: {_error_text(response)}",
                status_code=response.status_code,
            )

        return response.json()


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]

    messages = list(data.get("errorMessages", [])) + list(data.get("errors", {}).values())

    return "; ".join(str(message) for message in messages)[:500] or response.reason_phrase


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return None
