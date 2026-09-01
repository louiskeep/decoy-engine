"""Phase 4 slice 5: `bucket_perturb` (explicit date_format) on the chunked route
(`docs/plans/2026-09-01-p4-slice5-bucket-perturb-chunked.md`).

`apply_bucket_perturb` (`transforms/bucket_perturb.py`) parses each date
string with the resolved format, snaps it to a deterministic position within
its bucket via `derive(job_seed, namespace, canonicalize(value))`, and
reformats with `strftime`. With the format fixed, this is a pure function of
`(value, job_seed, namespace, bucket, date_format)`, so per-chunk masking
reproduces whole-column masking value-for-value. `bucket_perturb` stays OUT
of `CHUNK_SAFE_STRATEGIES` (it is a CONDITIONAL strategy, gated on an
explicit `date_format`), the code_set precedent, but is simpler: no corpus
to pin, no evidence to aggregate.

This module proves:

1. Byte-identity to the pandas oracle on the real `run_pipeline(auto_
   chunk=True)` route: week/month/quarter buckets, a chunk-boundary split,
   nulls, an unparseable passthrough, a real `mask_key`, and multiple
   namespaces.
2. Byte-identity to the out-of-core Group (c) route on the shared admitted
   shape (explicit date_format, string source, no `when:`).
3. Empty-chunk parity: an empty/all-null chunk yields Arrow `null`
   (promotable by `concat_masked_chunks`), matching the oracle.
4. Every non-admitted shape (autodetect, `date_format=""`, an invalid-but-
   truthy `date_format`, a non-string source on both entries, an FK key edge
   in both orientations, `when:`, namespace-less) takes the documented
   reject / full-frame / fail-closed path with this route's OWN codes.
5. Registry load-bearing: the source-dtype collector's `build_work_list`
   call cannot run with `registry=None` when a provider-backed faker
   sibling column is present.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import run_mask_pipeline_chunked, run_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import PolarsExecutionAdapter
from decoy_engine.execution._chunked import check_chunked_compatibility, concat_masked_chunks
from decoy_engine.execution._chunked_bucket_perturb import (
    bucket_perturb_conditional_failures,
    bucket_perturb_source_columns,
    reject_bucket_perturb_fk_keys,
    reject_bucket_perturb_missing_namespace,
    reject_bucket_perturb_when,
    reject_unsafe_bucket_perturb_chunk_schema,
    unsafe_bucket_perturb_source_columns,
)
from decoy_engine.execution._errors import StrategyError
from decoy_engine.plan import PlanCompileError

_ENGINE_VERSION = "p4-slice5-bucket-perturb-test"
_LOW_THRESHOLD = 10
_SEED = 424242


def _validated_dump(cfg: dict) -> dict:
    return PipelineConfig.model_validate(cfg).model_dump()


def _config(
    tmp_path: Path,
    columns: list[dict],
    *,
    table: str = "records",
    relationships: list[dict] | None = None,
    seed: int = _SEED,
) -> dict:
    tables = [{"name": table, "columns": columns}]
    sources = {table: {"type": "file", "format": "csv", "path": str(tmp_path / f"{table}.csv")}}
    targets = {table: {"type": "file", "format": "csv", "path": str(tmp_path / f"{table}_out.csv")}}
    cfg: dict = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": sources,
        "tables": tables,
        "targets": targets,
    }
    if relationships:
        cfg["relationships"] = relationships
    return _validated_dump(cfg)


def _pa_chunks(table: pa.Table, size: int) -> list[pa.Table]:
    return [table.slice(i, size) for i in range(0, table.num_rows, size)]


def _write_csv_stub(tmp_path: Path, name: str, table: pa.Table) -> None:
    """Best-effort CSV mirror of `table` so the config's declared source path
    exists; the actual masking data always comes from the `sources=` kwarg,
    never a re-read of this file, so a lossy round-trip here is harmless."""
    try:
        table.to_pandas().to_csv(tmp_path / f"{name}.csv", index=False)
    except Exception:
        pd.DataFrame({c: [] for c in table.column_names}).to_csv(
            tmp_path / f"{name}.csv", index=False
        )


def _bucket_perturb_col(
    name: str,
    *,
    bucket: str = "month",
    date_format: str | None = "%Y-%m-%d",
    namespace: str | None = "bp",
    provider_config_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg: dict[str, Any] = {"bucket": bucket}
    if date_format is not None:
        cfg["date_format"] = date_format
    if provider_config_extra:
        cfg.update(provider_config_extra)
    col: dict[str, Any] = {"name": name, "strategy": "bucket_perturb", "provider_config": cfg}
    if namespace is not None:
        col["namespace"] = namespace
    return col


def _when_bearing_bucket_perturb_cfg(tmp_path: Path) -> dict:
    # `when` is not a validated PipelineConfig field (schema-validated configs
    # cannot carry it today), so it is stamped onto the dict AFTER validation,
    # mirroring test_code_set_chunked.py's `_when_bearing_code_set_cfg`.
    cfg = _config(tmp_path, [_bucket_perturb_col("d")])
    cfg["tables"][0]["columns"][0]["when"] = "d != ''"
    return cfg


# ---------------------------------------------------------------------------
# 1. Byte-identity across chunkings: buckets, boundary split, nulls,
#    unparseable passthrough, a real mask_key, multiple namespaces.
# ---------------------------------------------------------------------------


class TestByteParity:
    @pytest.mark.parametrize("chunk_size", [1, 3, 4, 500])
    @pytest.mark.parametrize("bucket", ["week", "month", "quarter"])
    def test_chunked_equals_full_frame_oracle(self, tmp_path, chunk_size, bucket) -> None:
        # A null every 4th row and an unparseable value every 5th, spanning
        # several chunk sizes so at least one run splits a run across a
        # boundary.
        dates = ["2021-03-15", "2020-11-02", "2019-07-28", "2022-01-09", "2023-05-19"]
        rows: list[str | None] = []
        for i in range(20):
            if i % 4 == 0:
                rows.append(None)
            elif i % 5 == 0:
                rows.append("not-a-date")
            else:
                rows.append(dates[i % len(dates)])
        table = pa.table({"d": pa.array(rows, type=pa.string())})
        columns = [_bucket_perturb_col("d", bucket=bucket, namespace="diag")]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "records", table)
        sources = {"records": table}

        auto = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=chunk_size,
        )
        forced = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, auto_chunk=False
        )
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        assert forced.quality_metrics["auto_chunk"]["mode"] == "full_frame"
        a, f = auto.outputs["records"], forced.outputs["records"]
        assert a.equals(f)
        masked = a.column("d").to_pylist()
        assert masked[0] is None  # nulls preserved
        assert "not-a-date" in masked  # unparseable passthrough, byte-identical to source
        # A real masking happened: at least one parseable value changed.
        assert any(
            m != r for m, r in zip(masked, rows, strict=True) if r is not None and r != "not-a-date"
        )

    def test_multiple_distinct_namespaces_produce_independent_output(self, tmp_path) -> None:
        raw = "2021-03-15"
        table = pa.table(
            {"a": pa.array([raw], type=pa.string()), "b": pa.array([raw], type=pa.string())}
        )
        columns = [
            _bucket_perturb_col("a", namespace="ns_a"),
            _bucket_perturb_col("b", namespace="ns_b"),
        ]
        cfg = _config(tmp_path, columns)
        out = list(
            run_mask_pipeline_chunked(
                cfg, _pa_chunks(table, 1), table="records", engine_version=_ENGINE_VERSION
            )
        )
        combined = concat_masked_chunks(out, table="records")
        # Different namespaces feed derive() different key material, so a and
        # b need not draw the same output day -- pin against the oracle
        # rather than assert inequality outright.
        _write_csv_stub(tmp_path, "records", table)
        oracle = run_pipeline(
            cfg, sources={"records": table}, engine_version=_ENGINE_VERSION, auto_chunk=False
        ).outputs["records"]
        assert combined.equals(oracle)

    @pytest.mark.parametrize("dtype", [pa.string(), pa.large_string()])
    def test_real_secret_parity_across_namespaces_and_dtypes(self, tmp_path, dtype) -> None:
        # Exercise the GA keyed path (a real SecretKeyProvider, not the pre-GA
        # job_seed fallback) across two namespaces and both string dtypes, with
        # a chunk-boundary split, asserting full Table byte-identity to the
        # oracle.
        from decoy_engine.keyprovider import SecretKeyProvider

        secret = SecretKeyProvider(b"a-strong-32B+-managed-secret-value!!", key_version="v1")
        dates = ["2021-03-15", "2020-11-02", "2019-07-28", "2022-01-09", "2023-05-19"]
        rows: list[str | None] = [None if i % 4 == 0 else dates[i % len(dates)] for i in range(20)]
        table = pa.table({"a": pa.array(rows, type=dtype), "b": pa.array(rows, type=dtype)})
        columns = [
            _bucket_perturb_col("a", namespace="ns_a"),
            _bucket_perturb_col("b", namespace="ns_b"),
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "records", table)
        sources = {"records": table}
        auto = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=3,
            key_provider=secret,
        )
        forced = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk=False,
            key_provider=secret,
        )
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        assert auto.outputs["records"].equals(forced.outputs["records"])

    def test_large_string_source_parity(self, tmp_path) -> None:
        # The admission gate accepts large_string sources, not only string;
        # pin that a large_string source is byte-identical to the oracle end
        # to end (chunked vs full-frame), not merely admitted by the collector.
        dates = ["2021-03-15", "2020-11-02", "2019-07-28", "2022-01-09", "2023-05-19"]
        rows: list[str | None] = [None if i % 4 == 0 else dates[i % len(dates)] for i in range(20)]
        table = pa.table({"d": pa.array(rows, type=pa.large_string())})
        columns = [_bucket_perturb_col("d", namespace="diag")]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "records", table)
        sources = {"records": table}
        auto = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=3,
        )
        forced = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, auto_chunk=False
        )
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        assert auto.outputs["records"].equals(forced.outputs["records"])


# ---------------------------------------------------------------------------
# 2. Byte-identity to the out-of-core Group (c) route on the shared admitted
#    shape (explicit date_format, string source, no `when:`).
# ---------------------------------------------------------------------------


class TestOutOfCoreSharedShapeParity:
    def test_chunked_oracle_and_ooc_agree_on_shared_shape(self, tmp_path: Path) -> None:
        n = 16
        dates = ["2021-03-15", "2020-11-02", "2019-07-28", "2022-01-09"]
        parent = pa.table(
            {
                "id": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
                "pay": pa.array(
                    [None if i % 5 == 0 else dates[i % len(dates)] for i in range(n)],
                    type=pa.string(),
                ),
            }
        )
        child = pa.table(
            {
                "cid": pa.array([f"c{i}" for i in range(n)], type=pa.string()),
                "pid": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
            }
        )
        for name, tbl in [("parent", parent), ("child", child)]:
            pq.write_table(tbl, tmp_path / f"{name}.parquet")
        cfg = {
            "version": 1,
            "global_settings": {"seed": 909090},
            "sources": {
                n_: {"type": "file", "path": str(tmp_path / f"{n_}.parquet"), "format": "parquet"}
                for n_ in ("parent", "child")
            },
            "targets": {
                n_: {
                    "type": "file",
                    "path": str(tmp_path / f"{n_}.out.parquet"),
                    "format": "parquet",
                }
                for n_ in ("parent", "child")
            },
            "tables": [
                {
                    "name": "parent",
                    "columns": [
                        {"name": "id", "strategy": "hash", "namespace": "kns"},
                        _bucket_perturb_col("pay", namespace="bp"),
                    ],
                },
                {
                    "name": "child",
                    "columns": [
                        {"name": "cid", "strategy": "hash", "namespace": "cns"},
                        {"name": "pid", "strategy": "hash", "namespace": "kns"},
                    ],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["pid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "kns",
                }
            ],
        }
        cfg = _validated_dump(cfg)
        sources = {"parent": parent, "child": child}

        oracle = run_pipeline(
            cfg, sources, engine_version=_ENGINE_VERSION, execution_mode="full_frame"
        )
        ooc = run_pipeline(
            cfg, sources, engine_version=_ENGINE_VERSION, execution_mode="out_of_core"
        )
        assert oracle.outputs["parent"].column("pay").equals(ooc.outputs["parent"].column("pay"))

        # "pay" is a non-key payload column, and "parent" is never a CHILD in
        # this relationship, so the chunked route admits it.
        cfg_parent_only = {k: v for k, v in cfg.items() if k != "relationships"}
        cfg_parent_only["tables"] = [t for t in cfg["tables"] if t["name"] == "parent"]
        chunked_out = list(
            run_mask_pipeline_chunked(
                cfg_parent_only,
                _pa_chunks(parent, 3),
                table="parent",
                engine_version=_ENGINE_VERSION,
            )
        )
        chunked_pay = concat_masked_chunks(chunked_out, table="parent").column("pay").to_pylist()
        oracle_pay = oracle.outputs["parent"].column("pay").to_pylist()
        ooc_pay = ooc.outputs["parent"].column("pay").to_pylist()
        assert chunked_pay == oracle_pay
        assert chunked_pay == ooc_pay


# ---------------------------------------------------------------------------
# 3. Empty-chunk parity (Task 3b): null, not double, promotable.
# ---------------------------------------------------------------------------


class TestEmptyChunkParity:
    def test_empty_chunk_mixed_with_non_empty_concatenates_to_string(self, tmp_path) -> None:
        table = pa.table(
            {
                "d": pa.array(
                    ["2021-03-15", "2020-11-02", "2019-07-28", "2022-01-09"], type=pa.string()
                )
            }
        )
        columns = [_bucket_perturb_col("d")]
        cfg = _config(tmp_path, columns)
        chunks = [table.slice(0, 0), table.slice(0, 2), table.slice(2, 2)]
        out = list(
            run_mask_pipeline_chunked(cfg, chunks, table="records", engine_version=_ENGINE_VERSION)
        )
        assert len(out) == 3
        empty_chunk_type = out[0].schema.field("d").type
        assert pa.types.is_null(empty_chunk_type), (
            f"expected the empty chunk to be Arrow null (promotable), got {empty_chunk_type}"
        )
        combined = concat_masked_chunks(out, table="records")  # must not raise
        assert combined.schema.field("d").type == pa.string()
        assert combined.num_rows == 4

    def test_all_empty_or_all_null_column_matches_oracle_null(self, tmp_path) -> None:
        columns = [_bucket_perturb_col("d")]
        cfg = _config(tmp_path, columns)
        empty = pa.table({"d": pa.array([], type=pa.string())})
        out = list(
            run_mask_pipeline_chunked(
                cfg, [empty, empty], table="records", engine_version=_ENGINE_VERSION
            )
        )
        for chunk in out:
            assert pa.types.is_null(chunk.schema.field("d").type)
        _write_csv_stub(tmp_path, "records", empty)
        oracle = run_pipeline(
            cfg, sources={"records": empty}, engine_version=_ENGINE_VERSION, auto_chunk=False
        ).outputs["records"]
        assert pa.types.is_null(oracle.schema.field("d").type)


# ---------------------------------------------------------------------------
# 4. Every non-admitted shape takes the documented reject / full-frame /
#    fail-closed path.
# ---------------------------------------------------------------------------


class TestAdmissionBoundary:
    def test_autodetect_conditional_failure(self) -> None:
        col_entry = {"strategy": "bucket_perturb", "provider_config": {"bucket": "month"}}
        failures = bucket_perturb_conditional_failures(col_entry)
        assert failures and "date_format" in failures[0]

    def test_empty_string_date_format_conditional_failure(self) -> None:
        col_entry = {
            "strategy": "bucket_perturb",
            "provider_config": {"bucket": "month", "date_format": ""},
        }
        failures = bucket_perturb_conditional_failures(col_entry)
        assert failures and "date_format" in failures[0]

    def test_explicit_date_format_is_admitted(self) -> None:
        col_entry = {
            "strategy": "bucket_perturb",
            "provider_config": {"bucket": "month", "date_format": "%Y-%m-%d"},
        }
        assert bucket_perturb_conditional_failures(col_entry) == []

    def test_autodetect_rejected_via_check_chunked_compatibility(self, tmp_path) -> None:
        columns = [_bucket_perturb_col("d", date_format=None)]
        cfg = _config(tmp_path, columns)
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="records")
        assert exc.value.code == "chunked_strategy_conditions_unmet"

    def test_empty_string_date_format_rejected_via_check_chunked_compatibility(
        self, tmp_path
    ) -> None:
        columns = [_bucket_perturb_col("d", date_format="")]
        cfg = _config(tmp_path, columns)
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="records")
        assert exc.value.code == "chunked_strategy_conditions_unmet"

    def test_invalid_but_truthy_date_format_raises_equivalently_on_both_routes(
        self, tmp_path
    ) -> None:
        # "%Q" is truthy (admission does not validate format VALIDITY, only
        # presence) but is not a real strptime directive, so pandas raises a
        # bare ValueError from strptime on both routes -- neither route
        # swallows it or treats it as a silent passthrough.
        columns = [_bucket_perturb_col("d", date_format="%Q")]
        cfg = _config(tmp_path, columns)
        table = pa.table({"d": pa.array(["2024-01-15"], type=pa.string())})
        with pytest.raises(ValueError, match="bad directive"):
            list(
                run_mask_pipeline_chunked(
                    cfg, [table], table="records", engine_version=_ENGINE_VERSION
                )
            )
        _write_csv_stub(tmp_path, "records", table)
        with pytest.raises(ValueError, match="bad directive"):
            run_pipeline(
                cfg, sources={"records": table}, engine_version=_ENGINE_VERSION, auto_chunk=False
            )

    def test_when_rejected_manual_entry(self, tmp_path) -> None:
        cfg = _when_bearing_bucket_perturb_cfg(tmp_path)
        table = pa.table({"d": pa.array(["2021-03-15", "2020-11-02"], type=pa.string())})
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg, _pa_chunks(table, 1), table="records", engine_version=_ENGINE_VERSION
                )
            )
        assert exc.value.code == "chunked_bucket_perturb_when_not_supported"

    def test_when_rejected_direct_unit(self, tmp_path) -> None:
        cfg = _when_bearing_bucket_perturb_cfg(tmp_path)
        with pytest.raises(PlanCompileError) as exc:
            reject_bucket_perturb_when(cfg["tables"][0], table="records")
        assert exc.value.code == "chunked_bucket_perturb_when_not_supported"

    def test_when_absent_does_not_raise(self, tmp_path) -> None:
        cfg = _config(tmp_path, [_bucket_perturb_col("d")])
        reject_bucket_perturb_when(cfg["tables"][0], table="records")  # must not raise

    def test_when_auto_route_falls_back_to_full_frame(self, tmp_path) -> None:
        rows = [f"2021-0{(i % 9) + 1}-15" for i in range(30)]
        table = pa.table({"d": pa.array(rows, type=pa.string())})
        cfg = _when_bearing_bucket_perturb_cfg(tmp_path)
        _write_csv_stub(tmp_path, "records", table)
        result = run_pipeline(
            cfg,
            sources={"records": table},
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=7,
        )
        assert result.quality_metrics["auto_chunk"]["mode"] == "full_frame"

    def test_non_string_source_manual_entry_raises(self, tmp_path) -> None:
        columns = [_bucket_perturb_col("d")]
        cfg = _config(tmp_path, columns)
        table = pa.table({"d": pa.array([20240101, None], type=pa.int64())})
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg, _pa_chunks(table, 1), table="records", engine_version=_ENGINE_VERSION
                )
            )
        assert exc.value.code == "chunked_bucket_perturb_source_dtype_unsupported"
        assert "d" in exc.value.message

    def test_non_string_source_later_chunk_drift_raises(self, tmp_path) -> None:
        columns = [_bucket_perturb_col("d")]
        cfg = _config(tmp_path, columns)
        chunk1 = pa.table({"d": pa.array(["2021-03-15", "2020-11-02"], type=pa.string())})
        chunk2 = pa.table({"d": pa.array([20240101, None], type=pa.int64())})
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg, [chunk1, chunk2], table="records", engine_version=_ENGINE_VERSION
                )
            )
        assert exc.value.code == "chunked_bucket_perturb_source_dtype_unsupported"

    def test_non_string_source_auto_route_falls_back_to_oracle(self, tmp_path) -> None:
        table = pa.table({"d": pa.array([1, 2, 3, 4, 5, 6], type=pa.int64())})
        columns = [_bucket_perturb_col("d")]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "records", table)
        result = run_pipeline(
            cfg,
            sources={"records": table},
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=2,
        )
        assert result.quality_metrics["auto_chunk"]["mode"] == "full_frame"

    def test_source_dtype_collector_flags_non_string_admits_string(self) -> None:
        from types import SimpleNamespace

        def node(strategy: str, column: str, table: str = "records", kind: str = "scalar"):
            return SimpleNamespace(table=table, kind=kind, strategy=strategy, columns=(column,))

        str_schema = pa.schema([("d", pa.string())])
        int_schema = pa.schema([("d", pa.int64())])
        nodes = [node("bucket_perturb", "d"), node("hash", "other")]
        assert unsafe_bucket_perturb_source_columns(nodes, str_schema, table="records") == []
        assert unsafe_bucket_perturb_source_columns(nodes, int_schema, table="records") == ["d"]
        assert (
            unsafe_bucket_perturb_source_columns(
                nodes, pa.schema([("d", pa.large_string())]), table="records"
            )
            == []
        )

    def test_reject_unsafe_bucket_perturb_chunk_schema_direct_unit(self) -> None:
        reject_unsafe_bucket_perturb_chunk_schema(pa.schema([("d", pa.string())]), ["d"], table="t")
        with pytest.raises(PlanCompileError) as exc:
            reject_unsafe_bucket_perturb_chunk_schema(
                pa.schema([("d", pa.int64())]), ["d"], table="t"
            )
        assert exc.value.code == "chunked_bucket_perturb_source_dtype_unsupported"
        assert exc.value.path == "tables.t.columns"

    def test_fk_key_parent_orientation_rejected(self, tmp_path) -> None:
        cfg = {
            "tables": [
                {"name": "parent", "columns": [_bucket_perturb_col("id")]},
                {"name": "child", "columns": [{"name": "pid", "strategy": "bucket_perturb"}]},
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["pid"]}],
                    "orphan_policy": "remap",
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="parent")
        assert exc.value.code == "chunked_bucket_perturb_fk_key_unsupported"
        assert "id" in exc.value.message

    def test_fk_key_child_orientation_rejected(self, tmp_path) -> None:
        # child declares bucket_perturb matching the parent's key strategy:
        # the PRE-EXISTING gate_fk_child_edges already rejects this
        # (bucket_perturb is not in CHUNK_SAFE_STRATEGIES), proving the child
        # orientation is closed too -- whichever gate catches it first.
        cfg = {
            "tables": [
                {"name": "parent", "columns": [_bucket_perturb_col("id")]},
                {"name": "child", "columns": [_bucket_perturb_col("pid")]},
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["pid"]}],
                    "orphan_policy": "remap",
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="child")
        assert exc.value.code in (
            "chunked_bucket_perturb_fk_key_unsupported",
            "chunked_fk_parent_strategy_not_safe",
        )

    def test_reject_bucket_perturb_fk_keys_direct_unit_both_orientations(self) -> None:
        cfg = {
            "tables": [
                {"name": "parent", "columns": [_bucket_perturb_col("id")]},
                {"name": "child", "columns": [{"name": "pid", "strategy": "hash"}]},
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["pid"]}],
                    "orphan_policy": "remap",
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            reject_bucket_perturb_fk_keys(cfg, table="parent")
        assert exc.value.code == "chunked_bucket_perturb_fk_key_unsupported"
        reject_bucket_perturb_fk_keys(cfg, table="child")  # child key is "hash": must not raise

        cfg_child = {
            "tables": [
                {"name": "parent", "columns": [{"name": "id", "strategy": "hash"}]},
                {"name": "child", "columns": [_bucket_perturb_col("pid")]},
            ],
            "relationships": cfg["relationships"],
        }
        with pytest.raises(PlanCompileError) as exc:
            reject_bucket_perturb_fk_keys(cfg_child, table="child")
        assert exc.value.code == "chunked_bucket_perturb_fk_key_unsupported"

    def test_reject_bucket_perturb_fk_keys_ignores_non_key_payload_columns(self) -> None:
        cfg = {
            "tables": [
                {
                    "name": "parent",
                    "columns": [
                        {"name": "id", "strategy": "hash"},
                        _bucket_perturb_col("pay"),
                    ],
                },
                {"name": "child", "columns": [{"name": "pid", "strategy": "hash"}]},
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["pid"]}],
                    "orphan_policy": "remap",
                }
            ],
        }
        reject_bucket_perturb_fk_keys(cfg, table="parent")  # "pay" is not a key column: no raise

    def test_reject_bucket_perturb_fk_keys_no_relationships_is_a_noop(self) -> None:
        reject_bucket_perturb_fk_keys({"tables": []}, table="records")  # must not raise

    def test_namespace_less_config_raises_identically_manual_and_oracle(self, tmp_path) -> None:
        columns = [_bucket_perturb_col("d", namespace=None)]
        cfg = _config(tmp_path, columns)
        table = pa.table({"d": pa.array(["2024-01-15"], type=pa.string())})
        with pytest.raises(StrategyError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg, [table], table="records", engine_version=_ENGINE_VERSION
                )
            )
        assert exc.value.code == "bucket_perturb_requires_namespace"
        _write_csv_stub(tmp_path, "records", table)
        with pytest.raises(StrategyError) as exc2:
            run_pipeline(
                cfg, sources={"records": table}, engine_version=_ENGINE_VERSION, auto_chunk=False
            )
        assert exc2.value.code == "bucket_perturb_requires_namespace"

    def test_namespace_less_config_raises_on_zero_chunk_input(self, tmp_path) -> None:
        # A namespace-less bucket_perturb config with a ZERO-chunk input must
        # fail closed like the oracle: the data-independent namespace check runs
        # before the empty-input return, so the misconfig is caught even when no
        # chunk is ever dispatched (it would otherwise return empty silently).
        # A namespace-less redact column ordered BEFORE the bucket_perturb one
        # (redact needs no namespace) proves the check SKIPS non-bucket_perturb
        # nodes with `continue`, not `break`: a `break` would stop at the redact
        # node and miss the offending bucket_perturb column after it.
        columns = [
            {"name": "aaa", "strategy": "redact", "provider_config": {"redact_with": "X"}},
            _bucket_perturb_col("dcol", namespace=None),
        ]
        cfg = _config(tmp_path, columns)
        with pytest.raises(StrategyError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg, iter(()), table="records", engine_version=_ENGINE_VERSION
                )
            )
        assert exc.value.code == "bucket_perturb_requires_namespace"
        assert exc.value.strategy == "bucket_perturb"  # pins the coded strategy field
        assert "dcol" in exc.value.message  # pins the offending column name (not "aaa")


# ---------------------------------------------------------------------------
# Coded-field pinning (mutation bar).
# ---------------------------------------------------------------------------


class TestRejectionFieldPinning:
    """Pin the coded fields (path, offending column name, placeholder) of the
    bucket_perturb reject gates, and the reachable control-flow guards, so a
    mutation to any of them reddens a test rather than silently surviving.
    Free-text message prose stays adjudicated as accepted-non-contract in the
    mutation ledger; the machine-consumed fields are pinned here."""

    def test_reject_bucket_perturb_when_pins_path_columns_and_placeholder(self) -> None:
        # Two offending columns: one explicitly named (a name sharing no word
        # with the message prose, so the substring check cannot pass on a
        # gutted extraction), one with no `name` key (exercises the `"?"`
        # placeholder). Sorted order puts "?" first.
        table_cfg = {
            "columns": [
                {"name": "signupdaycol", "strategy": "bucket_perturb", "when": "a > 0"},
                {"strategy": "bucket_perturb", "when": "b > 0"},
            ]
        }
        with pytest.raises(PlanCompileError) as exc:
            reject_bucket_perturb_when(table_cfg, table="recs")
        assert exc.value.code == "chunked_bucket_perturb_when_not_supported"
        assert exc.value.path == "tables.recs.columns"
        # Pins the get("name", "?") extraction, the "?" placeholder, the
        # ", " join separator, and that message is not None.
        assert "?, signupdaycol" in exc.value.message

    def test_reject_bucket_perturb_fk_keys_parent_guard_is_and_not_or(self) -> None:
        # `table` owns a bucket_perturb column whose NAME collides with a
        # DIFFERENT table's parent key. `table` is not itself in any edge, so
        # it must NOT be rejected: the parent-side guard must be
        # `parent.table == table` (an `and`->`or` mutation would flag the
        # collision and over-reject).
        cfg = {
            "tables": [
                {"name": "t", "columns": [_bucket_perturb_col("shared")]},
                {"name": "other", "columns": [_bucket_perturb_col("shared")]},
            ],
            "relationships": [
                {
                    "parent": {"table": "other", "columns": ["shared"]},
                    "children": [{"table": "otherchild", "columns": ["fk"]}],
                    "orphan_policy": "remap",
                }
            ],
        }
        reject_bucket_perturb_fk_keys(cfg, table="t")  # must not raise

    def test_reject_bucket_perturb_fk_keys_scans_past_malformed_entries(self) -> None:
        # A non-dict relationship entry (and a non-dict child) must be
        # SKIPPED, not break the scan, or a real bucket_perturb FK key after
        # it is missed.
        cfg_rel = {
            "tables": [
                {"name": "t", "columns": [_bucket_perturb_col("id")]},
                {"name": "c", "columns": [{"name": "pid", "strategy": "hash"}]},
            ],
            "relationships": [
                123,
                {
                    "parent": {"table": "t", "columns": ["id"]},
                    "children": [{"table": "c", "columns": ["pid"]}],
                    "orphan_policy": "remap",
                },
            ],
        }
        with pytest.raises(PlanCompileError):
            reject_bucket_perturb_fk_keys(cfg_rel, table="t")
        cfg_child = {
            "tables": [
                {"name": "p", "columns": [{"name": "id", "strategy": "hash"}]},
                {"name": "t", "columns": [_bucket_perturb_col("k")]},
            ],
            "relationships": [
                {
                    "parent": {"table": "p", "columns": ["id"]},
                    "children": [999, {"table": "t", "columns": ["k"]}],
                    "orphan_policy": "remap",
                }
            ],
        }
        with pytest.raises(PlanCompileError):
            reject_bucket_perturb_fk_keys(cfg_child, table="t")

    def test_reject_bucket_perturb_fk_keys_scopes_the_lookup_to_table_and_column(self) -> None:
        """`_column_strategy`'s lookup must be scoped to BOTH the correct
        table AND an exact column-name match, not "any dict column/table
        encountered first" -- loosening either scoping guard would silently
        return the WRONG strategy without ever raising."""
        cfg = {
            "tables": [
                # Listed FIRST: a table-scoping bug would find THIS "id"
                # column before ever reaching "parent"'s own.
                {"name": "unrelated", "columns": [{"name": "id", "strategy": "redact"}]},
                {
                    "name": "parent",
                    "columns": [
                        # Listed BEFORE "id": a name-scoping bug would return
                        # THIS column's strategy instead.
                        {"name": "decoy", "strategy": "redact"},
                        {"name": "id", "strategy": "bucket_perturb"},
                    ],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["pid"]}],
                    "orphan_policy": "remap",
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            reject_bucket_perturb_fk_keys(cfg, table="parent")
        assert exc.value.code == "chunked_bucket_perturb_fk_key_unsupported"
        assert "id" in exc.value.message
        assert exc.value.path == "tables.parent.columns"


# ---------------------------------------------------------------------------
# Registry load-bearing (the cross-slice lesson).
# ---------------------------------------------------------------------------


class TestRegistryLoadBearing:
    def test_registry_is_load_bearing_with_a_provider_backed_sibling(self, tmp_path) -> None:
        # A bucket_perturb column beside a provider-backed faker column:
        # build_work_list consults the registry (provider_is_composite) for
        # the faker node, so the source-column collector must thread the REAL
        # registry through -- passing None would raise on the faker node.
        # Pins that the registry argument is load-bearing here.
        from decoy_engine.execution._chunked_profile import first_chunk_profile
        from decoy_engine.plan import compile_plan
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships import RelationshipGraph

        columns = [
            _bucket_perturb_col("d", namespace="diag"),
            {"name": "nm", "strategy": "faker", "provider": "person_first_name"},
        ]
        cfg = _config(tmp_path, columns)
        table = pa.table(
            {
                "d": pa.array(["2024-01-15"], type=pa.string()),
                "nm": pa.array(["Ann"], type=pa.string()),
            }
        )
        profile = first_chunk_profile(table, table="records", engine_version=_ENGINE_VERSION)
        plan = compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION, no_profile=True)
        reg = get_default_registry()
        graph = RelationshipGraph(edges=(), ordering=())
        assert bucket_perturb_source_columns(plan, reg, graph, table="records") == ["d"]
        # The namespace pre-check ALSO calls build_work_list(plan, registry), so
        # it too is registry-load-bearing: registry=None would raise on the
        # faker node. "d" has a namespace, so it returns without raising.
        reject_bucket_perturb_missing_namespace(plan, reg, graph, table="records")

    def test_missing_namespace_check_is_scoped_to_this_table(self, tmp_path) -> None:
        # The check only inspects bucket_perturb columns ON `table`: a
        # namespace-less bucket_perturb on ANOTHER table must be ignored (that
        # table's own check catches it when it is chunked). Kills the `or`->`and`
        # table-scope mutation that would inspect other tables' nodes.
        import pyarrow.csv as pcsv

        from decoy_engine.plan import compile_plan
        from decoy_engine.profile._source import profile_source
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships import RelationshipGraph

        ta = pa.table({"d": pa.array(["2024-01-15"], type=pa.string())})
        tb = pa.table({"e": pa.array(["2024-02-20"], type=pa.string())})
        pcsv.write_csv(ta, tmp_path / "a.csv")
        pcsv.write_csv(tb, tmp_path / "b.csv")
        cfg = {
            "version": 1,
            "global_settings": {"seed": _SEED},
            "sources": {
                "a": {"type": "file", "format": "csv", "path": str(tmp_path / "a.csv")},
                "b": {"type": "file", "format": "csv", "path": str(tmp_path / "b.csv")},
            },
            "targets": {
                "a": {"type": "file", "format": "csv", "path": str(tmp_path / "a_out.csv")},
                "b": {"type": "file", "format": "csv", "path": str(tmp_path / "b_out.csv")},
            },
            "tables": [
                {"name": "a", "columns": [_bucket_perturb_col("d", namespace="bp")]},
                {"name": "b", "columns": [_bucket_perturb_col("e", namespace=None)]},
            ],
        }
        cfg = _validated_dump(cfg)
        profile = profile_source(cfg, seed=_SEED)
        plan = compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION)
        reg = get_default_registry()
        graph = RelationshipGraph(edges=(), ordering=())
        # table "a"'s bucket_perturb has a namespace; table "b"'s missing
        # namespace must NOT surface here.
        reject_bucket_perturb_missing_namespace(plan, reg, graph, table="a")  # must not raise


# ---------------------------------------------------------------------------
# Admission surfaces + cross-substrate.
# ---------------------------------------------------------------------------


class TestAdmissionSurfaces:
    def test_manual_entry_admits_bucket_perturb_job(self, tmp_path) -> None:
        table = pa.table(
            {"d": pa.array([f"2024-0{(i % 9) + 1}-15" for i in range(5)], type=pa.string())}
        )
        columns = [_bucket_perturb_col("d")]
        cfg = _config(tmp_path, columns)
        out = list(
            run_mask_pipeline_chunked(
                cfg, _pa_chunks(table, 2), table="records", engine_version=_ENGINE_VERSION
            )
        )
        assert sum(c.num_rows for c in out) == 5

    def test_auto_route_selects_chunked_mode(self, tmp_path) -> None:
        table = pa.table(
            {"d": pa.array([f"2024-0{(i % 9) + 1}-15" for i in range(30)], type=pa.string())}
        )
        columns = [_bucket_perturb_col("d")]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "records", table)
        result = run_pipeline(
            cfg,
            sources={"records": table},
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=7,
        )
        assert result.quality_metrics["auto_chunk"]["mode"] == "chunked"

    def test_cross_substrate_polars_value_equals_pandas_oracle(self, tmp_path) -> None:
        # Include a null and an unparseable value so the polars path exercises
        # the passthrough branches (both must match the pandas oracle byte-for-byte).
        raw: list[str | None] = [f"2024-0{(i % 9) + 1}-15" for i in range(20)]
        raw[3] = None
        raw[7] = "not-a-date"
        table = pa.table({"d": pa.array(raw, type=pa.string())})
        columns = [_bucket_perturb_col("d")]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "records", table)
        full = run_pipeline(
            cfg, sources={"records": table}, engine_version=_ENGINE_VERSION
        ).outputs["records"]
        polars_chunked = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg,
                    _pa_chunks(table, 6),
                    table="records",
                    engine_version=_ENGINE_VERSION,
                    adapter=PolarsExecutionAdapter(),
                )
            )
        ).combine_chunks()
        assert polars_chunked.column_names == full.column_names
        assert polars_chunked.to_pydict() == full.to_pydict()
