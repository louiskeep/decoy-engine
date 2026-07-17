"""Codex round-7 P2 CROSS-INVOCATION MASKING/EVIDENCE DIVERGENCE regression.

The round-6 fix pinned one corpus record within a SINGLE ``CodeSetHandler.run``
so masking and evidence could not diverge on a mid-run customer-corpus file
replacement. But a ``code_set`` on an FK parent under ``orphan_policy=REMAP``
dispatches ``run`` TWICE against the same logical (table, column): once for the
parent column's real values, and once from the orphan-REMAP closure
(``_orphan.make_remap_fn``) which re-runs the parent's strategy to mask orphan
keys. Each ``run`` independently re-resolved from the loader cache, so a file
swapped between the two calls masked real parent values off v1 and remapped
orphans off v2, and the second call's evidence stamp overwrote the first's.

The round-7 fix pins the resolved record on ``StrategyContext`` keyed by
(current_table, column) for the life of the job (== the context), so both
invocations share ONE corpus version. These tests drive the two-invocation
shape directly at the handler level (a second ``run`` on the same ctx and key,
with the underlying file replaced in between) and assert the pin holds.
"""

from __future__ import annotations

import os
import pathlib

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._strategies._code_set import CodeSetHandler
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_SEED = (0x5A).to_bytes(8, "big")


def _ctx(table: str) -> StrategyContext:
    ctx = StrategyContext(
        registry=None,  # type: ignore[arg-type]
        pool_cache=PoolCache(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
        job_seed=_SEED,
    )
    # `current_table` is normally stamped by the dispatch layer before a
    # handler runs; set it here so evidence and the round-7 record pin key on
    # a stable (table, column) identity, exactly as the parent-column dispatch
    # and the orphan-REMAP closure both do.
    object.__setattr__(ctx, "current_table", table)
    return ctx


def _col(path: pathlib.Path, name: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="code_set",
        provider="code_set",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(
            ("code_set", name),
            ("corpus_source", f"customer:{path}"),
            ("mode", "mask"),
        ),
        coherent_with=(),
    )


def _write_corpus(path: pathlib.Path, codes: list[str], source_version: str) -> None:
    """A complete-provenance customer corpus so loads neither warn nor fail."""
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


def _swap_file(path: pathlib.Path, codes: list[str], source_version: str) -> None:
    """Replace the corpus at `path` and force a distinct cache identity.

    The loader keys its customer cache on (path, mtime_ns, ctime_ns, size);
    stamping a clearly different mtime (and, via that utime call, ctime)
    guarantees a fresh resolve would mint a NEW record from the swapped file --
    so if the round-7 pin were absent, the second run would pick up v2.
    """
    _write_corpus(path, codes, source_version)
    distinct = 1_600_000_000  # fixed, far from any real test-run mtime
    os.utime(path, (distinct, distinct))


class TestJobWideRecordPin:
    def test_second_run_same_column_reuses_pinned_corpus(self, tmp_path: pathlib.Path) -> None:
        """Two runs on the same (table, column) sharing one ctx must mask off
        the SAME corpus version even if the file is replaced between them --
        the orphan-REMAP second invocation cannot diverge from the parent's
        real values."""
        path = tmp_path / "parentcorpus.parquet"
        _write_corpus(path, ["A01", "A02", "A03", "A04"], "v1")
        v1_codes = {"A01", "A02", "A03", "A04"}
        v2_codes = {"B01", "B02", "B03", "B04"}

        ctx = _ctx("parent_t")
        seed = _col(path, "parentcorpus")

        # Run #1: the parent column's real values. Pins the v1 record.
        df1 = pd.DataFrame({"key": ["A01", "A02", "A03"]})
        out1, _ = CodeSetHandler().run(df1, "key", seed, ctx)
        assert set(out1["key"]).issubset(v1_codes)
        assert ("parent_t", "key") in ctx.code_set_records
        ev1 = ctx.code_set_corpora[("parent_t", "key")]
        assert ev1["source_version"] == "v1"

        # File replaced mid-job (v2) with a distinct cache identity.
        _swap_file(path, ["B01", "B02", "B03", "B04"], "v2")

        # Run #2: the orphan-REMAP re-run of the SAME parent strategy. Must
        # reuse the pinned v1 record, NOT resolve v2.
        df2 = pd.DataFrame({"key": ["A04", "A01"]})
        out2, _ = CodeSetHandler().run(df2, "key", seed, ctx)
        assert set(out2["key"]).issubset(v1_codes), (
            "orphan-REMAP second run masked off the swapped v2 corpus -- the "
            "job-wide record pin did not hold."
        )
        assert not set(out2["key"]) & v2_codes
        # Evidence still reports v1 (not overwritten by the second run).
        assert ctx.code_set_corpora[("parent_t", "key")]["source_version"] == "v1"

    def test_fresh_job_after_swap_sees_new_corpus(self, tmp_path: pathlib.Path) -> None:
        """Control: the pin is JOB-scoped, not a global staleness bug. A NEW
        ctx (new job) after the same swap resolves the v2 corpus, proving the
        file replacement is real and only the in-job pin suppresses it."""
        path = tmp_path / "parentcorpus2.parquet"
        _write_corpus(path, ["A01", "A02", "A03", "A04"], "v1")
        seed = _col(path, "parentcorpus2")

        ctx1 = _ctx("t")
        out1, _ = CodeSetHandler().run(pd.DataFrame({"key": ["A01", "A02"]}), "key", seed, ctx1)
        assert set(out1["key"]).issubset({"A01", "A02", "A03", "A04"})

        _swap_file(path, ["B01", "B02", "B03", "B04"], "v2")

        ctx2 = _ctx("t")  # a fresh job: empty code_set_records
        out2, _ = CodeSetHandler().run(pd.DataFrame({"key": ["A01", "A02"]}), "key", seed, ctx2)
        assert set(out2["key"]).issubset({"B01", "B02", "B03", "B04"}), (
            "a fresh job after the swap must pick up v2; if not, the file swap "
            "did not actually invalidate the loader cache and the pin test above "
            "would be vacuous."
        )
        assert ctx2.code_set_corpora[("t", "key")]["source_version"] == "v2"
