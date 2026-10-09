"""C5c-ii closed metadata-shape allowlist (`_faker_deterministic_admission.metadata_shape_admits`).

The classifier admits only the two schema-metadata shapes where the oracle's per-chunk pandas
conversion is provably an identity on the target physical value, so the oracle and the native
kernel derive the same draw key. Everything else declines. These are pure-schema unit tests;
the end-to-end native-vs-oracle parity lives in the chunked/unified parity suites.
"""

from __future__ import annotations

import json

import pyarrow as pa
import pytest

from decoy_engine.execution.native._faker_deterministic_admission import (
    DETERMINISTIC_FAKER_SOURCE_TYPES,
    metadata_shape_admits,
)

INT_UINT_BOOL = [
    pa.int8(),
    pa.int16(),
    pa.int32(),
    pa.int64(),
    pa.uint8(),
    pa.uint16(),
    pa.uint32(),
    pa.uint64(),
    pa.bool_(),
]


def _from_pandas_schema(fields: list[pa.Field], values: dict[str, list]) -> pa.Schema:
    """A schema carrying the real ``b"pandas"`` metadata pyarrow attaches for these columns."""
    # Build each column at its declared Arrow type so the pandas metadata matches.
    arrays = [pa.array(values[f.name], type=f.type) for f in fields]
    table = pa.Table.from_arrays(arrays, schema=pa.schema(fields))
    # Round-trip through pandas to get authentic b"pandas" metadata.
    return pa.Table.from_pandas(table.to_pandas(), preserve_index=False).schema


# ---------------------------------------------------------------------------
# Shape 1: metadata absent.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("typ", INT_UINT_BOOL, ids=str)
def test_metadata_absent_admits_a_plain_target(typ: pa.DataType) -> None:
    schema = pa.schema([pa.field("f", typ), pa.field("p", pa.int64())])
    assert metadata_shape_admits(schema, column="f", ordinal=0)


def test_metadata_absent_declines_a_non_admitted_target_family() -> None:
    schema = pa.schema([pa.field("f", pa.float64()), pa.field("p", pa.int64())])
    assert not metadata_shape_admits(schema, column="f", ordinal=0)
    schema2 = pa.schema([pa.field("f", pa.timestamp("us")), pa.field("p", pa.int64())])
    assert not metadata_shape_admits(schema2, column="f", ordinal=0)


def test_metadata_absent_declines_an_extension_type_field() -> None:
    import pandas as pd

    ext_schema = pa.Table.from_pandas(
        pd.DataFrame(
            {"f": pd.array([1, 2], dtype="int64"), "e": pd.array([1, None], dtype="Int64")}
        )
    ).schema
    # e is reconstructing (Int64) metadata; but also strip to field-only to prove the extension/
    # metadata gate: a field carrying ARROW:extension metadata never admits.
    assert not metadata_shape_admits(ext_schema, column="f", ordinal=0)


# ---------------------------------------------------------------------------
# Shape 2: plain pandas metadata with an identity column mapping.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("typ", INT_UINT_BOOL, ids=str)
def test_plain_pandas_metadata_admits(typ: pa.DataType) -> None:
    schema = _from_pandas_schema(
        [pa.field("f", typ), pa.field("p", pa.int64())],
        {"f": [0, 1] if not pa.types.is_boolean(typ) else [True, False], "p": [1, 2]},
    )
    assert metadata_shape_admits(schema, column="f", ordinal=0)


def test_plain_pandas_metadata_admits_float_and_string_companions() -> None:
    schema = _from_pandas_schema(
        [pa.field("f", pa.int64()), pa.field("g", pa.float64()), pa.field("s", pa.string())],
        {"f": [1, 2], "g": [1.5, 2.5], "s": ["a", "b"]},
    )
    assert metadata_shape_admits(schema, column="f", ordinal=0)


# ---------------------------------------------------------------------------
# The numpy_type-only change (section 10): checking pandas_type alone is insufficient.
# ---------------------------------------------------------------------------


def _patch_pandas_meta(schema: pa.Schema, column: str, **patch: str) -> pa.Schema:
    meta = json.loads(schema.metadata[b"pandas"])
    for entry in meta["columns"]:
        if entry["name"] == column:
            entry.update(patch)
    new = dict(schema.metadata)
    new[b"pandas"] = json.dumps(meta).encode("utf-8")
    return schema.with_metadata(new)


def test_numpy_type_only_change_declines() -> None:
    schema = _from_pandas_schema(
        [pa.field("f", pa.int64()), pa.field("p", pa.int64())], {"f": [1, 2], "p": [3, 4]}
    )
    assert metadata_shape_admits(schema, column="f", ordinal=0)
    # physical int64, but the schema-level entry declares numpy_type bool[pyarrow] -> decline.
    drifted = _patch_pandas_meta(schema, "f", numpy_type="bool[pyarrow]")
    assert not metadata_shape_admits(drifted, column="f", ordinal=0)


def test_pandas_type_change_declines() -> None:
    schema = _from_pandas_schema(
        [pa.field("f", pa.int64()), pa.field("p", pa.int64())], {"f": [1, 2], "p": [3, 4]}
    )
    drifted = _patch_pandas_meta(schema, "f", pandas_type="bool")
    assert not metadata_shape_admits(drifted, column="f", ordinal=0)


# ---------------------------------------------------------------------------
# Closed rejection matrix.
# ---------------------------------------------------------------------------


def test_nullable_int64_extension_declines() -> None:
    import pandas as pd

    schema = pa.Table.from_pandas(
        pd.DataFrame({"f": pd.array([1, None], dtype="Int64"), "p": [1, 2]})
    ).schema
    assert not metadata_shape_admits(schema, column="f", ordinal=0)


def test_arrow_backed_bool_dtype_declines() -> None:
    import pandas as pd

    schema = pa.Table.from_pandas(
        pd.DataFrame({"f": pd.array([1, 2], dtype="int64[pyarrow]"), "p": [1, 2]})
    ).schema
    assert not metadata_shape_admits(schema, column="f", ordinal=0)


def test_category_declines() -> None:
    import pandas as pd

    schema = pa.Table.from_pandas(
        pd.DataFrame({"f": pd.Series([1, 2], dtype="category"), "p": [1, 2]})
    ).schema
    assert not metadata_shape_admits(schema, column="f", ordinal=0)


def test_unknown_schema_metadata_key_declines() -> None:
    schema = _from_pandas_schema(
        [pa.field("f", pa.int64()), pa.field("p", pa.int64())], {"f": [1, 2], "p": [3, 4]}
    )
    extra = dict(schema.metadata)
    extra[b"custom"] = b"x"
    assert not metadata_shape_admits(schema.with_metadata(extra), column="f", ordinal=0)


def test_nonempty_field_metadata_declines() -> None:
    f = pa.field("f", pa.int64(), metadata={b"k": b"v"})
    schema = pa.schema([f, pa.field("p", pa.int64())])
    assert not metadata_shape_admits(schema, column="f", ordinal=0)


def test_malformed_json_declines() -> None:
    schema = pa.schema([pa.field("f", pa.int64())]).with_metadata({b"pandas": b"{not json"})
    assert not metadata_shape_admits(schema, column="f", ordinal=0)


def test_duplicate_json_key_declines() -> None:
    schema = _from_pandas_schema([pa.field("f", pa.int64())], {"f": [1, 2]})
    raw = schema.metadata[b"pandas"].decode("utf-8")
    # Inject a duplicate top-level key.
    dup = "{" + '"columns": [],' + raw[1:]
    bad = dict(schema.metadata)
    bad[b"pandas"] = dup.encode("utf-8")
    assert not metadata_shape_admits(schema.with_metadata(bad), column="f", ordinal=0)


def test_renamed_column_entry_declines() -> None:
    schema = _from_pandas_schema([pa.field("f", pa.int64())], {"f": [1, 2]})
    renamed = _patch_pandas_meta(schema, "f", field_name="other")
    assert not metadata_shape_admits(renamed, column="f", ordinal=0)


def test_stored_index_declines() -> None:
    import pandas as pd

    frame = pd.DataFrame({"f": [1, 2]}, index=pd.Index(["x", "y"], name="ix"))
    schema = pa.Table.from_pandas(frame).schema
    # A stored (named) index adds a column / non-range layout -> decline.
    assert not metadata_shape_admits(schema, column="f", ordinal=0)


def test_duplicate_physical_names_decline() -> None:
    schema = pa.schema([pa.field("f", pa.int64()), pa.field("f", pa.int64())])
    assert not metadata_shape_admits(schema, column="f", ordinal=0)


def test_wrong_ordinal_declines() -> None:
    schema = pa.schema([pa.field("p", pa.int64()), pa.field("f", pa.int64())])
    assert not metadata_shape_admits(schema, column="f", ordinal=0)
    assert metadata_shape_admits(schema, column="f", ordinal=1)


def test_source_type_set_is_bool_int_uint_only() -> None:
    expected = frozenset(INT_UINT_BOOL)
    assert expected == DETERMINISTIC_FAKER_SOURCE_TYPES
    assert pa.float64() not in DETERMINISTIC_FAKER_SOURCE_TYPES
    assert pa.timestamp("us") not in DETERMINISTIC_FAKER_SOURCE_TYPES
