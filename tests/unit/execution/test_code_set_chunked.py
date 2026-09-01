"""Phase 4 slice 4: `code_set` mask mode on the chunked route
(`docs/plans/2026-09-01-p4-slice4-code-set-chunked.md`).

`code_set` mask mode (`transforms/code_set._pick_mask`) selects a corpus code
via `HMAC(derive(ctx.mask_key, namespace or "code_set", salt), value) %
candidate_count`: a pure function of (value, corpus record, mask_key,
namespace), all per-column constants except the value, so per-chunk masking
reproduces whole-column masking value-for-value. `code_set` stays OUT of
`CHUNK_SAFE_STRATEGIES` (it is a CONDITIONAL strategy, gated on mask mode +
no `chapter_preserve`) and needs a NEW admission dimension the other slices
did not: pinning ONE corpus record across every chunk, since the chunked
route builds a fresh `StrategyContext` per chunk.

This module proves:

1. Byte-identity to the pinned pandas oracle on the real
   `run_pipeline(auto_chunk=True)` route, including a chunk-boundary split,
   nulls, repeated codes, a real `mask_key`, and multiple namespaces.
2. Byte-identity to the out-of-core Group (c) route on the shared admitted
   shape (mask mode, no chapter_preserve, string source, no `when:`).
3. Corpus pinning: one resolution across all chunks; a mid-stream file swap
   continues on the pinned record; an initial `corpus_source_version`
   mismatch fails closed before streaming; a zero-row job with an invalid
   corpus fails closed (eager resolution).
4. Every non-admitted shape (gen mode, `chapter_preserve`, non-string source
   on both entries, an FK key edge in both orientations, `when:`) takes the
   documented reject / full-frame path with this route's OWN codes.
5. Empty-chunk parity: an empty chunk mixed with a non-empty string chunk
   concatenates to the string type without `chunked_schema_mismatch`.
6. Evidence aggregation: `code_set_corpora` surfaces once per column on the
   chunked/auto-chunk route, `masked_any` semantics.
"""

from __future__ import annotations

import os
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
from decoy_engine.execution._chunked_code_set import (
    aggregate_chunk_code_set_corpora,
    code_set_conditional_failures,
    code_set_source_columns,
    reject_code_set_fk_keys,
    reject_code_set_when,
    reject_unsafe_code_set_chunk_schema,
    resolve_pinned_code_set_records,
    unsafe_code_set_source_columns,
)
from decoy_engine.plan import PlanCompileError

_ENGINE_VERSION = "p4-slice4-code-set-test"
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
    extra_tables: list[dict] | None = None,
) -> dict:
    tables = [{"name": table, "columns": columns}]
    sources = {table: {"type": "file", "format": "csv", "path": str(tmp_path / f"{table}.csv")}}
    targets = {table: {"type": "file", "format": "csv", "path": str(tmp_path / f"{table}_out.csv")}}
    for extra in extra_tables or []:
        tables.append(extra)
        name = extra["name"]
        sources[name] = {"type": "file", "format": "csv", "path": str(tmp_path / f"{name}.csv")}
        targets[name] = {"type": "file", "format": "csv", "path": str(tmp_path / f"{name}_out.csv")}
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


def _write_customer_corpus(path: Path, codes: list[str], source_version: str) -> None:
    """A complete-provenance customer corpus so loads neither warn nor fail
    for an unrelated reason (mirrors test_code_set_job_pinning.py)."""
    tbl = pa.table(
        {"code": pa.array(codes, type=pa.string())},
        metadata={
            b"decoy_corpus": path.stem.encode(),
            b"source": b"Test registry",
            b"source_version": source_version.encode(),
            b"effective_date": b"2026-01-01",
            b"license": b"Proprietary",
        },
    )
    pq.write_table(tbl, str(path))


def _swap_corpus_file(path: Path, codes: list[str], source_version: str) -> None:
    """Replace the corpus at `path` and force a distinct cache identity (the
    loader keys its customer cache on (path, mtime_ns, ctime_ns, size))."""
    _write_customer_corpus(path, codes, source_version)
    distinct = 1_650_000_000  # fixed, far from any real test-run mtime
    os.utime(path, (distinct, distinct))


def _code_set_col(
    name: str,
    corpus: str,
    *,
    namespace: str | None = None,
    provider_config_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg: dict[str, Any] = {"code_set": corpus}
    if provider_config_extra:
        cfg.update(provider_config_extra)
    col: dict[str, Any] = {"name": name, "strategy": "code_set", "provider_config": cfg}
    if namespace is not None:
        col["namespace"] = namespace
    return col


def _when_bearing_code_set_cfg(tmp_path: Path) -> dict:
    # `when` is not a validated PipelineConfig field (schema-validated configs
    # cannot carry it today), so it is stamped onto the dict AFTER validation,
    # mirroring test_text_mask_chunked.py's `_when_bearing_text_mask_cfg`.
    cfg = _config(tmp_path, [_code_set_col("code", "icd10")])
    cfg["tables"][0]["columns"][0]["when"] = "code != ''"
    return cfg


# ---------------------------------------------------------------------------
# 1. Byte-identity across chunkings: boundary split, nulls, repeated codes,
#    a real mask_key, multiple namespaces.
# ---------------------------------------------------------------------------


class TestByteParity:
    @pytest.mark.parametrize("chunk_size", [1, 3, 4, 500])
    def test_chunked_equals_full_frame_oracle(self, tmp_path, chunk_size) -> None:
        # Repeated codes + a null every 4th row, spanning several chunk sizes
        # so at least one run splits a repeated-code run across a boundary.
        codes = ["E11.9", "I21.0", "J45.9", "N18.6"]
        rows: list[str | None] = [None if i % 4 == 0 else codes[i % len(codes)] for i in range(20)]
        table = pa.table({"code": pa.array(rows, type=pa.string())})
        columns = [_code_set_col("code", "icd10", namespace="diag")]
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
        masked = a.column("code").to_pylist()
        assert masked[0] is None and masked[4] is None  # nulls preserved
        # A real masking happened: at least one non-null value changed.
        assert any(m != r for m, r in zip(masked, rows, strict=True) if r is not None)

    def test_multiple_distinct_namespaces_produce_independent_output(self, tmp_path) -> None:
        raw = "E11.9"
        table = pa.table(
            {"a": pa.array([raw], type=pa.string()), "b": pa.array([raw], type=pa.string())}
        )
        columns = [
            _code_set_col("a", "icd10", namespace="ns_a"),
            _code_set_col("b", "icd10", namespace="ns_b"),
        ]
        cfg = _config(tmp_path, columns)
        out = list(
            run_mask_pipeline_chunked(
                cfg, _pa_chunks(table, 1), table="records", engine_version=_ENGINE_VERSION
            )
        )
        combined = concat_masked_chunks(out, table="records")
        # Different namespaces feed derive() different key material
        # (HMAC(derive(mask_key, ns, salt), value)), so a and b need not draw
        # the same output code -- pin against the oracle rather than assert
        # inequality outright, since a shared draw is possible for some seeds.
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
        # oracle. mask mode keys on the resolved mask_key, so a real secret must
        # produce the same chunked-vs-full-frame output the fallback does.
        from decoy_engine.keyprovider import SecretKeyProvider

        secret = SecretKeyProvider(b"a-strong-32B+-managed-secret-value!!", key_version="v1")
        codes = ["E11.9", "I21.0", "J45.9", "N18.6"]
        rows: list[str | None] = [None if i % 4 == 0 else codes[i % len(codes)] for i in range(20)]
        table = pa.table({"a": pa.array(rows, type=dtype), "b": pa.array(rows, type=dtype)})
        columns = [
            _code_set_col("a", "icd10", namespace="ns_a"),
            _code_set_col("b", "icd10", namespace="ns_b"),
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
            cfg, sources=sources, engine_version=_ENGINE_VERSION, auto_chunk=False, key_provider=secret
        )
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        assert auto.outputs["records"].equals(forced.outputs["records"])

    def test_large_string_source_parity(self, tmp_path) -> None:
        # The admission gate accepts large_string sources, not only string;
        # pin that a large_string source is byte-identical to the oracle end to
        # end (chunked vs full-frame), not merely admitted by the collector.
        codes = ["E11.9", "I21.0", "J45.9", "N18.6"]
        rows: list[str | None] = [None if i % 4 == 0 else codes[i % len(codes)] for i in range(20)]
        table = pa.table({"code": pa.array(rows, type=pa.large_string())})
        columns = [_code_set_col("code", "icd10", namespace="diag")]
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
#    shape (mask mode, no chapter_preserve, string source, no `when:`).
# ---------------------------------------------------------------------------


class TestOutOfCoreSharedShapeParity:
    def test_chunked_oracle_and_ooc_agree_on_shared_shape(self, tmp_path: Path) -> None:
        n = 16
        parent = pa.table(
            {
                "id": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
                "pay": pa.array(
                    [None if i % 5 == 0 else f"src-{i}" for i in range(n)], type=pa.string()
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
                        _code_set_col("pay", "mcc", namespace="cs"),
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
        # this relationship, so the chunked route admits it (relationships
        # stripped: check_chunked_compatibility's FK gates only constrain a
        # table's own key-column participation, and "parent" has none here).
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
# 3. Corpus pinning (the build's central task).
# ---------------------------------------------------------------------------


class TestCorpusPinning:
    def test_one_resolution_across_all_chunks(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "corpus.parquet"
        _write_customer_corpus(path, ["A01", "A02", "A03", "A04", "A05"], "v1")
        columns = [
            {
                "name": "code",
                "strategy": "code_set",
                "provider_config": {"code_set": "custom", "corpus_source": f"customer:{path}"},
            }
        ]
        cfg = _config(tmp_path, columns)
        table = pa.table({"code": pa.array(["A01", "A02", "A03", "A04"], type=pa.string())})

        calls = {"n": 0}
        import decoy_engine.transforms.code_set as code_set_module

        real_resolve = code_set_module.resolve_corpus_record

        def _spy(config):
            calls["n"] += 1
            return real_resolve(config)

        monkeypatch.setattr(code_set_module, "resolve_corpus_record", _spy)
        out = list(
            run_mask_pipeline_chunked(
                cfg, _pa_chunks(table, 1), table="records", engine_version=_ENGINE_VERSION
            )
        )
        assert sum(c.num_rows for c in out) == 4
        assert calls["n"] == 1, f"expected exactly one resolution, got {calls['n']}"

    def test_mid_stream_file_swap_continues_on_pinned_record(self, tmp_path) -> None:
        path = tmp_path / "corpus.parquet"
        v1_codes = ["A01", "A02", "A03", "A04", "A05"]
        _write_customer_corpus(path, v1_codes, "v1")
        columns = [
            {
                "name": "code",
                "strategy": "code_set",
                "provider_config": {"code_set": "custom", "corpus_source": f"customer:{path}"},
            }
        ]
        cfg = _config(tmp_path, columns)
        table = pa.table({"code": pa.array(["A01", "A02", "A03", "A04"], type=pa.string())})

        gen = run_mask_pipeline_chunked(
            cfg, _pa_chunks(table, 1), table="records", engine_version=_ENGINE_VERSION
        )
        first_chunk = next(gen)
        assert first_chunk.column("code")[0].as_py() in v1_codes

        # Swap the corpus file mid-stream (a new version, distinct cache key).
        _swap_corpus_file(path, ["B01", "B02", "B03", "B04", "B05"], "v2")

        # The rest of the run must NOT fail, and must continue masking off the
        # ORIGINAL (pinned) v1 corpus, never the swapped v2.
        rest = list(gen)
        all_masked = [first_chunk, *rest]
        combined = pa.concat_tables(all_masked).combine_chunks()
        masked_codes = combined.column("code").to_pylist()
        assert set(masked_codes).issubset(set(v1_codes)), (
            f"masked codes {masked_codes} left the pinned v1 set {v1_codes} -- "
            "the mid-stream swap was NOT continued on the pinned record"
        )

    def test_initial_corpus_source_version_mismatch_fails_closed_before_streaming(
        self, tmp_path
    ) -> None:
        path = tmp_path / "corpus.parquet"
        _write_customer_corpus(path, ["A01", "A02", "A03"], "v1")
        columns = [
            {
                "name": "code",
                "strategy": "code_set",
                "provider_config": {
                    "code_set": "custom",
                    "corpus_source": f"customer:{path}",
                    "corpus_source_version": "v2-does-not-match",
                },
            }
        ]
        cfg = _config(tmp_path, columns)
        table = pa.table({"code": pa.array(["A01", "A02"], type=pa.string())})
        # No `list(...)` wrapper: resolution happens in the SYNCHRONOUS body of
        # run_mask_pipeline_chunked, before any chunk streams, so the call
        # itself must raise (proving eager resolution, not lazy-on-first-chunk).
        with pytest.raises(PlanCompileError) as exc:
            run_mask_pipeline_chunked(
                cfg, _pa_chunks(table, 1), table="records", engine_version=_ENGINE_VERSION
            )
        assert exc.value.code == "code_set_corpus_version_mismatch"

    def test_zero_row_job_with_invalid_corpus_fails_closed(self, tmp_path) -> None:
        columns = [
            {
                "name": "code",
                "strategy": "code_set",
                "provider_config": {
                    "code_set": "custom",
                    "corpus_source": f"customer:{tmp_path / 'does_not_exist.parquet'}",
                },
            }
        ]
        cfg = _config(tmp_path, columns)
        with pytest.raises(PlanCompileError) as exc:
            run_mask_pipeline_chunked(
                cfg, iter(()), table="records", engine_version=_ENGINE_VERSION
            )
        assert exc.value.code == "code_set_corpus_path_not_found"

    def test_resolve_pinned_code_set_records_direct_unit(self, tmp_path) -> None:
        from decoy_engine.execution._chunked_profile import first_chunk_profile
        from decoy_engine.plan import compile_plan
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships import RelationshipGraph

        columns = [_code_set_col("code", "icd10", namespace="diag")]
        cfg = _config(tmp_path, columns)
        table = pa.table({"code": pa.array(["E11.9"], type=pa.string())})
        profile = first_chunk_profile(table, table="records", engine_version=_ENGINE_VERSION)
        plan = compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION, no_profile=True)
        reg = get_default_registry()
        graph = RelationshipGraph(edges=(), ordering=())
        records = resolve_pinned_code_set_records(plan, reg, graph, table="records")
        assert set(records) == {("records", "code")}

    def test_registry_is_load_bearing_with_a_provider_backed_sibling(self, tmp_path) -> None:
        # A code_set column beside a provider-backed faker column: build_work_list
        # consults the registry (provider_is_composite) for the faker node, so
        # both the pinning resolver and the source-column collector must thread
        # the REAL registry through -- passing None would raise on the faker
        # node. Pins that the registry argument is load-bearing here.
        from decoy_engine.execution._chunked_profile import first_chunk_profile
        from decoy_engine.plan import compile_plan
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships import RelationshipGraph

        columns = [
            _code_set_col("code", "icd10", namespace="diag"),
            {"name": "nm", "strategy": "faker", "provider": "person_first_name"},
        ]
        cfg = _config(tmp_path, columns)
        table = pa.table(
            {
                "code": pa.array(["E11.9"], type=pa.string()),
                "nm": pa.array(["Ann"], type=pa.string()),
            }
        )
        profile = first_chunk_profile(table, table="records", engine_version=_ENGINE_VERSION)
        plan = compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION, no_profile=True)
        reg = get_default_registry()
        graph = RelationshipGraph(edges=(), ordering=())
        records = resolve_pinned_code_set_records(plan, reg, graph, table="records")
        assert set(records) == {("records", "code")}
        assert code_set_source_columns(plan, reg, graph, table="records") == ["code"]

    def test_resolve_pinned_code_set_records_scopes_to_table_and_scans_past_a_skip(
        self, tmp_path
    ) -> None:
        """Two tables (proves the per-node table filter, not accidental
        single-table scoping) and, on the target table, a non-code_set column
        ordered BEFORE the code_set column (proves the skip loop uses
        `continue`, not `break` -- a `break` would stop at the first
        non-matching node and silently miss every code_set column after it)."""
        import pyarrow.csv as pcsv

        from decoy_engine.plan import compile_plan
        from decoy_engine.profile._source import profile_source
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships import RelationshipGraph

        table_a = pa.table(
            {
                "id": pa.array(["x"], type=pa.string()),
                "code": pa.array(["E11.9"], type=pa.string()),
            }
        )
        table_b = pa.table({"code": pa.array(["xyz"], type=pa.string())})
        pcsv.write_csv(table_a, tmp_path / "a.csv")
        pcsv.write_csv(table_b, tmp_path / "b.csv")
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
                {
                    "name": "a",
                    "columns": [
                        {"name": "id", "strategy": "hash"},
                        _code_set_col("code", "icd10"),
                    ],
                },
                {"name": "b", "columns": [_code_set_col("code", "mcc")]},
            ],
        }
        cfg = _validated_dump(cfg)
        profile = profile_source(cfg, seed=_SEED)
        plan = compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION)
        reg = get_default_registry()
        graph = RelationshipGraph(edges=(), ordering=())
        records_a = resolve_pinned_code_set_records(plan, reg, graph, table="a")
        records_b = resolve_pinned_code_set_records(plan, reg, graph, table="b")
        assert set(records_a) == {("a", "code")}
        assert set(records_b) == {("b", "code")}
        # Both tables' code_set column happens to share the name "code", so
        # the dict KEY alone cannot expose a leaked cross-table node -- a
        # broken table filter would silently overwrite records_a[("a",
        # "code")]'s VALUE with table b's corpus (mcc) while the key set
        # looks unchanged. Pin the actual resolved corpus identity: icd10 and
        # mcc are different shipped corpora with different row counts, and a
        # leak would make table a's entry carry table b's (mcc) row count.
        from decoy_engine.transforms.code_set import CodeSetConfig, resolve_corpus_record

        icd10_rows = len(resolve_corpus_record(CodeSetConfig.from_dict({"code_set": "icd10"})).rows)
        mcc_rows = len(resolve_corpus_record(CodeSetConfig.from_dict({"code_set": "mcc"})).rows)
        assert icd10_rows != mcc_rows, "icd10/mcc must differ for this assertion to be meaningful"
        assert len(records_a[("a", "code")].rows) == icd10_rows
        assert len(records_b[("b", "code")].rows) == mcc_rows


class TestColumnStrategyScoping:
    def test_reject_code_set_fk_keys_scopes_the_lookup_to_table_and_column(self) -> None:
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
                        {"name": "id", "strategy": "code_set"},
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
            reject_code_set_fk_keys(cfg, table="parent")
        assert exc.value.code == "chunked_code_set_fk_key_unsupported"
        assert "id" in exc.value.message
        assert exc.value.path == "tables.parent.columns"


class TestRejectionFieldPinning:
    """Pin the coded fields (path, offending column name, placeholder) of the
    code_set reject gates, and the reachable control-flow guards, so a mutation
    to any of them reddens a test rather than silently surviving. Free-text
    message prose stays adjudicated as accepted-non-contract in the mutation
    ledger; the machine-consumed fields are pinned here."""

    def test_reject_code_set_when_pins_path_columns_and_placeholder(self) -> None:
        # Two offending columns: one explicitly named (a name sharing no word
        # with the message prose, so the substring check cannot pass on a
        # gutted extraction), one with no `name` key (exercises the `"?"`
        # placeholder). Sorted order puts "?" first.
        table_cfg = {
            "columns": [
                {"name": "diagnosiscol", "strategy": "code_set", "when": "a > 0"},
                {"strategy": "code_set", "when": "b > 0"},
            ]
        }
        with pytest.raises(PlanCompileError) as exc:
            reject_code_set_when(table_cfg, table="recs")
        assert exc.value.code == "chunked_code_set_when_not_supported"
        assert exc.value.path == "tables.recs.columns"
        # Pins the get("name", "?") extraction, the "?" placeholder, the
        # ", " join separator, and that message is not None.
        assert "?, diagnosiscol" in exc.value.message

    def test_reject_code_set_fk_keys_parent_guard_is_and_not_or(self) -> None:
        # `table` owns a code_set column whose NAME collides with a DIFFERENT
        # table's parent key. `table` is not itself in any edge, so it must NOT
        # be rejected: the parent-side guard must be `parent.table == table`
        # (an `and`->`or` mutation would flag the collision and over-reject).
        cfg = {
            "tables": [
                {"name": "t", "columns": [_code_set_col("shared", "icd10")]},
                {"name": "other", "columns": [_code_set_col("shared", "icd10")]},
            ],
            "relationships": [
                {
                    "parent": {"table": "other", "columns": ["shared"]},
                    "children": [{"table": "otherchild", "columns": ["fk"]}],
                    "orphan_policy": "remap",
                }
            ],
        }
        reject_code_set_fk_keys(cfg, table="t")  # must not raise

    def test_reject_code_set_fk_keys_scans_past_malformed_entries(self) -> None:
        # A non-dict relationship entry (and a non-dict child) must be SKIPPED,
        # not break the scan, or a real code_set FK key after it is missed.
        cfg_rel = {
            "tables": [
                {"name": "t", "columns": [_code_set_col("id", "icd10")]},
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
            reject_code_set_fk_keys(cfg_rel, table="t")
        cfg_child = {
            "tables": [
                {"name": "p", "columns": [{"name": "id", "strategy": "hash"}]},
                {"name": "t", "columns": [_code_set_col("k", "icd10")]},
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
            reject_code_set_fk_keys(cfg_child, table="t")


# ---------------------------------------------------------------------------
# 4. Every non-admitted shape takes the documented reject / full-frame path.
# ---------------------------------------------------------------------------


class TestAdmissionBoundary:
    def test_gen_mode_conditional_failure(self) -> None:
        col_entry = {
            "strategy": "code_set",
            "provider_config": {"code_set": "icd10", "mode": "gen"},
        }
        failures = code_set_conditional_failures(col_entry)
        assert failures and "mode 'mask'" in failures[0]

    def test_chapter_preserve_conditional_failure(self) -> None:
        col_entry = {
            "strategy": "code_set",
            "provider_config": {"code_set": "icd10", "chapter_preserve": True},
        }
        failures = code_set_conditional_failures(col_entry)
        assert failures and "chapter_preserve" in failures[0]

    def test_mask_mode_no_chapter_preserve_is_admitted(self) -> None:
        col_entry = {"strategy": "code_set", "provider_config": {"code_set": "icd10"}}
        assert code_set_conditional_failures(col_entry) == []

    def test_gen_mode_rejected_via_check_chunked_compatibility(self, tmp_path) -> None:
        columns = [
            {
                "name": "code",
                "strategy": "code_set",
                "namespace": "ns",
                "provider_config": {"code_set": "icd10", "mode": "gen"},
            }
        ]
        cfg = _config(tmp_path, columns)
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="records")
        assert exc.value.code == "chunked_strategy_conditions_unmet"

    def test_chapter_preserve_rejected_via_check_chunked_compatibility(self, tmp_path) -> None:
        columns = [_code_set_col("code", "icd10", provider_config_extra={"chapter_preserve": True})]
        cfg = _config(tmp_path, columns)
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="records")
        assert exc.value.code == "chunked_strategy_conditions_unmet"

    def test_when_rejected_manual_entry(self, tmp_path) -> None:
        cfg = _when_bearing_code_set_cfg(tmp_path)
        table = pa.table({"code": pa.array(["E11.9", "I21.0"], type=pa.string())})
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg, _pa_chunks(table, 1), table="records", engine_version=_ENGINE_VERSION
                )
            )
        assert exc.value.code == "chunked_code_set_when_not_supported"

    def test_when_rejected_direct_unit(self, tmp_path) -> None:
        cfg = _when_bearing_code_set_cfg(tmp_path)
        with pytest.raises(PlanCompileError) as exc:
            reject_code_set_when(cfg["tables"][0], table="records")
        assert exc.value.code == "chunked_code_set_when_not_supported"

    def test_when_absent_does_not_raise(self, tmp_path) -> None:
        cfg = _config(tmp_path, [_code_set_col("code", "icd10")])
        reject_code_set_when(cfg["tables"][0], table="records")  # must not raise

    def test_when_auto_route_falls_back_to_full_frame(self, tmp_path) -> None:
        rows = [f"code-{i}" for i in range(30)]
        table = pa.table({"code": pa.array(rows, type=pa.string())})
        cfg = _when_bearing_code_set_cfg(tmp_path)
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
        columns = [_code_set_col("amount", "icd10")]
        cfg = _config(tmp_path, columns)
        table = pa.table({"amount": pa.array([1, None, 2], type=pa.int64())})
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg, _pa_chunks(table, 1), table="records", engine_version=_ENGINE_VERSION
                )
            )
        assert exc.value.code == "chunked_code_set_source_dtype_unsupported"
        assert "amount" in exc.value.message

    def test_non_string_source_later_chunk_drift_raises(self, tmp_path) -> None:
        columns = [_code_set_col("amount", "icd10")]
        cfg = _config(tmp_path, columns)
        chunk1 = pa.table({"amount": pa.array(["E11.9", "I21.0"], type=pa.string())})
        chunk2 = pa.table({"amount": pa.array([1, None, 2], type=pa.int64())})
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg, [chunk1, chunk2], table="records", engine_version=_ENGINE_VERSION
                )
            )
        assert exc.value.code == "chunked_code_set_source_dtype_unsupported"

    def test_non_string_source_auto_route_falls_back_to_oracle(self, tmp_path) -> None:
        table = pa.table({"amount": pa.array([1, 2, 3, 4, 5, 6], type=pa.int64())})
        columns = [_code_set_col("amount", "icd10")]
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

        str_schema = pa.schema([("code", pa.string())])
        int_schema = pa.schema([("code", pa.int64())])
        nodes = [node("code_set", "code"), node("hash", "other")]
        assert unsafe_code_set_source_columns(nodes, str_schema, table="records") == []
        assert unsafe_code_set_source_columns(nodes, int_schema, table="records") == ["code"]
        assert (
            unsafe_code_set_source_columns(
                nodes, pa.schema([("code", pa.large_string())]), table="records"
            )
            == []
        )

    def test_reject_unsafe_code_set_chunk_schema_direct_unit(self) -> None:
        reject_unsafe_code_set_chunk_schema(pa.schema([("code", pa.string())]), ["code"], table="t")
        with pytest.raises(PlanCompileError) as exc:
            reject_unsafe_code_set_chunk_schema(
                pa.schema([("code", pa.int64())]), ["code"], table="t"
            )
        assert exc.value.code == "chunked_code_set_source_dtype_unsupported"
        assert exc.value.path == "tables.t.columns"

    def test_fk_key_parent_orientation_rejected(self, tmp_path) -> None:
        cfg = {
            "tables": [
                {"name": "parent", "columns": [_code_set_col("id", "icd10")]},
                {"name": "child", "columns": [{"name": "pid", "strategy": "code_set"}]},
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
        assert exc.value.code == "chunked_code_set_fk_key_unsupported"
        assert "id" in exc.value.message

    def test_fk_key_child_orientation_rejected(self, tmp_path) -> None:
        # child declares code_set matching the parent's code_set strategy: the
        # PRE-EXISTING gate_fk_child_edges already rejects this (code_set is
        # not in CHUNK_SAFE_STRATEGIES), proving the child orientation is
        # closed too -- whichever gate catches it first.
        cfg = {
            "tables": [
                {"name": "parent", "columns": [_code_set_col("id", "icd10")]},
                {"name": "child", "columns": [_code_set_col("pid", "icd10")]},
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
            "chunked_code_set_fk_key_unsupported",
            "chunked_fk_parent_strategy_not_safe",
        )

    def test_reject_code_set_fk_keys_direct_unit_both_orientations(self) -> None:
        cfg = {
            "tables": [
                {"name": "parent", "columns": [_code_set_col("id", "icd10")]},
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
            reject_code_set_fk_keys(cfg, table="parent")
        assert exc.value.code == "chunked_code_set_fk_key_unsupported"
        reject_code_set_fk_keys(cfg, table="child")  # child key is "hash": must not raise

        cfg_child = {
            "tables": [
                {"name": "parent", "columns": [{"name": "id", "strategy": "hash"}]},
                {"name": "child", "columns": [_code_set_col("pid", "icd10")]},
            ],
            "relationships": cfg["relationships"],
        }
        with pytest.raises(PlanCompileError) as exc:
            reject_code_set_fk_keys(cfg_child, table="child")
        assert exc.value.code == "chunked_code_set_fk_key_unsupported"

    def test_reject_code_set_fk_keys_ignores_non_key_payload_columns(self) -> None:
        cfg = {
            "tables": [
                {
                    "name": "parent",
                    "columns": [
                        {"name": "id", "strategy": "hash"},
                        _code_set_col("pay", "icd10"),
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
        reject_code_set_fk_keys(cfg, table="parent")  # "pay" is not a key column: no raise

    def test_reject_code_set_fk_keys_no_relationships_is_a_noop(self) -> None:
        reject_code_set_fk_keys({"tables": []}, table="records")  # must not raise


# ---------------------------------------------------------------------------
# 5. Empty-chunk parity (Task 3b).
# ---------------------------------------------------------------------------


class TestEmptyChunkParity:
    def test_empty_chunk_mixed_with_non_empty_concatenates_to_string(self, tmp_path) -> None:
        table = pa.table({"code": pa.array(["E11.9", "I21.0", "J45.9", "N18.6"], type=pa.string())})
        columns = [_code_set_col("code", "icd10")]
        cfg = _config(tmp_path, columns)
        chunks = [table.slice(0, 0), table.slice(0, 2), table.slice(2, 2)]
        out = list(
            run_mask_pipeline_chunked(cfg, chunks, table="records", engine_version=_ENGINE_VERSION)
        )
        assert len(out) == 3
        for chunk in out:
            field_type = chunk.schema.field("code").type
            assert field_type == pa.string(), f"empty/non-empty chunk type drifted: {field_type}"
        combined = concat_masked_chunks(out, table="records")  # must not raise
        assert combined.schema.field("code").type == pa.string()
        assert combined.num_rows == 4

    def test_all_empty_chunks_stay_string_typed(self, tmp_path) -> None:
        empty = pa.table({"code": pa.array([], type=pa.string())})
        columns = [_code_set_col("code", "icd10")]
        cfg = _config(tmp_path, columns)
        out = list(
            run_mask_pipeline_chunked(
                cfg, [empty, empty], table="records", engine_version=_ENGINE_VERSION
            )
        )
        for chunk in out:
            assert chunk.schema.field("code").type == pa.string()


# ---------------------------------------------------------------------------
# 6. Evidence aggregation (Task 7).
# ---------------------------------------------------------------------------


class TestEvidenceAggregation:
    def test_aggregate_chunk_code_set_corpora_dedupes_once_per_column(self) -> None:
        from types import SimpleNamespace

        entry = {
            "code_set": "icd10",
            "table": "records",
            "column": "code",
            "row_count": 65,
        }
        chunk_results = [
            SimpleNamespace(quality_metrics={"code_set_corpora": [entry]}),
            SimpleNamespace(quality_metrics={"code_set_corpora": [entry]}),
            SimpleNamespace(quality_metrics={}),
        ]
        agg = aggregate_chunk_code_set_corpora(chunk_results)
        assert agg == {"code_set_corpora": [entry]}

    def test_aggregate_dedupe_key_uses_both_table_and_column(self) -> None:
        """Three entries that each differ from the other two on EXACTLY one
        of (table, column) -- collapsing either dimension to a constant (a
        wrong dict key, a typo'd key string, a case mismatch) would silently
        merge two of them into one, dropping a real evidence entry."""
        from types import SimpleNamespace

        same_col_diff_table = [
            {"code_set": "icd10", "table": "t1", "column": "code", "row_count": 10},
            {"code_set": "mcc", "table": "t2", "column": "code", "row_count": 20},
        ]
        same_table_diff_col = {
            "code_set": "icd10",
            "table": "t1",
            "column": "other",
            "row_count": 30,
        }
        chunk_results = [
            SimpleNamespace(quality_metrics={"code_set_corpora": same_col_diff_table}),
            SimpleNamespace(quality_metrics={"code_set_corpora": [same_table_diff_col]}),
        ]
        agg = aggregate_chunk_code_set_corpora(chunk_results)
        keys = {(e["table"], e["column"]) for e in agg["code_set_corpora"]}
        assert keys == {("t1", "code"), ("t2", "code"), ("t1", "other")}

    def test_aggregate_returns_empty_dict_when_no_chunk_masked_code_set(self) -> None:
        from types import SimpleNamespace

        chunk_results = [SimpleNamespace(quality_metrics={}), SimpleNamespace(quality_metrics={})]
        assert aggregate_chunk_code_set_corpora(chunk_results) == {}

    def test_auto_chunk_route_surfaces_code_set_corpora_once(self, tmp_path) -> None:
        rows = [f"src-{i}" for i in range(30)]
        table = pa.table({"code": pa.array(rows, type=pa.string())})
        columns = [_code_set_col("code", "icd10")]
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
        corpora = result.quality_metrics.get("code_set_corpora")
        assert corpora is not None
        matching = [e for e in corpora if e["table"] == "records" and e["column"] == "code"]
        assert len(matching) == 1, f"expected exactly one entry, got {matching}"
        assert matching[0]["code_set"] == "icd10"

    def test_manual_chunked_entry_aggregates_via_chunk_result_sink(self, tmp_path) -> None:
        table = pa.table({"code": pa.array(["E11.9", "I21.0", "J45.9", "N18.6"], type=pa.string())})
        columns = [_code_set_col("code", "icd10")]
        cfg = _config(tmp_path, columns)
        sink: list[Any] = []
        list(
            run_mask_pipeline_chunked(
                cfg,
                _pa_chunks(table, 1),
                table="records",
                engine_version=_ENGINE_VERSION,
                chunk_result_sink=sink,
            )
        )
        agg = aggregate_chunk_code_set_corpora(sink)
        assert agg["code_set_corpora"][0]["code_set"] == "icd10"
        matching = [
            e for e in agg["code_set_corpora"] if e["table"] == "records" and e["column"] == "code"
        ]
        assert len(matching) == 1


# ---------------------------------------------------------------------------
# Admission surfaces + cross-substrate.
# ---------------------------------------------------------------------------


class TestAdmissionSurfaces:
    def test_manual_entry_admits_code_set_job(self, tmp_path) -> None:
        table = pa.table({"code": pa.array([f"src-{i}" for i in range(5)], type=pa.string())})
        columns = [_code_set_col("code", "icd10")]
        cfg = _config(tmp_path, columns)
        out = list(
            run_mask_pipeline_chunked(
                cfg, _pa_chunks(table, 2), table="records", engine_version=_ENGINE_VERSION
            )
        )
        assert sum(c.num_rows for c in out) == 5

    def test_auto_route_selects_chunked_mode(self, tmp_path) -> None:
        table = pa.table({"code": pa.array([f"src-{i}" for i in range(30)], type=pa.string())})
        columns = [_code_set_col("code", "icd10")]
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
        table = pa.table({"code": pa.array([f"src-{i}" for i in range(20)], type=pa.string())})
        columns = [_code_set_col("code", "icd10")]
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
