"""Phase 6 of polars-duckdb hybrid plan: edge-case parity matrix.

Each port (Phase 3 relational + Phase 4 source/sink) gets exercised
against the edges that surfaced in dogfood / surfaced in code review:
  - empty input
  - single-row input
  - all-null column
  - unicode strings
  - duplicate-laden frame

Documented divergences live in SEMANTIC_DIFFERENCES.md.
"""

from __future__ import annotations

import os
import tempfile

import pandas as pd
import polars as pl
import pyarrow as pa
import pytest

from decoy_engine.graph.ops import (
    dedupe,
    drop_column,
    filter_op,
    limit,
    select_column,
    sort,
    source_file,
)


def _norm(df) -> pd.DataFrame:
    if isinstance(df, pl.DataFrame):
        df = df.to_pandas()
    if isinstance(df, pa.Table):
        df = df.to_pandas()
    return df.reset_index(drop=True)


# -------- Empty input ------------------------------------------------------


@pytest.fixture
def empty_pandas() -> pd.DataFrame:
    return pd.DataFrame({"id": [], "name": []})


@pytest.fixture
def empty_polars(empty_pandas) -> pl.DataFrame:
    return pl.from_pandas(empty_pandas)


@pytest.mark.parametrize(
    "op,cfg",
    [
        (sort, {"by": ["id"]}),
        (dedupe, {"on": ["id"]}),
        (drop_column, {"columns": ["name"]}),
        (limit, {"n": 5}),
    ],
)
def test_empty_input_parity(op, cfg, empty_pandas, empty_polars):
    pd_out = op.apply([empty_pandas], cfg, ctx=None)
    pl_out = op.apply([empty_polars], cfg, ctx=None)
    assert len(_norm(pd_out)) == 0
    assert len(_norm(pl_out)) == 0


# -------- Single-row input -------------------------------------------------


def test_single_row_sort_parity():
    pdf = pd.DataFrame({"id": [42], "name": ["Alice"]})
    pdf_polars = pl.from_pandas(pdf)
    pd_out = sort.apply([pdf], {"by": ["id"]}, ctx=None)
    pl_out = sort.apply([pdf_polars], {"by": ["id"]}, ctx=None)
    pd.testing.assert_frame_equal(_norm(pd_out), _norm(pl_out), check_dtype=False)


def test_single_row_filter_no_match_parity():
    pdf = pd.DataFrame({"id": [42], "name": ["Alice"]})
    pdf_polars = pl.from_pandas(pdf)
    cfg = {"predicate": "id > 100"}
    pd_out = filter_op.apply([pdf], cfg, ctx=None)
    pl_out = filter_op.apply([pdf_polars], cfg, ctx=None)
    assert len(_norm(pd_out)) == 0
    assert len(_norm(pl_out)) == 0


# -------- All-null column --------------------------------------------------


def test_all_null_column_drop_column_parity():
    pdf = pd.DataFrame({"id": [1, 2], "obsolete": [None, None]})
    pdf_polars = pl.from_pandas(pdf)
    pd_out = drop_column.apply([pdf], {"columns": ["obsolete"]}, ctx=None)
    pl_out = drop_column.apply([pdf_polars], {"columns": ["obsolete"]}, ctx=None)
    pd.testing.assert_frame_equal(_norm(pd_out), _norm(pl_out), check_dtype=False)


def test_all_null_column_select_column_parity():
    pdf = pd.DataFrame({"id": [1, 2], "blank": [None, None]})
    pdf_polars = pl.from_pandas(pdf)
    pd_out = select_column.apply([pdf], {"columns": ["blank"]}, ctx=None)
    pl_out = select_column.apply([pdf_polars], {"columns": ["blank"]}, ctx=None)
    # Both produce a 2-row frame with one all-null column; types may differ
    # (object vs Null), but the row count and shape must match.
    assert _norm(pd_out).shape == _norm(pl_out).shape


# -------- Unicode strings --------------------------------------------------


def test_unicode_filter_parity():
    pdf = pd.DataFrame(
        {
            "name": ["Алёна", "Zoë", "山田", "Renée"],
            "id": [1, 2, 3, 4],
        }
    )
    pdf_polars = pl.from_pandas(pdf)
    cfg = {"predicate": "name == 'Zoë'"}
    pd_out = filter_op.apply([pdf], cfg, ctx=None)
    pl_out = filter_op.apply([pdf_polars], cfg, ctx=None)
    pd.testing.assert_frame_equal(
        _norm(pd_out).sort_values("id").reset_index(drop=True),
        _norm(pl_out).sort_values("id").reset_index(drop=True),
        check_dtype=False,
    )


def test_unicode_sort_parity():
    pdf = pd.DataFrame({"name": ["Zoë", "Алёна", "山田"], "id": [1, 2, 3]})
    pdf_polars = pl.from_pandas(pdf)
    pd_out = sort.apply([pdf], {"by": ["name"]}, ctx=None)
    pl_out = sort.apply([pdf_polars], {"by": ["name"]}, ctx=None)
    # Sort order for unicode is locale-dependent; both engines use
    # codepoint order by default. Assert the IDs come out in the same
    # order on both sides.
    assert _norm(pd_out)["id"].tolist() == _norm(pl_out)["id"].tolist()


# -------- Duplicate-laden frame --------------------------------------------


def test_dedupe_all_duplicates_parity():
    """Every row is identical → dedupe produces a single row."""
    pdf = pd.DataFrame({"id": [1, 1, 1], "name": ["a", "a", "a"]})
    pdf_polars = pl.from_pandas(pdf)
    pd_out = dedupe.apply([pdf], {}, ctx=None)
    pl_out = dedupe.apply([pdf_polars], {}, ctx=None)
    assert len(_norm(pd_out)) == 1
    assert len(_norm(pl_out)) == 1


def test_dedupe_no_duplicates_parity():
    """Every row unique → dedupe is a passthrough."""
    pdf = pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"]})
    pdf_polars = pl.from_pandas(pdf)
    pd_out = dedupe.apply([pdf], {"on": ["id"]}, ctx=None)
    pl_out = dedupe.apply([pdf_polars], {"on": ["id"]}, ctx=None)
    pd.testing.assert_frame_equal(
        _norm(pd_out).sort_values("id").reset_index(drop=True),
        _norm(pl_out).sort_values("id").reset_index(drop=True),
        check_dtype=False,
    )


# -------- source.file edge cases ------------------------------------------


def test_source_file_empty_csv_parity():
    """CSV with header but no data rows → empty frame on both engines."""
    tmpdir = tempfile.mkdtemp()
    src = os.path.join(tmpdir, "empty.csv")
    with open(src, "w") as f:
        f.write("id,name\n")

    pd_out = source_file.apply([], {"path": src, "__engine": "pandas"}, ctx=None)
    db_out = source_file.apply([], {"path": src, "__engine": "duckdb"}, ctx=None)
    assert len(_norm(pd_out)) == 0
    assert len(_norm(db_out)) == 0
    assert list(_norm(pd_out).columns) == list(_norm(db_out).columns)


def test_source_file_csv_with_unicode_data_parity():
    tmpdir = tempfile.mkdtemp()
    src = os.path.join(tmpdir, "unicode.csv")
    pd.DataFrame({"name": ["Zoë", "Алёна", "山田"], "id": [1, 2, 3]}).to_csv(src, index=False)

    pd_out = source_file.apply([], {"path": src, "__engine": "pandas"}, ctx=None)
    db_out = source_file.apply([], {"path": src, "__engine": "duckdb"}, ctx=None)
    pd.testing.assert_frame_equal(
        _norm(pd_out).sort_values("id").reset_index(drop=True),
        _norm(db_out).sort_values("id").reset_index(drop=True),
        check_dtype=False,
    )
