"""S5 (Sprint 2 honesty pack) row-error integration tests: full run_pipeline.

Mirrors tests/integration/test_quarantine_e2e.py's shape. Covers the D8
behavior change (GATE-1 Q3): bucketize/date_shift jobs with unparseable
non-null cells now fail loud by default, or quarantine under the
`format_error` trigger when opted in.
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


def _write_source(tmp_path: Path, table: pa.Table, name: str = "t") -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _bucketize_config(
    src_path: str, target_path: str, *, quarantine: dict[str, Any] | None = None
) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "sp2-row-errors-bucketize", "seed": 42},
        "sources": {"t": {"type": "file", "path": src_path, "format": "parquet"}},
        "targets": {"t": {"type": "file", "path": target_path, "format": "parquet"}},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}}
                ],
            }
        ],
        "relationships": [],
    }
    if quarantine is not None:
        cfg["quarantine"] = quarantine
    return cfg


def _date_shift_config(
    src_path: str, target_path: str, *, quarantine: dict[str, Any] | None = None
) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "sp2-row-errors-date-shift", "seed": 42},
        "sources": {"t": {"type": "file", "path": src_path, "format": "parquet"}},
        "targets": {"t": {"type": "file", "path": target_path, "format": "parquet"}},
        "tables": [
            {
                "name": "t",
                "columns": [{"name": "dob", "strategy": "date_shift", "namespace": "ns1"}],
            }
        ],
        "relationships": [],
    }
    if quarantine is not None:
        cfg["quarantine"] = quarantine
    return cfg


class TestBucketizeFailLoud:
    def test_no_quarantine_raises_row_errors_failed(self, tmp_path: Path) -> None:
        src = pa.table(
            {"age": pa.array(["23", "bad1", "47", "bad2", "8", "bad3"], type=pa.string())}
        )
        src_path = _write_source(tmp_path, src)
        config = _bucketize_config(src_path, str(tmp_path / "t.out.parquet"))
        sources = {"t": pq.read_table(src_path)}

        with pytest.raises(RowErrorsFailedError) as exc_info:
            run_pipeline(config, sources, engine_version="0.1.0")

        assert len(exc_info.value.records) == 3
        for rec in exc_info.value.records:
            assert rec.trigger == "format_error"
            assert rec.table == "t"
            assert rec.column == "age"
        # THE LEAK TEST: no cell values in the exception message.
        assert "bad1" not in str(exc_info.value)
        assert "bad2" not in str(exc_info.value)
        assert not (tmp_path / "t.out.parquet").exists()

    def test_quarantine_enabled_removes_bad_rows_job_succeeds(self, tmp_path: Path) -> None:
        src = pa.table(
            {"age": pa.array(["23", "bad1", "47", "bad2", "8", "bad3"], type=pa.string())}
        )
        src_path = _write_source(tmp_path, src)
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _bucketize_config(
            src_path,
            str(tmp_path / "t.out.parquet"),
            quarantine={"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
        )
        sources = {"t": pq.read_table(src_path)}

        result = run_pipeline(config, sources, engine_version="0.1.0")

        assert result.outputs["t"].num_rows == 3
        assert Path(qpath).exists()
        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        assert len(records) == 3
        assert {r["age"] for r in records} == {"bad1", "bad2", "bad3"}
        for r in records:
            assert r["_quarantine_trigger"] == "format_error"
        assert result.quality_metrics["quarantine"]["counts_by_trigger"] == {"format_error": 3}

    def test_source_null_cells_produce_no_row_errors_all_parseable_unchanged(
        self, tmp_path: Path
    ) -> None:
        """Golden pin: source-null cells stay null; all-parseable data is
        byte-identical to the pre-slice behavior."""
        src = pa.table({"age": pa.array(["23", None, "47"], type=pa.string())})
        src_path = _write_source(tmp_path, src)
        config = _bucketize_config(src_path, str(tmp_path / "t.out.parquet"))
        sources = {"t": pq.read_table(src_path)}
        result = run_pipeline(config, sources, engine_version="0.1.0")
        assert result.row_errors == ()
        assert result.outputs["t"].column("age").to_pylist() == ["20", None, "40"]

    def test_polars_substrate_parity(self, tmp_path: Path) -> None:
        """T7: the polars adapter shares the same handler; same fail-loud behavior."""
        from decoy_engine.execution.polars._polars_adapter import PolarsExecutionAdapter
        from decoy_engine.plan import compile_plan
        from decoy_engine.profile import profile_source
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships import RelationshipGraph, build_namespace_registry

        config = _bucketize_config(
            _write_source(
                tmp_path, pa.table({"age": pa.array(["23", "bad", "47"], type=pa.string())})
            ),
            str(tmp_path / "t.out.parquet"),
        )
        profile = profile_source(config, seed=0)
        plan = compile_plan(config, profile, decoy_engine_version="0.1.0")
        ns_registry = build_namespace_registry(config, profile)
        graph = RelationshipGraph(edges=(), ordering=())
        sources = {"t": pa.table({"age": pa.array(["23", "bad", "47"], type=pa.string())})}

        adapter = PolarsExecutionAdapter()
        result = adapter.run(
            plan,
            sources,
            registry=get_default_registry(),
            relationship_graph=graph,
            namespace_registry=ns_registry,
        )
        assert len(result.row_errors) == 1
        assert result.row_errors[0].column == "age"
        assert result.row_errors[0].trigger == "format_error"


class TestDateShiftFailLoud:
    def test_no_quarantine_raises_row_errors_failed(self, tmp_path: Path) -> None:
        src = pa.table({"dob": ["2020-01-01", "garbage-date", "2021-06-15"]})
        src_path = _write_source(tmp_path, src)
        config = _date_shift_config(src_path, str(tmp_path / "t.out.parquet"))
        sources = {"t": pq.read_table(src_path)}

        with pytest.raises(RowErrorsFailedError) as exc_info:
            run_pipeline(config, sources, engine_version="0.1.0")
        assert len(exc_info.value.records) == 1
        assert exc_info.value.records[0].trigger == "format_error"
        assert not (tmp_path / "t.out.parquet").exists()

    def test_quarantine_enabled_removes_bad_row_job_succeeds(self, tmp_path: Path) -> None:
        src = pa.table({"dob": ["2020-01-01", "garbage-date", "2021-06-15"]})
        src_path = _write_source(tmp_path, src)
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _date_shift_config(
            src_path,
            str(tmp_path / "t.out.parquet"),
            quarantine={"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
        )
        sources = {"t": pq.read_table(src_path)}
        result = run_pipeline(config, sources, engine_version="0.1.0")
        assert result.outputs["t"].num_rows == 2
        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        assert records[0]["dob"] == "garbage-date"


class TestMixedValidatorAndRowErrorRun:
    def test_one_combined_quarantine_file_correct_per_trigger_counts(self, tmp_path: Path) -> None:
        src = pa.table(
            {
                "age": pa.array(["23", "bad-age", "47"], type=pa.string()),
                "cc": [
                    "4111111111111111",
                    "4111111111111111",
                    "4532015112830367",
                ],  # row 2 bad luhn
            }
        )
        src_path = _write_source(tmp_path, src)
        qpath = str(tmp_path / "quarantine.jsonl")
        config: dict[str, Any] = {
            "version": 1,
            "global_settings": {"job_name": "sp2-mixed-run", "seed": 42},
            "sources": {"t": {"type": "file", "path": src_path, "format": "parquet"}},
            "targets": {
                "t": {"type": "file", "path": str(tmp_path / "t.out.parquet"), "format": "parquet"}
            },
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}},
                        {"name": "cc", "strategy": "passthrough"},
                    ],
                }
            ],
            "relationships": [],
            "validators": [{"name": "luhn", "columns": {"t": ["cc"]}}],
            "quarantine": {
                "enabled": True,
                "output_path": qpath,
                "triggers": ["validation_fail", "format_error"],
            },
        }
        sources = {"t": pq.read_table(src_path)}
        result = run_pipeline(config, sources, engine_version="0.1.0")

        assert result.outputs["t"].num_rows == 1  # only row 0 survives
        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        assert len(records) == 2  # rows 1 and 2, deduped (no overlap here)
        summary = result.quality_metrics["quarantine"]
        assert summary["counts_by_trigger"] == {"format_error": 1, "validation_fail": 1}
        assert summary["total_quarantined"] == 2


def _code_set_config(
    src_path: str, target_path: str, corpus_path: str, *, quarantine: dict[str, Any] | None = None
) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "sp2-row-errors-code-set", "seed": 42},
        "sources": {"t": {"type": "file", "path": src_path, "format": "parquet"}},
        "targets": {"t": {"type": "file", "path": target_path, "format": "parquet"}},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": "code",
                        "strategy": "code_set",
                        "provider_config": {
                            "code_set": "two_chapters",
                            "chapter_preserve": True,
                            "corpus_source": f"customer:{corpus_path}",
                            "mode": "mask",
                        },
                    }
                ],
            }
        ],
        "relationships": [],
    }
    if quarantine is not None:
        cfg["quarantine"] = quarantine
    return cfg


class TestCodeSetMaskErrorFailLoud:
    """S6 (Sprint 2 honesty pack): code_set mask_error wiring, pipeline level."""

    def _corpus(self, tmp_path: Path) -> str:
        path = tmp_path / "two_chapters.parquet"
        tbl = pa.table(
            {
                "code": pa.array(["A01", "A02", "B01", "B02"], type=pa.string()),
                "chapter": pa.array(["A", "A", "B", "B"], type=pa.string()),
            }
        )
        pq.write_table(tbl, path)
        return str(path)

    def test_no_quarantine_raises_row_errors_failed(self, tmp_path: Path) -> None:
        corpus_path = self._corpus(tmp_path)
        src = pa.table({"code": pa.array(["A01", "U07.1", "B01"], type=pa.string())})
        src_path = _write_source(tmp_path, src)
        config = _code_set_config(src_path, str(tmp_path / "t.out.parquet"), corpus_path)
        sources = {"t": pq.read_table(src_path)}

        with pytest.raises(RowErrorsFailedError) as exc_info:
            run_pipeline(config, sources, engine_version="0.1.0")
        assert len(exc_info.value.records) == 1
        assert exc_info.value.records[0].trigger == "mask_error"
        assert "U07.1" not in str(exc_info.value)
        assert not (tmp_path / "t.out.parquet").exists()

    def test_quarantine_enabled_removes_bad_row_job_succeeds(self, tmp_path: Path) -> None:
        corpus_path = self._corpus(tmp_path)
        src = pa.table({"code": pa.array(["A01", "U07.1", "B01"], type=pa.string())})
        src_path = _write_source(tmp_path, src)
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _code_set_config(
            src_path,
            str(tmp_path / "t.out.parquet"),
            corpus_path,
            quarantine={"enabled": True, "output_path": qpath, "triggers": ["mask_error"]},
        )
        sources = {"t": pq.read_table(src_path)}
        result = run_pipeline(config, sources, engine_version="0.1.0")
        assert result.outputs["t"].num_rows == 2
        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        assert records[0]["code"] == "U07.1"
        assert records[0]["_quarantine_trigger"] == "mask_error"


class TestQuarantineWriteTimingParity:
    """LOW-1 (S2 remediation guide section 8): full-frame `run_pipeline` must
    raise BEFORE writing the quarantine JSONL, matching the sequential path
    (which already raises before its single post-loop write). A job with BOTH
    a covered row-error (bucketize `format_error`, quarantine covers it) AND
    an uncovered row-error (code_set `mask_error`, NOT covered) must raise
    `RowErrorsFailedError` and publish NO quarantine JSONL at all -- not even
    a partial file for the covered remainder."""

    def _corpus(self, tmp_path: Path) -> str:
        path = tmp_path / "two_chapters.parquet"
        tbl = pa.table(
            {
                "code": pa.array(["A01", "A02", "B01", "B02"], type=pa.string()),
                "chapter": pa.array(["A", "A", "B", "B"], type=pa.string()),
            }
        )
        pq.write_table(tbl, path)
        return str(path)

    def test_mixed_covered_and_uncovered_raises_and_writes_no_jsonl(self, tmp_path: Path) -> None:
        corpus_path = self._corpus(tmp_path)
        src = pa.table(
            {
                "age": pa.array(["23", "bad-age", "47"], type=pa.string()),
                "code": pa.array(["A01", "A02", "U07.1"], type=pa.string()),
            }
        )
        src_path = _write_source(tmp_path, src)
        qpath = str(tmp_path / "quarantine.jsonl")
        config: dict[str, Any] = {
            "version": 1,
            "global_settings": {"job_name": "sp2-write-timing", "seed": 42},
            "sources": {"t": {"type": "file", "path": src_path, "format": "parquet"}},
            "targets": {
                "t": {"type": "file", "path": str(tmp_path / "t.out.parquet"), "format": "parquet"}
            },
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}},
                        {
                            "name": "code",
                            "strategy": "code_set",
                            "provider_config": {
                                "code_set": "two_chapters",
                                "chapter_preserve": True,
                                "corpus_source": f"customer:{corpus_path}",
                                "mode": "mask",
                            },
                        },
                    ],
                }
            ],
            "relationships": [],
            # Covers format_error (bucketize) only; mask_error (code_set) is
            # deliberately left uncovered.
            "quarantine": {"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
        }
        sources = {"t": pq.read_table(src_path)}

        with pytest.raises(RowErrorsFailedError) as exc_info:
            run_pipeline(config, sources, engine_version="0.1.0")

        uncovered = [r for r in exc_info.value.records if r.trigger == "mask_error"]
        assert len(uncovered) == 1
        # No partial JSONL from the covered format_error remainder: a
        # fail-loud run publishes nothing durable.
        assert not Path(qpath).exists()
        assert not (tmp_path / "t.out.parquet").exists()
