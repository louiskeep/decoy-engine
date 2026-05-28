"""Phase 3 of polars-duckdb hybrid plan: parity tests.

For each ported relational op, run the pandas implementation and the polars
implementation on the same input. Assert the outputs match (modulo the
documented divergences in SEMANTIC_DIFFERENCES.md).

Bypasses the runner — calls each op's `apply()` directly with explicitly-
shaped inputs. The runner's mode-resolution + conversion behavior is
covered in tests/integration/test_graph_hybrid_engine.py.
"""

from __future__ import annotations

import pandas as pd
import polars as pl
import pytest

from decoy_engine.graph.ops import (
    dedupe,
    derive,
    drop_column,
    filter_op,
    join,
    limit,
    select_column,
    sort,
)


@pytest.fixture
def standard_pandas() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": [1, 2, 3, 4, 5, 5],
            "state": ["CA", "NY", "CA", "TX", "CA", "CA"],
            "value": [10, 20, 30, 40, 50, 50],
            "name": ["Alice", "Bob", "Carol", "Dave", "Eve", "Eve"],
        }
    )


@pytest.fixture
def standard_polars(standard_pandas) -> pl.DataFrame:
    return pl.from_pandas(standard_pandas)


def _norm(df) -> pd.DataFrame:
    """Convert to pandas + reset index for assert_frame_equal."""
    if isinstance(df, pl.DataFrame):
        df = df.to_pandas()
    return df.reset_index(drop=True)


# -------- sort --------------------------------------------------------------


def test_sort_parity_single_key_asc(standard_pandas, standard_polars):
    cfg = {"by": ["value"]}
    pd_out = sort.apply([standard_pandas], cfg, ctx=None)
    pl_out = sort.apply([standard_polars], cfg, ctx=None)
    pd.testing.assert_frame_equal(_norm(pd_out), _norm(pl_out), check_dtype=False)


def test_sort_parity_single_key_desc(standard_pandas, standard_polars):
    cfg = {"by": ["value"], "order": "desc"}
    pd_out = sort.apply([standard_pandas], cfg, ctx=None)
    pl_out = sort.apply([standard_polars], cfg, ctx=None)
    pd.testing.assert_frame_equal(_norm(pd_out), _norm(pl_out), check_dtype=False)


def test_sort_parity_multi_key_mixed_order(standard_pandas, standard_polars):
    cfg = {"by": ["state", "value"], "order": ["asc", "desc"]}
    pd_out = sort.apply([standard_pandas], cfg, ctx=None)
    pl_out = sort.apply([standard_polars], cfg, ctx=None)
    pd.testing.assert_frame_equal(_norm(pd_out), _norm(pl_out), check_dtype=False)


def test_sort_parity_stable_for_tied_keys():
    """SEMANTIC_DIFFERENCES row 3: pandas mergesort and polars
    maintain_order=True both stable; rows with equal keys preserve input
    order on both sides."""
    pdf = pd.DataFrame({"k": [1, 1, 1, 2, 2], "tag": ["a", "b", "c", "d", "e"]})
    pdf_polars = pl.from_pandas(pdf)
    pd_out = sort.apply([pdf], {"by": ["k"]}, ctx=None)
    pl_out = sort.apply([pdf_polars], {"by": ["k"]}, ctx=None)
    pd.testing.assert_frame_equal(_norm(pd_out), _norm(pl_out), check_dtype=False)


# -------- dedupe ------------------------------------------------------------


def test_dedupe_parity_default_keep_first(standard_pandas, standard_polars):
    cfg = {"on": ["id"]}
    pd_out = dedupe.apply([standard_pandas], cfg, ctx=None)
    pl_out = dedupe.apply([standard_polars], cfg, ctx=None)
    pd.testing.assert_frame_equal(_norm(pd_out), _norm(pl_out), check_dtype=False)


def test_dedupe_parity_keep_last(standard_pandas, standard_polars):
    cfg = {"on": ["id"], "keep": "last"}
    pd_out = dedupe.apply([standard_pandas], cfg, ctx=None)
    pl_out = dedupe.apply([standard_polars], cfg, ctx=None)
    pd.testing.assert_frame_equal(_norm(pd_out), _norm(pl_out), check_dtype=False)


def test_dedupe_parity_no_subset_drops_full_row_dupes(standard_pandas, standard_polars):
    pd_out = dedupe.apply([standard_pandas], {}, ctx=None)
    pl_out = dedupe.apply([standard_polars], {}, ctx=None)
    pd.testing.assert_frame_equal(_norm(pd_out), _norm(pl_out), check_dtype=False)


# -------- drop_column / select_column / limit ------------------------------


def test_drop_column_parity(standard_pandas, standard_polars):
    cfg = {"columns": ["name"]}
    pd_out = drop_column.apply([standard_pandas], cfg, ctx=None)
    pl_out = drop_column.apply([standard_polars], cfg, ctx=None)
    pd.testing.assert_frame_equal(_norm(pd_out), _norm(pl_out), check_dtype=False)


def test_drop_column_empty_is_passthrough(standard_pandas, standard_polars):
    pd_out = drop_column.apply([standard_pandas], {}, ctx=None)
    pl_out = drop_column.apply([standard_polars], {}, ctx=None)
    pd.testing.assert_frame_equal(_norm(pd_out), _norm(pl_out), check_dtype=False)


def test_select_column_parity(standard_pandas, standard_polars):
    cfg = {"columns": ["state", "value"]}
    pd_out = select_column.apply([standard_pandas], cfg, ctx=None)
    pl_out = select_column.apply([standard_polars], cfg, ctx=None)
    pd.testing.assert_frame_equal(_norm(pd_out), _norm(pl_out), check_dtype=False)


def test_limit_parity(standard_pandas, standard_polars):
    cfg = {"n": 3}
    pd_out = limit.apply([standard_pandas], cfg, ctx=None)
    pl_out = limit.apply([standard_polars], cfg, ctx=None)
    pd.testing.assert_frame_equal(_norm(pd_out), _norm(pl_out), check_dtype=False)


def test_limit_zero_returns_empty_frame_both_engines(standard_pandas, standard_polars):
    pd_out = limit.apply([standard_pandas], {"n": 0}, ctx=None)
    pl_out = limit.apply([standard_polars], {"n": 0}, ctx=None)
    assert len(pd_out) == 0 and len(pl_out) == 0


# -------- filter ------------------------------------------------------------


@pytest.mark.parametrize(
    "predicate",
    [
        "state == 'CA'",
        "value > 20",
        "state == 'CA' and value >= 30",
        "state == 'NY' or state == 'TX'",
        "value != 50",
    ],
)
def test_filter_parity_basic(standard_pandas, standard_polars, predicate):
    cfg = {"predicate": predicate}
    pd_out = filter_op.apply([standard_pandas], cfg, ctx=None)
    pl_out = filter_op.apply([standard_polars], cfg, ctx=None)
    # Sort by a stable key so row-order differences (which would be a
    # separate divergence) don't masquerade as filter mismatches.
    pd_sorted = pd_out.sort_values("id").reset_index(drop=True)
    pl_sorted = pl_out.to_pandas().sort_values("id").reset_index(drop=True)
    pd.testing.assert_frame_equal(pd_sorted, pl_sorted, check_dtype=False)


def test_filter_polars_rejects_pandas_only_syntax(standard_polars):
    """SEMANTIC_DIFFERENCES row 5: pandas-query allows `is` / `in` via
    engine='python'; polars SQLContext rejects them. The op surfaces the
    failure as OpError."""
    from decoy_engine.graph.ops._base import OpError

    with pytest.raises(OpError):
        filter_op.apply(
            [standard_polars],
            {"predicate": "value is not None"},
            ctx=None,
        )


# -------- derive ------------------------------------------------------------


def test_derive_parity_arithmetic(standard_pandas, standard_polars):
    cfg = {"column": "double_value", "expression": "value * 2"}
    pd_out = derive.apply([standard_pandas], cfg, ctx=None)
    pl_out = derive.apply([standard_polars], cfg, ctx=None)
    # Both should produce a `double_value` column equal to `value * 2`.
    assert (_norm(pd_out)["double_value"] == _norm(pl_out)["double_value"]).all()


def test_derive_parity_columns_preserved(standard_pandas, standard_polars):
    cfg = {"column": "double_value", "expression": "value * 2"}
    pd_out = derive.apply([standard_pandas], cfg, ctx=None)
    pl_out = derive.apply([standard_polars], cfg, ctx=None)
    assert set(pd_out.columns) == set(_norm(pl_out).columns)
    assert "double_value" in pd_out.columns


# -------- engine-v2 S12 graph-op parity gate (D-S12-A) ----------------------
#
# The done-definition graph-op gate ("every core graph op has a green parity
# test") is satisfied AGAINST THE V1 GRAPH ENGINE under D-S12-A (the v2 execution
# adapter does not run these ops). filter / sort / dedupe / derive each have a
# real polars path and a green pandas-vs-polars parity test above. The two
# remaining core-six items are gaps recorded here as S13 readiness deferrals,
# NOT silently absent:
#
#   - join: NATIVE_ENGINE='pandas' (the runner materializes inputs to pandas at
#     the join boundary); there is no polars-native join path. Deferred; the
#     parity test is xfail below.
#   - aggregate: there is NO v2 aggregate graph op (the only groupby in the v2
#     path is S10's pandas-internal quality-summary scan, which is not
#     substrate-switched). The gate item is N/A for v2 (a phantom gate, the kind
#     D-S12 exists to settle); documented by test_aggregate_op_absent below.


@pytest.mark.xfail(
    reason="join is pandas-native (NATIVE_ENGINE='pandas'); polars-native join "
    "deferred per D-S12-A and recorded as an S13 readiness deferral",
    strict=False,
)
def test_join_polars_parity_deferred():
    left = pl.DataFrame({"id": [1, 2], "v": [10, 20]})
    right = pl.DataFrame({"cid": [1, 2], "w": [100, 200]})
    cfg = {"joins": [{"left_on": ["id"], "right_on": ["cid"], "join_type": "inner"}]}
    out = join.apply([left, right], cfg, ctx=None)
    # The gate this documents: join would have to run polars-native to be parity-
    # testable against pandas. It does not (it has no polars branch), so this
    # assertion is expected to fail until the deferral is closed.
    assert isinstance(out, pl.DataFrame)


def test_aggregate_op_absent():
    # No v2 aggregate graph op exists; the gate item is N/A (phantom gate). If a
    # future op adds one, this test fails and forces a parity entry to be written.
    import decoy_engine.graph.ops as ops_pkg

    assert not hasattr(ops_pkg, "aggregate")
