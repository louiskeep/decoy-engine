"""S2 (engine "Finish Open-Ended Surfaces" program) THE blocker proof.

`run_pipeline`'s default (`execution_mode="auto"`) routes a relationship-
bearing pure-mask job through `run_sequential` (see `_pipeline.py`,
`_sequential_eligible`). Because that makes `run_sequential` the default FK
mask path reachable from the public entry point, the S1 honesty-pack
fail-loud/quarantine guarantee MUST hold there exactly as it holds on the
full-frame `run()` path (mirrors `tests/integration/test_when_gate_row_error_leak.py`
and `tests/integration/test_row_errors_e2e.py`, but with a two-table FK job
routed via a `ParquetTransactionalSink`).

Shape: a parent/child FK pair (`parent.id` -> `child.parent_id`, one edge, no
orphans). The PARENT has a `bucketize` column with one uncoercible cell
("badX"). Asserts:
  (a) leak closed: the raw "badX" is absent from the committed parquet output.
  (b) quarantine JSONL carries the real bad value.
  (c) the innocent row is preserved; exactly one row removed.
  (d) fail-loud BEFORE commit: with no covering quarantine trigger,
      `run_pipeline` raises `RowErrorsFailedError` and the sink's target
      directory is never published (transactional abort, nothing committed).

Note: `profile_source` profiles a table by reading `config["sources"][name]`
straight off disk (see `profile/_source.py` module docstring), so every
table needs a real backing file even though `run_pipeline`'s `sources` kwarg
carries the in-memory Arrow tables actually masked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution._pipeline import run_pipeline

_AGE = ["10", "20", "30", "40", "50", "badX"]
_IDS = ["p0", "p1", "p2", "p3", "p4", "p5"]


def _faker_col(name: str, namespace: str) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": namespace,
    }


def _write_source(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _parent_table(*, gated: bool) -> pa.Table:
    cols: dict[str, Any] = {
        "id": pa.array(_IDS, type=pa.string()),
        "age": pa.array(_AGE, type=pa.string()),
    }
    if gated:
        # dennis's exact reproduction shape: keep=[0,0,0,1,1,1], the bad cell is
        # the last GATED row; "30" is an innocent UNMATCHED (keep=0) row.
        cols["keep"] = [0, 0, 0, 1, 1, 1]
    return pa.table(cols)


def _child_table() -> pa.Table:
    return pa.table(
        {
            "id": pa.array([f"c{i}" for i in range(len(_IDS))], type=pa.string()),
            "parent_id": pa.array(_IDS, type=pa.string()),
        }
    )


def _fk_config(
    tmp_path: Path,
    *,
    gated: bool = False,
    when: str | None = None,
    quarantine: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parent_src = _write_source(tmp_path, _parent_table(gated=gated), "parent")
    child_src = _write_source(tmp_path, _child_table(), "child")

    age_col: dict[str, Any] = {
        "name": "age",
        "strategy": "bucketize",
        "provider_config": {"width": 10},
    }
    parent_columns = [_faker_col("id", "parent_ns"), age_col]
    if gated:
        age_col["when"] = when
        parent_columns.append({"name": "keep", "strategy": "passthrough"})

    cfg: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "s2-fk-sequential-leak", "seed": 42},
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
            {"name": "parent", "columns": parent_columns},
            {"name": "child", "columns": [_faker_col("parent_id", "parent_ns")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "parent_ns",
            }
        ],
    }
    if quarantine is not None:
        cfg["quarantine"] = quarantine
    return cfg


def _sources(config: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}


class TestFkSequentialLeakClosure:
    """Ungated: `age` has no `when`, so every row is a mask candidate."""

    def test_leak_closed_and_innocent_row_preserved(self, tmp_path: Path) -> None:
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _fk_config(
            tmp_path,
            quarantine={"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
        )
        sources = _sources(config)
        sink = ParquetTransactionalSink(tmp_path / "out")

        result = run_pipeline(config, sources, engine_version="0.1.0", sink=sink)

        # The sequential route was taken (auto-eligible FK pure-mask job):
        # result.outputs is empty because a sink was supplied.
        assert result.outputs == {}
        assert result.quality_metrics["execution"]["execution_mode"] == "sequential"
        assert result.quality_metrics["execution"]["route_reason"] == "pure_mask_fk"

        # (a) leak closed: "badX" absent from the committed parquet.
        out_parent = pq.read_table(tmp_path / "out" / "parent.parquet")
        out_age = out_parent.column("age").to_pylist()
        assert "badX" not in out_age

        # (c) innocent row preserved; exactly one row removed.
        assert "30" in out_age
        assert out_parent.num_rows == 5

        # (b) quarantine JSONL carries the real bad value.
        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["age"] == "badX"
        assert records[0]["_quarantine_trigger"] == "format_error"

    def test_no_quarantine_fails_loud_before_commit(self, tmp_path: Path) -> None:
        config = _fk_config(tmp_path)  # no quarantine block: format_error is uncovered
        sources = _sources(config)
        target = tmp_path / "out2"
        sink = ParquetTransactionalSink(target)

        with pytest.raises(RowErrorsFailedError) as exc_info:
            run_pipeline(config, sources, engine_version="0.1.0", sink=sink)

        # (d) fail-loud before commit: nothing published.
        assert not target.exists()
        recs = [r for r in exc_info.value.records if r.table == "parent"]
        assert len(recs) == 1
        # Full-table position of "badX" (index 5).
        assert recs[0].row_index == 5
        assert recs[0].trigger == "format_error"
        # No cell value leaks into the exception message (trap T3).
        assert "badX" not in str(exc_info.value)


class TestFkSequentialLeakClosureWhenGated:
    """Gated variant: `age` carries a `when` predicate, proving the S1
    subset-index remap (full-table row_index, not gated-subset-relative) also
    holds on the sequential path."""

    def test_leak_closed_and_innocent_row_preserved(self, tmp_path: Path) -> None:
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _fk_config(
            tmp_path,
            gated=True,
            when="keep == 1",
            quarantine={"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
        )
        sources = _sources(config)
        sink = ParquetTransactionalSink(tmp_path / "out")

        result = run_pipeline(config, sources, engine_version="0.1.0", sink=sink)
        assert result.outputs == {}

        out_parent = pq.read_table(tmp_path / "out" / "parent.parquet")
        out_age = out_parent.column("age").to_pylist()
        assert "badX" not in out_age
        # "30" is the innocent UNMATCHED (keep=0) row; must not be deleted by a
        # mis-indexed filter.
        assert "30" in out_age
        assert out_parent.num_rows == 5

        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["age"] == "badX"

    def test_no_quarantine_fails_loud_full_table_index(self, tmp_path: Path) -> None:
        config = _fk_config(tmp_path, gated=True, when="keep == 1")
        sources = _sources(config)
        target = tmp_path / "out2"
        sink = ParquetTransactionalSink(target)

        with pytest.raises(RowErrorsFailedError) as exc_info:
            run_pipeline(config, sources, engine_version="0.1.0", sink=sink)

        assert not target.exists()
        recs = [r for r in exc_info.value.records if r.table == "parent"]
        assert len(recs) == 1
        # The full-table position of "badX" is 5, not the gated-subset position 2.
        assert recs[0].row_index == 5
