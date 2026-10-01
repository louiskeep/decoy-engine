"""Arrow-table front end for per-table transforms: `apply_table_transforms`.

`_transforms` holds the pandas ops. This module wraps them so a table's source
columns keep their exact Arrow values and types:

1. Convert the table the way the no-transform masking path does (pandas-metadata
   aware), except every null-bearing integer data column is read through the
   exact nullable dtypes (`to_pandas_fk_safe`), so no integer is rounded through
   float64. A stored pandas index stays out of the columns and is replaced by a
   fresh `RangeIndex`: the labels are the source row ordinals.
2. Run the ops with the ordinals carried as the index. pandas decides which rows
   survive, in what order, and computes derived columns. It never supplies the
   values of a source column.
3. Rebuild the output: a source column is `take(<original column>, <ordinals>)`
   under its original field (type, nullability, field metadata); a derived column
   is converted from pandas with an inferred type.
4. Keep the source's non-pandas schema metadata and attach fresh `pandas`
   metadata describing the final columns with the dtypes they had in the
   transform frame, so the adapter's metadata-aware conversion rebuilds them.

Filter predicates, sort keys and dedupe keys are evaluated on the pandas view, so
pandas semantics apply to those decisions (NaN and null both count as missing in
a predicate; dedupe treats them as equal). The emitted values are exact.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.config import TableConfig
from decoy_engine.config._transforms import DeriveOp, DropColumnOp, TransformOp
from decoy_engine.execution import _transforms
from decoy_engine.execution._fk_keys import to_pandas_fk_safe
from decoy_engine.execution._transforms_gate import find_table_config
from decoy_engine.profile._readers import LazySource

__all__ = [
    "apply_resident_table",
    "apply_table_transforms",
    "to_transform_frame",
    "transform_resolved_source",
]

_SOURCE = "source"
_DERIVED = "derived"


def to_transform_frame(table: pa.Table) -> pd.DataFrame:
    """The pandas frame the ops run on, indexed by source row ordinal."""
    stored_index = _transforms.stored_index_fields(table.schema)
    null_bearing_ints = [
        field.name
        for field in table.schema
        if field.name not in stored_index
        and pa.types.is_integer(field.type)
        and table.column(field.name).null_count > 0
    ]
    frame = to_pandas_fk_safe(table, null_bearing_ints)
    frame.index = pd.RangeIndex(table.num_rows)
    return frame


def _final_bindings(
    frame_columns: Sequence[Any], ops: Sequence[TransformOp]
) -> list[tuple[str, str]]:
    """`(name, origin)` per output column after the ordered ops.

    A dropped-then-derived name is a new `derived` binding, never the old source
    column.
    """
    bindings = [(str(c), _SOURCE) for c in frame_columns]
    for op in ops:
        if isinstance(op, DropColumnOp):
            dropped = set(op.columns)
            bindings = [b for b in bindings if b[0] not in dropped]
        elif isinstance(op, DeriveOp):
            bindings.append((op.column, _DERIVED))
    return bindings


def _apply_ops(table: pa.Table, ops: Sequence[TransformOp]) -> pa.Table:
    frame = to_transform_frame(table)
    start_columns = list(frame.columns)
    frame = _transforms.apply_transforms(frame, list(ops))
    bindings = _final_bindings(start_columns, ops)
    if [name for name, _ in bindings] != [str(c) for c in frame.columns]:
        raise _transforms.TransformError(
            code="transform_lineage_mismatch",
            message="transform output columns did not match the tracked bindings",
        )
    ordinals = pa.array(frame.index.to_numpy(dtype="int64"), type=pa.int64())
    arrays: list[pa.Array | pa.ChunkedArray] = []
    fields: list[pa.Field] = []
    for name, origin in bindings:
        if origin == _SOURCE:
            position = table.schema.get_field_index(name)
            arrays.append(pc.take(table.column(position), ordinals))
            fields.append(table.schema.field(position))
        else:
            try:
                derived = pa.array(frame[name], from_pandas=True)
            except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError) as exc:
                raise _transforms.TransformError(
                    code="derive_result_not_convertible",
                    message=f"derived column {name!r} could not be converted to Arrow",
                ) from exc
            arrays.append(derived)
            fields.append(pa.field(name, derived.type))
    out = pa.Table.from_arrays(arrays, schema=pa.schema(fields))
    # Schema-only: the pandas metadata `from_pandas` would write, without
    # materializing a second Arrow copy of the data.
    fresh = pa.Schema.from_pandas(frame, preserve_index=False)
    meta = {k: v for k, v in (table.schema.metadata or {}).items() if k != b"pandas"}
    meta[b"pandas"] = (fresh.metadata or {})[b"pandas"]
    return out.replace_schema_metadata(meta)


def _transform_ops(config: Mapping[str, Any], table_name: str) -> list[TransformOp]:
    entry = find_table_config(config, table_name)
    if entry is None or entry.get("generate_columns") or not entry.get("transforms"):
        return []
    return list(TableConfig.model_validate(dict(entry)).transforms)


def apply_resident_table(config: Mapping[str, Any], table_name: str, table: pa.Table) -> pa.Table:
    """Apply the configured transforms to a resident table whose schema the caller
    has already guarded (the preparation step checks it once)."""
    return _apply_ops(table, _transform_ops(config, table_name))


def apply_table_transforms(config: Mapping[str, Any], table_name: str, table: pa.Table) -> pa.Table:
    """Apply `table_name`'s configured transforms to `table` and return the result.

    `config` is the validated pipeline config dump. The table is returned as the
    same object when it declares no transforms or is generate-kind. Otherwise the
    ops are validated through `TableConfig`, a schema-only guard rejects duplicate
    physical field names (`duplicate_source_field_names`) and structured config
    references to a stored pandas index field (`config_references_stored_index`),
    and the ops run with row-lineage reconstruction, so surviving source columns
    keep their exact Arrow type, nullability, field metadata and values, and
    every null-bearing integer column stays exact.

    Filter, sort and dedupe decisions use pandas semantics (NaN and null both
    count as missing in a predicate; dedupe treats them as equal). Raises
    `TransformError` for an invalid op or expression.
    """
    ops = _transform_ops(config, table_name)
    if not ops:
        return table
    _transforms.check_transform_source_schema(config, table_name, table.schema)
    return _apply_ops(table, ops)


def transform_resolved_source(
    config: Mapping[str, Any], table_name: str, source: pa.Table | LazySource
) -> pa.Table:
    """Route-side entry: guard first on the schema (a `LazySource` exposes its
    footer schema without reading values), then materialize and apply."""
    ops = _transform_ops(config, table_name)
    if not ops:
        return source.to_table() if isinstance(source, LazySource) else source
    _transforms.check_transform_source_schema(config, table_name, source.schema)
    table = source.to_table() if isinstance(source, LazySource) else source
    return _apply_ops(table, ops)
