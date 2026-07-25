"""BLOCKER B1 remediation (Sprint 2 honesty pack, dennis review 2026-07-04).

Reproduces and pins the fix for the real leak dennis found: under a `when:`
gate, a strategy handler records `RowError.row_index` positional in the
GATED SUBSET, but the pipeline / quarantine machinery consumes it as a
FULL-TABLE position. Result before the fix: the raw uncoercible source
value shipped in the main output AND an innocent (unmatched) row was
deleted from it.

The fix remaps subset-relative indices back to full-table positions inside
`run_with_when_gate` / `run_with_when_gate_polars` before the RowError
leaves the gate. These tests assert, on BOTH substrates:
  - the uncoercible / leaked value is quarantined and ABSENT from main
    output;
  - the innocent unmatched row is NOT deleted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution._pipeline import run_pipeline


def _write_source(tmp_path: Path, table: pa.Table) -> str:
    p = tmp_path / "t.parquet"
    pq.write_table(table, p)
    return str(p)


def _bucketize_when_config(
    src_path: str, target_path: str, *, quarantine: dict[str, Any] | None = None
) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "b1-when-bucketize", "seed": 42},
        "sources": {"t": {"type": "file", "path": src_path, "format": "parquet"}},
        "targets": {"t": {"type": "file", "path": target_path, "format": "parquet"}},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": "age",
                        "strategy": "bucketize",
                        "provider_config": {"width": 10},
                        "when": "keep == 1",
                    },
                    {"name": "keep", "strategy": "passthrough"},
                ],
            }
        ],
        "relationships": [],
    }
    if quarantine is not None:
        cfg["quarantine"] = quarantine
    return cfg


def _date_shift_when_config(
    src_path: str, target_path: str, *, quarantine: dict[str, Any] | None = None
) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "b1-when-date-shift", "seed": 42},
        "sources": {"t": {"type": "file", "path": src_path, "format": "parquet"}},
        "targets": {"t": {"type": "file", "path": target_path, "format": "parquet"}},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": "dob",
                        "strategy": "date_shift",
                        "namespace": "ns1",
                        # A nonzero-offset window so a masked date is provably
                        # different from its source (rules out an accidental
                        # zero-shift pass).
                        "provider_config": {"min_days": 30, "max_days": 60},
                        "when": "keep == 1",
                    },
                    {"name": "keep", "strategy": "passthrough"},
                ],
            }
        ],
        "relationships": [],
    }
    if quarantine is not None:
        cfg["quarantine"] = quarantine
    return cfg


# dennis's exact reproduction: 6 rows, keep = [0,0,0,1,1,1], the bad cell
# ("badX") is the last GATED row; the innocent row "30" is an UNMATCHED row.
_BUCKETIZE_SRC = pa.table(
    {
        "age": pa.array(["10", "20", "30", "40", "50", "badX"], type=pa.string()),
        "keep": [0, 0, 0, 1, 1, 1],
    }
)


class TestB1WhenBucketize:
    def test_no_quarantine_fails_loud_full_table_index(self, tmp_path: Path) -> None:
        src_path = _write_source(tmp_path, _BUCKETIZE_SRC)
        config = _bucketize_when_config(src_path, str(tmp_path / "t.out.parquet"))
        result_or_raise = None
        with pytest.raises(RowErrorsFailedError) as exc_info:
            run_pipeline(config, {"t": pq.read_table(src_path)}, engine_version="0.1.0")
        # The recorded row index must be the FULL-TABLE position of "badX" (5),
        # not the subset position (2).
        assert len(exc_info.value.records) == 1
        assert exc_info.value.records[0].row_index == 5
        assert result_or_raise is None

    def test_quarantine_removes_bad_row_keeps_innocent_row(self, tmp_path: Path) -> None:
        src_path = _write_source(tmp_path, _BUCKETIZE_SRC)
        qpath = str(tmp_path / "q.jsonl")
        config = _bucketize_when_config(
            src_path,
            str(tmp_path / "t.out.parquet"),
            quarantine={"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
        )
        result = run_pipeline(config, {"t": pq.read_table(src_path)}, engine_version="0.1.0")

        out_age = result.outputs["t"].column("age").to_pylist()
        # THE LEAK TEST: the raw "badX" must NOT be in the main output.
        assert "badX" not in out_age
        # The innocent unmatched row "30" (keep==0, passed through) must still
        # be present -- it must NOT have been deleted by a mis-indexed filter.
        assert "30" in out_age
        # Exactly one row removed.
        assert result.outputs["t"].num_rows == 5
        # The quarantine file carries the real bad value.
        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["age"] == "badX"


class TestB1WhenDateShift:
    _SRC = pa.table(
        {
            "dob": pa.array(
                [
                    "2020-01-01",
                    "2020-02-02",
                    "2020-03-03",
                    "2020-04-04",
                    "2020-05-05",
                    "not-a-date",
                ],
                type=pa.string(),
            ),
            "keep": [0, 0, 0, 1, 1, 1],
        }
    )

    def test_no_quarantine_fails_loud_full_table_index(self, tmp_path: Path) -> None:
        src_path = _write_source(tmp_path, self._SRC)
        config = _date_shift_when_config(src_path, str(tmp_path / "t.out.parquet"))
        with pytest.raises(RowErrorsFailedError) as exc_info:
            run_pipeline(config, {"t": pq.read_table(src_path)}, engine_version="0.1.0")
        assert len(exc_info.value.records) == 1
        assert exc_info.value.records[0].row_index == 5

    def test_quarantine_removes_bad_row_keeps_innocent_row(self, tmp_path: Path) -> None:
        src_path = _write_source(tmp_path, self._SRC)
        qpath = str(tmp_path / "q.jsonl")
        config = _date_shift_when_config(
            src_path,
            str(tmp_path / "t.out.parquet"),
            quarantine={"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
        )
        result = run_pipeline(config, {"t": pq.read_table(src_path)}, engine_version="0.1.0")
        out_dob = result.outputs["t"].column("dob").to_pylist()
        assert "not-a-date" not in out_dob
        assert "2020-03-03" in out_dob  # innocent unmatched row kept
        assert result.outputs["t"].num_rows == 5
        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        assert records[0]["dob"] == "not-a-date"


class TestB1PolarsParity:
    """T7 parity: the polars gate must remap identically."""

    def _run_polars(self, config: dict[str, Any], sources: dict[str, pa.Table]) -> Any:
        from decoy_engine.execution.polars._polars_adapter import PolarsExecutionAdapter
        from decoy_engine.plan import compile_plan
        from decoy_engine.profile import profile_source
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships import RelationshipGraph, build_namespace_registry

        profile = profile_source(config, seed=0)
        plan = compile_plan(config, profile, decoy_engine_version="0.1.0")
        ns_registry = build_namespace_registry(config, profile)
        graph = RelationshipGraph(edges=(), ordering=())
        adapter = PolarsExecutionAdapter()
        return adapter.run(
            plan,
            sources,
            registry=get_default_registry(),
            relationship_graph=graph,
            namespace_registry=ns_registry,
        )

    def test_polars_bucketize_when_reports_full_table_index(self, tmp_path: Path) -> None:
        config = _bucketize_when_config(
            _write_source(tmp_path, _BUCKETIZE_SRC), str(tmp_path / "t.out.parquet")
        )
        result = self._run_polars(config, {"t": _BUCKETIZE_SRC})
        assert len(result.row_errors) == 1
        # Full-table position of "badX" is 5, not the subset position 2.
        assert result.row_errors[0].row_index == 5
        assert result.row_errors[0].column == "age"
