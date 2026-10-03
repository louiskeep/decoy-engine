"""Acceptance tests 3 and 10 of plan 2026-10-02-b6b-lazy-batch-input (rev 2.1): the route is
decided from Parquet footer facts, captured once and carried to the lane, and the physical
plan hashes the same facts.

These tests are written before the implementation. Do not delete one, add a skip or xfail,
or loosen a call-order assertion without a new plan gate.
"""

from __future__ import annotations

import decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._planner import _runtime_source_rejections, classify_job
from decoy_engine.profile._readers import LazySource
from tests.unit.execution import _auto_chunk_strategies as strategies
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _b6b_support as b6b
from tests.unit.execution.test_execution_planner import _planner_inputs

THRESHOLD = 10


def _col(name: str, strategy: str, **kwargs: Any) -> dict[str, Any]:
    return {"name": name, "strategy": strategy, **kwargs}


def _nulls(n: int) -> list[int | None]:
    return [None if i % 4 == 0 else i for i in range(n)]


def _bucketize(name: str, data: pa.Array) -> tuple[list[dict[str, Any]], dict[str, pa.Array]]:
    return [_col(name, "bucketize", provider_config={"width": 50})], {name: data}


N = 12

# name -> (columns, data, expected fragment of the chunked rejection or None when none is named)
GATE_CASES: dict[str, tuple[list[dict[str, Any]], dict[str, pa.Array], str | None]] = {
    "int_with_nulls": (
        [support.pass_col("x")],
        {"x": pa.array(_nulls(N), pa.int64())},
        "x (integer with nulls)",
    ),
    "int_null_free": ([support.pass_col("x")], {"x": pa.array(range(N), pa.int64())}, None),
    "bucketize_with_nulls": (
        *_bucketize("v", pa.array([None if i % 3 == 0 else i * 1.5 for i in range(N)])),
        "bucketize_source_not_null_free_numeric",
    ),
    "bucketize_non_numeric": (
        *_bucketize("v", pa.array([f"s{i}" for i in range(N)])),
        "is not numeric",
    ),
    "decimal_column": (
        [support.pass_col("x")],
        {"x": pa.array([decimal.Decimal(i) for i in range(N)], pa.decimal128(10, 2))},
        "non-chunk-stable pandas round-trip dtypes",
    ),
    "dictionary_column": (
        [support.pass_col("x")],
        {"x": pa.array([f"c{i % 3}" for i in range(N)]).dictionary_encode()},
        "non-chunk-stable pandas round-trip dtypes",
    ),
    "group_key_on_int_group": (
        [
            support.pass_col("g"),
            _col("val", "group_key", provider_config={"group_by": "g"}),
        ],
        {"g": pa.array(range(N), pa.int64()), "val": pa.array([f"x{i}" for i in range(N)])},
        None,
    ),
    "text_mask_on_int": (
        [_col("val", "text_mask", namespace="tm_ns")],
        {"val": pa.array(range(N), pa.int64())},
        "chunked_text_mask_source_dtype_unsupported",
    ),
    "code_set_on_int": (
        [_col("val", "code_set", provider_config={"code_set": "icd10"})],
        {"val": pa.array(range(N), pa.int64())},
        "chunked_code_set_source_dtype_unsupported",
    ),
    "bucket_perturb_on_int": (
        [
            _col(
                "val",
                "bucket_perturb",
                namespace="bp",
                provider_config={"bucket": "month", "date_format": "%Y-%m-%d"},
            )
        ],
        {"val": pa.array(range(N), pa.int64())},
        "chunked_bucket_perturb_source_dtype_unsupported",
    ),
}


def _classify(cfg: dict[str, Any], sources: dict[str, Any], threshold: int) -> Any:
    return classify_job(
        cfg,
        **_planner_inputs(cfg, substrate="pandas"),
        source_tables=sources,
        auto_chunk_threshold_rows=threshold,
    )


def _job(
    columns: list[dict[str, Any]], data: dict[str, pa.Array], tmp_path: Path, **kwargs: Any
) -> tuple[dict[str, Any], LazySource, pa.Table]:
    path = b6b.write(pa.table(data), tmp_path / "s.parquet", **kwargs)
    return support.make_cfg(columns, path=path), b6b.lazy(path), pq.read_table(path)


@pytest.mark.parametrize("name", sorted(GATE_CASES))
def test_classify_job_gives_a_lazy_source_the_twins_decision(name: str, tmp_path: Path) -> None:
    columns, data, fragment = GATE_CASES[name]
    cfg, lazy_src, resident = _job(columns, data, tmp_path)
    got = _classify(cfg, {b6b.TABLE: lazy_src}, THRESHOLD)
    want = _classify(cfg, {b6b.TABLE: resident}, THRESHOLD)
    assert (got.mode, got.reason, dict(got.rejections)) == (
        want.mode,
        want.reason,
        dict(want.rejections),
    )
    if fragment is not None:
        assert fragment in want.rejections["chunked"]
        assert fragment in got.rejections["chunked"]


@pytest.mark.parametrize(
    ("rows", "mode"), [(THRESHOLD - 1, "pandas_fallback"), (THRESHOLD, "chunked")]
)
def test_a_lazy_source_is_judged_against_the_threshold_like_a_table(
    rows: int, mode: str, tmp_path: Path
) -> None:
    cfg, lazy_src, resident = _job(
        [support.redact_col("r")], {"r": pa.array(["a"] * rows)}, tmp_path
    )
    got = _classify(cfg, {b6b.TABLE: lazy_src}, THRESHOLD)
    want = _classify(cfg, {b6b.TABLE: resident}, THRESHOLD)
    assert got.mode == want.mode == mode
    assert got.reason == want.reason
    assert dict(got.rejections) == dict(want.rejections)


def test_an_extra_source_frame_rejects_a_lazy_source_like_a_table(tmp_path: Path) -> None:
    cfg, lazy_src, resident = _job([support.redact_col("r")], {"r": pa.array(["a"] * N)}, tmp_path)
    extra = pa.table({"k": pa.array([1])})
    got = _classify(cfg, {b6b.TABLE: lazy_src, "extra": extra}, THRESHOLD)
    want = _classify(cfg, {b6b.TABLE: resident, "extra": extra}, THRESHOLD)
    assert "extra loaded source frame(s) extra" in want.rejections["chunked"]
    assert dict(got.rejections) == dict(want.rejections)
    assert got.mode == want.mode


@pytest.mark.parametrize("name", sorted(GATE_CASES))
def test_runtime_gates_equal_on_a_lazy_source_and_its_table(name: str, tmp_path: Path) -> None:
    columns, data, _fragment = GATE_CASES[name]
    cfg, lazy_src, resident = _job(columns, data, tmp_path)
    bucketize = [c["name"] for c in columns if c["strategy"] == "bucketize"]
    got = _runtime_source_rejections(
        {b6b.TABLE: lazy_src},
        table=b6b.TABLE,
        auto_chunk_threshold_rows=THRESHOLD,
        bucketize_columns=bucketize,
    )
    want = _runtime_source_rejections(
        {b6b.TABLE: resident},
        table=b6b.TABLE,
        auto_chunk_threshold_rows=THRESHOLD,
        bucketize_columns=bucketize,
    )
    assert got == want


def test_a_lazy_source_without_statistics_and_no_gated_column_still_routes_chunked(
    tmp_path: Path,
) -> None:
    cfg, lazy_src, _res = _job(
        [support.redact_col("r")],
        {"r": pa.array([f"s{i}" for i in range(N)])},
        tmp_path,
        write_statistics=False,
    )
    got = _classify(cfg, {b6b.TABLE: lazy_src}, THRESHOLD)
    assert got.mode == "chunked"


def test_an_integer_column_without_footer_statistics_declines_and_runs_full_frame(
    tmp_path: Path,
) -> None:
    columns = [support.redact_col("r"), support.pass_col("x")]
    data = {
        "r": pa.array([f"s{i}" for i in range(N)]),
        "x": pa.array(range(N), pa.int64()),
    }
    cfg, lazy_src, _res = _job(columns, data, tmp_path, write_statistics=False)
    got = _classify(cfg, {b6b.TABLE: lazy_src}, THRESHOLD)
    assert got.mode == "pandas_fallback"
    reason = got.rejections["chunked"]
    assert reason.startswith("lazy_source_null_count_unavailable:") or (
        "; lazy_source_null_count_unavailable:" in reason
    )
    assert "x" in reason and "table 't'" in reason
    result = b6b.run(cfg, {b6b.TABLE: lazy_src}, b6b.b6a.RecordingSink())
    block = result.quality_metrics["auto_chunk"]
    assert block["mode"] == "full_frame"
    assert "lazy_source_null_count_unavailable" in block["reason"]
    full = b6b.run(cfg, b6b.twin_sources({b6b.TABLE: lazy_src}), None, auto_chunk=False)
    assert result.outputs[b6b.TABLE].equals(full.outputs[b6b.TABLE], check_metadata=True)


def test_the_planner_reads_no_data_page_to_decide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    columns, data, _f = GATE_CASES["int_null_free"]
    cfg, lazy_src, _res = _job(columns, data, tmp_path)
    inputs = _planner_inputs(cfg, substrate="pandas")
    spies = b6b.ReadSpies(monkeypatch)
    batches: list[Any] = []
    b6b.spy_on(monkeypatch, LazySource, "iter_batches", batches)
    b6b.spy_on(monkeypatch, LazySource, "open_batches", batches)
    classify_job(
        cfg, **inputs, source_tables={b6b.TABLE: lazy_src}, auto_chunk_threshold_rows=THRESHOLD
    )
    assert spies.none_called() and batches == []


# ---------------------------------------------------------------------------
# footer_facts: one open handle.
# ---------------------------------------------------------------------------


def test_footer_facts_opens_the_file_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = pa.table(
        {"i": pa.array([1, None, 3, 4], pa.int64()), "s": pa.array(["a", "b", "c", "d"])}
    )
    path = b6b.write(table, tmp_path / "t.parquet", row_group_size=3)
    src = b6b.lazy(path)
    opened = b6b.ParquetFileSpy(monkeypatch)
    log: list[Any] = []
    b6b.spy_on(monkeypatch, pq, "read_metadata", log)
    for name in ("num_rows", "schema"):
        real = LazySource.__dict__[name]
        monkeypatch.setattr(
            LazySource,
            name,
            property(lambda self, real=real, name=name: (log.append(name), real.fget(self))[1]),
        )
    b6b.spy_on(monkeypatch, LazySource, "column_null_counts", log)
    facts = src.footer_facts()
    assert len(opened.opened) == 1
    assert log == []
    assert facts.num_rows == 4
    assert facts.schema.equals(pq.read_schema(path), check_metadata=True)
    assert dict(facts.null_counts) == {"i": 1, "s": 0}
    assert (facts.row_groups, facts.max_row_group_rows) == (2, 3)
    assert all(pf in opened.closed for pf in opened.instances)


def test_footer_facts_reports_a_missing_statistic_as_none(tmp_path: Path) -> None:
    table = pa.table({"i": pa.array([1, 2, 3], pa.int64())})
    src = b6b.lazy(b6b.write(table, tmp_path / "t.parquet", write_statistics=False))
    assert dict(src.footer_facts().null_counts) == {"i": None}


def test_footer_facts_of_a_zero_row_file_has_no_groups_worth_counting(tmp_path: Path) -> None:
    table = pa.table({"i": pa.array([], pa.int64())})
    facts = b6b.lazy(b6b.write(table, tmp_path / "t.parquet")).footer_facts()
    assert facts.num_rows == 0
    assert dict(facts.null_counts) == {"i": 0}


# ---------------------------------------------------------------------------
# Call order: one capture, the same object everywhere, no second metadata read.
# ---------------------------------------------------------------------------


def _watch_metadata(monkeypatch: pytest.MonkeyPatch, events: list[tuple[str, Any]]) -> None:
    for name in ("num_rows", "schema"):
        real = LazySource.__dict__[name]
        monkeypatch.setattr(
            LazySource,
            name,
            property(
                lambda self, real=real, name=name: (
                    events.append((name, self.path)),
                    real.fget(self),
                )[1]
            ),
        )
    for name in ("column_null_counts", "footer_facts"):
        real_fn = getattr(LazySource, name)
        monkeypatch.setattr(
            LazySource,
            name,
            lambda self, *a, real_fn=real_fn, name=name, **k: (
                events.append((name, self.path)),
                real_fn(self, *a, **k),
            )[1],
        )
    real_meta = pq.read_metadata
    monkeypatch.setattr(
        pq,
        "read_metadata",
        lambda *a, **k: (events.append(("read_metadata", a)), real_meta(*a, **k))[1],
    )
    real_pf = pq.ParquetFile

    class Spy(real_pf):  # type: ignore[valid-type, misc]
        def __init__(self, source: Any, *args: Any, **kwargs: Any) -> None:
            events.append(("ParquetFile", source))
            super().__init__(source, *args, **kwargs)

    monkeypatch.setattr(pq, "ParquetFile", Spy)


_METADATA = {
    "num_rows",
    "schema",
    "column_null_counts",
    "footer_facts",
    "read_metadata",
    "ParquetFile",
}


def _instrument(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[tuple[str, Any]], dict[str, list[Any]]]:
    mod = b6b.chunked_input()
    events: list[tuple[str, Any]] = []
    seen: dict[str, list[Any]] = {"captured": [], "open_input": [], "classify": []}
    real_capture = mod.capture_source_facts
    real_open = mod.open_input

    def capture(*args: Any, **kwargs: Any) -> Any:
        events.append(("capture", None))
        out = real_capture(*args, **kwargs)
        seen["captured"].append(out)
        return out

    def open_input(source: Any, **kwargs: Any) -> Any:
        events.append(("open_input", kwargs.get("table")))
        seen["open_input"].append(kwargs)
        return real_open(source, **kwargs)

    monkeypatch.setattr(mod, "capture_source_facts", capture)
    monkeypatch.setattr(mod, "open_input", open_input)
    from decoy_engine.execution import _planner

    real_classify = _planner.classify_job

    def classify(*args: Any, **kwargs: Any) -> Any:
        events.append(("classify", None))
        seen["classify"].append(kwargs.get("source_facts"))
        return real_classify(*args, **kwargs)

    monkeypatch.setattr(_planner, "classify_job", classify)
    _watch_metadata(monkeypatch, events)
    return events, seen


def _between(events: list[tuple[str, Any]], start: str, end: str) -> list[str]:
    names = [e[0] for e in events]
    first = names.index(start)
    last = len(names) - 1 - names[::-1].index(end) if end in names else len(names)
    return [n for n in names[first + 1 : last] if n in _METADATA]


def test_routing_captures_one_footer_snapshot_and_the_lane_gets_that_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = pa.table({"r": pa.array([f"s{i}" for i in range(100)])})
    path = b6b.write(src, tmp_path / "t.parquet", row_group_size=30)
    cfg = support.make_cfg([support.redact_col("r")], path=path)
    events, seen = _instrument(monkeypatch)
    result = b6b.run(cfg, {b6b.TABLE: b6b.lazy(path)}, b6b.b6a.RecordingSink(), chunk_size_rows=16)
    assert result.quality_metrics["auto_chunk"]["input"]["mode"] == "lazy"
    assert len(seen["captured"]) == 1, "exactly one footer capture per run"
    assert len([e for e in events if e[0] == "footer_facts"]) == 1
    snapshot = seen["captured"][0][b6b.TABLE]
    assert seen["classify"] and all(
        m is not None and m[b6b.TABLE] is snapshot for m in seen["classify"]
    )
    assert len(seen["open_input"]) == 1
    assert seen["open_input"][0]["expected"] is snapshot
    assert _between(events, "capture", "open_input") == []


def test_a_split_hands_each_dispatched_table_its_own_snapshot_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tables = {
        "a": ([support.redact_col("r")], pa.table({"r": pa.array([f"a{i}" for i in range(40)])})),
        "b": ([support.redact_col("r")], pa.table({"r": pa.array([f"b{i}" for i in range(40)])})),
    }
    cfg, resident = b6b.mt.build_job(tmp_path, tables)
    handed = {name: b6b.lazy(cfg["sources"][name]["path"]) for name in resident}
    events, seen = _instrument(monkeypatch)
    result = b6b.run(cfg, handed, b6b.b6a.RecordingSink())
    assert b6b.mt.dispatched_tables(result) == ["a", "b"]
    assert len(seen["captured"]) == 1
    snapshots = seen["captured"][0]
    assert set(snapshots) == {"a", "b"}
    by_table = {k["table"]: k["expected"] for k in seen["open_input"]}
    assert by_table["a"] is snapshots["a"] and by_table["b"] is snapshots["b"]
    assert _between(events, "capture", "open_input") == []


def test_a_split_decision_equals_the_twins_for_lazy_tables(tmp_path: Path) -> None:
    tables = {
        "big": ([support.redact_col("r")], pa.table({"r": pa.array([f"a{i}" for i in range(40)])})),
        "small": ([support.redact_col("r")], pa.table({"r": pa.array(["x"] * 5)})),
    }
    cfg, resident = b6b.mt.build_job(tmp_path, tables)
    handed = {name: b6b.lazy(cfg["sources"][name]["path"]) for name in resident}
    got = b6b.run(cfg, handed, b6b.b6a.RecordingSink())
    want = b6b.run(cfg, resident, b6b.b6a.RecordingSink())
    got_tables = {
        t["table"]: (t["dispatched"], t["reason"])
        for t in got.quality_metrics["auto_chunk"]["tables"]
    }
    want_tables = {
        t["table"]: (t["dispatched"], t["reason"])
        for t in want.quality_metrics["auto_chunk"]["tables"]
    }
    assert got_tables == want_tables
    assert got_tables["big"][0] is True and got_tables["small"][0] is False


# ---------------------------------------------------------------------------
# Test 10: physical plan.
# ---------------------------------------------------------------------------


def _plan_inputs(tmp_path: Path, table: pa.Table, **write_kwargs: Any) -> Any:
    from decoy_engine.config import PipelineConfig
    from decoy_engine.execution.physical import capture_physical_plan_inputs

    # The config always profiles one fixed file, so the profile hash is the same for every
    # capture; only the footer facts of the handed `LazySource` may differ between captures.
    profiled = b6b.write(_two_col(False, 4), tmp_path / "profiled.parquet")
    path = b6b.write(table, tmp_path / "handed.parquet", **write_kwargs)
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": profiled}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {"name": "note", "strategy": "redact"},
                        {"name": "n", "strategy": "passthrough"},
                    ],
                }
            ],
        }
    ).model_dump()
    return capture_physical_plan_inputs(
        config,
        {"t": b6b.lazy(path)},
        engine_version="unit-test",
        auto_chunk=True,
        auto_chunk_threshold_rows=2,
        chunk_size_rows=2,
    )


def _two_col(nulls: bool, rows: int = 4) -> pa.Table:
    return pa.table(
        {
            "note": pa.array([f"s{i}" for i in range(rows)]),
            "n": pa.array([None if nulls and i == 1 else i for i in range(rows)], pa.int64()),
        }
    )


def test_plan_hash_changes_with_a_lazy_gated_columns_footer_null_count(tmp_path: Path) -> None:
    clean = _plan_inputs(tmp_path, _two_col(False)).plan_hash()
    dirty = _plan_inputs(tmp_path, _two_col(True)).plan_hash()
    assert clean != dirty


def test_plan_hash_changes_with_a_lazy_sources_row_count(tmp_path: Path) -> None:
    four = _plan_inputs(tmp_path, _two_col(False, 4)).plan_hash()
    five = _plan_inputs(tmp_path, _two_col(False, 5)).plan_hash()
    assert four != five


def test_plan_hash_is_stable_for_an_unchanged_lazy_source(tmp_path: Path) -> None:
    one = _plan_inputs(tmp_path, _two_col(False)).plan_hash()
    two = _plan_inputs(tmp_path, _two_col(False)).plan_hash()
    assert one == two


def test_the_plan_layer_two_decision_equals_classify_job_for_lazy_sources(tmp_path: Path) -> None:
    from decoy_engine.execution.physical._compiler import layer2_chunk_decision
    from decoy_engine.execution.physical._inputs import thaw_config

    inputs = _plan_inputs(tmp_path, _two_col(False))
    decision, routed = layer2_chunk_decision(inputs)
    direct = classify_job(
        thaw_config(inputs.config),
        plan=inputs.plan,
        registry=inputs.registry,
        relationship_graph=inputs.graph,
        substrate=inputs.resolved_substrate,
        source_tables=inputs.caller_sources,
        auto_chunk_threshold_rows=inputs.auto_chunk_threshold_rows,
    )
    assert decision is not None
    assert (decision.mode, decision.reason, dict(decision.rejections)) == (
        direct.mode,
        direct.reason,
        dict(direct.rejections),
    )
    assert decision.mode == "chunked" and routed is True


def test_the_footer_null_count_prose_translates_to_one_stable_code() -> None:
    from decoy_engine.execution.physical import _reasons

    prose = (
        "lazy_source_null_count_unavailable: column(s) x of table 't' have no footer null "
        "count; auto-chunk declines rather than read the column to count nulls"
    )
    codes = _reasons.translate_chunked_rejection(prose)
    assert codes == (_reasons.CODE_CHUNKED_LAZY_SOURCE_NULL_COUNT_UNAVAILABLE,)
    assert _reasons.CODE_CHUNKED_LAZY_SOURCE_NULL_COUNT_UNAVAILABLE == (
        "lazy_source_null_count_unavailable"
    )


def test_strategy_fixtures_are_still_the_matrix_source() -> None:
    """Guards the shared fixture import this module relies on for gate-case construction."""
    assert "text_mask" in strategies.STRATEGY_FIXTURES
