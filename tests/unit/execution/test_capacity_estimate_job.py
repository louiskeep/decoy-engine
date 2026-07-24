"""`estimate_job_capacity`: end-to-end over real fixture files (T6/T7/T8
groundwork, docs/plans/2026-07-24-oom-checker-cli-v1.md).

Covers: NOT_APPLICABLE for a no-relationships config and for a job below the
out-of-core size threshold; Parquet footer-only row counts (never a full
frame read); a CSV parent table forcing UNKNOWN (R6); FIT/INSUFFICIENT
through the real budget-resolution path; and base_dir-driven (not CWD-driven)
source path resolution (R2).

`decide_execution_route`'s `out_of_core_threshold_rows` default is
5,000,000 rows, and `estimate_job_capacity` has no kwarg to override it (R2's
signature is `config_dump`/`base_dir`/`budget_bytes` only) -- the
`low_threshold` fixture monkeypatches `capacity.decide_execution_route` with
a thin wrapper that lowers it, the same trick
`test_lazy_path_route_admission.py` uses one layer up as a `run_pipeline`
kwarg, so a 40-row fixture exercises the same routing decision a
multi-million-row job would, without the data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import capacity as capacity_mod
from decoy_engine.execution.capacity import estimate_job_capacity
from decoy_engine.execution.out_of_core._memory_estimate import CapacityVerdict

_N = 40
_MIB = 1024 * 1024
_GIB = 1024 * _MIB


def _hash_col(name: str, namespace: str) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": namespace}


def _fk_relationships() -> list[dict[str, Any]]:
    return [
        {
            "parent": {"table": "parent", "columns": ["id"]},
            "children": [{"table": "child", "columns": ["parent_id"]}],
            "orphan_policy": "preserve",
            "namespace": "ns",
        }
    ]


def _parent_child_tables(n: int = _N) -> tuple[pa.Table, pa.Table]:
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
            "note": pa.array([f"secret{i}" for i in range(n)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(n)], type=pa.string()),
            "parent_id": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
        }
    )
    return parent, child


def _write_parquet(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _write_csv(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.csv"
    table.to_pandas().to_csv(p, index=False)
    return str(p)


def _ooc_config(
    tmp_path: Path,
    *,
    parent_fmt: str = "parquet",
    child_fmt: str = "parquet",
    tables: tuple[pa.Table, pa.Table] | None = None,
) -> dict[str, Any]:
    """A pure-mask FK job whose every strategy (hash keys + a redact payload)
    is in the out-of-core supported set -- the shape that auto-routes to
    out-of-core once it clears the size threshold."""
    parent, child = tables if tables is not None else _parent_child_tables()
    parent_src = (
        _write_parquet(tmp_path, parent, "parent")
        if parent_fmt == "parquet"
        else _write_csv(tmp_path, parent, "parent")
    )
    child_src = (
        _write_parquet(tmp_path, child, "child")
        if child_fmt == "parquet"
        else _write_csv(tmp_path, child, "child")
    )
    return {
        "version": 1,
        "global_settings": {"job_name": "capacity-estimate-test", "seed": 7},
        "sources": {
            "parent": {"type": "file", "path": parent_src, "format": parent_fmt},
            "child": {"type": "file", "path": child_src, "format": child_fmt},
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
            {
                "name": "parent",
                "columns": [_hash_col("id", "ns"), {"name": "note", "strategy": "redact"}],
            },
            {"name": "child", "columns": [_hash_col("cid", "cns"), _hash_col("parent_id", "ns")]},
        ],
        "relationships": _fk_relationships(),
    }


@pytest.fixture
def low_threshold(monkeypatch):
    """Lower `decide_execution_route`'s size thresholds so the 40-row fixture
    routes `out_of_core` instead of `sequential` -- mirrors
    `test_lazy_path_route_admission.py`'s `run_pipeline(out_of_core_threshold_
    rows=10)`, one layer down (`estimate_job_capacity` exposes no such kwarg
    itself, so the wrapper is patched in at the call `capacity.py` makes)."""
    real_decide = capacity_mod.decide_execution_route

    def _patched(*args, **kwargs):
        kwargs.setdefault("out_of_core_threshold_rows", 10)
        kwargs.setdefault("full_frame_reject_rows", 10)
        return real_decide(*args, **kwargs)

    monkeypatch.setattr(capacity_mod, "decide_execution_route", _patched)


class TestNotApplicable:
    def test_no_relationships(self, tmp_path: Path) -> None:
        config = {
            "version": 1,
            "global_settings": {"job_name": "no-fk", "seed": 1},
            "sources": {},
            "targets": {},
            "tables": [],
        }
        est = estimate_job_capacity(config, tmp_path)
        assert est.verdict is CapacityVerdict.NOT_APPLICABLE
        assert est.code is None

    def test_below_size_threshold_routes_sequential_not_out_of_core(self, tmp_path: Path) -> None:
        # No low_threshold fixture: at the real 5,000,000-row default, this
        # 40-row fixture is genuinely sequential-bound, not out-of-core-bound.
        config = _ooc_config(tmp_path)
        est = estimate_job_capacity(config, tmp_path)
        assert est.verdict is CapacityVerdict.NOT_APPLICABLE
        assert est.route == "sequential"


class TestParquetFooterOnly:
    def test_fit_never_materializes_a_full_frame(
        self, tmp_path: Path, low_threshold, monkeypatch
    ) -> None:
        config = _ooc_config(tmp_path)
        # T6: the row-count derivation must read the Parquet footer only.
        # `ParquetFileSource.sample_frame` uses `iter_batches`; only a
        # `to_frame()` whole-file read (never exercised on the bounded
        # profiling path this estimator uses) calls `read_table` -- a spy
        # that raises if it IS called proves that path stays cold.
        monkeypatch.setattr(
            pq, "read_table", mock.Mock(side_effect=AssertionError("must not read a full frame"))
        )
        est = estimate_job_capacity(config, tmp_path, budget_bytes=64 * _GIB)
        assert est.verdict is CapacityVerdict.FIT
        assert est.route == "out_of_core"

    def test_insufficient_on_a_tiny_budget(self, tmp_path: Path, low_threshold) -> None:
        # `resolve_ooc_memory_limit` floors any explicit budget at
        # `_MIN_BUDGET_BYTES` (64 MiB), so a large-enough parent table is
        # needed to push its floor past even that minimum cap -- a 300k-row
        # parent's floor (~81 MB) comfortably clears the ~64 MB cap a 64 MiB
        # budget resolves to for a single live instance.
        big_parent, big_child = _parent_child_tables(300_000)
        config = _ooc_config(tmp_path, tables=(big_parent, big_child))
        est = estimate_job_capacity(config, tmp_path, budget_bytes=1 * _MIB)
        assert est.verdict is CapacityVerdict.INSUFFICIENT
        assert est.code in {"out_of_core_insufficient_memory", "out_of_core_fanin_exceeds_budget"}
        assert est.needed_bytes is None or est.needed_bytes > 0


class TestCsvParentForcesUnknown:
    def test_csv_parent_row_count_is_never_trusted_for_the_floor(
        self, tmp_path: Path, low_threshold
    ) -> None:
        config = _ooc_config(tmp_path, parent_fmt="csv", child_fmt="csv")
        est = estimate_job_capacity(config, tmp_path, budget_bytes=64 * _GIB)
        assert est.verdict is CapacityVerdict.UNKNOWN
        assert "parent" in est.message


class TestBaseDirResolution:
    def test_relative_source_path_resolves_against_base_dir_not_cwd(
        self, tmp_path: Path, low_threshold
    ) -> None:
        config = _ooc_config(tmp_path)
        # Rewrite source paths to be RELATIVE (as a real pipeline YAML would
        # declare them) and confirm the estimator still finds the files via
        # base_dir, matching decoy run's own path-resolution convention (R2)
        # -- independent of whatever the test runner's CWD happens to be.
        for spec in config["sources"].values():
            spec["path"] = Path(spec["path"]).name
        est = estimate_job_capacity(config, tmp_path, budget_bytes=64 * _GIB)
        assert est.verdict is CapacityVerdict.FIT
