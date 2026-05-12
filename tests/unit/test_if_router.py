"""Tests for IF router (two-output-port row router)."""
import pandas as pd
import polars as pl
import pytest

from decoy_engine.graph.ops import if_router
from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError


def _pd(records):
    return pd.DataFrame(records)


def _pl(records):
    return pl.DataFrame(records)


class TestValidateConfig:
    def test_valid(self):
        if_router.validate_config({"predicate": "age >= 18"})

    def test_empty_predicate_raises(self):
        with pytest.raises(ValidationError):
            if_router.validate_config({"predicate": ""})

    def test_missing_predicate_raises(self):
        with pytest.raises(ValidationError):
            if_router.validate_config({})

    def test_non_string_predicate_raises(self):
        with pytest.raises(ValidationError):
            if_router.validate_config({"predicate": 42})


class TestApplyPolars:
    def test_basic_split(self):
        df = _pl([{"age": 10, "name": "Alice"}, {"age": 20, "name": "Bob"}, {"age": 15, "name": "Carol"}])
        result = if_router.apply([df], {"predicate": "age >= 18"})
        assert isinstance(result, dict)
        assert set(result.keys()) == {"pass", "fail"}
        assert len(result["pass"]) == 1
        assert result["pass"]["name"][0] == "Bob"
        assert len(result["fail"]) == 2

    def test_all_pass(self):
        df = _pl([{"x": 1}, {"x": 2}])
        result = if_router.apply([df], {"predicate": "x > 0"})
        assert len(result["pass"]) == 2
        assert len(result["fail"]) == 0

    def test_all_fail(self):
        df = _pl([{"x": 1}, {"x": 2}])
        result = if_router.apply([df], {"predicate": "x > 100"})
        assert len(result["pass"]) == 0
        assert len(result["fail"]) == 2

    def test_none_input_returns_none_ports(self):
        result = if_router.apply([None], {"predicate": "x > 0"})
        assert result["pass"] is None
        assert result["fail"] is None

    def test_bad_predicate_raises_op_error(self):
        df = _pl([{"x": 1}])
        with pytest.raises(OpError):
            if_router.apply([df], {"predicate": "totally_missing_col > 0 AND also_missing = 1"})

    def test_row_counts_sum_to_total(self):
        df = _pl([{"v": i} for i in range(20)])
        result = if_router.apply([df], {"predicate": "v < 10"})
        assert len(result["pass"]) + len(result["fail"]) == 20


class TestApplyPandas:
    def test_basic_split(self):
        df = _pd([{"age": 10}, {"age": 20}, {"age": 15}])
        result = if_router.apply([df], {"predicate": "age >= 18"})
        assert len(result["pass"]) == 1
        assert len(result["fail"]) == 2

    def test_index_reset(self):
        df = _pd([{"v": 1}, {"v": 2}, {"v": 3}])
        result = if_router.apply([df], {"predicate": "v > 1"})
        assert list(result["pass"].index) == list(range(len(result["pass"])))
        assert list(result["fail"].index) == list(range(len(result["fail"])))

    def test_row_counts_sum_to_total(self):
        df = _pd([{"v": i} for i in range(10)])
        result = if_router.apply([df], {"predicate": "v < 5"})
        assert len(result["pass"]) + len(result["fail"]) == 10


class TestMetadata:
    def test_kind(self):
        assert if_router.KIND == "if"

    def test_output_ports(self):
        assert if_router.OUTPUT_PORTS == ("pass", "fail")

    def test_output_kind(self):
        assert if_router.OUTPUT_KIND == "split"

    def test_native_engine(self):
        assert if_router.NATIVE_ENGINE == "polars"

    def test_input_arity(self):
        assert if_router.INPUT_ARITY == (1, 1)
