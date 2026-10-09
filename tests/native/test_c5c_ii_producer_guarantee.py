"""C5c-ii: the producer stream guarantee and the ordinary-iterable fallback.

A `_FixedSchemaChunks` producer guarantees every emitted chunk's schema equals its captured
`source_schema` metadata-inclusive; only the two internal factories build one, and an arbitrary
iterable (even one exposing a `.source_schema` attribute) is not recognized. A table that needs
C5c-ii admission from an ordinary iterable declines UP-FRONT to the oracle, even when its first
chunk's schema is allowlisted and even when later chunks drift their pandas metadata.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._chunked_input import (
    _FixedSchemaChunks,
    fixed_schema_chunks_from_resident,
)
from decoy_engine.execution.native import _chunked_entry
from tests.native._b8_support import FORCE, Run, identical, run_one, with_force
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    column_values,
    faker_col,
    force_oracle,
    key_provider,
    make_config,
    passthrough,
)

GS = {"unconfigured_column_policy": "warn"}


def _meta_table(values: list[Any], typ: pa.DataType, *, field_meta: bool = False) -> pa.Table:
    """A resident table carrying real `b"pandas"` schema metadata, optionally with field metadata."""
    arrays = [pa.array(values, type=typ), pa.array(list(range(len(values))), pa.int64())]
    table = pa.Table.from_arrays(arrays, names=["f", "p"])
    table = pa.Table.from_pandas(table.to_pandas(), preserve_index=False)
    if field_meta:
        schema = table.schema.set(0, table.schema.field(0).with_metadata({b"note": b"x"}))
        table = table.cast(schema)
    return table


# ---------------------------------------------------------------------------
# 4. The producer guarantee covers the complete schema.
# ---------------------------------------------------------------------------


def test_resident_producer_guarantees_the_complete_schema() -> None:
    source = _meta_table([1, 2, 3, 4, 5], pa.int64(), field_meta=True)
    producer = fixed_schema_chunks_from_resident(source, 2)
    assert producer.source_schema.equals(source.schema, check_metadata=True)
    chunks = list(producer)
    assert sum(c.num_rows for c in chunks) == 5
    for chunk in chunks:
        assert chunk.schema.equals(source.schema, check_metadata=True)


def test_resident_producer_is_reiterable() -> None:
    source = _meta_table([1, 2, 3], pa.int64())
    producer = fixed_schema_chunks_from_resident(source, 2)
    first = [c.num_rows for c in producer]
    second = [c.num_rows for c in producer]
    assert first == second == [2, 1]


def test_an_ordinary_iterable_is_not_a_producer() -> None:
    class Faux:
        source_schema = pa.schema([pa.field("f", pa.int64())])

        def __iter__(self) -> Any:
            return iter(())

    # Recognition is by the concrete internal type, never a duck-typed attribute or a list.
    assert not isinstance(Faux(), _FixedSchemaChunks)
    assert not isinstance([pa.table({"f": [1]})], _FixedSchemaChunks)
    assert isinstance(
        fixed_schema_chunks_from_resident(pa.table({"f": [1]}), 1), _FixedSchemaChunks
    )


# ---------------------------------------------------------------------------
# 6. Per-chunk pandas-metadata drift declines before output (ordinary iterable).
# ---------------------------------------------------------------------------


def _bool_reconstructing(values: list[int]) -> pa.Table:
    """A physical int64 table whose schema-level pandas metadata declares the column bool[pyarrow],
    so `to_pandas()` reconstructs booleans even though the physical buffer is int64."""
    base = _meta_table(values, pa.int64())
    meta = json.loads(base.schema.metadata[b"pandas"])
    for entry in meta["columns"]:
        if entry["name"] == "f":
            entry["numpy_type"] = "bool[pyarrow]"
            entry["pandas_type"] = "bool"
    return base.replace_schema_metadata({b"pandas": json.dumps(meta).encode("utf-8")})


@pytest.fixture
def _poison_native_masker() -> Any:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the native masker ran on a table that must stay on the oracle")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_chunked_entry, "_mask_chunk_native", boom)
        yield


@NEEDS_COMPANION
def test_6_two_chunk_metadata_drift_declines_to_the_oracle(_poison_native_masker: Any) -> None:
    # chunk 1: allowlisted int metadata; chunk 2: physical int64 but bool-reconstructing metadata.
    chunk1 = _meta_table([1, 2], pa.int64())
    chunk2 = _bool_reconstructing([3, 3])
    cols = [faker_col("f"), passthrough("p")]
    config = make_config(cols, global_settings=GS)
    ev: list[Any] = []
    sink: list[Any] = []
    out = list(
        _chunked_entry.run_mask_chunked(
            config,
            [chunk1, chunk2],
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            route_evidence_sink=ev,
            chunk_result_sink=sink,
        )
    )
    native = Run(out, sink, ev, frozenset())
    # Declines up-front (ordinary iterable has no producer guarantee); the native masker never ran.
    assert native.ev[0].native_admitted is False
    assert "faker_conversion_schema_not_guaranteed" in (native.ev[0].reroute_reason or "")
    # Both chunks are processed on the oracle with their OWN per-chunk conversion; no new drift
    # error. Compare against a forced-oracle leg over the exact same chunks.
    forced = run_one(
        make_config([*cols, force_oracle(FORCE)], global_settings=GS),
        [with_force(chunk1), with_force(chunk2)],
    )
    assert len(native.out) == len(forced.out) == 2
    for got, want in zip(native.out, forced.out, strict=True):
        assert identical(got, want.drop_columns([FORCE]))
    # chunk 1 (ints) and chunk 2 (bools) key differently, so the two chunks' draws differ.
    assert column_values(native.out[:1], "f") != [None, None]


@NEEDS_COMPANION
def test_6_all_null_leading_chunk_then_drift_declines(_poison_native_masker: Any) -> None:
    chunk1 = _meta_table([1, 2], pa.int64())
    # An all-null leading chunk followed by bool-reconstructing metadata, still an ordinary list.
    lead = pa.table({"f": pa.nulls(2, pa.int64()), "p": pa.array([0, 1], pa.int64())})
    chunk2 = _bool_reconstructing([3, 4])
    cols = [faker_col("f"), passthrough("p")]
    config = make_config(cols, global_settings=GS)
    ev: list[Any] = []
    out = list(
        _chunked_entry.run_mask_chunked(
            config,
            [lead, chunk1, chunk2],
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            route_evidence_sink=ev,
        )
    )
    assert ev[0].native_admitted is False
    forced = run_one(
        make_config([*cols, force_oracle(FORCE)], global_settings=GS),
        [with_force(lead), with_force(chunk1), with_force(chunk2)],
    )
    for got, want in zip(out, forced.out, strict=True):
        assert identical(got, want.drop_columns([FORCE]))
