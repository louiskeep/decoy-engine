"""Unit tests for the sort / limit / derive graph ops added in P1
(dev-prod-environments plan, Decision B)."""

import pandas as pd
import pytest

from decoy_engine.graph.ops import OPS, derive, limit, sort
from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError


@pytest.fixture
def df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": [3, 1, 2, 1, 4],
            "state": ["CA", "NY", "CA", "NY", "TX"],
            "score": [10, 20, 30, 20, 40],
        }
    )


class TestRegistry:
    def test_new_kinds_registered(self):
        for kind in ("sort", "limit", "derive"):
            assert kind in OPS, f"{kind!r} not registered in graph.ops.OPS"


class TestSort:
    def test_sort_ascending_single_column(self, df):
        sort.validate_config({"by": ["id"]})
        out = sort.apply([df], {"by": ["id"]}, None)
        assert list(out["id"]) == [1, 1, 2, 3, 4]

    def test_sort_descending(self, df):
        sort.validate_config({"by": ["score"], "order": "desc"})
        out = sort.apply([df], {"by": ["score"], "order": "desc"}, None)
        assert list(out["score"]) == [40, 30, 20, 20, 10]

    def test_sort_multi_column_mixed_directions(self, df):
        # state asc, then score desc within state
        cfg = {"by": ["state", "score"], "order": ["asc", "desc"]}
        sort.validate_config(cfg)
        out = sort.apply([df], cfg, None)
        assert list(out["state"]) == ["CA", "CA", "NY", "NY", "TX"]
        assert list(out["score"][:2]) == [30, 10]

    def test_sort_is_stable(self):
        # Two rows tying on key — original order preserved (mergesort).
        d = pd.DataFrame({"k": [1, 1, 1], "tag": ["a", "b", "c"]})
        out = sort.apply([d], {"by": ["k"]}, None)
        assert list(out["tag"]) == ["a", "b", "c"]

    def test_sort_resets_index(self, df):
        out = sort.apply([df], {"by": ["id"]}, None)
        assert list(out.index) == [0, 1, 2, 3, 4]

    def test_sort_missing_column_raises_op_error(self, df):
        with pytest.raises(OpError, match="not in input"):
            sort.apply([df], {"by": ["nope"]}, None)

    @pytest.mark.parametrize(
        "cfg,path_substr",
        [
            ({}, "config.by"),
            ({"by": []}, "config.by"),
            ({"by": ["id"], "order": "sideways"}, "config.order"),
            ({"by": ["id", "state"], "order": ["asc"]}, "config.order"),
            ({"by": ["id"], "order": ["nope"]}, "config.order"),
            ({"by": ["id"], "order": 5}, "config.order"),
        ],
    )
    def test_sort_validation_errors(self, cfg, path_substr):
        with pytest.raises(ValidationError) as exc:
            sort.validate_config(cfg)
        assert path_substr in (exc.value.path or "")


class TestLimit:
    def test_limit_keeps_first_n(self, df):
        limit.validate_config({"n": 2})
        out = limit.apply([df], {"n": 2}, None)
        assert len(out) == 2
        assert list(out["id"]) == [3, 1]

    def test_limit_zero(self, df):
        out = limit.apply([df], {"n": 0}, None)
        assert len(out) == 0

    def test_limit_larger_than_input(self, df):
        out = limit.apply([df], {"n": 100}, None)
        assert len(out) == 5

    @pytest.mark.parametrize("bad", [None, -1, "5", 1.5, True])
    def test_limit_validation_errors(self, bad):
        with pytest.raises(ValidationError):
            limit.validate_config({"n": bad})


class TestDerive:
    def test_derive_arithmetic(self, df):
        cfg = {"column": "doubled", "expression": "score * 2"}
        derive.validate_config(cfg)
        out = derive.apply([df], cfg, None)
        assert list(out["doubled"]) == [20, 40, 60, 40, 80]

    def test_derive_constant(self, df):
        out = derive.apply([df], {"column": "tag", "expression": "1"}, None)
        assert list(out["tag"]) == [1, 1, 1, 1, 1]

    def test_derive_overwrites_existing_column(self, df):
        out = derive.apply([df], {"column": "score", "expression": "score + 1"}, None)
        assert list(out["score"]) == [11, 21, 31, 21, 41]
        # Input frame must not be mutated.
        assert list(df["score"]) == [10, 20, 30, 20, 40]

    def test_derive_multi_column_expression(self, df):
        out = derive.apply([df], {"column": "score_x_id", "expression": "score * id"}, None)
        assert list(out["score_x_id"]) == [30, 20, 60, 20, 160]

    def test_derive_bad_expression_raises_op_error(self, df):
        with pytest.raises(OpError, match="derive expression failed"):
            derive.apply([df], {"column": "x", "expression": "no_such_column + 1"}, None)

    @pytest.mark.parametrize(
        "cfg,path_substr",
        [
            ({"expression": "id + 1"}, "config.column"),
            ({"column": "  ", "expression": "id"}, "config.column"),
            ({"column": "x"}, "config.expression"),
            ({"column": "x", "expression": ""}, "config.expression"),
            ({"column": "x", "expression": 5}, "config.expression"),
        ],
    )
    def test_derive_validation_errors(self, cfg, path_substr):
        with pytest.raises(ValidationError) as exc:
            derive.validate_config(cfg)
        assert path_substr in (exc.value.path or "")
