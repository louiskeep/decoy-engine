"""End-to-end tests for the graph pipeline runtime.

Verifies the public surface (validate_graph / run_graph / preview_graph) on
small synthetic graphs covering the MVP op set.
"""

import os
import tempfile

import pandas as pd
import pytest
import yaml

from decoy_engine import (
    preview_graph,
    run_graph,
    validate_graph,
)
from decoy_engine.exceptions import PipelineValidationError


@pytest.fixture
def tmp_csv():
    tmpdir = tempfile.mkdtemp()
    src = os.path.join(tmpdir, "in.csv")
    out = os.path.join(tmpdir, "out.csv")
    df = pd.DataFrame({
        "id": [1, 2, 3, 4, 5, 5],
        "state": ["CA", "NY", "CA", "TX", "CA", "CA"],
        "email": ["a@x.com", "b@x.com", "c@x.com", "d@x.com", "e@x.com", "e@x.com"],
        "ssn": ["111", "222", "333", "444", "555", "555"],
    })
    df.to_csv(src, index=False)
    return src, out


def _yaml(d):
    return yaml.safe_dump(d)


def test_run_drop_column_chain(tmp_csv):
    src, out = tmp_csv
    cfg = _yaml({
        "mode": "graph",
        "nodes": [
            {"id": "s", "kind": "source.file", "config": {"path": src}},
            {"id": "d", "kind": "drop_column", "config": {"columns": ["ssn"]}},
            {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
        ],
        "edges": [{"from": "s", "to": "d"}, {"from": "d", "to": "t"}],
    })
    validate_graph(cfg)
    result = run_graph(cfg)
    assert result["success"] is True
    written = pd.read_csv(out)
    assert "ssn" not in written.columns
    assert len(written) == 6


def test_drop_column_empty_is_noop(tmp_csv):
    """Empty / missing `columns` validates and runs as a no-op pass-through.

    Matches the canvas's drag-then-configure flow: dropping a drop_column
    node from the library shouldn't fail-validate the rest of the graph
    just because the user hasn't picked which columns to drop yet."""
    src, out = tmp_csv
    cfg = _yaml({
        "mode": "graph",
        "nodes": [
            {"id": "s", "kind": "source.file", "config": {"path": src}},
            # No `columns` field at all — fresh-from-library state.
            {"id": "d", "kind": "drop_column", "config": {}},
            {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
        ],
        "edges": [{"from": "s", "to": "d"}, {"from": "d", "to": "t"}],
    })
    validate_graph(cfg)   # must not raise
    result = run_graph(cfg)
    assert result["success"] is True
    written = pd.read_csv(out)
    # Source had `ssn` — drop_column was a no-op, so it survives.
    assert "ssn" in written.columns
    assert len(written) == 6


def test_run_filter_dedupe_mask(tmp_csv):
    src, out = tmp_csv
    cfg = _yaml({
        "mode": "graph",
        "nodes": [
            {"id": "s", "kind": "source.file", "config": {"path": src}},
            {"id": "f", "kind": "filter", "config": {"predicate": "state == 'CA'"}},
            {"id": "d", "kind": "dedupe", "config": {"on": ["id"]}},
            {"id": "m", "kind": "mask", "config": {"columns": {
                "email": {"strategy": "redact", "redact_with": "***"},
            }}},
            {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
        ],
        "edges": [
            {"from": "s", "to": "f"},
            {"from": "f", "to": "d"},
            {"from": "d", "to": "m"},
            {"from": "m", "to": "t"},
        ],
    })
    validate_graph(cfg)
    result = run_graph(cfg)
    assert result["success"] is True
    written = pd.read_csv(out)
    assert (written["state"] == "CA").all()
    assert (written["email"] == "***").all()
    # Three distinct CA ids: 1, 3, 5
    assert sorted(written["id"].tolist()) == [1, 3, 5]


def test_preview_at_each_node(tmp_csv):
    src, _ = tmp_csv
    cfg = _yaml({
        "mode": "graph",
        "nodes": [
            {"id": "s", "kind": "source.file", "config": {"path": src}},
            {"id": "f", "kind": "filter", "config": {"predicate": "state == 'CA'"}},
            {"id": "d", "kind": "drop_column", "config": {"columns": ["ssn"]}},
        ],
        "edges": [
            {"from": "s", "to": "f"},
            {"from": "f", "to": "d"},
        ],
    })
    p_src = preview_graph(cfg, "s", row_limit=10)
    p_filter = preview_graph(cfg, "f", row_limit=10)
    p_drop = preview_graph(cfg, "d", row_limit=10)

    assert "ssn" in p_src["columns"]
    assert p_src["row_count"] == 6
    assert p_filter["row_count"] == 4   # 4 CA rows
    assert "ssn" not in p_drop["columns"]
    assert p_drop["applied_chain"] == ["s", "f", "d"]


def test_run_records_failure(tmp_csv):
    src, out = tmp_csv
    cfg = _yaml({
        "mode": "graph",
        "nodes": [
            {"id": "s", "kind": "source.file", "config": {"path": src}},
            {"id": "d", "kind": "drop_column", "config": {"columns": ["does_not_exist"]}},
            {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
        ],
        "edges": [{"from": "s", "to": "d"}, {"from": "d", "to": "t"}],
    })
    result = run_graph(cfg)
    assert result["success"] is False
    failed = next(n for n in result["nodes"] if n["status"] == "error")
    assert failed["node_id"] == "d"
    assert "does_not_exist" in failed["error"]


@pytest.mark.parametrize("bad_cfg, msg_substr", [
    # cycle
    ({
        "mode": "graph",
        "nodes": [
            {"id": "a", "kind": "drop_column", "config": {"columns": ["x"]}},
            {"id": "b", "kind": "drop_column", "config": {"columns": ["y"]}},
        ],
        "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
    }, "cycle"),
    # unknown kind
    ({
        "mode": "graph",
        "nodes": [{"id": "x", "kind": "frobnicate", "config": {}}],
        "edges": [],
    }, "unknown kind"),
    # bad id
    ({
        "mode": "graph",
        "nodes": [{"id": "1bad", "kind": "drop_column", "config": {"columns": ["x"]}}],
        "edges": [],
    }, "id must match"),
    # duplicate ids
    ({
        "mode": "graph",
        "nodes": [
            {"id": "a", "kind": "drop_column", "config": {"columns": ["x"]}},
            {"id": "a", "kind": "drop_column", "config": {"columns": ["y"]}},
        ],
        "edges": [],
    }, "duplicate"),
    # ghost edge
    ({
        "mode": "graph",
        "nodes": [{"id": "a", "kind": "drop_column", "config": {"columns": ["x"]}}],
        "edges": [{"from": "a", "to": "ghost"}],
    }, "unknown node"),
])
def test_validator_rejects(bad_cfg, msg_substr):
    with pytest.raises(PipelineValidationError) as ei:
        validate_graph(yaml.safe_dump(bad_cfg))
    assert msg_substr in str(ei.value).lower()
