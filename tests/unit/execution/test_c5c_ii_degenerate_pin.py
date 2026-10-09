"""C5c-ii degenerate-pin predicate and retype helper (`_faker_degenerate_pin`).

Pure unit tests for the pin-set membership (effective-deterministic Faker over bool/int/uint, a
string-output allowlisted provider, not `when:`-bearing, not an FK child) and the degenerate
retype (all-null or empty -> Arrow `string` with the string pandas metadata; value-bearing and
unrelated columns untouched).
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa

from decoy_engine.execution._faker_degenerate_pin import (
    deterministic_faker_pin_columns,
    pin_degenerate_to_string,
)

INT_SCHEMA = pa.schema([pa.field("f", pa.int64()), pa.field("p", pa.int64())])


def _faker(**over: Any) -> dict[str, Any]:
    col: dict[str, Any] = {
        "name": "f",
        "strategy": "faker",
        "provider": "person_first_name",
        "deterministic": True,
        "namespace": "ns",
        "pool_size": 40,
    }
    col.update(over)
    return col


def _cfg(
    col: dict[str, Any], *, relationships: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "tables": [{"name": "t", "columns": [col, {"name": "p", "strategy": "passthrough"}]}]
    }
    if relationships is not None:
        cfg["relationships"] = relationships
    return cfg


def test_includes_plain_deterministic_int_faker() -> None:
    assert deterministic_faker_pin_columns(_cfg(_faker()), "t", INT_SCHEMA) == frozenset({"f"})


def test_includes_allow_collisions_alias() -> None:
    col = _faker(deterministic=False, allow_collisions=True)
    assert deterministic_faker_pin_columns(_cfg(col), "t", INT_SCHEMA) == frozenset({"f"})


def test_includes_bool_source() -> None:
    schema = pa.schema([pa.field("f", pa.bool_()), pa.field("p", pa.int64())])
    assert deterministic_faker_pin_columns(_cfg(_faker()), "t", schema) == frozenset({"f"})


def test_excludes_when_bearing() -> None:
    # A `when:` predicate can leave value-bearing cells, so the column is never pinned.
    assert (
        deterministic_faker_pin_columns(_cfg(_faker(when="p > 0")), "t", INT_SCHEMA) == frozenset()
    )


def test_excludes_non_allowlisted_provider() -> None:
    # person_dob is not a string-output provider, so its numeric output must keep its own type.
    assert (
        deterministic_faker_pin_columns(_cfg(_faker(provider="person_dob")), "t", INT_SCHEMA)
        == frozenset()
    )


def test_excludes_non_deterministic() -> None:
    col = _faker(deterministic=False)
    assert deterministic_faker_pin_columns(_cfg(col), "t", INT_SCHEMA) == frozenset()


def test_excludes_float_and_string_sources() -> None:
    for typ in (pa.float64(), pa.string()):
        schema = pa.schema([pa.field("f", typ), pa.field("p", pa.int64())])
        assert deterministic_faker_pin_columns(_cfg(_faker()), "t", schema) == frozenset()


def test_excludes_fk_child_column() -> None:
    rel = [
        {
            "parent": {"table": "pt", "columns": ["k"]},
            "children": [{"table": "t", "columns": ["f"]}],
        }
    ]
    assert (
        deterministic_faker_pin_columns(_cfg(_faker(), relationships=rel), "t", INT_SCHEMA)
        == frozenset()
    )


def test_retype_pins_all_null_to_string() -> None:
    table = pa.table({"f": pa.array([None, None], pa.float64()), "g": pa.array([1, 2], pa.int64())})
    out = pin_degenerate_to_string(table, frozenset({"f"}))
    assert out.schema.field("f").type == pa.string()
    assert out.column("f").to_pylist() == [None, None]
    assert out.schema.field("g").type == pa.int64()


def test_retype_pins_empty_to_string() -> None:
    table = pa.table({"f": pa.array([], pa.float64())})
    assert pin_degenerate_to_string(table, frozenset({"f"})).schema.field("f").type == pa.string()


def test_retype_leaves_value_bearing_untouched() -> None:
    table = pa.table({"f": pa.array([1, None, 3], pa.int64())})
    # A value-bearing column is not degenerate, so it is not retyped (its native output would
    # already be string; a non-string value-bearing column is left as its route produced it).
    assert pin_degenerate_to_string(table, frozenset({"f"})).schema.field("f").type == pa.int64()


def test_retype_is_a_noop_without_pinned_columns() -> None:
    table = pa.table({"f": pa.array([None, None], pa.null())})
    assert pin_degenerate_to_string(table, frozenset()) is table


def test_retype_rewrites_pandas_metadata_to_string_shape() -> None:
    import pandas as pd

    frame = pd.DataFrame({"f": pd.Series([None, None], dtype="object"), "g": [1, 2]})
    table = pa.Table.from_pandas(frame, preserve_index=False)
    out = pin_degenerate_to_string(table, frozenset({"f"}))
    meta = json.loads(out.schema.metadata[b"pandas"])
    f_entry = next(c for c in meta["columns"] if c["name"] == "f")
    assert (f_entry["pandas_type"], f_entry["numpy_type"]) == ("unicode", "object")
    g_entry = next(c for c in meta["columns"] if c["name"] == "g")
    assert g_entry["pandas_type"] == "int64"
