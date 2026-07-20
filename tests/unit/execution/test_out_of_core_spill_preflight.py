"""OOC-D: disk-aware out-of-core routing.

Three pieces, tested at the level each one is easiest to pin correctly:

1. `predict_ooc_spill_bytes` (`out_of_core/_spill_estimate.py`) -- calibrated
   against the measured benchmark (`docs/product/benchmarks/scaling-and-
   capacity.md`, decoy-platform repo): a 3-table parent->child->grandchild FK
   chain, 16 payload columns/table, spills ~0.11 GiB per 1M total rows (5.55
   GiB @ 50M, 11.1 GiB @ 100M). Reconstructs that exact shape as
   `TableSizeSpec`s and checks the predicted spill lands within [1.0x, 2.5x]
   of the measured figure at both calibration points -- over-predicting is
   the safe direction (see the module docstring's "SAFETY MARGIN" section),
   so the window is asymmetric on purpose.
2. The fail-closed preflight (`enforce_ooc_disk_preflight`, wired into
   `_pipeline_routing_signals.resolve_execution_route`) -- an OOC-eligible
   job with insufficient free temp disk raises a coded, actionable
   `ExecutionError` before any read/mask work starts; with sufficient disk it
   proceeds AND threads a non-`None` `temp_disk_budget_bytes` into
   `run_fk_out_of_core` (`_pipeline_route_exec.run_out_of_core_route`'s own
   OOC-D wiring, enforcing the pre-existing runtime cap on this route for the
   first time).
3. Scope: an OOC-INCOMPATIBLE job (a cross-table FK cycle) never reaches the
   preflight at all -- it hits its pre-existing `decide_execution_route`
   behavior completely unchanged, even under a hostile (near-zero free disk)
   monkeypatch, proving the gate is a pure backstop and never a rerouting
   decision.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._mem_estimate import ColumnSizeSpec, TableSizeSpec
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution.out_of_core import _budget as budget_mod
from decoy_engine.execution.out_of_core._spill_estimate import (
    SPILL_FACTOR,
    SPILL_SAFETY_MARGIN,
    default_ooc_temp_root,
    ooc_disk_spill_preflight,
    predict_ooc_spill_bytes,
)

_N = 20

_GIB = 1024 * 1024 * 1024

# ---------------------------------------------------------------------------
# 1. Calibration: predict_ooc_spill_bytes against the measured benchmark.
# ---------------------------------------------------------------------------


def _calibration_shape_tables(rows_per_table: int) -> tuple[TableSizeSpec, ...]:
    """The benchmark's exact schema (`tests/perf_fixtures/fk_relational.py`):
    parent/child/grandchild, 16 pooled 12-byte payload columns/table, plus
    short numeric-string key columns (`_keys`'s "p"/"c" + index)."""

    def payload_columns() -> list[ColumnSizeSpec]:
        return [
            ColumnSizeSpec(name=f"payload_{i:02d}", dtype="object", string_width_bytes=12.0)
            for i in range(16)
        ]

    def key(name: str) -> ColumnSizeSpec:
        return ColumnSizeSpec(name=name, dtype="object", string_width_bytes=9.0)

    parent = TableSizeSpec(
        name="parent", row_count=rows_per_table, columns=tuple([key("id"), *payload_columns()])
    )
    child = TableSizeSpec(
        name="child",
        row_count=rows_per_table,
        columns=tuple([key("id"), key("parent_id"), *payload_columns()]),
    )
    grandchild = TableSizeSpec(
        name="grandchild",
        row_count=rows_per_table,
        columns=tuple([key("child_id"), *payload_columns()]),
    )
    return (parent, child, grandchild)


class TestPredictOocSpillBytesCalibration:
    @pytest.mark.parametrize(
        "total_rows,measured_gib",
        [
            (50_000_000, 5.55),
            (100_000_000, 11.1),
        ],
    )
    def test_calibration_shape_prediction_is_within_the_asymmetric_window(
        self, total_rows: int, measured_gib: float
    ) -> None:
        tables = _calibration_shape_tables(total_rows // 3)
        predicted = predict_ooc_spill_bytes(tables)
        measured_bytes = measured_gib * _GIB
        ratio = predicted / measured_bytes
        # Over-predicting is the safe direction (mid-run disk-full vs. an
        # early clean reject); the window is asymmetric on purpose.
        assert 1.0 <= ratio <= 2.5, (
            f"predicted={predicted / _GIB:.2f} GiB, measured={measured_gib} GiB, "
            f"ratio={ratio:.3f} (expected within [1.0, 2.5])"
        )

    def test_prediction_scales_linearly_with_total_rows(self) -> None:
        small = predict_ooc_spill_bytes(_calibration_shape_tables(1_000_000))
        big = predict_ooc_spill_bytes(_calibration_shape_tables(10_000_000))
        # 10x the rows/table -> 10x the total rows -> ~10x the predicted spill
        # (raw_data_bytes is linear in rows; SPILL_FACTOR/SPILL_SAFETY_MARGIN
        # are both row-count-independent constants).
        assert big == pytest.approx(small * 10, rel=1e-6)

    def test_empty_tables_predicts_zero(self) -> None:
        assert predict_ooc_spill_bytes(()) == 0

    def test_unpriceable_column_is_priced_at_the_fallback_not_dropped(self) -> None:
        priced = TableSizeSpec(
            name="t",
            row_count=1_000,
            columns=(ColumnSizeSpec(name="payload", dtype="object", string_width_bytes=12.0),),
        )
        unpriceable = TableSizeSpec(
            name="t",
            row_count=1_000,
            columns=(ColumnSizeSpec(name="payload", dtype="object", unpriceable=True),),
        )
        # Same fallback width (12 bytes) as a KNOWN 12-byte column -> identical
        # prediction, not zero (dropping it would under-count the unsafe way).
        assert predict_ooc_spill_bytes((priced,)) == predict_ooc_spill_bytes((unpriceable,))
        assert predict_ooc_spill_bytes((unpriceable,)) > 0

    def test_constants_compose_as_documented(self) -> None:
        # SPILL_FACTOR * SPILL_SAFETY_MARGIN is applied on top of raw bytes,
        # not folded into a single opaque constant (module docstring "SAFETY
        # MARGIN"): pin both factors are independently what the module claims.
        assert pytest.approx(0.1) == SPILL_FACTOR
        assert pytest.approx(1.5) == SPILL_SAFETY_MARGIN


class TestOocDiskSpillPreflightHelper:
    def test_wraps_check_disk_spill_preflight_with_the_estimator(self, tmp_path: Path) -> None:
        tables = _calibration_shape_tables(1_000)
        result = ooc_disk_spill_preflight(tmp_path, tables)
        assert result.predicted_bytes == predict_ooc_spill_bytes(tables)
        assert result.ok is True  # trivial spill vs. real free disk

    def test_default_ooc_temp_root_matches_tempfile_gettempdir(self) -> None:
        import tempfile

        assert default_ooc_temp_root() == Path(tempfile.gettempdir())


# ---------------------------------------------------------------------------
# 2 & 3. Routing-level wiring: run_pipeline end to end.
# ---------------------------------------------------------------------------


def _write_source(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _hash_col(name: str, namespace: str) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": namespace}


def _fk_ooc_config(tmp_path: Path) -> dict[str, Any]:
    """A pure-mask FK job whose every strategy is out-of-core supported
    (hash keys), so it auto-routes to out-of-core once large / forced."""
    parent = pa.table({"id": pa.array([f"p{i}" for i in range(_N)], type=pa.string())})
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(_N)], type=pa.string()),
            "parent_id": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
        }
    )
    parent_src = _write_source(tmp_path, parent, "parent")
    child_src = _write_source(tmp_path, child, "child")
    return {
        "version": 1,
        "global_settings": {"job_name": "ooc-d-disk-preflight", "seed": 7},
        "sources": {
            "parent": {"type": "file", "path": parent_src, "format": "parquet"},
            "child": {"type": "file", "path": child_src, "format": "parquet"},
        },
        "targets": {
            "parent": {
                "type": "file",
                "path": str(tmp_path / "parent.out.parquet"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": str(tmp_path / "child.out.parquet"),
                "format": "parquet",
            },
        },
        "tables": [
            {"name": "parent", "columns": [_hash_col("id", "ns")]},
            {"name": "child", "columns": [_hash_col("cid", "cns"), _hash_col("parent_id", "ns")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ],
    }


def _cyclic_fk_config(tmp_path: Path) -> dict[str, Any]:
    """Two tables with reciprocal FKs (a -> b -> a): a cross-table cycle,
    OOC-INCOMPATIBLE (`check_out_of_core_compatibility` rejects it) --
    `decide_execution_route` never returns `"out_of_core"` for this job at
    any size, so it must never reach the disk preflight."""
    a = pa.table(
        {
            "id": pa.array([f"a{i}" for i in range(_N)], type=pa.string()),
            "ref_b": pa.array([f"b{i}" for i in range(_N)], type=pa.string()),
        }
    )
    b = pa.table(
        {
            "id": pa.array([f"b{i}" for i in range(_N)], type=pa.string()),
            "ref_a": pa.array([f"a{i}" for i in range(_N)], type=pa.string()),
        }
    )
    a_src = _write_source(tmp_path, a, "a")
    b_src = _write_source(tmp_path, b, "b")
    return {
        "version": 1,
        "global_settings": {"job_name": "ooc-d-cyclic-unaffected", "seed": 7},
        "sources": {
            "a": {"type": "file", "path": a_src, "format": "parquet"},
            "b": {"type": "file", "path": b_src, "format": "parquet"},
        },
        "targets": {
            "a": {"type": "file", "path": str(tmp_path / "a.out.parquet"), "format": "parquet"},
            "b": {"type": "file", "path": str(tmp_path / "b.out.parquet"), "format": "parquet"},
        },
        "tables": [
            {
                "name": "a",
                "columns": [_hash_col("id", "na"), _hash_col("ref_b", "nb")],
            },
            {
                "name": "b",
                "columns": [_hash_col("id", "nb"), _hash_col("ref_a", "na")],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "a", "columns": ["id"]},
                "children": [{"table": "b", "columns": ["ref_a"]}],
                "orphan_policy": "preserve",
                "namespace": "na",
            },
            {
                "parent": {"table": "b", "columns": ["id"]},
                "children": [{"table": "a", "columns": ["ref_b"]}],
                "orphan_policy": "preserve",
                "namespace": "nb",
            },
        ],
    }


def _sources(config: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}


class _TinyDiskUsage:
    """A `shutil.disk_usage` stand-in reporting near-zero free space --
    far below any predicted spill for a 20-row fixture."""

    def __init__(self) -> None:
        self.total = 1_000
        self.used = 900
        self.free = 100


class TestOocDiskPreflightBlocksOnInsufficientDisk:
    def test_forced_out_of_core_raises_actionable_error_when_disk_is_short(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _fk_ooc_config(tmp_path)
        sources = _sources(config)
        monkeypatch.setattr(budget_mod.shutil, "disk_usage", lambda path: _TinyDiskUsage())

        with pytest.raises(ExecutionError) as excinfo:
            run_pipeline(config, sources, engine_version="0.1.0", execution_mode="out_of_core")
        assert excinfo.value.code == "out_of_core_disk_preflight_insufficient"
        message = excinfo.value.message
        assert "GB free temp disk" in message
        assert "GB is available" in message
        # Total rows across both mask tables (20 parent + 20 child = 40).
        assert "40" in message

    def test_auto_large_fk_job_also_gets_the_preflight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _fk_ooc_config(tmp_path)
        sources = _sources(config)
        monkeypatch.setattr(budget_mod.shutil, "disk_usage", lambda path: _TinyDiskUsage())

        with pytest.raises(ExecutionError) as excinfo:
            run_pipeline(
                config,
                sources,
                engine_version="0.1.0",
                out_of_core_threshold_rows=10,
                use_byte_estimate_routing=False,
            )
        assert excinfo.value.code == "out_of_core_disk_preflight_insufficient"


class TestOocDiskPreflightPassesAndThreadsRuntimeBudget:
    def test_forced_out_of_core_proceeds_and_threads_temp_disk_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import decoy_engine.execution.out_of_core as ooc_pkg

        config = _fk_ooc_config(tmp_path)
        sources = _sources(config)
        captured: dict[str, Any] = {}
        real_run_fk_out_of_core = ooc_pkg.run_fk_out_of_core

        def spy(*args: object, **kwargs: object) -> object:
            captured["temp_disk_budget_bytes"] = kwargs.get("temp_disk_budget_bytes")
            return real_run_fk_out_of_core(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(ooc_pkg, "run_fk_out_of_core", spy)

        result = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="out_of_core")

        assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"
        assert "temp_disk_budget_bytes" in captured
        assert captured["temp_disk_budget_bytes"] is not None
        assert captured["temp_disk_budget_bytes"] > 0


class TestOocIncompatibleJobIsUnaffectedByThePreflight:
    def test_cyclic_fk_job_never_reaches_the_disk_preflight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even a catastrophically-insufficient disk must have NO effect on a
        # job that never routes out_of_core in the first place -- the gate
        # is a backstop, never a rerouting decision.
        config = _cyclic_fk_config(tmp_path)
        sources = _sources(config)
        monkeypatch.setattr(budget_mod.shutil, "disk_usage", lambda path: _TinyDiskUsage())

        result = run_pipeline(
            config,
            sources,
            engine_version="0.1.0",
            use_byte_estimate_routing=False,
        )
        assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"
        assert result.quality_metrics["execution"]["route_reason"] == "cross_table_cycle"

    def test_cyclic_fk_job_behavior_is_identical_with_and_without_the_hostile_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _cyclic_fk_config(tmp_path)
        sources = _sources(config)

        baseline = run_pipeline(
            config, sources, engine_version="0.1.0", use_byte_estimate_routing=False
        )
        monkeypatch.setattr(budget_mod.shutil, "disk_usage", lambda path: _TinyDiskUsage())
        under_hostile_disk = run_pipeline(
            config, sources, engine_version="0.1.0", use_byte_estimate_routing=False
        )
        assert (
            baseline.quality_metrics["execution"] == under_hostile_disk.quality_metrics["execution"]
        )
        for table in ("a", "b"):
            assert baseline.outputs[table].equals(under_hostile_disk.outputs[table])
