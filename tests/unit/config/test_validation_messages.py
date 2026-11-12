"""The text policy table covers every Pydantic error type and formats only schema-derived keys."""

from __future__ import annotations

import copy
import typing
from typing import Any

import pytest
from pydantic_core import core_schema
from pydantic_core._pydantic_core import list_all_errors

from decoy_engine import parse_pipeline_config
from decoy_engine.config import PipelineConfigError
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


# -- the shipped validators: rule text returns, submitted values do not ------------------------

VALUE = "ZZ_DATA_VALUE_91837"


def _base() -> dict[str, Any]:
    return {
        "version": 2,
        "global_settings": {"seed": 1},
        "sources": {"customers_prod": {"type": "file", "format": "csv", "path": "in.csv"}},
        "tables": [{"name": "customers_prod", "columns": [{"name": "a", "strategy": "redact"}]}],
        "targets": {"out": {"type": "file", "format": "csv", "path": "out.csv"}},
    }


def _with(**edits: Any) -> dict[str, Any]:
    cfg = copy.deepcopy(_base())
    cfg.update(edits)
    return cfg


def _error(cfg: dict[str, Any]) -> PipelineConfigError:
    with pytest.raises(PipelineConfigError) as info:
        parse_pipeline_config(cfg)
    return info.value


def test_producer_rule_text_reaches_the_user_without_the_submitted_delimiter() -> None:
    src = {
        "type": "file",
        "format": "csv",
        "path": "in.csv",
        "has_header": True,
        "delimiter": VALUE,
    }
    err = _error(_with(sources={"customers_prod": src}))
    assert "`delimiter` must be exactly one character" in str(err)
    assert VALUE not in str(err)


def test_quarantine_trigger_text_names_the_wired_set_not_the_trigger() -> None:
    quarantine = {"enabled": True, "output_path": "q.jsonl", "triggers": [VALUE]}
    err = _error(_with(quarantine=quarantine))
    assert "Wired triggers" in str(err)
    assert VALUE not in str(err)


def test_table_rule_text_names_the_table() -> None:
    bad = _with(tables=[{"name": "customers_prod"}])
    assert "a mask table requires at least one column" in str(_error(bad))


def test_fixed_width_overlap_text_names_the_column() -> None:
    layout = {
        "columns": [
            {"name": "a", "start": 0, "width": 5},
            {"name": "b", "start": 2, "width": 3},
        ]
    }
    src = {"type": "file", "format": "fixed_width", "path": "in.dat", "layout": layout}
    text = str(_error(_with(sources={"customers_prod": src})))
    assert "column 'b'" in text and "overlaps a preceding column" in text
