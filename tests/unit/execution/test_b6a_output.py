"""Acceptance tests 1 to 5 of plan 2026-10-02-b6a-incremental-output-sink (rev 2.1):
a routed run with a sink streams its output, and the streamed output equals B2's.

These tests are written before the implementation. Do not delete one, add a skip or
xfail outside `NEEDS_COMPANION`, shrink a cross-product, relax a byte-identity or
`check_metadata=True` comparison, or raise a structural bound without a new plan
gate: a failing test is a defect in the code or a finding for the plan.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ExecutionError
from tests.unit.execution import _auto_chunk_matrix as matrix
from tests.unit.execution import _auto_chunk_strategies as strategies
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _b6a_support as b6a
from tests.unit.execution.test_auto_chunk_output_contract import (
    PASSTHROUGH_TYPES,
    SHAPES,
    _base,
    _masked_trio_columns,
)

COMPANION_PARAMS = [
    pytest.param("present", marks=support.NEEDS_COMPANION),
    "absent",
]


@pytest.fixture(autouse=True)
def _companion_state(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if "companion" in request.fixturenames and request.getfixturevalue("companion") == "absent":
        support.remove_companion(monkeypatch)


def _check_case(cfg: dict[str, Any], src: pa.Table, tmp_path: Path, **extra: Any) -> None:
    """Test 1's per-case assertions: a recording sink forwarding to a real Parquet sink."""
    expected = b6a.reference(cfg, src, **extra)[support.TABLE]
    sink, target = b6a.real_sink(tmp_path)
    result = b6a.run_streamed(cfg, src, sink, **extra)
    assert result.outputs == {}
    assert sink.calls == [("write_batches", support.TABLE), ("commit",)]
    b6a.assert_streamed_equals(sink, expected)
    published = (target / f"{support.TABLE}.parquet").read_bytes()
    assert published == b6a.parquet_bytes(expected, tmp_path / "reference.parquet")
    assert sorted(p.name for p in target.iterdir()) == [f"{support.TABLE}.parquet"]


# ---------------------------------------------------------------------------
# Test 1: output equals B2 (B2 test 3's case matrix).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("companion", COMPANION_PARAMS)
@pytest.mark.parametrize("key", sorted(strategies.STRATEGY_FIXTURES))
def test_streamed_output_equals_b2_per_strategy(
    key: str, companion: str, threads: int, tmp_path: Path
) -> None:
    columns, data = strategies.STRATEGY_FIXTURES[key]
    src = pa.table(data)
    cfg = support.make_cfg(columns, path=support.write_source(src, tmp_path / "s.parquet"))
    _check_case(cfg, src, tmp_path, native_threads=threads)


@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("companion", COMPANION_PARAMS)
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_streamed_output_equals_b2_per_shape(
    shape: str, companion: str, threads: int, tmp_path: Path
) -> None:
    cols = _base()
    for name, edit in SHAPES[shape].items():
        cols[name] = edit(cols[name])
    src = support.table_of(cols)
    cfg = support.make_cfg(
        _masked_trio_columns(), path=support.write_source(src, tmp_path / "s.parquet")
    )
    _check_case(cfg, src, tmp_path, native_threads=threads)


@pytest.mark.parametrize("companion", COMPANION_PARAMS)
@pytest.mark.parametrize("typ", PASSTHROUGH_TYPES)
def test_streamed_output_equals_b2_for_every_admitted_passthrough_type(
    typ: str, companion: str, tmp_path: Path
) -> None:
    arr = matrix.BUILDERS[typ]()
    src = support.table_of({**support.string_source(), "x": arr})
    cfg = support.make_cfg(
        [support.hash_col("h"), support.redact_col("r"), support.pass_col("x")],
        path=support.write_source(src, tmp_path / "s.parquet"),
    )
    try:
        b6a.reference(cfg, src)
    except Exception as exc:
        sink, target = b6a.real_sink(tmp_path)
        with pytest.raises(type(exc)):
            b6a.run_streamed(cfg, src, sink)
        assert not target.exists()
        return
    _check_case(cfg, src, tmp_path)


@pytest.mark.parametrize("companion", COMPANION_PARAMS)
@pytest.mark.parametrize(
    "variant", ["nonnullable_field", "field_metadata", "uint64_max", "unconfigured_column"]
)
def test_streamed_output_equals_b2_for_passthrough_field_shapes(
    variant: str, companion: str, tmp_path: Path
) -> None:
    arr: pa.Array = pa.array([f"k{i}" for i in range(support.ROWS)])
    fields: dict[str, pa.Field] = {}
    configured = True
    if variant == "nonnullable_field":
        fields["x"] = pa.field("x", pa.string(), nullable=False)
    elif variant == "field_metadata":
        fields["x"] = pa.field("x", pa.string(), metadata={b"owner": b"b6a"})
    elif variant == "uint64_max":
        arr = pa.array([2**64 - 1 - i for i in range(support.ROWS)], pa.uint64())
    else:
        configured = False
    src = support.table_of({**support.string_source(), "x": arr}, fields)
    cols = [support.hash_col("h"), support.redact_col("r")] + (
        [support.pass_col("x")] if configured else []
    )
    cfg = support.make_cfg(cols, path=support.write_source(src, tmp_path / "s.parquet"))
    _check_case(cfg, src, tmp_path)


@pytest.mark.parametrize("companion", COMPANION_PARAMS)
def test_streamed_output_equals_b2_for_a_native_faker_over_an_all_null_source(
    companion: str, tmp_path: Path
) -> None:
    columns, _data = strategies.STRATEGY_FIXTURES["faker:deterministic_native"]
    src = pa.table({"val": pa.nulls(support.ROWS, pa.string())})
    cfg = support.make_cfg(columns, path=support.write_source(src, tmp_path / "s.parquet"))
    _check_case(cfg, src, tmp_path)


# ---------------------------------------------------------------------------
# Test 2: row groups.
# ---------------------------------------------------------------------------


def _wide_job(tmp_path: Path, rows: int) -> tuple[dict[str, Any], pa.Table]:
    src = pa.table(
        {
            "r": pa.array([f"s{i}" for i in range(rows)]),
            "p": pa.array(range(rows), pa.int64()),
        }
    )
    cfg = support.make_cfg(
        [support.redact_col("r"), support.pass_col("p")],
        path=support.write_source(src, tmp_path / "s.parquet"),
    )
    return cfg, src


def _row_groups(path: Path) -> list[int]:
    meta = pq.ParquetFile(path).metadata
    return [meta.row_group(i).num_rows for i in range(meta.num_row_groups)]


def test_row_group_rows_is_the_pyarrow_default(tmp_path: Path) -> None:
    mod = b6a.sink_module()
    table = pa.table({"a": pa.array(range(mod.ROW_GROUP_ROWS + 5), pa.int64())})
    path = tmp_path / "default.parquet"
    pq.write_table(table, path)
    assert _row_groups(path)[0] == mod.ROW_GROUP_ROWS == 1024 * 1024


def test_two_million_rows_make_three_row_groups_with_pyarrow_bytes(tmp_path: Path) -> None:
    rows = 2_200_000
    cfg, src = _wide_job(tmp_path, rows)
    extra = {"chunk_size_rows": 100_000}
    expected = b6a.reference(cfg, src, **extra)[support.TABLE]
    sink, target = b6a.real_sink(tmp_path)
    result = b6a.run_streamed(cfg, src, sink, **extra)
    published = target / f"{support.TABLE}.parquet"
    assert _row_groups(published) == [1_048_576, 1_048_576, 102_848]
    assert published.read_bytes() == b6a.parquet_bytes(expected, tmp_path / "ref.parquet")
    block = result.quality_metrics["auto_chunk"]["output"]
    assert (block["mode"], block["row_groups"], block["byte_cut_row_groups"]) == ("streamed", 3, 0)


def test_patched_row_group_rows_cut_at_that_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = b6a.sink_module()
    monkeypatch.setattr(mod, "ROW_GROUP_ROWS", 120_000)
    rows = 430_000
    cfg, src = _wide_job(tmp_path, rows)
    extra = {"chunk_size_rows": 50_000}
    expected = b6a.reference(cfg, src, **extra)[support.TABLE]
    sink, target = b6a.real_sink(tmp_path)
    b6a.run_streamed(cfg, src, sink, **extra)
    published = target / f"{support.TABLE}.parquet"
    assert _row_groups(published) == [120_000, 120_000, 120_000, 70_000]
    assert published.read_bytes() == b6a.parquet_bytes(
        expected, tmp_path / "ref.parquet", row_group_size=120_000
    )


# ---------------------------------------------------------------------------
# Test 3: byte cut.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cap", [1, 1500])
def test_a_byte_cut_row_group_ends_at_a_chunk_boundary(
    cap: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = b6a.sink_module()
    monkeypatch.setattr(mod, "ROW_GROUP_BYTES", cap)
    cfg, src = _wide_job(tmp_path, 160)
    expected = b6a.reference(cfg, src)[support.TABLE]
    sink, target = b6a.real_sink(tmp_path)
    result = b6a.run_streamed(cfg, src, sink)
    b6a.assert_streamed_equals(sink, expected)
    groups = _row_groups(target / f"{support.TABLE}.parquet")
    assert all(g % support.CHUNK == 0 for g in groups)
    block = result.quality_metrics["auto_chunk"]["output"]
    assert 1 <= block["byte_cut_row_groups"] <= len(groups)
    assert block["row_groups"] == len(groups)
    if cap == 1:
        assert groups == [support.CHUNK] * (160 // support.CHUNK)
        assert block["byte_cut_row_groups"] == len(groups)


# ---------------------------------------------------------------------------
# Test 4: hold-back and spill (B1's oracle route, a column typed late).
# ---------------------------------------------------------------------------

_LATE_ROWS = 160
_LATE_NULL_CHUNKS = 4


def _late_typed_job(
    tmp_path: Path, *, typed: bool = True, other_type: bool = False
) -> tuple[dict[str, Any], pa.Table]:
    nulls = _LATE_NULL_CHUNKS * support.CHUNK
    tiers = [["bronze", "silver", "gold"][i % 3] for i in range(_LATE_ROWS - nulls)]
    val = [None] * nulls + (tiers if typed else [None] * len(tiers))
    src = pa.table(
        {
            "h": pa.array([f"u{i}@x.example" for i in range(_LATE_ROWS)]),
            "val": pa.array(val, pa.string()),
        }
    )
    column = {
        "name": "val",
        "strategy": "categorical",
        "deterministic": True,
        "namespace": "tier_ns",
        "provider_config": {"categories": ["free", "pro", "team"], "weights": [0.6, 0.3, 0.1]},
    }
    cfg = support.make_cfg(
        [support.hash_col("h"), column], path=support.write_source(src, tmp_path / "s.parquet")
    )
    return cfg, src


def test_chunks_before_the_schema_resolves_are_held_back_and_spilled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = b6a.sink_module()
    monkeypatch.setattr(mod, "ROW_GROUP_ROWS", 20)
    spill = tmp_path / "tmp"
    cfg, src = _late_typed_job(tmp_path)
    expected = b6a.reference(cfg, src)[support.TABLE]
    with b6a.patched_tempdir(monkeypatch, spill):
        sink, target = b6a.real_sink(tmp_path)
        result = b6a.run_streamed(cfg, src, sink)
        assert b6a.leftovers(spill) == []
    b6a.assert_streamed_equals(sink, expected)
    block = result.quality_metrics["auto_chunk"]["output"]
    assert block["held_back_chunks"] == _LATE_NULL_CHUNKS
    assert block["spilled_chunks"] > 0
    assert (target / f"{support.TABLE}.parquet").read_bytes() == b6a.parquet_bytes(
        expected, tmp_path / "ref.parquet", row_group_size=20
    )


def test_a_failure_after_the_schema_resolves_leaves_no_spill_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = b6a.sink_module()
    monkeypatch.setattr(mod, "ROW_GROUP_ROWS", 20)
    spill = tmp_path / "tmp"
    cfg, src = _late_typed_job(tmp_path)
    b6a.ChunkSpy(monkeypatch, fail_at=_LATE_NULL_CHUNKS + 1)
    with b6a.patched_tempdir(monkeypatch, spill):
        sink, target = b6a.real_sink(tmp_path)
        with pytest.raises(RuntimeError, match="injected kernel failure"):
            b6a.run_streamed(cfg, src, sink)
        assert b6a.leftovers(spill) == []
    assert sink.count("abort") == 1 and sink.count("commit") == 0
    assert not target.exists()
    assert b6a.leftovers(tmp_path) == []


def test_a_failure_while_chunks_are_still_spilling_leaves_no_spill_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = b6a.sink_module()
    monkeypatch.setattr(mod, "ROW_GROUP_ROWS", 20)
    spill = tmp_path / "tmp"
    cfg, src = _late_typed_job(tmp_path)
    b6a.ChunkSpy(monkeypatch, fail_at=3)
    with b6a.patched_tempdir(monkeypatch, spill):
        sink, target = b6a.real_sink(tmp_path)
        with pytest.raises(RuntimeError, match="injected kernel failure"):
            b6a.run_streamed(cfg, src, sink)
        assert b6a.leftovers(spill) == []
    assert sink.calls == [("abort",)]
    assert not target.exists()


def test_a_column_null_in_every_chunk_stays_null_typed_as_in_the_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = b6a.sink_module()
    monkeypatch.setattr(mod, "ROW_GROUP_ROWS", 20)
    spill = tmp_path / "tmp"
    cfg, src = _late_typed_job(tmp_path, typed=False)
    expected = b6a.reference(cfg, src)[support.TABLE]
    assert expected.schema.field("val").type == pa.null()
    with b6a.patched_tempdir(monkeypatch, spill):
        sink, _target = b6a.real_sink(tmp_path)
        result = b6a.run_streamed(cfg, src, sink)
        assert b6a.leftovers(spill) == []
    b6a.assert_streamed_equals(sink, expected)
    assert result.quality_metrics["auto_chunk"]["output"]["spilled_chunks"] > 0


def test_a_later_chunk_with_a_disagreeing_non_null_type_is_a_schema_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution.native import _chunked_entry

    cfg, src = _late_typed_job(tmp_path)
    real = _chunked_entry.run_mask_chunked

    def drifting(config: Any, chunks: Any, **kwargs: Any) -> Any:
        for index, chunk in enumerate(real(config, chunks, **kwargs)):
            if index == _LATE_NULL_CHUNKS + 2:
                chunk = chunk.set_column(
                    1,
                    pa.field("val", pa.int64()),
                    pa.array(range(chunk.num_rows), pa.int64()),
                )
            yield chunk

    monkeypatch.setattr(_chunked_entry, "run_mask_chunked", drifting)
    sink, target = b6a.real_sink(tmp_path)
    with pytest.raises(ExecutionError) as err:
        b6a.run_streamed(cfg, src, sink)
    assert err.value.code == "chunked_schema_mismatch"
    assert sink.count("abort") == 1 and sink.count("commit") == 0
    assert not target.exists()


# ---------------------------------------------------------------------------
# Test 5: bounded structure.
# ---------------------------------------------------------------------------


def _native_job(tmp_path: Path, rows: int) -> tuple[dict[str, Any], pa.Table]:
    src = pa.table(
        {
            "r": pa.array([f"s{i}" for i in range(rows)]),
            "z": pa.array([f"{i:05d}" for i in range(rows)]),
            "p": pa.array([f"keep-{i}" for i in range(rows)]),
        }
    )
    cfg = support.make_cfg(
        [support.redact_col("r"), support.truncate_col("z"), support.pass_col("p")],
        path=support.write_source(src, tmp_path / "s.parquet"),
    )
    return cfg, src


def test_live_chunks_stay_within_one_row_group_plus_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = b6a.sink_module()
    monkeypatch.setattr(mod, "ROW_GROUP_ROWS", 32)
    rows = 400
    cfg, src = _native_job(tmp_path, rows)
    spy = b6a.ChunkSpy(monkeypatch)
    seen: list[int] = []
    sink = b6a.RecordingSink(on_batch=lambda _t, _b: seen.append(spy.alive(support.TABLE)))
    b6a.run_streamed(cfg, src, sink)
    bound = math.ceil(32 / support.CHUNK) + 1
    assert len(seen) == math.ceil(rows / 32)
    assert max(seen) <= bound, (seen, bound)
    results = spy.result_lists[support.TABLE]
    assert len(results) == math.ceil(rows / support.CHUNK)
    assert all(r.outputs == {} for r in results)


@pytest.mark.parametrize("route", ["native", "oracle"])
def test_output_free_evidence_equals_the_resident_runs_evidence(
    route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if route == "oracle":
        support.remove_companion(monkeypatch)
    elif not support.COMPANION_PRESENT:
        pytest.skip("compiled companion not installed")
    src = pa.table(support.string_source())
    cfg = support.make_cfg(
        [support.hash_col("h"), support.redact_col("r")],
        path=support.write_source(src, tmp_path / "s.parquet"),
    )
    resident = b6a.run_streamed(cfg, src, None, **b6a.resident_kw())
    sink = b6a.RecordingSink()
    streamed = b6a.run_streamed(cfg, src, sink)
    assert sink.count("commit") == 1
    assert streamed.quality_metrics["chunked_route"] == resident.quality_metrics["chunked_route"]
    assert streamed.quality_metrics.get("code_set_corpora") == resident.quality_metrics.get(
        "code_set_corpora"
    )
    assert streamed.warnings == resident.warnings
    assert [(t.strategy_type, t.column) for t in streamed.timings] == [
        (t.strategy_type, t.column) for t in resident.timings
    ]
    if route == "native":
        assert streamed.boundary_conversion_ms == resident.boundary_conversion_ms == 0.0
    else:
        assert streamed.boundary_conversion_ms >= 0.0 and resident.boundary_conversion_ms >= 0.0
