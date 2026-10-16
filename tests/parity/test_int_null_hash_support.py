"""Hashing an integer column that holds nulls works the same on every route.

Pandas widens an Arrow integer column with a null to float64, and the hash kernel refuses a
float. The pandas handlers therefore key from the column's exact Arrow values, recorded at
ingest, so the tokens equal the ones the Arrow routes (native chunked, out-of-core) already
produce. Every expected token below is a literal from `_d1a_hash_data.EXPECTED_TOKENS`,
captured from the pure-Python `hash_array` before any of this existed; none is computed from the
output under test.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    hash_col,
    key_provider,
    make_config,
)
from tests.parity import _strict_compare as sc
from tests.parity._d1a_hash_data import EXPECTED_TOKENS, hash_seeds, run_chain, source_columns
from tests.parity._d2a_seq_data import run_full, run_seq
from tests.parity._seq_witness import (
    MARK,
    MarkingKernel,
    hide_companion,
    install,
    spy_python_hash,
)

_INTS = [n for n, a in source_columns().items() if pa.types.is_integer(a.type)]
_ALL_TRUE = dict.fromkeys(sc.DIMENSIONS, True)
# Each width's type min/max (uint64 max, int64 min) and an interleaved null are in the fixture.
_WIDTHS = {
    "c_i8": pa.int8(),
    "c_i16": pa.int16(),
    "c_i32": pa.int32(),
    "c_i64": pa.int64(),
    "c_u8": pa.uint8(),
    "c_u16": pa.uint16(),
    "c_u32": pa.uint32(),
    "c_u64": pa.uint64(),
}


def _payload() -> pa.Table:
    cols = source_columns()
    return pa.table({n: cols[n] for n in _INTS})


def _t_tokens(outputs: dict[str, pa.Table]) -> dict[str, list[Any]]:
    return {n: outputs["t"].column(n).to_pylist() for n in _INTS}


def test_fixture_really_carries_a_null_and_the_width_extremes() -> None:
    cols = source_columns()
    assert all(cols[n].null_count == 1 for n in _INTS)
    assert 2**64 - 1 in cols["c_u64"].to_pylist()
    assert -(2**63) in cols["c_i64"].to_pylist()


def test_full_frame_matches_the_frozen_tokens_for_all_eight_widths() -> None:
    out = run_full(_payload(), hash_seeds(_INTS)).outputs
    assert _t_tokens(out) == {n: EXPECTED_TOKENS[n] for n in _INTS}


def test_sequential_without_the_companion_matches_the_frozen_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hide_companion(monkeypatch)
    rows = spy_python_hash(monkeypatch)
    out = run_seq(_payload(), hash_seeds(_INTS)).outputs
    assert _t_tokens(out) == {n: EXPECTED_TOKENS[n] for n in _INTS}
    assert rows.count(10) >= len(_INTS)  # the Python handler ran for every integer column


def test_sequential_with_the_companion_feeds_the_kernel_the_source_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    out = run_seq(_payload(), hash_seeds(_INTS)).outputs
    expected = {n: [None if t is None else MARK + t for t in EXPECTED_TOKENS[n]] for n in _INTS}
    assert _t_tokens(out) == expected
    # Companion-present kept uint64 max and int64 min exact: the kernel saw each source width.
    payload_types = [c.arrow_type for c in kernel.calls if c.rows == 10]
    for width in _WIDTHS.values():
        assert width in payload_types


def test_sequential_real_kernel_equals_python_and_full_frame() -> None:
    pytest.importorskip("decoy_engine_native")
    seq = run_seq(_payload(), hash_seeds(_INTS)).outputs
    full = run_full(_payload(), hash_seeds(_INTS)).outputs
    assert _t_tokens(seq) == {n: EXPECTED_TOKENS[n] for n in _INTS}
    assert sc.compare_strict(full["t"], seq["t"]) == _ALL_TRUE


@pytest.mark.parametrize("batch_rows", [None, 3])
def test_out_of_core_matches_full_frame_cell_for_cell(batch_rows: int | None) -> None:
    ooc = run_chain(_payload(), hash_seeds(_INTS), batch_rows=batch_rows).tables
    full = run_full(_payload(), hash_seeds(_INTS)).outputs
    assert _t_tokens(ooc) == _t_tokens(full)


def test_a_null_free_integer_column_is_unchanged_by_the_snapshot_path() -> None:
    # No null: no widening, no snapshot. The tokens equal the same values in the null-bearing
    # column (index 0..4 of c_i64 carry the same values).
    values = source_columns()["c_i64"].to_pylist()[:5]
    clean = pa.table({"c_i64": pa.array(values, pa.int64())})
    out = run_full(clean, hash_seeds(["c_i64"])).outputs
    assert out["t"].column("c_i64").to_pylist() == EXPECTED_TOKENS["c_i64"][:5]


def test_sibling_passthrough_and_faker_free_columns_are_not_converted() -> None:
    # Only the hashed column reads the snapshot. A passthrough int+null sibling still widens to
    # float64, exactly as before this change.
    from tests.parity._d1a_hash_data import column_seed

    payload = pa.table(
        {
            "h": pa.array([1, None, 3], pa.int64()),
            "p": pa.array([4, None, 6], pa.int64()),
        }
    )
    passthrough = column_seed("passthrough", namespace=None)
    out = run_full(payload, (("h", column_seed()), ("p", passthrough))).outputs
    assert out["t"].column("h").to_pylist()[1] is None
    assert out["t"].column("p").type == pa.float64()
    assert out["t"].column("p").to_pylist() == [4.0, None, 6.0]


def test_orphan_remap_keys_hash_from_themselves_not_the_parent_snapshot() -> None:
    # REMAP re-runs the parent column's handler on a frame of orphan keys under the parent's
    # own table and column name. A registered snapshot must not leak into that call.
    from decoy_engine.relationships._graph import OrphanPolicy
    from tests.parity._d1a_hash_data import chain_sources, column_seed

    base = chain_sources(pa.table({"a": pa.array(list("abcd"))}))
    t = base["t"]
    parent_hk = pa.array([0, None, 2, 3], pa.int64())
    base["t"] = t.set_column(t.schema.get_field_index("hk"), "hk", parent_hk)
    base["child"] = pa.table({"ck": pa.array([0, 2, 3, 7], pa.int64())})
    seeds = (("a", column_seed()),)
    full = run_full(None, seeds, policy=OrphanPolicy.REMAP, sources=base).outputs
    seq = run_seq(None, seeds, policy=OrphanPolicy.REMAP, sources=base).outputs
    assert sc.compare_strict(full["child"], seq["child"]) == _ALL_TRUE


# ---------------------------------------------------------------------------
# Chunked routes: the same values must come out whatever the chunk boundaries are.
# ---------------------------------------------------------------------------

_PARTITIONS = {
    "null-free-then-mixed": [(0, 5), (5, 10)],
    "mixed-first": [(0, 6), (6, 10)],
    "all-null-chunk": [(0, 5), (5, 6), (6, 10)],
    "row-by-row": [(i, i + 1) for i in range(10)],
}


def _chunks(name: str, parts: list[tuple[int, int]]) -> list[pa.Table]:
    values = source_columns()[name]
    return [pa.table({"c": values.slice(a, b - a)}) for a, b in parts]


def _drive(fn: Any, chunks: list[pa.Table]) -> list[Any]:
    out = fn(
        make_config([hash_col("c", "ns_h")]),
        chunks,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
    )
    return [v for t in out for v in t.column("c").to_pylist()]


@NEEDS_COMPANION
@pytest.mark.parametrize("partition", list(_PARTITIONS))
@pytest.mark.parametrize("name", ["c_i64", "c_u64", "c_i8"])
@pytest.mark.parametrize("fn", [run_mask_chunked, run_mask_pipeline_chunked])
def test_chunked_routes_are_partition_invariant_and_match_the_null_free_tokens(
    fn: Any, name: str, partition: str
) -> None:
    values = source_columns()[name]
    got = _drive(fn, _chunks(name, _PARTITIONS[partition]))
    # Reference: the same values with the null removed, which is a null-free integer column
    # that was always supported, with the null put back at its position.
    present = [v for v in values.to_pylist() if v is not None]
    clean = _drive(fn, [pa.table({"c": pa.array(present, values.type)})])
    it = iter(clean)
    expected = [None if v is None else next(it) for v in values.to_pylist()]
    assert got == expected
    assert got.count(None) == 1


# ---------------------------------------------------------------------------
# What stays as it was.
# ---------------------------------------------------------------------------


def _truncate_seed() -> Any:
    from tests.parity._d1a_hash_data import column_seed

    return column_seed("truncate", namespace=None, provider_config=(("length", 3),))


def _categorical_seed() -> Any:
    from tests.parity._d1a_hash_data import column_seed

    return column_seed(
        "categorical", namespace="ns_cat", provider_config=(("categories", ("a", "b")),)
    )


@pytest.mark.parametrize(
    "seed", [_truncate_seed, _categorical_seed], ids=["truncate", "categorical"]
)
@pytest.mark.parametrize("run", [run_full, run_seq], ids=["full", "seq"])
def test_truncate_and_categorical_over_integer_with_null_are_still_rejected(
    seed: Any, run: Any
) -> None:
    from decoy_engine.execution._errors import ExecutionError

    payload = pa.table({"n": pa.array([1, None, 3], pa.int64())})
    with pytest.raises(ExecutionError) as exc:
        run(payload, (("n", seed()),))
    assert exc.value.code == "null_bearing_int_unsupported"
    assert "truncate/categorical" in str(exc.value)
    assert "hash" not in str(exc.value)


def test_a_when_on_a_passthrough_sibling_still_reads_the_widened_frame_column() -> None:
    # Gated string hash selecting on an int+null passthrough sibling: the frame stays float64,
    # so the predicate selects exactly the rows it always did.
    from dataclasses import replace

    from tests.parity._d1a_hash_data import column_seed

    payload = pa.table(
        {"p": pa.array([1, None, 3], pa.int64()), "s": pa.array(["a", "b", "c"], pa.string())}
    )
    gated = replace(column_seed(namespace="ns_s"), when="p > 1")
    passthrough = column_seed("passthrough", namespace=None)
    for run in (run_full, run_seq):
        out = run(payload, (("p", passthrough), ("s", gated))).outputs["t"]
        assert out.column("p").to_pylist() == [1.0, None, 3.0]
        s = out.column("s").to_pylist()
        assert s[:2] == ["a", "b"] and s[2] not in ("c", None)


def test_a_when_on_the_hashed_integer_column_behaves_as_it_does_for_a_null_free_one() -> None:
    # The hash source column is masked before a sibling's predicate reads it, with or without a
    # null in it. Unchanged by the snapshot, so both fail the same way.
    from dataclasses import replace

    from tests.parity._d1a_hash_data import column_seed

    gated = replace(column_seed(namespace="ns_s"), when="h > 1")
    codes = []
    for values in ([1, 2, 3], [1, None, 3]):
        payload = pa.table({"h": pa.array(values, pa.int64()), "s": pa.array(["a", "b", "c"])})
        with pytest.raises(Exception) as exc:
            run_full(payload, (("h", column_seed()), ("s", gated)))
        codes.append(getattr(exc.value, "code", None))
    assert codes[0] == codes[1] == "when_expression_error"
