"""job-time estimator Slice 2: the advisory `estimate_job` primitive.

Covers the advisory contract end-to-end over real fixture files: a numeric mask
job prices (route + peak + runtime band), a string-only job without a sample is
honestly UNPRICEABLE (not a guess, not a raise), a generate job gets a rows-based
runtime band, and a broken config returns a flagged result rather than crashing.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import PipelineConfig, estimate_job

_PATHS = {"full_frame", "out_of_core", "sequential"}


def _write_parquet(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _single_table_config(tmp_path: Path, table: pa.Table, column: dict) -> dict:
    src = _write_parquet(tmp_path, table, "t")
    return {
        "version": 2,
        "global_settings": {"seed": 7},
        "sources": {"t": {"type": "file", "path": src, "format": "parquet"}},
        "targets": {
            "t": {"type": "file", "path": str(tmp_path / "t.out.parquet"), "format": "parquet"}
        },
        "tables": [{"name": "t", "columns": [column]}],
    }


def test_numeric_mask_job_is_priceable(tmp_path: Path) -> None:
    # Fixed-width int column: priceable from the schema label alone, no sample needed.
    table = pa.table({"n": pa.array(list(range(200)), type=pa.int64())})
    cfg = _single_table_config(tmp_path, table, {"name": "n", "strategy": "redact"})
    est = estimate_job(cfg, tmp_path)
    assert est.route in _PATHS, est
    assert est.priceable is True
    assert est.peak_bytes is not None and est.peak_bytes > 0
    assert est.runtime is not None
    assert 0 < est.runtime.low_seconds <= est.runtime.high_seconds


def test_string_without_sample_is_unpriceable_not_a_raise(tmp_path: Path) -> None:
    # A variable-width column with no resident sample cannot be priced pre-run
    # (the profile carries no string-length stat) -- honest UNPRICEABLE.
    table = pa.table({"s": pa.array([f"v{i}" for i in range(200)], type=pa.string())})
    cfg = _single_table_config(tmp_path, table, {"name": "s", "strategy": "redact"})
    est = estimate_job(cfg, tmp_path)
    assert est.route in _PATHS  # route is still reported honestly
    assert est.priceable is False
    assert est.peak_bytes is None and est.runtime is None
    assert est.notes  # a reason is given


def _generate_config(tmp_path: Path, rows: int) -> dict:
    raw = {
        "version": 2,
        "global_settings": {"seed": 7},
        "sources": {},
        "targets": {
            "g": {"type": "file", "path": str(tmp_path / "g.out.parquet"), "format": "parquet"}
        },
        "tables": [
            {
                "name": "g",
                "row_count": rows,
                "generate_columns": [{"name": "x", "type": "sequence", "start": 1}],
            }
        ],
    }
    return PipelineConfig.model_validate(raw).model_dump()


def test_generate_job_gets_a_rows_based_runtime_band(tmp_path: Path) -> None:
    small = estimate_job(_generate_config(tmp_path, 1_000_000), tmp_path)
    large = estimate_job(_generate_config(tmp_path, 100_000_000), tmp_path)
    for est in (small, large):
        assert est.priceable is True, est
        assert est.peak_bytes is None  # generate-only: intentionally unpriced peak
        assert est.runtime is not None
        assert 0 < est.runtime.low_seconds <= est.runtime.high_seconds
        assert "generat" in est.runtime.basis.lower()
    assert small.runtime is not None and large.runtime is not None
    assert large.runtime.high_seconds > small.runtime.high_seconds


def test_generate_job_with_declared_zero_rows_is_priceable_zero_work(tmp_path: Path) -> None:
    # An explicit row_count of 0 is declared zero-work, distinct from an undeclared
    # count: it prices to the fixed-overhead floor band, it is not UNPRICEABLE.
    est = estimate_job(_generate_config(tmp_path, 0), tmp_path)
    assert est.priceable is True, est
    assert est.runtime is not None
    assert est.runtime.low_seconds > 0


def test_mixed_mask_and_generate_is_unpriceable(tmp_path: Path) -> None:
    table = pa.table({"n": pa.array(list(range(50)), type=pa.int64())})
    cfg = _single_table_config(tmp_path, table, {"name": "n", "strategy": "redact"})
    gen = _generate_config(tmp_path, 1_000_000)
    cfg["tables"].extend(gen["tables"])
    cfg["targets"].update(gen["targets"])
    cfg = PipelineConfig.model_validate(cfg).model_dump()
    est = estimate_job(cfg, tmp_path)
    assert est.priceable is False
    assert est.runtime is None and est.peak_bytes is None
    assert any("mixed" in n for n in est.notes)


def test_post_scan_integrity_is_forwarded_to_the_router(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _pipeline_routing as routing

    seen: list[bool] = []
    real = routing.decide_execution_route

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(bool(kwargs.get("post_scan_integrity")))
        return real(*args, **kwargs)

    monkeypatch.setattr(routing, "decide_execution_route", spy)
    table = pa.table({"n": pa.array(list(range(50)), type=pa.int64())})
    cfg = _single_table_config(tmp_path, table, {"name": "n", "strategy": "redact"})
    estimate_job(cfg, tmp_path)
    estimate_job({**cfg, "post_scan": {"integrity": True}}, tmp_path)
    assert seen == [False, True]


def test_router_rejection_returns_unknown_unpriceable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _pipeline_routing as routing
    from decoy_engine.execution._errors import ExecutionError

    captured: dict[str, Any] = {}

    def reject(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise ExecutionError(code="fk_full_frame_oom_risk_rejected", message="rejected")

    monkeypatch.setattr(routing, "decide_execution_route", reject)
    table = pa.table({"n": pa.array(list(range(50)), type=pa.int64())})
    cfg = _single_table_config(tmp_path, table, {"name": "n", "strategy": "redact"})
    before = copy.deepcopy(cfg)
    est = estimate_job(cfg, tmp_path)
    assert est.route == "unknown"
    assert est.priceable is False
    assert est.notes and "ExecutionError" in est.notes[0]
    assert captured["use_byte_estimate_routing"] is False
    assert captured["use_probe_routing"] is False
    assert cfg == before


def test_estimate_never_raises_even_when_the_error_str_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The advisory fallback names only the exception TYPE: formatting the exception
    # itself must not be able to escape the never-raises contract.
    from decoy_engine.execution import _pipeline_routing as routing

    class BrokenStrError(Exception):
        def __str__(self) -> str:
            raise RuntimeError("str boom")

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise BrokenStrError()

    monkeypatch.setattr(routing, "decide_execution_route", boom)
    table = pa.table({"n": pa.array(list(range(50)), type=pa.int64())})
    cfg = _single_table_config(tmp_path, table, {"name": "n", "strategy": "redact"})
    est = estimate_job(cfg, tmp_path)  # must not raise
    assert est.route == "unknown"
    assert est.priceable is False
    assert est.notes and "BrokenStrError" in est.notes[0]


def test_estimate_reads_a_bounded_sample_never_the_whole_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.profile import _readers

    to_frame_calls = 0
    real_to_frame = _readers.ParquetFileSource.to_frame

    def counting_to_frame(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal to_frame_calls
        to_frame_calls += 1
        return real_to_frame(self, *args, **kwargs)

    monkeypatch.setattr(_readers.ParquetFileSource, "to_frame", counting_to_frame)
    table = pa.table({"n": pa.array(list(range(30_000)), type=pa.int64())})
    cfg = _single_table_config(tmp_path, table, {"name": "n", "strategy": "redact"})
    est = estimate_job(cfg, tmp_path)
    # Prove the full-read seam is never hit directly, not merely that a hit would
    # be swallowed into priceable=False.
    assert to_frame_calls == 0
    assert est.priceable is True, est


def test_unprofilable_source_returns_unknown_never_raises(tmp_path: Path) -> None:
    # A source file that does not exist makes profiling fail; the advisory
    # estimate must return a flagged "unknown", never propagate the error.
    cfg = {
        "version": 2,
        "global_settings": {"seed": 1},
        "sources": {
            "t": {"type": "file", "path": str(tmp_path / "missing.parquet"), "format": "parquet"}
        },
        "targets": {
            "t": {"type": "file", "path": str(tmp_path / "o.parquet"), "format": "parquet"}
        },
        "tables": [{"name": "t", "columns": [{"name": "n", "strategy": "redact"}]}],
    }
    est = estimate_job(cfg, tmp_path)
    assert est.route == "unknown"
    assert est.priceable is False
    assert est.notes
