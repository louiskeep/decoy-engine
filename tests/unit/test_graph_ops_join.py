"""Unit tests for the `join` op and the multi-input arity error hint."""

import pandas as pd
import pytest

from decoy_engine.graph.ops import OPS, join
from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import GraphConfigValidator, ValidationError


class TestRegistry:
    def test_join_kind_registered(self):
        assert "join" in OPS, "'join' not registered in graph.ops.OPS"

    def test_join_arity_is_two_or_more(self):
        assert join.INPUT_ARITY == (2, None)


class TestJoinPositional:
    def test_concat_distinct_columns(self):
        df1 = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
        df2 = pd.DataFrame({"c": [5, 6], "d": [7, 8]})
        out = join.apply([df1, df2], {}, None)
        assert list(out.columns) == ["a", "b", "c", "d"]
        assert len(out) == 2
        assert list(out["c"]) == [5, 6]

    def test_three_way_positional(self):
        df1 = pd.DataFrame({"a": [1, 2]})
        df2 = pd.DataFrame({"b": [3, 4]})
        df3 = pd.DataFrame({"c": [5, 6]})
        out = join.apply([df1, df2, df3], {}, None)
        assert list(out.columns) == ["a", "b", "c"]

    def test_misaligned_indexes_are_normalized(self):
        # Indexes don't match — without reset_index, pd.concat would
        # NaN-fill. Confirm the op resets indexes so rows line up by position.
        df1 = pd.DataFrame({"a": [1, 2]}, index=[10, 20])
        df2 = pd.DataFrame({"b": [3, 4]}, index=[100, 200])
        out = join.apply([df1, df2], {}, None)
        assert list(out["a"]) == [1, 2]
        assert list(out["b"]) == [3, 4]
        assert not out.isna().any().any()


class TestJoinKeyed:
    def test_inner_join_on_single_column(self):
        df1 = pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"]})
        df2 = pd.DataFrame({"id": [1, 2, 3], "age": [25, 30, 35]})
        cfg = {"on": ["id"]}
        join.validate_config(cfg)
        out = join.apply([df1, df2], cfg, None)
        assert list(out.columns) == ["id", "name", "age"]
        assert len(out) == 3

    def test_inner_drops_unmatched(self):
        df1 = pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"]})
        df2 = pd.DataFrame({"id": [2, 3, 4], "age": [30, 35, 40]})
        out = join.apply([df1, df2], {"on": ["id"], "join_type": "inner"}, None)
        assert sorted(out["id"].tolist()) == [2, 3]

    def test_left_join_keeps_all_left(self):
        df1 = pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"]})
        df2 = pd.DataFrame({"id": [2, 3], "age": [30, 35]})
        out = join.apply([df1, df2], {"on": ["id"], "join_type": "left"}, None)
        assert sorted(out["id"].tolist()) == [1, 2, 3]
        assert pd.isna(out.loc[out["id"] == 1, "age"]).all()

    def test_outer_join_keeps_all(self):
        df1 = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})
        df2 = pd.DataFrame({"id": [2, 3], "age": [30, 35]})
        out = join.apply([df1, df2], {"on": ["id"], "join_type": "outer"}, None)
        assert sorted(out["id"].tolist()) == [1, 2, 3]

    def test_three_way_keyed(self):
        df1 = pd.DataFrame({"id": [1, 2], "x": [10, 20]})
        df2 = pd.DataFrame({"id": [1, 2], "y": [11, 22]})
        df3 = pd.DataFrame({"id": [1, 2], "z": [12, 24]})
        out = join.apply([df1, df2, df3], {"on": ["id"]}, None)
        assert sorted(out.columns.tolist()) == ["id", "x", "y", "z"]
        assert len(out) == 2

    def test_custom_suffixes_applied_on_collision(self):
        df1 = pd.DataFrame({"id": [1, 2], "v": [10, 20]})
        df2 = pd.DataFrame({"id": [1, 2], "v": [100, 200]})
        out = join.apply(
            [df1, df2],
            {"on": ["id"], "suffixes": ["_a", "_b"]},
            None,
        )
        assert "v_a" in out.columns and "v_b" in out.columns

    def test_missing_on_column_raises_op_error(self):
        df1 = pd.DataFrame({"id": [1], "x": [10]})
        df2 = pd.DataFrame({"oid": [1], "y": [20]})
        with pytest.raises(OpError, match="missing 'on'"):
            join.apply([df1, df2], {"on": ["id"]}, None)


class TestJoinValidation:
    def test_too_few_inputs_raises_op_error(self):
        df = pd.DataFrame({"x": [1]})
        with pytest.raises(OpError, match="at least 2"):
            join.apply([df], {}, None)

    @pytest.mark.parametrize(
        "cfg,path_substr",
        [
            ({"on": "id"}, "config.on"),
            ({"on": []}, "config.on"),
            ({"on": [""]}, "config.on"),
            ({"on": [1, 2]}, "config.on"),
            ({"join_type": "cross"}, "config.join_type"),
            ({"suffixes": ["_a"]}, "config.suffixes"),
            ({"suffixes": "x"}, "config.suffixes"),
            ({"suffixes": ["_a", 5]}, "config.suffixes"),
        ],
    )
    def test_validate_rejects_bad_config(self, cfg, path_substr):
        with pytest.raises(ValidationError) as exc:
            join.validate_config(cfg)
        assert path_substr in (exc.value.path or "")

    def test_validate_accepts_minimal_config(self):
        join.validate_config({})
        join.validate_config({"on": ["id"]})
        join.validate_config(
            {"on": ["id"], "join_type": "outer", "suffixes": ["_x", "_y"]}
        )


class TestArityHintsAtJoin:
    """The graph validator nudges users toward `join` when a single-input
    op (mask/gen/derive/...) is wired with too many incoming edges."""

    def _validate(self, cfg):
        import logging

        log = logging.getLogger("test_join_arity")
        if not log.handlers:
            log.addHandler(logging.NullHandler())
        GraphConfigValidator(log).validate(cfg)

    def test_two_edges_into_mask_hints_at_join(self):
        cfg = {
            "mode": "graph",
            "nodes": [
                {"id": "s1", "kind": "source.file", "config": {"path": "/tmp/a"}},
                {"id": "s2", "kind": "source.file", "config": {"path": "/tmp/b"}},
                {"id": "m", "kind": "mask", "config": {}},
            ],
            "edges": [{"from": "s1", "to": "m"}, {"from": "s2", "to": "m"}],
        }
        with pytest.raises(ValidationError) as exc:
            self._validate(cfg)
        assert "join" in str(exc.value)
        assert "at most 1" in str(exc.value)

    def test_two_edges_into_generate_hints_at_join(self):
        cfg = {
            "mode": "graph",
            "nodes": [
                {"id": "s1", "kind": "source.file", "config": {"path": "/tmp/a"}},
                {"id": "s2", "kind": "source.file", "config": {"path": "/tmp/b"}},
                {"id": "g", "kind": "generate", "config": {}},
            ],
            "edges": [{"from": "s1", "to": "g"}, {"from": "s2", "to": "g"}],
        }
        with pytest.raises(ValidationError) as exc:
            self._validate(cfg)
        assert "join" in str(exc.value)


class TestNodeNameField:
    """The optional `name` field on nodes — accepted by the validator,
    surfaces in run logs (covered in test_graph_logging)."""

    def _validate(self, cfg):
        import logging

        log = logging.getLogger("test_join_name")
        if not log.handlers:
            log.addHandler(logging.NullHandler())
        GraphConfigValidator(log).validate(cfg)

    def test_node_name_optional(self):
        # No `name` — still valid.
        self._validate({
            "mode": "graph",
            "nodes": [
                {"id": "s", "kind": "source.file", "config": {"path": "/tmp/a"}},
            ],
            "edges": [],
        })

    def test_node_name_string_accepted(self):
        self._validate({
            "mode": "graph",
            "nodes": [
                {
                    "id": "s",
                    "kind": "source.file",
                    "name": "Customers PII",
                    "config": {"path": "/tmp/a"},
                },
            ],
            "edges": [],
        })

    @pytest.mark.parametrize("bad", ["", "  ", 42, [], {}])
    def test_node_name_rejects_non_string_or_blank(self, bad):
        cfg = {
            "mode": "graph",
            "nodes": [
                {
                    "id": "s",
                    "kind": "source.file",
                    "name": bad,
                    "config": {"path": "/tmp/a"},
                },
            ],
            "edges": [],
        }
        with pytest.raises(ValidationError) as exc:
            self._validate(cfg)
        assert "name" in (exc.value.path or "")


class TestJoinPerPairJoins:
    """Per-pair joins (config.joins): each entry pins one merge in the
    chain to its own left_on / right_on / join_type. Lets a multi-way
    join use different keys for different pairs."""

    def test_two_way_per_pair_join(self):
        customers = pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"]})
        orders = pd.DataFrame({"customer_id": [1, 2, 3], "amount": [10, 20, 30]})
        cfg = {
            "joins": [
                {
                    "left_on": ["id"],
                    "right_on": ["customer_id"],
                    "join_type": "inner",
                },
            ],
        }
        join.validate_config(cfg)
        out = join.apply([customers, orders], cfg, None)
        assert list(out["amount"]) == [10, 20, 30]
        assert "id" in out.columns and "customer_id" in out.columns

    def test_three_way_per_pair_different_keys(self):
        customers = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})
        orders = pd.DataFrame({"customer_id": [1, 2], "amount": [10, 20]})
        products = pd.DataFrame({"buyer_id": [1, 2], "sku": ["x", "y"]})
        cfg = {
            "joins": [
                {"left_on": ["id"], "right_on": ["customer_id"], "join_type": "inner"},
                {"left_on": ["id"], "right_on": ["buyer_id"], "join_type": "left"},
            ],
        }
        join.validate_config(cfg)
        out = join.apply([customers, orders, products], cfg, None)
        assert list(out["amount"]) == [10, 20]
        assert list(out["sku"]) == ["x", "y"]

    def test_joins_length_mismatch_raises(self):
        df1 = pd.DataFrame({"id": [1]})
        df2 = pd.DataFrame({"id": [1]})
        df3 = pd.DataFrame({"id": [1]})
        # Only one join spec but three inputs (needs 2 pairings).
        cfg = {
            "joins": [
                {"left_on": ["id"], "right_on": ["id"], "join_type": "inner"},
            ],
        }
        with pytest.raises(OpError) as exc:
            join.apply([df1, df2, df3], cfg, None)
        assert "length" in str(exc.value).lower()

    def test_joins_and_on_are_mutually_exclusive(self):
        cfg = {
            "joins": [
                {"left_on": ["id"], "right_on": ["id"], "join_type": "inner"},
            ],
            "on": ["id"],
        }
        with pytest.raises(ValidationError) as exc:
            join.validate_config(cfg)
        assert "mutually exclusive" in str(exc.value).lower()

    def test_missing_left_on_column_raises(self):
        df1 = pd.DataFrame({"a": [1]})
        df2 = pd.DataFrame({"id": [1]})
        cfg = {
            "joins": [
                {"left_on": ["id"], "right_on": ["id"], "join_type": "inner"},
            ],
        }
        with pytest.raises(OpError) as exc:
            join.apply([df1, df2], cfg, None)
        assert "left_on" in str(exc.value) and "missing" in str(exc.value)
