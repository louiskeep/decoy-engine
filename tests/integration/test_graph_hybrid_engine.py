"""Phase 3 + 4: end-to-end integration tests for the `engine: hybrid` flag.

Confirms the runner reads the YAML key, dispatches each op through the
registry, and produces the same output on hybrid as on pandas.

Phase 3 covers the relational ops (filter / sort / dedupe / derive /
drop_column / select_column / limit). Phase 4 will extend this file to
cover the DuckDB-ported source / sink ops.
"""

from __future__ import annotations

import os
import tempfile

import pandas as pd
import pytest
import yaml

from decoy_engine import preview_graph, run_graph, validate_graph
from decoy_engine.exceptions import PipelineValidationError


@pytest.fixture
def tmp_csv():
    tmpdir = tempfile.mkdtemp()
    src = os.path.join(tmpdir, "in.csv")
    out_pandas = os.path.join(tmpdir, "out_pandas.csv")
    out_hybrid = os.path.join(tmpdir, "out_hybrid.csv")
    df = pd.DataFrame({
        "id": [1, 2, 3, 4, 5, 5],
        "state": ["CA", "NY", "CA", "TX", "CA", "CA"],
        "value": [10, 20, 30, 40, 50, 50],
    })
    df.to_csv(src, index=False)
    return src, out_pandas, out_hybrid


def _yaml(d: dict) -> str:
    return yaml.safe_dump(d)


def test_engine_pandas_default_unchanged(tmp_csv):
    """Pre-Phase-8: no `engine:` key meant pandas mode. Post-Phase-8 the
    default flipped to hybrid; this test confirms the no-key path still
    runs cleanly (just routes through the polars / duckdb ops now)."""
    src, out, _ = tmp_csv
    cfg = _yaml({
        "mode": "graph",
        "nodes": [
            {"id": "s", "kind": "source.file", "config": {"path": src}},
            {"id": "f", "kind": "filter", "config": {"predicate": "state == 'CA'"}},
            {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
        ],
        "edges": [{"from": "s", "to": "f"}, {"from": "f", "to": "t"}],
    })
    result = run_graph(cfg)
    assert result["success"] is True
    written = pd.read_csv(out)
    assert (written["state"] == "CA").all()


def test_engine_default_is_hybrid_after_phase_8(tmp_csv):
    """Phase 8 flip: graphs without an `engine:` key get hybrid mode."""
    from decoy_engine.graph.runner import _resolve_engine_mode

    cfg = {"mode": "graph", "nodes": [{"id": "x", "kind": "drop_column", "config": {"columns": []}}]}
    assert _resolve_engine_mode(cfg) == "hybrid"


def test_engine_pandas_opt_out_still_works(tmp_csv):
    """The one-release-cycle safety hatch: `engine: pandas` is the opt-out
    that forces every op through its pandas fallback regardless of
    declared NATIVE_ENGINE."""
    src, out, _ = tmp_csv
    cfg = _yaml({
        "mode": "graph",
        "engine": "pandas",
        "nodes": [
            {"id": "s", "kind": "source.file", "config": {"path": src}},
            {"id": "f", "kind": "filter", "config": {"predicate": "state == 'CA'"}},
            {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
        ],
        "edges": [{"from": "s", "to": "f"}, {"from": "f", "to": "t"}],
    })
    result = run_graph(cfg)
    assert result["success"] is True
    written = pd.read_csv(out)
    assert (written["state"] == "CA").all()


def test_engine_hybrid_filter_sort_dedupe(tmp_csv):
    src, _, out = tmp_csv
    cfg = _yaml({
        "mode": "graph",
        "engine": "hybrid",  # opt in
        "nodes": [
            {"id": "s", "kind": "source.file", "config": {"path": src}},
            {"id": "f", "kind": "filter", "config": {"predicate": "state == 'CA'"}},
            {"id": "d", "kind": "dedupe", "config": {"on": ["id"]}},
            {"id": "so", "kind": "sort", "config": {"by": ["id"], "order": "desc"}},
            {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
        ],
        "edges": [
            {"from": "s", "to": "f"},
            {"from": "f", "to": "d"},
            {"from": "d", "to": "so"},
            {"from": "so", "to": "t"},
        ],
    })
    result = run_graph(cfg)
    assert result["success"] is True
    written = pd.read_csv(out)
    # Three distinct CA ids: 1, 3, 5 (sorted desc)
    assert written["id"].tolist() == [5, 3, 1]


def test_engine_hybrid_and_pandas_produce_same_output(tmp_csv):
    """Same pipeline, two engine modes; outputs must match."""
    src, out_p, out_h = tmp_csv

    def _cfg(engine: str, output_path: str) -> str:
        d = {
            "mode": "graph",
            "nodes": [
                {"id": "s", "kind": "source.file", "config": {"path": src}},
                {"id": "f", "kind": "filter", "config": {"predicate": "value >= 20"}},
                {"id": "d", "kind": "dedupe", "config": {"on": ["id"]}},
                {"id": "so", "kind": "sort", "config": {"by": ["id"]}},
                {"id": "t", "kind": "target.file", "config": {"output_filename": output_path}},
            ],
            "edges": [
                {"from": "s", "to": "f"},
                {"from": "f", "to": "d"},
                {"from": "d", "to": "so"},
                {"from": "so", "to": "t"},
            ],
        }
        if engine != "default":
            d["engine"] = engine
        return yaml.safe_dump(d)

    assert run_graph(_cfg("default", out_p))["success"]
    assert run_graph(_cfg("hybrid", out_h))["success"]
    pd.testing.assert_frame_equal(
        pd.read_csv(out_p).reset_index(drop=True),
        pd.read_csv(out_h).reset_index(drop=True),
        check_dtype=False,
    )


def test_engine_hybrid_derive_adds_column(tmp_csv):
    src, _, out = tmp_csv
    cfg = _yaml({
        "mode": "graph",
        "engine": "hybrid",
        "nodes": [
            {"id": "s", "kind": "source.file", "config": {"path": src}},
            {"id": "dr", "kind": "derive", "config": {
                "column": "doubled",
                "expression": "value * 2",
            }},
            {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
        ],
        "edges": [{"from": "s", "to": "dr"}, {"from": "dr", "to": "t"}],
    })
    result = run_graph(cfg)
    assert result["success"] is True
    written = pd.read_csv(out)
    assert "doubled" in written.columns
    assert (written["doubled"] == written["value"] * 2).all()


def test_invalid_engine_value_rejected_at_validation():
    cfg = _yaml({
        "mode": "graph",
        "engine": "spark",  # not supported
        "nodes": [
            {"id": "a", "kind": "drop_column", "config": {"columns": ["x"]}},
        ],
        "edges": [],
    })
    with pytest.raises(PipelineValidationError) as ei:
        validate_graph(cfg)
    assert "engine" in str(ei.value).lower()


def test_preview_works_in_hybrid_mode(tmp_csv):
    src, _, _ = tmp_csv
    cfg = _yaml({
        "mode": "graph",
        "engine": "hybrid",
        "nodes": [
            {"id": "s", "kind": "source.file", "config": {"path": src}},
            {"id": "f", "kind": "filter", "config": {"predicate": "state == 'CA'"}},
        ],
        "edges": [{"from": "s", "to": "f"}],
    })
    p = preview_graph(cfg, "f", row_limit=10)
    assert p["row_count"] == 4
    assert p["error"] is None


def test_hybrid_end_to_end_three_engines_in_one_pipeline(tmp_csv):
    """The architecture's main claim: DuckDB at I/O, Polars in the middle,
    pandas at the mask boundary, all sharing Arrow. This test exercises
    a pipeline that crosses every engine boundary."""
    src, _, out = tmp_csv
    cfg = _yaml({
        "mode": "graph",
        "engine": "hybrid",
        "nodes": [
            # source.file → duckdb
            {"id": "s", "kind": "source.file", "config": {"path": src}},
            # filter → polars
            {"id": "f", "kind": "filter", "config": {"predicate": "state == 'CA'"}},
            # mask → pandas (per-row Faker / redact)
            {"id": "m", "kind": "mask", "config": {"columns": {
                "state": {"strategy": "redact", "redact_with": "***"},
            }}},
            # sort → polars
            {"id": "so", "kind": "sort", "config": {"by": ["id"]}},
            # target.file → duckdb
            {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
        ],
        "edges": [
            {"from": "s", "to": "f"},
            {"from": "f", "to": "m"},
            {"from": "m", "to": "so"},
            {"from": "so", "to": "t"},
        ],
    })
    result = run_graph(cfg)
    assert result["success"] is True
    written = pd.read_csv(out)
    # Filter kept CA rows; mask redacted state; sort stabilized order.
    assert (written["state"] == "***").all()
    assert sorted(written["id"].tolist()) == [1, 3, 5, 5]
