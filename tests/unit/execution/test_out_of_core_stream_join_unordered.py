"""P4-A.3 Task B: the unordered join's pinned physical plan (acceptance test #5).

`StreamFkJoiner.explain_join()` parses the unordered join's own
`EXPLAIN (FORMAT JSON)` plan; this test asserts on operator TYPES -- a hash
join with the pinned optimizers disabled, no global sort operator, and the
build side is the deduplicated PARENT (a `read_parquet` scan) with the child
(a registered Arrow reader) as the streamed probe -- never the query's view
aliases. A plan that regains a global sort or flips the build side to the
child must redden this test (see `_stream_join.py::_verify_unordered_plan_or_raise`,
the same structural check `_iter_unordered_join_rows` runs on every real drain).
"""

from __future__ import annotations

from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner

from ._ooc_fixtures import simple_edge_fixture


def test_unordered_join_plan_pinned(tmp_path) -> None:
    fx = simple_edge_fixture(tmp_path / "simple")
    with StreamFkJoiner(
        edge=fx.edge,
        parent_relation=fx.parent_relation,
        child_key_types=fx.child_key_types,
        temp_dir=tmp_path / "join",
    ) as joiner:
        joiner.stage_keys(fx.child.to_batches())
        result = joiner.explain_join()

        # Pinned optimizers actually disabled on this connection.
        assert "build_side_probe_side" in result["disabled_optimizers"]
        assert "join_order" in result["disabled_optimizers"]

        plan = result["plan"]
        nodes: list[dict] = []
        stack = [plan]
        while stack:
            node = stack.pop()
            nodes.append(node)
            stack.extend(node.get("children", []))

        # No global sort/order operator anywhere in the unordered join's plan.
        names = {node["name"] for node in nodes}
        assert "ORDER_BY" not in names
        assert "TOP_N" not in names

        joins = [node for node in nodes if node["name"] == "HASH_JOIN"]
        assert len(joins) == 1
        join = joins[0]
        assert join["extra_info"]["Join Type"] == "LEFT"
        probe, build = join["children"]
        # Structural: the child is a registered Arrow reader (ARROW_SCAN, the
        # probe/streamed side); the deduplicated parent is a read_parquet
        # view (READ_PARQUET, the build side) -- never a check on the SQL
        # aliases "child_keys"/"parent_keys" themselves.
        assert "ARROW" in probe["name"]
        assert "PARQUET" in build["name"]
