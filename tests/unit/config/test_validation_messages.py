"""The text policy table covers every Pydantic error type and formats only schema-derived keys."""

from __future__ import annotations

import typing

from pydantic_core import core_schema
from pydantic_core._pydantic_core import list_all_errors

from decoy_engine.safe_output import _validation as v

# ctx keys whose value comes from the input, or from a parser's own error text.
INPUT_DERIVED = frozenset(
    {"tag", "error", "input", "actual_length", "tz_actual", "encoding_error", "attribute"}
)


def test_policy_table_equals_the_library_catalog() -> None:
    assert set(typing.get_args(core_schema.ErrorType)) == v.POLICY_TYPES


def test_each_row_formats_only_schema_derived_keys_the_library_provides() -> None:
    placeholders = {e["type"]: e["example_context"] or {} for e in list_all_errors()}
    for err_type, (_template, keys) in v._FORMATTED.items():
        assert not set(keys) & INPUT_DERIVED, err_type
        assert set(keys) <= set(placeholders[err_type]) | {"class"}, err_type


def test_fixed_rows_never_format_a_placeholder() -> None:
    assert not [t for t, text in v._FIXED.items() if "{" in text]


def test_location_joins_names_and_indexes() -> None:
    assert v._location(("tables", 0, "columns", 2, "name")) == "tables[0].columns[2].name"
    assert v._location(()) == ""


def test_plural_suffix_is_derived_from_the_count() -> None:
    assert (
        v._text_for("string_too_long", {"max_length": 1})
        == "String should have at most 1 character"
    )
    assert (
        v._text_for("string_too_long", {"max_length": 3})
        == "String should have at most 3 characters"
    )
    ctx = {"field_type": "List", "min_length": 2, "actual_length": 99}
    assert v._text_for("too_short", ctx) == "List should have at least 2 items"
