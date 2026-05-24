"""Unit tests for ExecutionContext.export().

The runner sets `_current_node_id` before each `op.apply()`; ops call
`ctx.export(key, value)` from inside apply(). Per-node exports
accumulate; outside a node scope the call no-ops so callers (preview,
external integrators) don't need to set anything.
"""

from decoy_engine.context import ExecutionContext


def test_export_outside_node_scope_no_ops():
    """When the runner hasn't set _current_node_id, export() silently
    drops the value rather than raising — preview and external callers
    pass op code without a recording scope and shouldn't crash."""
    ctx = ExecutionContext()
    ctx.export("rows_processed", 100)
    assert ctx._exports == {}


def test_export_inside_node_scope_records():
    ctx = ExecutionContext()
    ctx._current_node_id = "src1"
    ctx.export("row_count", 42)
    assert ctx._exports == {"src1": {"row_count": 42}}


def test_multiple_exports_accumulate():
    ctx = ExecutionContext()
    ctx._current_node_id = "src1"
    ctx.export("row_count", 42)
    ctx.export("column_count", 5)
    ctx.export("inferred_format", "csv")
    assert ctx._exports == {"src1": {"row_count": 42, "column_count": 5, "inferred_format": "csv"}}


def test_exports_isolated_per_node():
    ctx = ExecutionContext()
    ctx._current_node_id = "src1"
    ctx.export("row_count", 100)
    ctx._current_node_id = "mask1"
    ctx.export("rows_processed", 100)
    ctx._current_node_id = "tgt1"
    ctx.export("rows_written", 100)
    assert ctx._exports == {
        "src1": {"row_count": 100},
        "mask1": {"rows_processed": 100},
        "tgt1": {"rows_written": 100},
    }


def test_export_overwrites_same_key():
    ctx = ExecutionContext()
    ctx._current_node_id = "src1"
    ctx.export("row_count", 100)
    ctx.export("row_count", 999)
    assert ctx._exports["src1"]["row_count"] == 999
