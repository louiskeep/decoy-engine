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


def test_preview_ignores_broken_downstream_nodes(tmp_csv):
    """Sampling at an upstream node must work even when a downstream node
    has a validation error. If the pipeline works up to `f`, the user
    should be able to grab data out of `f` for a sample regardless of
    what's wrong further down the graph."""
    src, _ = tmp_csv
    cfg = _yaml({
        "mode": "graph",
        "nodes": [
            {"id": "s", "kind": "source.file", "config": {"path": src}},
            {"id": "f", "kind": "filter", "config": {"predicate": "state == 'CA'"}},
            # Broken downstream: unknown kind. Full-pipeline validation
            # would reject this whole config.
            {"id": "bad", "kind": "no_such_op", "config": {}},
        ],
        "edges": [
            {"from": "s", "to": "f"},
            {"from": "f", "to": "bad"},
        ],
    })
    # validate_graph still rejects the full config.
    with pytest.raises(PipelineValidationError):
        validate_graph(cfg)

    # But sampling an upstream node succeeds.
    p_filter = preview_graph(cfg, "f", row_limit=10)
    assert p_filter["row_count"] == 4
    assert p_filter["applied_chain"] == ["s", "f"]
    assert p_filter["error"] is None

    # And sampling the source node also succeeds.
    p_src = preview_graph(cfg, "s", row_limit=10)
    assert p_src["row_count"] == 6
    assert p_src["error"] is None


def test_preview_ignores_unrelated_broken_branch(tmp_csv):
    """A broken node on a parallel branch must not block sampling on the
    healthy branch."""
    src, _ = tmp_csv
    cfg = _yaml({
        "mode": "graph",
        "nodes": [
            {"id": "s", "kind": "source.file", "config": {"path": src}},
            {"id": "f", "kind": "filter", "config": {"predicate": "state == 'CA'"}},
            # Sibling branch off `s` with a broken kind.
            {"id": "junk", "kind": "no_such_op", "config": {}},
        ],
        "edges": [
            {"from": "s", "to": "f"},
            {"from": "s", "to": "junk"},
        ],
    })
    p_filter = preview_graph(cfg, "f", row_limit=10)
    assert p_filter["row_count"] == 4
    assert p_filter["error"] is None


def test_preview_still_rejects_broken_upstream(tmp_csv):
    """If the path *to* the target is broken, sampling must still raise."""
    src, _ = tmp_csv
    cfg = _yaml({
        "mode": "graph",
        "nodes": [
            {"id": "s", "kind": "source.file", "config": {"path": src}},
            {"id": "bad", "kind": "no_such_op", "config": {}},
            {"id": "f", "kind": "filter", "config": {"predicate": "state == 'CA'"}},
        ],
        "edges": [
            {"from": "s", "to": "bad"},
            {"from": "bad", "to": "f"},
        ],
    })
    with pytest.raises(PipelineValidationError):
        preview_graph(cfg, "f", row_limit=10)


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


@pytest.fixture
def tmp_two_csvs():
    """Two CSVs that share an `id` column so `join` can keyed-join them."""
    tmpdir = tempfile.mkdtemp()
    src1 = os.path.join(tmpdir, "people.csv")
    src2 = os.path.join(tmpdir, "scores.csv")
    out = os.path.join(tmpdir, "out.csv")
    pd.DataFrame({
        "id": [1, 2, 3],
        "name": ["ana", "bo", "cy"],
    }).to_csv(src1, index=False)
    pd.DataFrame({
        "id": [1, 2, 3],
        "score": [99, 87, 73],
    }).to_csv(src2, index=False)
    return src1, src2, out


def test_run_join_keyed_join(tmp_two_csvs):
    """join combines two upstream tables into one wider one before mask."""
    src1, src2, out = tmp_two_csvs
    cfg = _yaml({
        "mode": "graph",
        "nodes": [
            {"id": "p", "kind": "source.file", "config": {"path": src1}},
            {"id": "s", "kind": "source.file", "config": {"path": src2}},
            {"id": "u", "kind": "join", "config": {"on": ["id"]}},
            {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
        ],
        "edges": [
            {"from": "p", "to": "u"},
            {"from": "s", "to": "u"},
            {"from": "u", "to": "t"},
        ],
    })
    validate_graph(cfg)
    result = run_graph(cfg)
    assert result["success"] is True
    written = pd.read_csv(out)
    assert sorted(written.columns.tolist()) == ["id", "name", "score"]
    assert len(written) == 3


def test_run_join_then_mask(tmp_two_csvs):
    """End-to-end: two sources → join → mask. The motivation for `join`:
    mask only accepts one input, so multi-source pipelines must merge first."""
    src1, src2, out = tmp_two_csvs
    cfg = _yaml({
        "mode": "graph",
        "nodes": [
            {"id": "p", "kind": "source.file", "config": {"path": src1}},
            {"id": "s", "kind": "source.file", "config": {"path": src2}},
            {"id": "u", "kind": "join", "config": {"on": ["id"]}},
            {"id": "m", "kind": "mask", "config": {"columns": {
                "name": {"strategy": "redact", "redact_with": "***"},
            }}},
            {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
        ],
        "edges": [
            {"from": "p", "to": "u"},
            {"from": "s", "to": "u"},
            {"from": "u", "to": "m"},
            {"from": "m", "to": "t"},
        ],
    })
    validate_graph(cfg)
    result = run_graph(cfg)
    assert result["success"] is True
    written = pd.read_csv(out)
    assert (written["name"] == "***").all()
    assert sorted(written["score"].tolist()) == [73, 87, 99]


def test_arity_error_hints_at_join_for_mask():
    cfg = _yaml({
        "mode": "graph",
        "nodes": [
            {"id": "s1", "kind": "source.file", "config": {"path": "/tmp/a"}},
            {"id": "s2", "kind": "source.file", "config": {"path": "/tmp/b"}},
            {"id": "m", "kind": "mask", "config": {}},
        ],
        "edges": [{"from": "s1", "to": "m"}, {"from": "s2", "to": "m"}],
    })
    with pytest.raises(PipelineValidationError) as ei:
        validate_graph(cfg)
    assert "join" in str(ei.value)


def test_run_logs_include_node_name_when_set(tmp_csv):
    """The optional `name` field surfaces in logs alongside the id+kind tail."""
    from decoy_engine import ExecutionContext

    class CapturingLogger:
        def __init__(self):
            self.lines: list[str] = []

        def _emit(self, msg, args):
            self.lines.append(msg % args if args else msg)

        def debug(self, msg, *args, **kwargs): self._emit(msg, args)
        def info(self, msg, *args, **kwargs): self._emit(msg, args)
        def warning(self, msg, *args, **kwargs): self._emit(msg, args)
        def error(self, msg, *args, **kwargs): self._emit(msg, args)

    src, out = tmp_csv
    cfg = _yaml({
        "mode": "graph",
        "nodes": [
            {"id": "src1", "kind": "source.file", "name": "Customers PII", "config": {"path": src}},
            {"id": "tgt1", "kind": "target.file", "name": "Cleaned export", "config": {"output_filename": out}},
        ],
        "edges": [{"from": "src1", "to": "tgt1"}],
    })
    logger = CapturingLogger()
    result = run_graph(cfg, ctx=ExecutionContext(logger=logger))
    assert result["success"] is True
    joined = "\n".join(logger.lines)
    # Each log line carries id, kind, and (when set) the human name.
    assert "id=src1" in joined and "kind=source.file" in joined
    assert "Customers PII" in joined
    assert "id=tgt1" in joined and "kind=target.file" in joined
    assert "Cleaned export" in joined


def test_run_logs_fallback_without_name(tmp_csv):
    """Nodes without `name` still log a clean id+kind descriptor."""
    from decoy_engine import ExecutionContext

    class CapturingLogger:
        def __init__(self):
            self.lines: list[str] = []

        def _emit(self, msg, args):
            self.lines.append(msg % args if args else msg)

        def debug(self, msg, *args, **kwargs): self._emit(msg, args)
        def info(self, msg, *args, **kwargs): self._emit(msg, args)
        def warning(self, msg, *args, **kwargs): self._emit(msg, args)
        def error(self, msg, *args, **kwargs): self._emit(msg, args)

    src, out = tmp_csv
    cfg = _yaml({
        "mode": "graph",
        "nodes": [
            {"id": "s", "kind": "source.file", "config": {"path": src}},
            {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
        ],
        "edges": [{"from": "s", "to": "t"}],
    })
    logger = CapturingLogger()
    result = run_graph(cfg, ctx=ExecutionContext(logger=logger))
    assert result["success"] is True
    joined = "\n".join(logger.lines)
    assert "id=s" in joined and "id=t" in joined
    assert "kind=source.file" in joined and "kind=target.file" in joined


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
