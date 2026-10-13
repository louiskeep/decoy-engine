"""Phase D / FK-key-native: shared builders and assertions for an FK key under each key strategy.

`to_fpe` turns any hash-keyed `_d2b_shapes` / `_dcomp_shapes` shape into the same shape keyed by
fpe, so every shape and orphan policy already proven for a hash key is proven for an fpe key from
one definition and not a copy. Key values are mapped one-to-one onto ten-digit strings (the FF1
domain floor needs radix**length >= 1,000,000), consistently across every table so the FK join
still matches; a null stays null.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pyarrow as pa

from decoy_engine.execution._sequential_native import REASON_KEY_NOT_INJECTIVE
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from tests.parity._d1a_hash_data import JOB_SEED
from tests.parity._d2b_shapes import Shape
from tests.parity._d2c_fpe_data import MARK


def _is_hash(seed: ColumnSeed) -> bool:
    return seed.strategy == "hash"


def to_fpe(shape: Shape) -> Shape:
    """`shape` with every hash column an fpe column and its string values mapped to digit keys."""
    tables = dict(shape.plan.seed_envelope.per_table)
    # A composite child lists its member columns on a group seed, not in `per_column`.
    keyed = {
        t: {
            c
            for c in {c for c, cs in ts.per_column if _is_hash(cs)}
            | {c for _, g in ts.per_group for c in g.coherent_columns}
            if pa.types.is_string(shape.sources[t].schema.field(c).type)
        }
        for t, ts in tables.items()
    }
    mapping: dict[str, str] = {}
    for table, columns in keyed.items():
        for column in sorted(columns):
            for v in shape.sources[table].column(column).to_pylist():
                if v is not None and v not in mapping:
                    mapping[v] = f"{7_000_000_000 + len(mapping):010d}"
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=JOB_SEED,
            per_table=tuple(
                (
                    t,
                    TableSeed(
                        per_column=tuple(
                            (c, replace(cs, strategy="fpe") if _is_hash(cs) else cs)
                            for c, cs in ts.per_column
                        ),
                        per_group=ts.per_group,
                    ),
                )
                for t, ts in tables.items()
            ),
        )
    )
    sources: dict[str, pa.Table] = {}
    for table, src in shape.sources.items():
        cols = dict(zip(src.column_names, src.columns, strict=True))
        for column in keyed[table]:
            values = [None if v is None else mapping[v] for v in src.column(column).to_pylist()]
            cols[column] = pa.array(values, pa.string())
        sources[table] = pa.table(cols)
    return Shape(plan, shape.graph, sources)


def key_columns(shape: Shape, strategy: str) -> set[tuple[str, str]]:
    """Every (table, column) of `strategy` that is an FK parent or child key in `shape`."""
    keys: set[tuple[str, str]] = set()
    for e in shape.graph.edges:
        keys |= {(e.parent_table, c) for c in e.parent_columns}
        keys |= {(e.child_table, c) for c in e.child_columns}
    tables = dict(shape.plan.seed_envelope.per_table)
    return {
        (t, c)
        for t, c in keys
        if t in tables
        and dict(tables[t].per_column).get(c, None) is not None
        and dict(tables[t].per_column)[c].strategy == strategy
    }


def assert_keys_declined(result: Any, shape: Shape, strategy: str, disposition_key: str) -> None:
    """Every `strategy` FK key column is reported as a value-free, reporting-only decline."""
    entries = result.quality_metrics[disposition_key]
    expected = key_columns(shape, strategy)
    assert expected, "the shape has no key column of this strategy"
    assert {(e["table"], e["column"]) for e in entries} == expected
    assert len(entries) == len(expected)
    tables = shape.sources
    for e in entries:
        assert e["path"] == "fk_key_declined"
        assert e["executed_backend"] == "python_scalar"
        arrow = tables[e["table"]].schema.field(e["column"]).type
        assert e["reason"] == f"{REASON_KEY_NOT_INJECTIVE}:{strategy}:{e['column']}:{arrow}"
        assert set(e) == {"table", "column", "path", "executed_backend", "reason"}


def fpe_marked_expected(oracle: pa.Table, source: pa.Table, columns: set[str]) -> dict[str, Any]:
    """The oracle's table with the stand-in kernel's mark on every key cell the key strategy
    derived. A cell equal to its source value was preserved (a PRESERVE/WARN orphan) and is not
    marked; no ten-digit FF1 ciphertext equals its own plaintext on this fixture."""
    out = oracle.to_pydict()
    raw = source.to_pydict()
    for column in columns:
        out[column] = [
            v if v is None or v == "" or v == s else MARK + v
            for v, s in zip(out[column], raw[column], strict=True)
        ]
    return out
