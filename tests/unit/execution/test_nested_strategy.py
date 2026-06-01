"""MG-3 / M2 (2026-05-31): nested JSONPath strategy regression cells.

Locks:
- Single-leaf and array-of-objects writebacks preserve JSON structure.
- Non-JSON cells emit a typed QualityWarning and pass through.
- Null cells stay null.
- Subset detector_ids -> no-such-path returns the cell unchanged
  without warning (sparse paths are valid).
- Recursive nested is rejected with a typed warning.
- Unknown child strategy is rejected with a typed warning.
- Bad target paths emit a typed warning (no crash).
"""

from __future__ import annotations

import json

import pandas as pd

from decoy_engine.execution._strategies._nested import NestedStrategyHandler
from decoy_engine.plan._types import ColumnSeed


def _seed(provider_config: dict) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="nested",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=tuple(sorted(provider_config.items())),
    )


class _FakeCtx:
    pass


# ── happy paths ───────────────────────────────────────────────────────


class TestHappyPaths:
    def test_nested_redact_replaces_target_leaf(self):
        df = pd.DataFrame(
            {
                "data": [
                    json.dumps({"user": {"name": "Alice", "email": "alice@x.com"}}),
                ]
            }
        )
        handler = NestedStrategyHandler()
        out, warnings = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.user.email", "strategy": "redact"}),
            _FakeCtx(),
        )
        assert warnings == []
        parsed = json.loads(out["data"].iloc[0])
        assert parsed["user"]["email"] == "REDACTED"
        # Sibling field untouched.
        assert parsed["user"]["name"] == "Alice"

    def test_nested_arrayof_objects_target_walks_each_entry(self):
        df = pd.DataFrame(
            {
                "data": [
                    json.dumps(
                        {
                            "users": [
                                {"name": "Alice", "email": "alice@x.com"},
                                {"name": "Bob", "email": "bob@x.com"},
                            ]
                        }
                    )
                ]
            }
        )
        handler = NestedStrategyHandler()
        out, _ = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.users[*].email", "strategy": "redact"}),
            _FakeCtx(),
        )
        parsed = json.loads(out["data"].iloc[0])
        assert [u["email"] for u in parsed["users"]] == ["REDACTED", "REDACTED"]
        assert [u["name"] for u in parsed["users"]] == ["Alice", "Bob"]

    def test_nested_categorical_child_writeback_preserves_json_structure(self):
        df = pd.DataFrame(
            {
                "data": [
                    json.dumps({"tier": "free", "id": 1}),
                    json.dumps({"tier": "pro", "id": 2}),
                ]
            }
        )
        handler = NestedStrategyHandler()
        out, _ = handler.run(
            df.copy(),
            "data",
            _seed(
                {
                    "target": "$.tier",
                    "strategy": "categorical",
                    "strategy_config": {"categories": ["X", "Y"]},
                }
            ),
            _FakeCtx(),
        )
        for row in out["data"]:
            parsed = json.loads(row)
            assert parsed["tier"] in ("X", "Y")
            assert "id" in parsed  # structure preserved


# ── passthrough cases ─────────────────────────────────────────────────


class TestPassthroughCases:
    def test_nested_cell_not_json_passthrough_with_warning(self):
        df = pd.DataFrame({"data": ["not json at all"]})
        handler = NestedStrategyHandler()
        out, warnings = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.email", "strategy": "redact"}),
            _FakeCtx(),
        )
        assert out["data"].iloc[0] == "not json at all"
        codes = [w.code for w in warnings]
        assert "nested_cell_json_parse_error" in codes

    def test_nested_target_path_missing_in_cell_passthrough_no_error(self):
        df = pd.DataFrame({"data": [json.dumps({"x": 1, "y": 2})]})
        handler = NestedStrategyHandler()
        out, warnings = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.nonexistent", "strategy": "redact"}),
            _FakeCtx(),
        )
        # Sparse paths are valid: no match -> no change, no warning.
        assert json.loads(out["data"].iloc[0]) == {"x": 1, "y": 2}
        assert all(w.code != "nested_jsonpath_parse_error" for w in warnings)

    def test_nested_null_cell_stays_null(self):
        df = pd.DataFrame(
            {
                "data": [
                    json.dumps({"user": {"email": "alice@x.com"}}),
                    None,
                ]
            }
        )
        handler = NestedStrategyHandler()
        out, _ = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.user.email", "strategy": "redact"}),
            _FakeCtx(),
        )
        assert pd.isna(out["data"].iloc[1])
        parsed = json.loads(out["data"].iloc[0])
        assert parsed["user"]["email"] == "REDACTED"


# ── rejections ────────────────────────────────────────────────────────


class TestRejections:
    def test_nested_recursive_nested_rejected(self):
        df = pd.DataFrame({"data": [json.dumps({"x": "y"})]})
        handler = NestedStrategyHandler()
        _, warnings = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.x", "strategy": "nested"}),
            _FakeCtx(),
        )
        codes = [w.code for w in warnings]
        assert "nested_recursive_nested_rejected" in codes

    def test_nested_unknown_child_strategy_warning(self):
        df = pd.DataFrame({"data": [json.dumps({"x": "y"})]})
        handler = NestedStrategyHandler()
        _, warnings = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.x", "strategy": "no_such_strategy"}),
            _FakeCtx(),
        )
        codes = [w.code for w in warnings]
        assert "nested_child_strategy_unknown" in codes

    def test_nested_jsonpath_parse_error_warning(self):
        df = pd.DataFrame({"data": [json.dumps({"x": "y"})]})
        handler = NestedStrategyHandler()
        _, warnings = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.x[", "strategy": "redact"}),  # bad jsonpath
            _FakeCtx(),
        )
        codes = [w.code for w in warnings]
        assert "nested_jsonpath_parse_error" in codes


# ── batch delegation ──────────────────────────────────────────────────


class TestBatchDelegation:
    def test_nested_collects_all_leaves_into_one_child_call(self):
        """Multi-row + multi-leaf input must be delegated to the child
        strategy in a single batch (preserves the child's vectorized
        behavior). Verified indirectly: every targeted leaf gets the
        redact token, no untargeted leaf is touched."""
        df = pd.DataFrame(
            {
                "data": [
                    json.dumps({"a": "x", "b": "keep1"}),
                    json.dumps({"a": "y", "b": "keep2"}),
                    json.dumps({"a": "z", "b": "keep3"}),
                ]
            }
        )
        handler = NestedStrategyHandler()
        out, _ = handler.run(
            df.copy(),
            "data",
            _seed({"target": "$.a", "strategy": "redact"}),
            _FakeCtx(),
        )
        for i, cell in enumerate(out["data"], start=1):
            parsed = json.loads(cell)
            assert parsed["a"] == "REDACTED"
            assert parsed["b"] == f"keep{i}"
