"""Acceptance tests 1, 2 and 9 of plan 2026-10-02-b6b-lazy-batch-input (rev 2.1): a routed
run over `LazySource` inputs equals its resident twin, chunk for chunk, and says how it read.

These tests are written before the implementation. Do not delete one, add a skip or xfail
outside `NEEDS_COMPANION`, shrink a matrix, relax a byte-identity or `check_metadata=True`
comparison, or raise a structural bound without a new plan gate: a failing test is a defect in
the code or a finding for the plan.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import RowErrorsFailedError
from tests.unit.execution import _auto_chunk_matrix as matrix
from tests.unit.execution import _auto_chunk_strategies as strategies
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _b6b_support as b6b
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

# (chunk_size_rows, source row-group size). The plan's chunk sizes 50,000 and 7,919 against
# the plan's layouts (pyarrow default, 33,333, one group), plus two sizes small enough that
# the 40-row fixtures really split into several chunks and several row groups.
LAYOUTS = [(16, None), (7, 7), (7919, 33_333), (50_000, 40)]
LAYOUT_IDS = [f"chunk{c}-rg{g}" for c, g in LAYOUTS]


@pytest.fixture(autouse=True)
def _companion_state(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if "companion" in request.fixturenames and request.getfixturevalue("companion") == "absent":
        support.remove_companion(monkeypatch)


def _case_job(
    columns: list[dict[str, Any]],
    src: pa.Table,
    tmp_path: Path,
    row_group_size: int | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = b6b.write(src, tmp_path / "s.parquet", row_group_size)
    return support.make_cfg(columns, path=path), {b6b.TABLE: b6b.lazy(path)}


def _check_leaves(got: Any, want: Any, lsink: Any, path: str) -> None:
    """The two leaves guarantee 5 adds, asserted with their exact lazy / resident values."""
    streamed = any(c[0] == "write_batches" for c in lsink.calls)
    block = got.quality_metrics.get("auto_chunk", {})
    twin_block = want.quality_metrics.get("auto_chunk", {})
    if "input" not in block:
        assert "input" not in twin_block
        assert got.quality_metrics["execution"]["loaded_fully_in_memory"] is True
        return
    meta = pq.ParquetFile(path).metadata
    groups = [meta.row_group(i).num_rows for i in range(meta.num_row_groups)]
    assert twin_block["input"] == {"mode": "resident", "reason": "resident_source"}
    assert streamed
    assert block["input"] == {
        "mode": "lazy",
        "reason": "eligible",
        "source_row_groups": len(groups),
        "source_max_row_group_rows": max(groups),
    }
    assert got.quality_metrics["execution"]["loaded_fully_in_memory"] is False
    assert want.quality_metrics["execution"]["loaded_fully_in_memory"] is True


def _check_case(
    cfg: dict[str, Any],
    sources: dict[str, Any],
    tmp_path: Path,
    **extra: Any,
) -> None:
    """Test 1's per-case assertions: a recording sink forwarding to a real Parquet sink."""
    path = str(sources[b6b.TABLE].path)
    twin_sink, twin_target = b6b.b6a.real_sink(tmp_path, "twin_out")
    lsink, ltarget = b6b.b6a.real_sink(tmp_path, "lazy_out")
    twin = b6b.twin_sources(sources)
    try:
        want = b6b.run(cfg, twin, twin_sink, **extra)
    except Exception as exc:
        with pytest.raises(type(exc)) as err:
            b6b.run(cfg, sources, lsink, **extra)
        assert getattr(err.value, "code", None) == getattr(exc, "code", None)
        assert not ltarget.exists()
        return
    with pytest.MonkeyPatch.context() as mp:
        spies = b6b.ReadSpies(mp)
        got = b6b.run(cfg, sources, lsink, **extra)
        never_read_whole = spies.none_called()
    b6b.assert_lazy_equals_twin(got, want, lsink, twin_sink)
    _check_leaves(got, want, lsink, path)
    if any(c[0] == "write_batches" for c in lsink.calls):
        assert never_read_whole, "a streamed lazy run read a whole file"
        published = ltarget / f"{b6b.TABLE}.parquet"
        assert published.read_bytes() == (twin_target / f"{b6b.TABLE}.parquet").read_bytes()
        assert sorted(p.name for p in ltarget.iterdir()) == [f"{b6b.TABLE}.parquet"]


# ---------------------------------------------------------------------------
# Test 1: equals the resident twin (B6a test 1's case matrix).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("chunk", "group"), LAYOUTS, ids=LAYOUT_IDS)
@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("companion", COMPANION_PARAMS)
@pytest.mark.parametrize("key", sorted(strategies.STRATEGY_FIXTURES))
def test_lazy_equals_the_twin_per_strategy(
    key: str, companion: str, threads: int, chunk: int, group: int | None, tmp_path: Path
) -> None:
    columns, data = strategies.STRATEGY_FIXTURES[key]
    cfg, sources = _case_job(columns, pa.table(data), tmp_path, group)
    _check_case(cfg, sources, tmp_path, native_threads=threads, chunk_size_rows=chunk)


@pytest.mark.parametrize(("chunk", "group"), LAYOUTS, ids=LAYOUT_IDS)
@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("companion", COMPANION_PARAMS)
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_lazy_equals_the_twin_per_shape(
    shape: str, companion: str, threads: int, chunk: int, group: int | None, tmp_path: Path
) -> None:
    cols = _base()
    for name, edit in SHAPES[shape].items():
        cols[name] = edit(cols[name])
    cfg, sources = _case_job(_masked_trio_columns(), support.table_of(cols), tmp_path, group)
    _check_case(cfg, sources, tmp_path, native_threads=threads, chunk_size_rows=chunk)


@pytest.mark.parametrize(("chunk", "group"), LAYOUTS, ids=LAYOUT_IDS)
@pytest.mark.parametrize("companion", COMPANION_PARAMS)
@pytest.mark.parametrize("typ", PASSTHROUGH_TYPES)
def test_lazy_equals_the_twin_for_every_admitted_passthrough_type(
    typ: str, companion: str, chunk: int, group: int | None, tmp_path: Path
) -> None:
    arr = matrix.BUILDERS[typ]()
    src = support.table_of({**support.string_source(), "x": arr})
    columns = [support.hash_col("h"), support.redact_col("r"), support.pass_col("x")]
    cfg, sources = _case_job(columns, src, tmp_path, group)
    _check_case(cfg, sources, tmp_path, chunk_size_rows=chunk)


@pytest.mark.parametrize(("chunk", "group"), LAYOUTS, ids=LAYOUT_IDS)
@pytest.mark.parametrize("companion", COMPANION_PARAMS)
@pytest.mark.parametrize(
    "variant", ["nonnullable_field", "field_metadata", "uint64_max", "unconfigured_column"]
)
def test_lazy_equals_the_twin_for_passthrough_field_shapes(
    variant: str, companion: str, chunk: int, group: int | None, tmp_path: Path
) -> None:
    arr: pa.Array = pa.array([f"k{i}" for i in range(support.ROWS)])
    fields: dict[str, pa.Field] = {}
    configured = True
    if variant == "nonnullable_field":
        fields["x"] = pa.field("x", pa.string(), nullable=False)
    elif variant == "field_metadata":
        fields["x"] = pa.field("x", pa.string(), metadata={b"owner": b"b6b"})
    elif variant == "uint64_max":
        arr = pa.array([2**64 - 1 - i for i in range(support.ROWS)], pa.uint64())
    else:
        configured = False
    src = support.table_of({**support.string_source(), "x": arr}, fields)
    columns = [support.hash_col("h"), support.redact_col("r")] + (
        [support.pass_col("x")] if configured else []
    )
    cfg, sources = _case_job(columns, src, tmp_path, group)
    _check_case(cfg, sources, tmp_path, chunk_size_rows=chunk)


@pytest.mark.parametrize(("chunk", "group"), LAYOUTS, ids=LAYOUT_IDS)
@pytest.mark.parametrize("companion", COMPANION_PARAMS)
def test_lazy_equals_the_twin_for_a_native_faker_over_an_all_null_source(
    companion: str, chunk: int, group: int | None, tmp_path: Path
) -> None:
    columns, _data = strategies.STRATEGY_FIXTURES["faker:deterministic_native"]
    src = pa.table({"val": pa.nulls(support.ROWS, pa.string())})
    cfg, sources = _case_job(columns, src, tmp_path, group)
    _check_case(cfg, sources, tmp_path, chunk_size_rows=chunk)


@pytest.mark.parametrize("chunk_size", [50_000, 7_919])
@pytest.mark.parametrize("group", [None, 33_333, "one"])
@pytest.mark.parametrize("companion", COMPANION_PARAMS)
def test_lazy_equals_the_twin_on_a_source_larger_than_a_chunk(
    companion: str, group: int | str | None, chunk_size: int, tmp_path: Path
) -> None:
    rows = 120_000
    src = pa.table(
        {
            "h": pa.array([f"u{i}@x.example" for i in range(rows)]),
            "r": pa.array([f"s{i}" for i in range(rows)]),
            "p": pa.array([i if i % 3 else None for i in range(rows)], pa.float64()),
        }
    )
    size = rows if group == "one" else group
    columns = [support.hash_col("h"), support.redact_col("r"), support.pass_col("p")]
    cfg, sources = _case_job(columns, src, tmp_path, size)
    _check_case(cfg, sources, tmp_path, chunk_size_rows=chunk_size, auto_chunk_threshold_rows=1000)


def test_lazy_equals_the_twin_with_a_plain_recording_sink(tmp_path: Path) -> None:
    src = pa.table(support.string_source(100))
    columns = [support.redact_col("h"), support.redact_col("r")]
    cfg, sources = _case_job(columns, src, tmp_path, 30)
    got, want, lsink, tsink, _a, _b = b6b.run_both(cfg, sources, tmp_path, chunk_size_rows=16)
    assert lsink.count("write_batches") == 1
    b6b.assert_lazy_equals_twin(got, want, lsink, tsink)


# ---------------------------------------------------------------------------
# Test 2: chunk boundaries and row errors.
# ---------------------------------------------------------------------------


def _record_chunk_sizes(monkeypatch: pytest.MonkeyPatch) -> list[list[int]]:
    from decoy_engine.execution.native import _chunked_entry

    calls: list[list[int]] = []
    real = _chunked_entry.run_mask_chunked

    def spy(config: Any, chunks: Any, **kwargs: Any) -> Any:
        sizes: list[int] = []
        calls.append(sizes)

        def counted() -> Any:
            for chunk in chunks:
                sizes.append(chunk.num_rows)
                yield chunk

        return real(config, counted(), **kwargs)

    monkeypatch.setattr(_chunked_entry, "run_mask_chunked", spy)
    return calls


@pytest.mark.parametrize("group", [None, 7, 16, 25, 100])
@pytest.mark.parametrize("chunk", [16, 7, 33])
def test_every_row_group_layout_yields_the_twins_chunk_sizes(
    chunk: int, group: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = 100
    src = pa.table({"r": pa.array([f"s{i}" for i in range(rows)])})
    cfg, sources = _case_job([support.redact_col("r")], src, tmp_path, group)
    calls = _record_chunk_sizes(monkeypatch)
    sink = b6b.b6a.RecordingSink()
    b6b.run(cfg, sources, sink, chunk_size_rows=chunk)
    b6b.run(cfg, b6b.twin_sources(sources), b6b.b6a.RecordingSink(), chunk_size_rows=chunk)
    assert len(calls) == 2
    lazy_sizes, twin_sizes = calls
    assert lazy_sizes == twin_sizes
    assert all(s == chunk for s in lazy_sizes[:-1])
    assert sum(lazy_sizes) == rows
    assert sink.count("write_batches") == 1


def _planted_row_error_job(tmp_path: Path, bad_row: int) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = 80
    values = [f"2020-01-{1 + i % 28:02d}" for i in range(rows)]
    values[bad_row] = "not-a-date"
    column = {
        "name": "val",
        "strategy": "date_shift",
        "namespace": "d",
        "provider_config": {"min_days": -3, "max_days": 3, "date_format": "%Y-%m-%d"},
    }
    return _case_job([column], pa.table({"val": pa.array(values)}), tmp_path, 25)


def test_a_row_error_in_chunk_three_fails_like_the_twin_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support.remove_companion(monkeypatch)
    cfg, sources = _planted_row_error_job(tmp_path, bad_row=3 * support.CHUNK + 2)
    twin_sink, _tt = b6b.b6a.real_sink(tmp_path, "twin_out")
    with pytest.raises(RowErrorsFailedError) as twin:
        b6b.run(cfg, b6b.twin_sources(sources), twin_sink)
    sink, target = b6b.b6a.real_sink(tmp_path, "lazy_out")
    with pytest.raises(RowErrorsFailedError) as got:
        b6b.run(cfg, sources, sink)
    assert [(r.table, r.column, r.trigger) for r in got.value.records] == [
        (r.table, r.column, r.trigger) for r in twin.value.records
    ]
    assert str(got.value) == str(twin.value)
    assert sink.calls == twin_sink.calls
    assert sink.count("abort") == 1 and sink.count("commit") == 0
    assert not target.exists()
    assert b6b.b6a.leftovers(tmp_path) == []


# ---------------------------------------------------------------------------
# Test 9: evidence.
# ---------------------------------------------------------------------------


def _native_cfg(tmp_path: Path, rows: int = 100) -> tuple[dict[str, Any], dict[str, Any]]:
    src = pa.table({"r": pa.array([f"s{i}" for i in range(rows)])})
    return _case_job([support.redact_col("r")], src, tmp_path, 30)


def test_the_input_block_has_exactly_the_documented_keys(tmp_path: Path) -> None:
    cfg, sources = _native_cfg(tmp_path)
    got = b6b.run(cfg, sources, b6b.b6a.RecordingSink(), chunk_size_rows=16)
    block = got.quality_metrics["auto_chunk"]["input"]
    assert set(block) == {"mode", "reason", "source_row_groups", "source_max_row_group_rows"}
    assert block["mode"] == "lazy"
    assert (block["source_row_groups"], block["source_max_row_group_rows"]) == (4, 30)
    resident = b6b.run(cfg, b6b.twin_sources(sources), b6b.b6a.RecordingSink(), chunk_size_rows=16)
    assert resident.quality_metrics["auto_chunk"]["input"] == {
        "mode": "resident",
        "reason": "resident_source",
    }
    assert got.quality_metrics["execution"]["loaded_fully_in_memory"] is False
    assert resident.quality_metrics["execution"]["loaded_fully_in_memory"] is True


def test_the_payload_is_json_safe_and_deterministic(tmp_path: Path) -> None:
    cfg, sources = _native_cfg(tmp_path)
    one = b6b.run(cfg, sources, b6b.b6a.RecordingSink(), chunk_size_rows=16)
    two = b6b.run(cfg, sources, b6b.b6a.RecordingSink(), chunk_size_rows=16)
    assert b6b.json_safe(b6b.mt.strip_elapsed(one.quality_metrics)) == b6b.json_safe(
        b6b.mt.strip_elapsed(two.quality_metrics)
    )
    assert b6b.mt.strip_elapsed(one.quality_metrics) == b6b.mt.strip_elapsed(two.quality_metrics)


def test_a_resident_only_caller_differs_from_b6a_only_by_the_input_block(
    tmp_path: Path,
) -> None:
    """Guarantee 7: the resident run's metrics are B6a's plus the one `input` block."""
    cfg, sources = _native_cfg(tmp_path)
    resident = b6b.twin_sources(sources)
    streamed = b6b.run(cfg, resident, b6b.b6a.RecordingSink(), chunk_size_rows=16)
    off = b6b.run(cfg, resident, None, chunk_size_rows=16, **b6b.b6a.resident_kw())
    block = streamed.quality_metrics["auto_chunk"]
    assert block.pop("input") == {"mode": "resident", "reason": "resident_source"}
    assert off.quality_metrics["auto_chunk"].pop("input") == {
        "mode": "resident",
        "reason": "resident_source",
    }
    assert streamed.quality_metrics["execution"]["loaded_fully_in_memory"] is True
    assert set(block) >= {"mode", "chunk_size_rows", "output", "lane"}
