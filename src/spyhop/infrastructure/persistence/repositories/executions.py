"""The executions of the agents, for their history."""

import dataclasses
import json
import sqlite3
from datetime import datetime

from spyhop.domain.models import AgentExecution
from spyhop.infrastructure.persistence.repositories._mapping import _to_datetime, _to_text

_COLUMNS = [field.name for field in dataclasses.fields(AgentExecution)]


class ExecutionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def save(self, execution: AgentExecution) -> None:
        """Adds the execution, or replaces the row of the same session."""

        values = [
            _to_text(value)
            if isinstance(value, datetime)
            else json.dumps(value)
            if isinstance(value, dict)
            else value
            for value in (getattr(execution, name) for name in _COLUMNS)
        ]

        self._connection.execute(
            f"INSERT OR REPLACE INTO agent_executions ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
            values,
        )

    def list(
        self, agent: str | None = None, since: datetime | None = None, source: str | None = None
    ) -> list[AgentExecution]:
        """Oldest first."""

        rows = self._connection.execute(
            "SELECT * FROM agent_executions WHERE (? IS NULL OR agent = ?) "
            "AND (? IS NULL OR started_at >= ?) AND (? IS NULL OR source = ?) "
            "ORDER BY started_at",
            (agent, agent, _to_text(since), _to_text(since), source, source),
        )

        return [_to_execution(row) for row in rows]


def _to_execution(row: sqlite3.Row) -> AgentExecution:
    values = {name: row[name] for name in _COLUMNS}

    values["started_at"] = _to_datetime(row["started_at"])
    values["finished_at"] = _to_datetime(row["finished_at"])
    values["profile"] = json.loads(row["profile"]) if row["profile"] else None

    return AgentExecution(**values)
