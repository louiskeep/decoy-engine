"""GEN-TYPES E3: the `lookup` options in the registry, the catalog and the schema."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from decoy_engine import reasons
from decoy_engine.config import PipelineConfig
from decoy_engine.config import _generate_option_catalog as catalog
from decoy_engine.config import _generate_options as opts
from decoy_engine.config._tables import GENERATE_TYPES, GenerateColumnConfig

TYPE = str(reasons.config.GENERATE_OPTION_TYPE)
VALUE = str(reasons.config.GENERATE_OPTION_VALUE_UNKNOWN)
UNKNOWN = str(reasons.config.GENERATE_OPTION_UNKNOWN)
UNSUPPORTED = str(reasons.config.GENERATE_UNIQUE_UNSUPPORTED)


def _cfg(col: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 2,
        "global_settings": {"seed": 1},
        "sources": {},
        "tables": [{"name": "t", "row_count": 3, "generate_columns": [col]}],
        "targets": {"t": {"type": "file", "format": "csv", "path": "o.csv"}},
    }


def _types(col: dict[str, Any]) -> list[str]:
    with pytest.raises(ValidationError) as exc:
        PipelineConfig.model_validate(_cfg({"name": "x", "type": "lookup", **col}))
    return [str(e["type"]) for e in exc.value.errors()]


def test_lookup_is_a_registered_generate_type() -> None:
    assert "lookup" in GENERATE_TYPES
    assert set(opts.GENERATE_OPTIONS["lookup"].keys) - {"output_type"} == {
        "file",
        "codeset",
        "value_column",
        "weight_column",
    }


def test_either_file_or_codeset_is_accepted() -> None:
    for col in ({"file": "a.csv"}, {"codeset": "hcpcs"}):
        PipelineConfig.model_validate(_cfg({"name": "x", "type": "lookup", **col}))


def test_giving_neither_is_a_missing_option() -> None:
    assert "missing" in _types({})


def test_giving_both_is_refused() -> None:
    assert VALUE in _types({"file": "a.csv", "codeset": "hcpcs"})


@pytest.mark.parametrize("key", ["file", "codeset", "value_column", "weight_column"])
def test_each_option_must_be_text(key: str) -> None:
    extra = {"file": "a.csv"} if key != "file" else {}
    assert TYPE in _types({**extra, key: 3})


def test_a_foreign_key_is_unknown() -> None:
    assert UNKNOWN in _types({"file": "a.csv", "separator": ","})


def test_unique_is_refused_on_a_lookup_column() -> None:
    with pytest.raises(ValidationError) as exc:
        GenerateColumnConfig(name="c", type="lookup", file="a.csv", unique=True)
    assert UNSUPPORTED in [str(e["type"]) for e in exc.value.errors()]


def test_output_type_is_accepted_on_a_lookup_column() -> None:
    PipelineConfig.model_validate(
        _cfg({"name": "x", "type": "lookup", "file": "a.csv", "output_type": "int"})
    )


def test_the_catalog_and_schema_say_exactly_one_of() -> None:
    entry = catalog.generate_option_catalog()["lookup"]
    assert entry.one_of == ("file", "codeset")
    assert entry.required == ()
    schema: dict[str, Any] = {}
    catalog.closed_key_schema(schema)
    branch = next(
        b["then"] for b in schema["allOf"] if b["if"]["properties"]["type"]["const"] == "lookup"
    )
    assert branch["oneOf"] == [{"required": ["file"]}, {"required": ["codeset"]}]
    assert "oneOf" not in next(
        b["then"] for b in schema["allOf"] if b["if"]["properties"]["type"]["const"] == "constant"
    )
