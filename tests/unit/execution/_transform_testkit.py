"""Shared fixtures and the test-local reference implementation for the A8
engine-owned-transforms acceptance tests.

The reference (`reference_transform`) is deliberately independent of the code
under test: it never imports `decoy_engine.execution._transforms_table` or calls
`to_pandas_fk_safe`. It builds the transform frame with pandas nullable dtypes
from `to_pylist()`, applies the ops with plain pandas, and rebuilds Arrow from
the source by `take`. Only the public op semantics (what filter, sort, limit,
dedupe, derive and drop_column mean) are shared with production.
"""

from __future__ import annotations

import copy
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from decoy_engine.config import PipelineConfig

ENGINE_VERSION = "a8-engine-transforms-test"

_NULLABLE = {
    "int8": "Int8",
    "int16": "Int16",
    "int32": "Int32",
    "int64": "Int64",
    "uint8": "UInt8",
    "uint16": "UInt16",
    "uint32": "UInt32",
    "uint64": "UInt64",
}


def validated(cfg: dict[str, Any]) -> dict[str, Any]:
    return PipelineConfig.model_validate(cfg).model_dump()


def write_parquet(tmp_path: Path, table: pa.Table, name: str) -> str:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return str(path)


def hash_col(name: str, namespace: str) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": namespace}


def single_table_config(
    tmp_path: Path,
    table: pa.Table,
    *,
    transforms: Sequence[dict[str, Any]] = (),
    name: str = "t",
    columns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One mask table read from a Parquet descriptor (so `profile_source` sees
    the real schema). The default column spec redacts `s` and passes the rest
    through."""
    src = write_parquet(tmp_path, table, f"{name}_in")
    return validated(
        {
            "version": 1,
            "global_settings": {"seed": 11},
            "sources": {name: {"type": "file", "format": "parquet", "path": src}},
            "tables": [
                {
                    "name": name,
                    "columns": columns or [{"name": "s", "strategy": "redact"}],
                    "transforms": list(transforms),
                }
            ],
            "targets": {
                name: {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / f"{name}_out.parquet"),
                }
            },
        }
    )


def fk_config(
    tmp_path: Path,
    parent: pa.Table,
    child: pa.Table,
    *,
    parent_transforms: Sequence[dict[str, Any]] = (),
    child_transforms: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """A pure-mask FK job whose strategies are all out-of-core-compatible
    (hash keys plus a redact payload)."""
    parent_src = write_parquet(tmp_path, parent, "parent_in")
    child_src = write_parquet(tmp_path, child, "child_in")
    return validated(
        {
            "version": 1,
            "global_settings": {"seed": 7},
            "sources": {
                "parent": {"type": "file", "format": "parquet", "path": parent_src},
                "child": {"type": "file", "format": "parquet", "path": child_src},
            },
            "tables": [
                {
                    "name": "parent",
                    "columns": [hash_col("id", "ns"), {"name": "note", "strategy": "redact"}],
                    "transforms": list(parent_transforms),
                },
                {
                    "name": "child",
                    "columns": [hash_col("cid", "cns"), hash_col("parent_id", "ns")],
                    "transforms": list(child_transforms),
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["parent_id"]}],
                    "orphan_policy": "preserve",
                    "namespace": "ns",
                }
            ],
            "targets": {
                "parent": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "parent_out.parquet"),
                },
                "child": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "child_out.parquet"),
                },
            },
        }
    )


def fk_tables(n: int = 12) -> tuple[pa.Table, pa.Table]:
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(n)]),
            "note": pa.array([f"secret{i}" for i in range(n)]),
            "n": pa.array(list(range(n)), pa.int64()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(n * 2)]),
            "parent_id": pa.array([f"p{i % n}" for i in range(n * 2)]),
            "qty": pa.array([None if i % 5 == 0 else i for i in range(n * 2)], pa.int32()),
        }
    )
    return parent, child


def cleared(config: dict[str, Any]) -> dict[str, Any]:
    cfg = copy.deepcopy(config)
    for t in cfg["tables"]:
        t["transforms"] = []
    return cfg


def base_table(n: int = 12) -> pa.Table:
    """Mixed-type table: unique int key, raw nullable int, duplicate string key,
    float column with NaN and null, payload string, extra column to drop."""
    return pa.table(
        {
            "id": pa.array(list(range(n)), pa.int64()),
            "qn": pa.array([None if i % 4 == 1 else i * 3 for i in range(n)], pa.int32()),
            "grp": pa.array([f"g{i % 4}" for i in range(n)]),
            "f": pa.array(
                [float("nan") if i % 5 == 2 else (None if i % 5 == 3 else i / 2) for i in range(n)],
                pa.float64(),
            ),
            "s": pa.array([f"secret{i}" for i in range(n)]),
            "extra": pa.array([f"e{i}" for i in range(n)]),
        }
    )


# --------------------------------------------------------------------------
# Reference implementation (independent of the code under test)
# --------------------------------------------------------------------------


def _index_field_names(table: pa.Table) -> set[str]:
    meta = table.schema.pandas_metadata or {}
    return {c for c in meta.get("index_columns", []) if isinstance(c, str)}


def reference_frame(table: pa.Table) -> pd.DataFrame:
    """The transform frame, built without the production helper: the no-transform
    pandas view, except raw null-bearing integer data columns become exact
    nullable dtypes built from Python values."""
    index_names = _index_field_names(table)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = table.to_pandas()
    df = df.reset_index(drop=True)
    for field in table.schema:
        if field.name in index_names:
            continue
        name = str(field.type)
        if name in _NULLABLE and table.column(field.name).null_count > 0:
            df[field.name] = pd.array(table.column(field.name).to_pylist(), dtype=_NULLABLE[name])
    return df


def _eval(df: pd.DataFrame, expression: str) -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return df.eval(expression)


def reference_apply(df: pd.DataFrame, op: dict[str, Any]) -> pd.DataFrame:
    kind = op["op"]
    if kind == "filter":
        return df[_eval(df, op["expression"])]
    if kind == "sort":
        return df.sort_values(by=op["by"], ascending=op.get("ascending", True), kind="stable")
    if kind == "limit":
        return df.head(op["n"])
    if kind == "dedupe":
        return df.drop_duplicates(subset=op.get("columns"))
    if kind == "derive":
        return df.assign(**{op["column"]: _eval(df, op["expression"])})
    if kind == "drop_column":
        return df.drop(columns=op["columns"])
    raise AssertionError(kind)


def reference_bindings(table: pa.Table, ops: Sequence[dict[str, Any]]) -> list[tuple[str, str]]:
    index_names = _index_field_names(table)
    bindings = [(f.name, "source") for f in table.schema if f.name not in index_names]
    for op in ops:
        if op["op"] == "drop_column":
            bindings = [b for b in bindings if b[0] not in op["columns"]]
        elif op["op"] == "derive":
            bindings.append((op["column"], "derived"))
    return bindings


def reference_transform(table: pa.Table, ops: Sequence[dict[str, Any]]) -> pa.Table:
    """R: the table a reference implementation produces from T and the ops."""
    df = reference_frame(table)
    for op in ops:
        df = reference_apply(df, op)
    ordinals = pa.array(df.index.to_numpy(), pa.int64())
    bindings = reference_bindings(table, ops)
    assert [b[0] for b in bindings] == list(df.columns)
    arrays: list[Any] = []
    fields: list[pa.Field] = []
    for name, origin in bindings:
        if origin == "source":
            arrays.append(pc.take(table.column(name), ordinals))
            fields.append(table.schema.field(name))
        else:
            arr = pa.array(df[name], from_pandas=True)
            arrays.append(arr)
            fields.append(pa.field(name, arr.type))
    out = pa.Table.from_arrays(arrays, schema=pa.schema(fields))
    fresh = pa.Table.from_pandas(df.reset_index(drop=True), preserve_index=False)
    meta = {k: v for k, v in (table.schema.metadata or {}).items() if k != b"pandas"}
    meta[b"pandas"] = fresh.schema.metadata[b"pandas"]
    return out.replace_schema_metadata(meta)


def tables_equal(actual: pa.Table, expected: pa.Table) -> None:
    """Full Arrow schema (names, types, nullability) and values, exact."""
    assert actual.schema.equals(expected.schema, check_metadata=False), (
        f"schema mismatch:\n{actual.schema}\n!=\n{expected.schema}"
    )
    assert actual.num_rows == expected.num_rows
    for name in expected.column_names:
        a = actual.column(name).combine_chunks()
        e = expected.column(name).combine_chunks()
        assert a.equals(e), f"column {name!r} differs: {a.to_pylist()} != {e.to_pylist()}"
