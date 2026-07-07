"""S5 (Sprint 2 honesty pack): bucketize + date_shift format_error production.

TDD: written before the implementation. Both strategies previously kept the
ORIGINAL source value in the frame for a non-null cell that failed numeric/
date coercion -- a silent per-value leak (discovery 0.1 / D8's "the
intentional behavior change"). They now record a `RowError` (trigger
"format_error") via `ctx.row_errors` and still leave the original value in
the frame (trap T4: never rewrite/null the cell here; the pipeline-level
rule in `_pipeline.py` is what guarantees the row never reaches the main
output). Source-null cells are UNCHANGED (no row error; null passthrough is
not a leak).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._strategies._bucketize import BucketizeStrategyHandler
from decoy_engine.execution._strategies._date_shift import DateShiftStrategyHandler
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_SEED = (0x55).to_bytes(8, "big")


def _ctx() -> StrategyContext:
    return StrategyContext(
        registry=None,  # type: ignore[arg-type]
        pool_cache=PoolCache(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
        job_seed=_SEED,
    )


def _col(
    strategy: str, *, namespace: str | None, provider_config: tuple[tuple[str, Any], ...] = ()
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=provider_config,
        coherent_with=(),
    )


class TestBucketizeRowErrors:
    def test_non_numeric_non_null_cell_records_format_error(self) -> None:
        df = pd.DataFrame({"age": [23, "not-a-number", 47]})
        seed = _col("bucketize", namespace=None, provider_config=(("width", 10),))
        ctx = _ctx()
        out_df, warnings = BucketizeStrategyHandler().run(df, "age", seed, ctx)
        assert len(ctx.row_errors) == 1
        err = ctx.row_errors[0]
        assert err.column == "age"
        assert err.row_index == 1
        assert err.trigger == "format_error"
        assert "not-a-number" not in err.reason  # no cell values in the reason (trap T3)

    def test_original_value_left_in_frame_not_rewritten(self) -> None:
        """T4: the strategy must not null/rewrite the bad cell; the pipeline
        layer (not the strategy) guarantees it never reaches the main output."""
        df = pd.DataFrame({"age": ["bad-value"]})
        seed = _col("bucketize", namespace=None, provider_config=(("width", 10),))
        ctx = _ctx()
        out_df, _ = BucketizeStrategyHandler().run(df, "age", seed, ctx)
        assert out_df["age"].iloc[0] == "bad-value"

    def test_source_null_cell_no_row_error(self) -> None:
        df = pd.DataFrame({"age": [23, None]})
        seed = _col("bucketize", namespace=None, provider_config=(("width", 10),))
        ctx = _ctx()
        BucketizeStrategyHandler().run(df, "age", seed, ctx)
        assert ctx.row_errors == []

    def test_all_parseable_output_unchanged_golden(self) -> None:
        """Byte-identical to the pre-slice golden for all-parseable input."""
        df = pd.DataFrame({"age": [23, 47, 8, None]})
        seed = _col("bucketize", namespace=None, provider_config=(("width", 10),))
        ctx = _ctx()
        out_df, _ = BucketizeStrategyHandler().run(df, "age", seed, ctx)
        # Row 3 is the null-source passthrough; pd.isna() (not `is None`)
        # because a raw pandas frame keeps it as NaN pre-Arrow-round-trip
        # (the adapter's `pa.Table.from_pandas` maps NaN -> null on egress;
        # see the ExecutionResult-level golden pin in test_hash_bucketize.py).
        out_list = out_df["age"].tolist()
        assert out_list[:3] == ["20", "40", "0"]
        assert pd.isna(out_list[3])
        assert ctx.row_errors == []


class TestDateShiftRowErrors:
    def test_non_parseable_non_null_cell_records_format_error(self) -> None:
        df = pd.DataFrame({"dob": ["2020-01-01", "not-a-date", "2021-06-15"]})
        seed = _col("date_shift", namespace="ns1")
        ctx = _ctx()
        out_df, _ = DateShiftStrategyHandler().run(df, "dob", seed, ctx)
        assert len(ctx.row_errors) == 1
        err = ctx.row_errors[0]
        assert err.column == "dob"
        assert err.row_index == 1
        assert err.trigger == "format_error"
        assert "not-a-date" not in err.reason

    def test_original_value_left_in_frame_not_rewritten(self) -> None:
        df = pd.DataFrame({"dob": ["garbage"]})
        seed = _col("date_shift", namespace="ns1")
        ctx = _ctx()
        out_df, _ = DateShiftStrategyHandler().run(df, "dob", seed, ctx)
        assert out_df["dob"].iloc[0] == "garbage"

    def test_source_null_cell_no_row_error(self) -> None:
        df = pd.DataFrame({"dob": ["2020-01-01", None]})
        seed = _col("date_shift", namespace="ns1")
        ctx = _ctx()
        DateShiftStrategyHandler().run(df, "dob", seed, ctx)
        assert ctx.row_errors == []
