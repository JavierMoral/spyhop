import json

import httpx
import pytest
import respx

from spyhop.config.credentials import JiraCredentials
from spyhop.domain.exceptions import (
    JiraAuthenticationError,
    JiraError,
    JiraRateLimitedError,
)
from spyhop.infrastructure.jira.client import JiraClient

BASE_URL = "https://example.atlassian.net"
TOKEN = "super-secret-token"


def client() -> JiraClient:
    return JiraClient(BASE_URL, JiraCredentials(user="me@example.com", token=TOKEN), timeout=5)


@respx.mock
async def test_search_follows_next_page_token() -> None:
    route = respx.post(f"{BASE_URL}/rest/api/3/search/jql").mock(
        side_effect=[
            httpx.Response(
                200, json={"issues": [{"key": "A-1"}], "nextPageToken": "p2", "isLast": False}
            ),
            httpx.Response(200, json={"issues": [{"key": "A-2"}], "isLast": True}),
        ]
    )

    async with client() as jira:
        issues = await jira.search("project = A", ["status"])

    assert [issue["key"] for issue in issues] == ["A-1", "A-2"]

    first, second = (json.loads(call.request.content) for call in route.calls)

    assert first["expand"] == "changelog"
    assert "nextPageToken" not in first
    assert second["nextPageToken"] == "p2"
    assert route.calls[0].request.headers["Authorization"].startswith("Basic ")


@respx.mock
async def test_changelog_paginates_with_start_at() -> None:
    route = respx.get(f"{BASE_URL}/rest/api/3/issue/A-1/changelog").mock(
        side_effect=[
            httpx.Response(200, json={"values": [{"id": "1"}, {"id": "2"}], "isLast": False}),
            httpx.Response(200, json={"values": [{"id": "3"}], "isLast": True}),
        ]
    )

    async with client() as jira:
        histories = await jira.changelog("A-1")

    assert [history["id"] for history in histories] == ["1", "2", "3"]
    assert route.calls[1].request.url.params["startAt"] == "2"


@respx.mock
async def test_myself_reads_time_zone() -> None:
    respx.get(f"{BASE_URL}/rest/api/3/myself").mock(
        return_value=httpx.Response(
            200, json={"accountId": "abc", "displayName": "Me", "timeZone": "Europe/Madrid"}
        )
    )

    async with client() as jira:
        user = await jira.myself()

    assert user.time_zone == "Europe/Madrid"


@respx.mock
async def test_unauthorized_raises_authentication_error() -> None:
    respx.get(f"{BASE_URL}/rest/api/3/myself").mock(return_value=httpx.Response(401))

    async with client() as jira:
        with pytest.raises(JiraAuthenticationError) as error:
            await jira.myself()

    assert TOKEN not in str(error.value)


@respx.mock
async def test_rate_limit_reports_retry_after() -> None:
    respx.get(f"{BASE_URL}/rest/api/3/myself").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "12"})
    )

    async with client() as jira:
        with pytest.raises(JiraRateLimitedError) as error:
            await jira.myself()

    assert error.value.retry_after == 12


@respx.mock
async def test_errors_include_jira_messages_but_never_the_token() -> None:
    respx.post(f"{BASE_URL}/rest/api/3/search/jql").mock(
        return_value=httpx.Response(400, json={"errorMessages": ["Field 'x' does not exist"]})
    )

    async with client() as jira:
        with pytest.raises(JiraError, match="Field 'x' does not exist") as error:
            await jira.search("x = 1", [])

    assert TOKEN not in str(error.value)


@respx.mock
async def test_network_errors_are_wrapped() -> None:
    respx.get(f"{BASE_URL}/rest/api/3/myself").mock(side_effect=httpx.ConnectError("boom"))

    async with client() as jira:
        with pytest.raises(JiraError, match="ConnectError"):
            await jira.myself()


@respx.mock
async def test_project_statuses_by_issue_type() -> None:
    respx.get(f"{BASE_URL}/rest/api/3/project/LPD/statuses").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "Bug", "statuses": [{"name": "Open"}, {"name": "In Progress"}]},
                {"name": "Story", "statuses": [{"name": "In Development"}]},
            ],
        )
    )

    async with client() as jira:
        statuses = await jira.project_statuses("LPD")

    assert statuses == {"Bug": ["Open", "In Progress"], "Story": ["In Development"]}


@respx.mock
async def test_unknown_project_has_no_statuses() -> None:
    respx.get(f"{BASE_URL}/rest/api/3/project/NOPE/statuses").mock(
        return_value=httpx.Response(404, json={"errorMessages": ["No project could be found"]})
    )

    async with client() as jira:
        assert await jira.project_statuses("NOPE") is None
