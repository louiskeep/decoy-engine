"""C5c-ii: schema-aware admission of DETERMINISTIC Faker over bool/int/uint sources.

The native routes key a deterministic Faker column from the SOURCE value, canonicalized by
`generation.pool._canonicalize._canonicalize_source` and run through the compiled (or
reference) `derive_index` kernel. The oracle keys the SAME kernel from the pandas-materialized
value (`PoolSampler._deterministic` -> `_derive_pool_indices`, which feeds the kernel a
`pa.Array.from_pandas(series)`). So the two routes agree on a column's draw key iff the
pandas conversion reconstructs the SAME physical value the resident Arrow array holds.

pandas reconstructs dtypes and column identity from the source's schema-level ``b"pandas"``
metadata and each field's metadata: an ``int64`` array tagged ``bool[pyarrow]`` reconstructs
to booleans, an ``Int64``/``Float64`` extension tag to a different logical dtype. A `pa.Field`
alone cannot prove the conversion is identity-preserving. This module is the CLOSED
metadata-shape allowlist that admits only the two shapes where the conversion is provably an
identity on the target physical value (Codex-authored rev-4 plan, docs/plans/
2026-10-09-c5c-ii-deterministic-nonstring-faker.md, section 10). Anything else declines to the
oracle, which keeps its existing behavior for that input.

This is an ADMISSION predicate, not a new public input validator: a parse or shape failure is
a decline, never a raised error.
"""

from __future__ import annotations

import json
from typing import Any, Final

import pyarrow as pa

__all__ = [
    "DETERMINISTIC_FAKER_SOURCE_TYPES",
    "SCHEMA_NOT_GUARANTEED",
    "metadata_shape_admits",
]

# The NEW deterministic-Faker source families this slice opens: bool, signed and unsigned
# integers. NOT float (``_canonicalize_source`` hard-errors on float) and NOT temporal
# (Arrow -2**63 -> pandas NaT sentinel hazard, deferred). Explicit instances, not a
# predicate: admission compares exact datatypes, per C5c-i precedent.
DETERMINISTIC_FAKER_SOURCE_TYPES: Final = frozenset(
    {
        pa.int8(),
        pa.int16(),
        pa.int32(),
        pa.int64(),
        pa.uint8(),
        pa.uint16(),
        pa.uint32(),
        pa.uint64(),
        pa.bool_(),
    }
)

# Coded decline reason: the chunked stream carried no producer guarantee, so the schema the
# oracle will actually convert per chunk cannot be proven equal to the admission schema.
SCHEMA_NOT_GUARANTEED = "faker_conversion_schema_not_guaranteed"
# Coded decline reason: the guaranteed schema's metadata shape is outside the closed allowlist.
METADATA_NOT_ALLOWLISTED = "faker_conversion_metadata_not_allowlisted"

# The explicit physical-Arrow-type -> (pandas_type, numpy_type) table (section 10). A physical
# field whose type is absent here makes the whole pandas-metadata shape ineligible: an unlisted
# companion type (timestamp, decimal, ...) is not proven convertible, so the shape declines.
# The float and string rows permit ORDINARY COMPANION columns; they do not expand the Faker
# target domain (the caller gates the target type separately via DETERMINISTIC_FAKER_SOURCE_TYPES).
_PANDAS_TYPE_TABLE: Final[dict[pa.DataType, tuple[str, str]]] = {
    pa.bool_(): ("bool", "bool"),
    pa.int8(): ("int8", "int8"),
    pa.int16(): ("int16", "int16"),
    pa.int32(): ("int32", "int32"),
    pa.int64(): ("int64", "int64"),
    pa.uint8(): ("uint8", "uint8"),
    pa.uint16(): ("uint16", "uint16"),
    pa.uint32(): ("uint32", "uint32"),
    pa.uint64(): ("uint64", "uint64"),
    pa.float32(): ("float32", "float32"),
    pa.float64(): ("float64", "float64"),
    pa.string(): ("unicode", "object"),
    pa.large_string(): ("unicode", "object"),
}

_REQUIRED_PANDAS_KEYS: Final = frozenset(
    {"index_columns", "column_indexes", "columns", "creator", "pandas_version"}
)
_REQUIRED_COLUMN_KEYS: Final = frozenset(
    {"name", "field_name", "pandas_type", "numpy_type", "metadata"}
)
_COLUMN_INDEX_SHAPE: Final = {
    "name": None,
    "field_name": None,
    "pandas_type": "unicode",
    "numpy_type": "object",
    "metadata": {"encoding": "UTF-8"},
}


def _strict_json_object(raw: bytes) -> dict[str, Any] | None:
    """Parse ``raw`` as a UTF-8 JSON OBJECT with the standard parser, rejecting duplicate
    object keys, malformed JSON, non-UTF-8 bytes and nonstandard numeric constants
    (NaN/Infinity). Returns the object, or ``None`` on any failure or a non-object top level."""

    def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                raise ValueError("duplicate object key")
            seen[key] = value
        return seen

    def _reject_constant(_token: str) -> Any:
        raise ValueError("nonstandard JSON constant")

    try:
        text = raw.decode("utf-8")
        decoded = json.loads(
            text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _field_has_metadata(field: pa.Field) -> bool:
    meta = field.metadata
    return meta is not None and len(meta) > 0


def _is_extension(field: pa.Field) -> bool:
    return isinstance(field.type, pa.ExtensionType)


def _physical_type_entry_ok(field: pa.Field, entry: dict[str, Any]) -> bool:
    """One ``columns`` entry matches its physical field: exact key set, name/field_name equal
    to the physical name, ``metadata == null``, and BOTH ``pandas_type`` AND ``numpy_type``
    equal the physical-type table's pair. Checking ``pandas_type`` alone is insufficient: a
    physical int64 tagged ``numpy_type: bool[pyarrow]`` must decline (section 10)."""
    if set(entry) != _REQUIRED_COLUMN_KEYS:
        return False
    name, field_name = entry["name"], entry["field_name"]
    if not isinstance(name, str) or not isinstance(field_name, str):
        return False
    if name != field.name or field_name != field.name:
        return False
    if entry["metadata"] is not None:
        return False
    pair = _PANDAS_TYPE_TABLE.get(field.type)
    if pair is None:
        return False
    return (entry["pandas_type"], entry["numpy_type"]) == pair


def _index_layout_ok(obj: dict[str, Any], *, num_fields: int) -> bool:
    """The decoded pandas object's index layout is one of the two accepted shapes: an empty
    index, or a single default RangeIndex with the UTF-8 column-index entry (section 10).
    ``attributes`` may be absent or exactly ``{}``; no other keys qualify."""
    keys = set(obj)
    if not (_REQUIRED_PANDAS_KEYS <= keys <= _REQUIRED_PANDAS_KEYS | {"attributes"}):
        return False
    if "attributes" in obj and obj["attributes"] != {}:
        return False
    creator = obj["creator"]
    if not (
        isinstance(creator, dict)
        and set(creator) == {"library", "version"}
        and creator["library"] == "pyarrow"
        and isinstance(creator["version"], str)
        and creator["version"]
    ):
        return False
    if not (isinstance(obj["pandas_version"], str) and obj["pandas_version"]):
        return False
    index_columns, column_indexes = obj["index_columns"], obj["column_indexes"]
    if index_columns == [] and column_indexes == []:
        return True
    if not (isinstance(index_columns, list) and len(index_columns) == 1):
        return False
    entry = index_columns[0]
    if not isinstance(entry, dict) or set(entry) != {"kind", "name", "start", "stop", "step"}:
        return False
    stop = entry["stop"]
    if not (
        entry["kind"] == "range"
        and entry["name"] is None
        and entry["start"] == 0
        and entry["step"] == 1
        # bool is an int subclass in Python/JSON; a boolean ``stop`` is not a valid length.
        and isinstance(stop, int)
        and not isinstance(stop, bool)
        and stop >= 0
    ):
        return False
    return column_indexes == [_COLUMN_INDEX_SHAPE]


def metadata_shape_admits(schema: pa.Schema, *, column: str, ordinal: int) -> bool:
    """True iff ``schema`` matches the closed metadata-shape allowlist for the Faker target
    ``column`` at position ``ordinal`` (section 10). Two shapes admit; everything else declines.

    Shape 1 -- metadata absent: schema metadata empty, every field's metadata empty, no field
    an Arrow extension type. The target is a plain bool/int/uint field.

    Shape 2 -- plain pandas metadata with an identity column mapping: schema metadata is exactly
    ``{b"pandas": <json>}``, all field metadata empty, no field an extension type, and the
    decoded pandas object has the required key set, a pyarrow ``creator``, one ``columns`` entry
    per physical field (in order, name == field_name == physical name, ``metadata: null``) whose
    ``pandas_type`` AND ``numpy_type`` match the physical-type table, and an accepted index
    layout.

    An unrecognized shape is NOT repaired, stripped, or interpreted optimistically.
    """
    names = schema.names
    # Unique names and an exact name/ordinal match for the target.
    if len(names) != len(set(names)):
        return False
    if not (0 <= ordinal < len(names)) or names[ordinal] != column:
        return False
    if any(_is_extension(f) for f in schema):
        return False
    if any(_field_has_metadata(f) for f in schema):
        return False

    schema_meta = schema.metadata
    if schema_meta is None or len(schema_meta) == 0:
        # Shape 1: the target must be a plain admitted physical type.
        return schema.field(ordinal).type in DETERMINISTIC_FAKER_SOURCE_TYPES

    # Shape 2: exactly the b"pandas" key, nothing else.
    if set(schema_meta) != {b"pandas"}:
        return False
    obj = _strict_json_object(schema_meta[b"pandas"])
    if obj is None:
        return False
    if not _index_layout_ok(obj, num_fields=len(names)):
        return False
    columns = obj["columns"]
    if not isinstance(columns, list) or len(columns) != len(names):
        return False
    for field, entry in zip(schema, columns, strict=True):
        if not isinstance(entry, dict) or not _physical_type_entry_ok(field, entry):
            return False
    # The target itself must be an admitted physical family (the table also lists float/string
    # for companions, so the per-entry check above does not by itself bound the target).
    return schema.field(ordinal).type in DETERMINISTIC_FAKER_SOURCE_TYPES
