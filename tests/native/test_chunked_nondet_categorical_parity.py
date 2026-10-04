"""C1b-ii acceptance: seeded non-deterministic categorical on the native CHUNKED route.

Native chunked == oracle chunked == whole-frame, byte for byte, where the draw for the
non-null row at global position `g = base_row_offset + local` is
`derive_index(mask_key, namespace, encode_int(g), pool_size)` (plan section 5, tests 1-3, 7, 8).
The oracle leg is the same table with a still-vetoed `date_shift` column beside it
(`run_pair`), which asserts the exact forced-oracle reason on every call (5.10).

Expected categories in the KAT tests are FROZEN LITERALS captured once from direct scalar
`derive_index(vault_key(), "ns_c", encode_int(g), ...)` calls, never recomputed here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.native._chunked_entry import aggregate_chunked_route_evidence
from tests.native._b8_support import run_one
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

_SHAPES: dict[str, list[str | None]] = {
    "all_null_non_empty": [None] * 7,
    "single_row": ["only"],
    "ragged": _REPEATING,
    "all_null_beside_valued": [None] * 3 + ["a", "b"] + [None] * 2 + ["c"],
    "null_block_then_valued": [None] * 7 + [f"w{i}" for i in range(9)],
    "same_value_everywhere": ["same"] * 20,
}


def _nd(weighted: bool = False, **kw: Any) -> dict[str, Any]:
    return cat_col(mode=None, weighted=weighted, **kw)


# ---------------------------------------------------------------------------
# 1. Parity matrix: native chunked == oracle chunked (== whole-frame).
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("size", [1, 7, 50_000])
@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_native_chunked_equals_oracle_chunked(
    shape: str, weighted: bool, size: int, threads: int
) -> None:
    columns = [_nd(weighted), passthrough("p")]
    native, forced = run_pair(
        columns, split(source(_SHAPES[shape]), size), native_threads=threads
    )
    assert_same_as_oracle(native, forced)
    assert native.ev[0].node_routes[0].route == "native_kernel"
    for out in native.out:
        assert out.schema.field("c").type == pa.string()


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
def test_a_zero_row_table_is_identical_on_both_legs(weighted: bool, threads: int) -> None:
    native, forced = run_pair(
        [_nd(weighted), passthrough("p")], [source([])], native_threads=threads
    )
    assert_same_as_oracle(native, forced)
    assert native.out[0].num_rows == 0
    assert native.out[0].schema.field("c").type == pa.string()


@NEEDS_COMPANION
@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_chunked_native_equals_the_whole_frame_run(
    shape: str, weighted: bool, tmp_path: Path
) -> None:
    table = source(_SHAPES[shape])
    config = make_config([_nd(weighted), passthrough("p")])
    run = run_one(config, split(table, 7))
    assert run.ev[0].native_admitted is True
    full = _full_frame(config, table, tmp_path)
    assert column_values(run.out, "c") == full.column("c").to_pylist()


@NEEDS_COMPANION
@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
def test_a_null_typed_later_chunk_is_identical_on_both_legs(weighted: bool) -> None:
    valued = source(["a", "b", None, "a"])
    null_typed = pa.table({"c": pa.nulls(3), "p": pa.array([7, 8, 9], pa.int64())})
    native, forced = run_pair([_nd(weighted), passthrough("p")], [valued, null_typed])
    assert_same_as_oracle(native, forced)
    assert {o.schema.field("c").type for o in native.out} == {pa.string()}


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
def test_a_multi_chunk_fifty_thousand_row_run_is_identical_on_both_legs(threads: int) -> None:
    values = [None if i % 11 == 3 else f"v{i % 997}" for i in range(100_003)]
    native, forced = run_pair(
        [_nd(True), passthrough("p")], split(source(values), 50_000), native_threads=threads
    )
    assert len(native.out) == 3
    assert_same_as_oracle(native, forced)


@NEEDS_COMPANION
@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
def test_the_same_value_gets_different_categories_by_global_position_across_chunks(
    weighted: bool, tmp_path: Path
) -> None:
    table = source(["same"] * 40)
    config = make_config([_nd(weighted), passthrough("p")])
    run = run_one(config, split(table, 7))
    got = column_values(run.out, "c")
    assert run.ev[0].native_admitted is True
    assert len(set(got)) > 1, "a value-keyed draw would give every row the same category"
    # The first row of each chunk is a different global position, so the chunk-start rows
    # must not all collapse to one category either.
    assert len({chunk.column("c")[0].as_py() for chunk in run.out}) > 1
    assert got == _full_frame(config, table, tmp_path).column("c").to_pylist()


# ---------------------------------------------------------------------------
# 2. Global offset + uint64 domain.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
@pytest.mark.parametrize("base", [1, 1000])
def test_a_nonzero_base_row_offset_matches_the_whole_frame_at_those_positions(
    weighted: bool, base: int, tmp_path: Path
) -> None:
    n = 25
    values = [None if i % 6 == 2 else f"v{i % 5}" for i in range(n)]
    # The whole-frame run sees `base` filler rows first, so the data starts at position `base`.
    config = make_config([_nd(weighted), passthrough("p")])
    whole = _full_frame(config, source([f"f{i}" for i in range(base)] + values), tmp_path)
    expected = whole.column("c").to_pylist()[base:]
    run = run_one(config, split(source(values), 7), base_row_offset=base)
    assert run.ev[0].native_admitted is True
    assert column_values(run.out, "c") == expected


@NEEDS_COMPANION
@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
def test_native_equals_oracle_with_a_nonzero_base_offset(weighted: bool) -> None:
    values = [None if i % 6 == 2 else f"v{i % 5}" for i in range(31)]
    native, forced = run_pair(
        [_nd(weighted), passthrough("p")], split(source(values), 7), base_row_offset=9_001
    )
    assert_same_as_oracle(native, forced)


# Frozen literals: scalar derive_index(vault_key(), "ns_c", encode_int(g), pool) per g, mapped
# through CATEGORIES (uniform, pool 4) or bisect_right over the WEIGHTS CDF (weighted).
_BOUNDARY_KATS: dict[int, dict[str, list[str]]] = {
    0: {
        "uniform": ["alpha", "alpha", "delta"],
        "weighted": ["alpha", "beta", "beta"],
    },
    2**63 - 2: {  # g = 2**63-2, 2**63-1, 2**63
        "uniform": ["alpha", "delta", "alpha"],
        "weighted": ["beta", "alpha", "alpha"],
    },
    2**64 - 3: {  # g = 2**64-3, 2**64-2, 2**64-1 (the last in-domain row)
        "uniform": ["gamma", "gamma", "gamma"],
        "weighted": ["alpha", "alpha", "beta"],
    },
}


@NEEDS_COMPANION
@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
@pytest.mark.parametrize("base", sorted(_BOUNDARY_KATS))
def test_boundary_offsets_match_frozen_literals_on_both_legs(weighted: bool, base: int) -> None:
    key = "weighted" if weighted else "uniform"
    native, forced = run_pair(
        [_nd(weighted), passthrough("p")],
        [source(["x", "y", "z"])],
        base_row_offset=base,
    )
    assert_same_as_oracle(native, forced)
    assert column_values(native.out, "c") == _BOUNDARY_KATS[base][key]
    assert column_values(forced.out, "c") == _BOUNDARY_KATS[base][key]


@NEEDS_COMPANION
@pytest.mark.parametrize("native", [True, False], ids=["native", "oracle"])
def test_a_chunk_extending_past_the_uint64_domain_is_rejected(native: bool) -> None:
    columns = [_nd(), passthrough("p")]
    chunks = [source(["x", "y", "z"])]
    if not native:
        columns = [*columns, force_oracle(FORCE)]
        chunks = [with_force(c) for c in chunks]
    with pytest.raises(ExecutionError) as info:
        list(
            run_mask_chunked(
                make_config(columns),
                chunks,
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
                base_row_offset=2**64 - 2,
            )
        )
    assert info.value.code == "chunked_row_offset_out_of_domain"


# ---------------------------------------------------------------------------
# 7. Output-type pin on both chunked legs.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_oracle_leg_pins_the_seeded_categorical_to_string(shape: str) -> None:
    columns = [_nd(), passthrough("p"), force_oracle(FORCE)]
    chunks = [with_force(c) for c in split(source(_SHAPES[shape]), 7)]
    run = run_one(make_config(columns), chunks)
    assert run.ev[0].native_admitted is False
    assert {o.schema.field("c").type for o in run.out} == {pa.string()}


# ---------------------------------------------------------------------------
# 8. Evidence: zero-row chunk is idle, all-null non-empty chunk ran the kernel.
# ---------------------------------------------------------------------------


def _col_evidence(run: Any) -> list[dict[str, Any]]:
    return [
        {c["column"]: c for c in r.quality_metrics["chunked_route"]["columns"]}["c"]
        for r in run.sink
    ]


@NEEDS_COMPANION
@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
def test_admitted_run_reports_rust_companion_and_one_call_per_non_empty_chunk(
    weighted: bool,
) -> None:
    chunks = split(source(["a", "b", None, "c", "d", "a", "b", None, "c"]), 4)
    run = run_one(make_config([_nd(weighted), passthrough("p")]), chunks)
    ev = run.ev[0]
    assert ev.native_admitted is True and ev.compiled_kernel_executed is True
    assert ev.kernel_calls["categorical"] == len(chunks) == 3
    col = {c["column"]: c for c in aggregate_chunked_route_evidence(run.sink)["columns"]}["c"]
    assert col["planned_backend"] == "rust_companion"
    assert col["executed_backend"] == "rust_companion"


@NEEDS_COMPANION
def test_a_lone_zero_row_chunk_is_idle_not_a_compiled_run() -> None:
    run = run_one(make_config([_nd(), passthrough("p")]), [source([])])
    ev = run.ev[0]
    assert ev.native_admitted is True
    assert ev.compiled_kernel_executed is False
    assert "categorical" not in ev.kernel_calls
    (col,) = _col_evidence(run)
    assert col["planned_backend"] == "rust_companion"
    assert col["executed_backend"] == "arrow_python"
    assert run.out[0].schema.field("c").type == pa.string()


@NEEDS_COMPANION
def test_a_zero_row_chunk_among_valued_chunks_is_idle_and_not_counted() -> None:
    chunks = [source(["a", "b"]), source([]), source(["c", None])]
    run = run_one(make_config([_nd(), passthrough("p")]), chunks)
    ev = run.ev[0]
    assert ev.compiled_kernel_executed is True
    assert ev.kernel_calls["categorical"] == 2  # the empty chunk made no compiled call
    backends = [c["executed_backend"] for c in _col_evidence(run)]
    assert backends == ["rust_companion", "arrow_python", "rust_companion"]


@NEEDS_COMPANION
def test_an_all_null_non_empty_chunk_honestly_reports_a_compiled_run() -> None:
    run = run_one(make_config([_nd(), passthrough("p")]), [source([None, None, None])])
    ev = run.ev[0]
    assert ev.native_admitted is True
    assert ev.compiled_kernel_executed is True
    assert ev.kernel_calls["categorical"] == 1
    (col,) = _col_evidence(run)
    assert col["executed_backend"] == "rust_companion"
    assert column_values(run.out, "c") == [None, None, None]


def test_the_oracle_leg_reports_no_compiled_work() -> None:
    columns = [_nd(), passthrough("p"), force_oracle(FORCE)]
    run = run_one(make_config(columns), [with_force(source(["a", "b"]))])
    assert run.ev[0].native_admitted is False
    assert run.ev[0].compiled_kernel_executed is False
