"""Phase 5 of polars-duckdb hybrid plan: engine error translation.

Tests the `translate()` function on representative polars / duckdb
exceptions and confirms the runner surfaces translated messages in the
NodeRunRecord."""

from __future__ import annotations

import os
import tempfile

import pandas as pd
import pytest
import yaml

from decoy_engine import run_graph
from decoy_engine.graph.errors import translate
from decoy_engine.graph.ops._base import OpError


@pytest.fixture
def tmp_csv():
    tmpdir = tempfile.mkdtemp()
    src = os.path.join(tmpdir, "in.csv")
    pd.DataFrame({"id": [1, 2, 3], "state": ["CA", "NY", "TX"]}).to_csv(src, index=False)
    return src


def test_translate_polars_column_not_found_is_user_friendly():
    import polars as pl

    df = pl.DataFrame({"a": [1, 2]})
    with pytest.raises(pl.exceptions.ColumnNotFoundError) as ei:
        df.select("does_not_exist")

    out = translate(ei.value, op_kind="filter", node_id="f1")
    assert isinstance(out, OpError)
    msg = str(out)
    assert "f1" in msg
    assert "filter" in msg
    assert "not found" in msg


def test_translate_polars_compute_error_is_translated():
    import polars as pl

    with pytest.raises(Exception) as ei:
        with pl.SQLContext(df=pl.DataFrame({"a": [1]}), eager=True) as ctx:
            ctx.execute("SELECT * FROM df WHERE a banana 'x'")

    out = translate(ei.value, op_kind="filter", node_id="f1")
    assert isinstance(out, OpError)
    assert "f1" in str(out)


def test_translate_duckdb_catalog_exception():
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        with pytest.raises(duckdb.CatalogException) as ei:
            con.execute("SELECT * FROM does_not_exist").to_arrow_table()
    finally:
        con.close()

    out = translate(ei.value, op_kind="source.db", node_id="s1")
    assert isinstance(out, OpError)
    assert "s1" in str(out)
    assert "table or column missing" in str(out)


def test_translate_oprerror_passes_through_with_node_context():
    inner = OpError("filter predicate failed")
    out = translate(inner, op_kind="filter", node_id="f1")
    msg = str(out)
    assert "f1" in msg
    assert "filter predicate failed" in msg


def test_translate_oprerror_already_with_node_context_is_unchanged():
    inner = OpError("Node 'f1' (filter): predicate failed")
    out = translate(inner, op_kind="filter", node_id="f1")
    # The translator preserves messages that already carry node context
    # rather than double-prefixing them.
    assert str(out).count("Node 'f1'") == 1


def test_translate_unknown_exception_keeps_original_message():
    err = RuntimeError("something internal blew up")
    out = translate(err, op_kind="mask", node_id="m1")
    msg = str(out)
    assert "m1" in msg
    assert "mask" in msg
    assert "something internal blew up" in msg


def test_runner_surfaces_translated_message_for_polars_failure(tmp_csv):
    """End-to-end: a polars op raises ColumnNotFoundError; the runner
    catches, translates, and the NodeRunRecord carries the friendly msg."""
    cfg = yaml.safe_dump({
        "mode": "graph",
        "engine": "hybrid",
        "nodes": [
            {"id": "s", "kind": "source.file", "config": {"path": tmp_csv}},
            {"id": "so", "kind": "sort", "config": {"by": ["does_not_exist"]}},
        ],
        "edges": [{"from": "s", "to": "so"}],
    })
    result = run_graph(cfg)
    assert result["success"] is False
    failed = next(n for n in result["nodes"] if n["status"] == "error")
    assert failed["node_id"] == "so"
    assert "does_not_exist" in failed["error"]
    # The error must include the node id in the friendly format.
    assert "'so'" in failed["error"]


def test_translate_forwards_validation_error_code_and_path():
    """R3.4 typed errors: translate() should promote ValidationError.code
    and .path onto the returned OpError so the runner can persist them
    on the records dict."""
    from decoy_engine.graph.errors import translate
    from decoy_engine.internal.validator import ValidationError

    src = ValidationError(
        "missing required field 'path'", "config.path",
        code="source_file.missing_path",
    )
    out = translate(src, "source.file", "src_1")
    assert getattr(out, "code", None) == "source_file.missing_path"
    assert getattr(out, "path", None) == "config.path"
    # The user-facing message still names the node.
    assert "'src_1'" in str(out)


def test_translate_passes_through_op_error_with_metadata():
    """OpError already user-friendly. If we attach .code / .path to one,
    translate() should preserve them through the node-prefix path."""
    from decoy_engine.graph.errors import translate
    from decoy_engine.graph.ops._base import OpError

    src = OpError("something went wrong")
    src.code = "custom.code"  # type: ignore[attr-defined]
    src.path = "config.x"  # type: ignore[attr-defined]
    out = translate(src, "mask", "m1")
    assert getattr(out, "code", None) == "custom.code"
    assert getattr(out, "path", None) == "config.x"


def test_translate_bare_exception_has_no_metadata():
    """A plain Python exception with no code/path attribute should not
    cause translate() to crash; the returned OpError carries no metadata."""
    from decoy_engine.graph.errors import translate

    src = RuntimeError("kaboom")
    out = translate(src, "mask", "m1")
    assert getattr(out, "code", None) is None
    assert getattr(out, "path", None) is None
    assert "'m1'" in str(out)
