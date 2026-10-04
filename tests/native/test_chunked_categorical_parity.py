"""C1 acceptance: deterministic categorical on the chunked route, parity and output type.

Native chunked output must equal the pandas-oracle chunked output (values, Arrow field
type, metadata) across chunk shapes, chunk sizes and thread counts, and the output type
of a native-admissible categorical column is pinned to `string` on both chunked legs.
Cases the native route does not admit stay on the oracle with their existing types.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.execution.native._dispatch import NativeRouteEvidence
from tests.native._b8_support import identical, run_one
from tests.native._chunked_categorical_support import (
    FORCE,
    assert_same_as_oracle,
    cat_col,
    make_config,
    passthrough,
    run_pair,
    source,
    with_force,
)
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    column_values,
    force_oracle,
    key_provider,
    split,
)
from tests.native.test_chunked_entry_values_schema import _full_frame

_REPEATING = [None if i % 6 == 2 else f"v{i % 5:02d}" for i in range(23)]

# Content shapes. Chunk size then decides where the boundaries fall inside each one.
_SHAPES: dict[str, list[str | None]] = {
    "all_null": [None] * 7,
    "single_row": ["only"],
    "ragged": _REPEATING,
    "null_block_then_valued": [None] * 7 + [f"w{i}" for i in range(9)],
    "valued_then_null_block": [f"w{i}" for i in range(9)] + [None] * 7,
}


def _chunks(shape: str, size: int) -> list[pa.Table]:
    return split(source(_SHAPES[shape]), size)


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("size", [1, 7, 50_000])
@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_native_chunked_equals_oracle_chunked(
    shape: str, weighted: bool, size: int, threads: int
) -> None:
    columns = [cat_col(weighted=weighted), passthrough("p")]
    native, forced = run_pair(columns, _chunks(shape, size), native_threads=threads)
    assert_same_as_oracle(native, forced)
    for out in native.out:
        assert out.schema.field("c").type == pa.string()


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
def test_an_empty_chunk_is_identical_on_both_legs(weighted: bool, threads: int) -> None:
    columns = [cat_col(weighted=weighted), passthrough("p")]
    native, forced = run_pair(columns, [source([])], native_threads=threads)
    assert_same_as_oracle(native, forced)
    assert native.out[0].schema.field("c").type == pa.string()
    assert native.out[0].num_rows == 0


@NEEDS_COMPANION
@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
def test_a_null_typed_later_chunk_is_identical_on_both_legs(weighted: bool) -> None:
    valued = source(["a", "b", None, "a"])
    null_typed = pa.table({"c": pa.nulls(3), "p": pa.array([7, 8, 9], pa.int64())})
    native, forced = run_pair([cat_col(weighted=weighted), passthrough("p")], [valued, null_typed])
    assert_same_as_oracle(native, forced)
    assert {o.schema.field("c").type for o in native.out} == {pa.string()}


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
def test_a_multi_chunk_fifty_thousand_row_run_is_identical_on_both_legs(threads: int) -> None:
    values = [None if i % 11 == 3 else f"v{i % 997}" for i in range(100_003)]
    native, forced = run_pair(
        [cat_col(weighted=True), passthrough("p")],
        split(source(values), 50_000),
        native_threads=threads,
    )
    assert len(native.out) == 3
    assert_same_as_oracle(native, forced)


# ---------------------------------------------------------------------------
# 2. Determinism across chunks and against the full-frame route.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
def test_same_source_value_maps_to_the_same_category_in_every_chunk(
    weighted: bool, tmp_path: Path
) -> None:
    values = [None if i % 6 == 2 else f"v{i % 5:02d}" for i in range(40)]
    table = source(values)
    config = make_config([cat_col(weighted=weighted), passthrough("p")])
    run = run_one(config, split(table, 7))
    assert run.ev[0].native_admitted is True
    chunked = run.out
    seen: dict[str, str] = {}
    for src, got in zip(values, column_values(chunked, "c"), strict=True):
        if src is None:
            assert got is None
            continue
        assert seen.setdefault(src, got) == got
    assert len(seen) == 5
    full = _full_frame(config, table, tmp_path)
    assert column_values(chunked, "c") == full.column("c").to_pylist()


# ---------------------------------------------------------------------------
# 3. Output-type pin: conditional, both chunked legs, chunk-count invariant.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_oracle_leg_pins_native_admissible_categorical_to_string(shape: str) -> None:
    columns = [cat_col(), passthrough("p"), force_oracle(FORCE)]
    chunks = [with_force(c) for c in _chunks(shape, 4)]
    out = run_one(make_config(columns), chunks).out
    assert {o.schema.field("c").type for o in out} == {pa.string()}


@pytest.mark.parametrize("size", [1, 4, 50_000])
def test_oracle_leg_type_is_independent_of_chunk_count(size: int) -> None:
    values = [None] * 5 + ["a", "b", None, "c", "a", None, None]
    out = run_one(
        make_config([cat_col(), passthrough("p"), force_oracle(FORCE)]),
        [with_force(c) for c in split(source(values), size)],
    ).out
    assert {o.schema.field("c").type for o in out} == {pa.string()}


@NEEDS_COMPANION
@pytest.mark.parametrize("size", [1, 4, 50_000])
def test_native_leg_type_is_independent_of_chunk_count(size: int) -> None:
    values = [None] * 5 + ["a", "b", None, "c", "a", None, None]
    run = run_one(make_config([cat_col(), passthrough("p")]), split(source(values), size))
    assert run.ev[0].native_admitted is True
    assert {o.schema.field("c").type for o in run.out} == {pa.string()}


def _public_oracle(config: dict[str, Any], chunks: list[pa.Table]) -> list[pa.Table]:
    return list(
        run_mask_pipeline_chunked(
            config,
            list(chunks),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    )


def _entry(
    config: dict[str, Any], chunks: list[pa.Table]
) -> tuple[list[pa.Table], NativeRouteEvidence]:
    evidence: list[NativeRouteEvidence] = []
    out = list(
        run_mask_chunked(
            config,
            list(chunks),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            route_evidence_sink=evidence,
        )
    )
    return out, evidence[0]


def test_numeric_categories_stay_on_the_oracle_and_are_not_string_cast() -> None:
    config = make_config([cat_col(categories=[1, 2, 3]), passthrough("p")])
    chunks = split(source(["a", "b", None, "c", "d", "a", None]), 3)
    got, evidence = _entry(config, chunks)
    want = _public_oracle(config, chunks)
    assert evidence.native_admitted is False
    assert [c.schema.field("c").type for c in got] == [c.schema.field("c").type for c in want]
    assert [c.column("c").to_pylist() for c in got] == [c.column("c").to_pylist() for c in want]
    assert any(c.schema.field("c").type != pa.string() for c in got)


def test_all_string_config_over_an_empty_int_source_keeps_the_oracle_type() -> None:
    """The oracle gives `double` for an empty int64 source, so a wrongful string cast of
    this column would change a type this slice does not own."""
    config = make_config([cat_col(), passthrough("p")])
    chunks = [
        pa.table({"c": pa.array([], pa.int64()), "p": pa.array([], pa.int64())}),
    ]
    got, evidence = _entry(config, chunks)
    want = _public_oracle(config, chunks)
    assert evidence.native_admitted is False
    assert want[0].schema.field("c").type == pa.float64()
    assert got[0].schema.field("c").type == pa.float64()
    assert got[0].schema.field("c").type != pa.string()


def test_all_string_config_over_a_large_string_source_stays_on_the_oracle() -> None:
    config = make_config([cat_col(), passthrough("p")])
    chunks = split(source(["a", None, "b", "c", "a"], typ=pa.large_string()), 2)
    got, evidence = _entry(config, chunks)
    want = _public_oracle(config, chunks)
    assert evidence.native_admitted is False
    assert [c.schema.field("c").type for c in got] == [c.schema.field("c").type for c in want]
    assert [c.column("c").to_pylist() for c in got] == [c.column("c").to_pylist() for c in want]


# ---------------------------------------------------------------------------
# 10. Thread-count invariance.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
def test_output_bytes_do_not_depend_on_the_thread_count(weighted: bool) -> None:
    values = [None if i % 9 == 4 else f"v{i % 211}" for i in range(6_000)]
    config = make_config([cat_col(weighted=weighted), passthrough("p")])
    chunks = split(source(values), 1_700)
    first = run_one(config, chunks, native_threads=1)
    assert first.ev[0].native_admitted is True
    base = first.out
    for threads in (2, 4, 8):
        other = run_one(config, chunks, native_threads=threads).out
        assert len(other) == len(base)
        assert all(identical(a, b) for a, b in zip(base, other, strict=True)), threads
