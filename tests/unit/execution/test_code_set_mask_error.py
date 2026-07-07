"""S6 (Sprint 2 honesty pack): code_set mask_error row-error production.

TDD: written before the implementation. `CodeSetHandler.run` wraps the
existing per-value `apply_code_set` call in try/except for the PER-VALUE
raise cases (chapter_preserve unknown chapter absent, sole-member bucket):
records a `RowError` (trigger "mask_error"), keeps the original value in the
frame (trap T4), and continues the loop. Corpus-level failures (corpus not
loadable, missing columns, empty, missing chapter column) stay job-fatal --
they affect every row and are config bugs, not row defects; the
discriminator is the PlanCompileError `.code`, not string-matching.
"""

from __future__ import annotations

import pathlib

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._strategies._code_set import CodeSetHandler
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_SEED = (0x77).to_bytes(8, "big")


def _ctx() -> StrategyContext:
    return StrategyContext(
        registry=None,  # type: ignore[arg-type]
        pool_cache=PoolCache(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
        job_seed=_SEED,
    )


def _col(provider_config: tuple[tuple[str, object], ...]) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="code_set",
        provider="code_set",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=provider_config,
        coherent_with=(),
    )


def _two_chapter_corpus(tmp_path: pathlib.Path) -> pathlib.Path:
    import pyarrow.parquet as pq

    path = tmp_path / "two_chapters.parquet"
    tbl = pa.table(
        {
            "code": pa.array(["A01", "A02", "B01", "B02"], type=pa.string()),
            "chapter": pa.array(["A", "A", "B", "B"], type=pa.string()),
        }
    )
    pq.write_table(tbl, str(path))
    return path


class TestChapterAbsentIsPerValueMaskError:
    def test_unknown_chapter_value_records_mask_error_others_still_masked(
        self, tmp_path: pathlib.Path
    ) -> None:
        path = _two_chapter_corpus(tmp_path)
        df = pd.DataFrame({"code": ["A01", "U07.1", "B01"]})  # U chapter absent
        seed = _col(
            (
                ("code_set", "two_chapters"),
                ("chapter_preserve", True),
                ("corpus_source", f"customer:{path}"),
                ("mode", "mask"),
            )
        )
        ctx = _ctx()
        out_df, _ = CodeSetHandler().run(df, "code", seed, ctx)

        assert len(ctx.row_errors) == 1
        err = ctx.row_errors[0]
        assert err.row_index == 1
        assert err.trigger == "mask_error"
        assert "U07.1" not in err.reason  # no cell values (trap T3)

        # Rows 0 and 2 still masked normally (their own chapter is present).
        assert out_df["code"].iloc[0] != "A01"
        assert out_df["code"].iloc[2] != "B01"

    def test_bad_value_left_unchanged_in_frame(self, tmp_path: pathlib.Path) -> None:
        path = _two_chapter_corpus(tmp_path)
        df = pd.DataFrame({"code": ["U07.1"]})
        seed = _col(
            (
                ("code_set", "two_chapters"),
                ("chapter_preserve", True),
                ("corpus_source", f"customer:{path}"),
                ("mode", "mask"),
            )
        )
        ctx = _ctx()
        out_df, _ = CodeSetHandler().run(df, "code", seed, ctx)
        assert out_df["code"].iloc[0] == "U07.1"  # trap T4: not rewritten


class TestSoleMemberBucketIsPerValueMaskError:
    def test_sole_member_bucket_records_mask_error(self, tmp_path: pathlib.Path) -> None:
        import pyarrow.parquet as pq

        path = tmp_path / "sole_member.parquet"
        tbl = pa.table(
            {
                "code": pa.array(["A01", "B01", "B02"], type=pa.string()),
                "chapter": pa.array(["A", "B", "B"], type=pa.string()),
            }
        )
        pq.write_table(tbl, str(path))
        df = pd.DataFrame({"code": ["A01"]})  # sole member of chapter A
        seed = _col(
            (
                ("code_set", "sole_member"),
                ("chapter_preserve", True),
                ("corpus_source", f"customer:{path}"),
                ("mode", "mask"),
            )
        )
        ctx = _ctx()
        out_df, _ = CodeSetHandler().run(df, "code", seed, ctx)
        assert len(ctx.row_errors) == 1
        assert ctx.row_errors[0].trigger == "mask_error"
        assert out_df["code"].iloc[0] == "A01"


class TestCorpusLevelFailuresStayJobFatal:
    def test_missing_corpus_still_raises(self) -> None:
        """Corpus-level failures affect every row and are config bugs, not
        row defects: they must still raise, never become a per-row error."""
        df = pd.DataFrame({"code": ["A01"]})
        seed = _col((("code_set", "totally_unknown_corpus_xyz"),))
        ctx = _ctx()
        with pytest.raises(PlanCompileError):
            CodeSetHandler().run(df, "code", seed, ctx)
        assert ctx.row_errors == []

    def test_empty_corpus_still_raises(self, tmp_path: pathlib.Path) -> None:
        import pyarrow.parquet as pq

        path = tmp_path / "empty.parquet"
        pq.write_table(pa.table({"code": pa.array([], type=pa.string())}), str(path))
        df = pd.DataFrame({"code": ["A01"]})
        seed = _col((("code_set", "empty"), ("corpus_source", f"customer:{path}")))
        ctx = _ctx()
        with pytest.raises(PlanCompileError):
            CodeSetHandler().run(df, "code", seed, ctx)
        assert ctx.row_errors == []


class TestNullPassthroughUnaffected:
    def test_null_value_no_row_error(self, tmp_path: pathlib.Path) -> None:
        path = _two_chapter_corpus(tmp_path)
        df = pd.DataFrame({"code": ["A01", None]})
        seed = _col(
            (
                ("code_set", "two_chapters"),
                ("chapter_preserve", True),
                ("corpus_source", f"customer:{path}"),
                ("mode", "mask"),
            )
        )
        ctx = _ctx()
        out_df, _ = CodeSetHandler().run(df, "code", seed, ctx)
        assert ctx.row_errors == []
        # pandas silently widens a mixed str/None object column's None to
        # NaN at construction (a pandas quirk, not an engine behavior); the
        # Arrow round trip on egress restores real None (proven by the
        # existing test_code_set_null_passthrough via the full adapter).
        assert pd.isna(out_df["code"].iloc[1])
