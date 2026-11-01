from typing import Any, Protocol

from spyhop.domain.models import JiraUser


class JiraGateway(Protocol):
    async def myself(self) -> JiraUser: ...

    async def search(self, jql: str, fields: list[str]) -> list[dict[str, Any]]: ...

    async def changelog(self, issue_key: str) -> list[dict[str, Any]]: ...
