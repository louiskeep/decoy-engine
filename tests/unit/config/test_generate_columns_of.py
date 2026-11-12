"""`config.generate_columns_of`: the one read of a table's generate columns as a list."""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine import config

_COLUMNS = [{"name": "id", "type": "sequence"}]


@pytest.mark.parametrize(
    "table",
    [{"name": "t"}, {"name": "t", "generate_columns": None}, {"name": "t", "generate_columns": []}],
    ids=["missing", "none", "empty"],
)
def test_a_table_without_generate_columns_gives_an_empty_list(table: dict[str, Any]) -> None:
    found = config.generate_columns_of(table)
    assert found == []
    assert type(found) is list


def test_the_declared_list_is_returned_itself_not_a_copy() -> None:
    table = {"name": "t", "generate_columns": _COLUMNS}
    assert config.generate_columns_of(table) is _COLUMNS


def test_the_helper_is_a_public_config_export() -> None:
    assert "generate_columns_of" in config.__all__


@pytest.mark.parametrize(
    ("table", "kind"),
    [
        ({"name": "t"}, "mask"),
        ({"name": "t", "generate_columns": None}, "mask"),
        ({"name": "t", "generate_columns": []}, "mask"),
        ({"name": "t", "generate_columns": _COLUMNS}, "generate"),
    ],
    ids=["missing", "none", "empty", "non_empty"],
)
def test_the_classifier_reads_each_table_shape_as_before(table: dict[str, Any], kind: str) -> None:
    assert config.classify_table_kinds({"tables": [table]}) == {"t": kind}
