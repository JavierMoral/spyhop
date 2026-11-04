import sqlite3
from pathlib import Path

import pytest

from spyhop.domain.exceptions import OrcaError
from spyhop.infrastructure.orca.board import BoardColumn, read_board_columns
from tests.factories import DEFAULT_COLUMNS, orca_dir


def test_columns_of_the_active_profile_are_read(tmp_path: Path) -> None:
    columns = [*DEFAULT_COLUMNS, {"id": "status-5", "label": "Plan review"}]

    assert read_board_columns(orca_dir(tmp_path, columns, profile="work"))[-1] == BoardColumn(
        "status-5", "Plan review"
    )


def test_missing_profile_index_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(OrcaError, match="active profile"):
        read_board_columns(tmp_path)


def test_missing_profile_database_is_an_error(tmp_path: Path) -> None:
    directory = orca_dir(tmp_path)

    (directory / "profiles" / "local-default" / "profile-state.db").unlink()

    with pytest.raises(OrcaError, match="not found"):
        read_board_columns(directory)


def test_unexpected_database_shape_is_an_error(tmp_path: Path) -> None:
    directory = orca_dir(tmp_path)
    connection = sqlite3.connect(directory / "profiles" / "local-default" / "profile-state.db")

    connection.execute("DELETE FROM profile_state_documents")
    connection.commit()
    connection.close()

    with pytest.raises(OrcaError, match="no board columns"):
        read_board_columns(directory)
