"""Reads the columns of Orca's workspace board.

The Orca CLI has no command that lists them, so they are read from the active
profile's state, an internal format of Orca. The database is opened read-only
and any surprise in its shape is reported as an OrcaError.
"""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from spyhop.domain.exceptions import OrcaError


@dataclass(frozen=True)
class BoardColumn:
    id: str
    label: str


def read_board_columns(orca_dir: Path) -> list[BoardColumn]:
    database = orca_dir / "profiles" / _active_profile(orca_dir) / "profile-state.db"

    if not database.is_file():
        raise OrcaError(f"{database} not found")

    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)

        try:
            row = connection.execute(
                "SELECT payload FROM profile_state_documents WHERE domain = 'ui'"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise OrcaError(f"could not read {database}: {error}") from error

    try:
        statuses = json.loads(row[0])["workspaceStatuses"]

        return [BoardColumn(status["id"], status.get("label", "")) for status in statuses]
    except (TypeError, ValueError, KeyError) as error:
        raise OrcaError(f"no board columns found in {database}") from error


def _active_profile(orca_dir: Path) -> str:
    index = orca_dir / "orca-profile-index.json"

    try:
        profile = json.loads(index.read_text())["activeProfileId"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise OrcaError(f"could not read the active profile from {index}") from error

    if not isinstance(profile, str) or not profile:
        raise OrcaError(f"no active profile in {index}")

    return profile
