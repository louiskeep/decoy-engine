"""Sprint 2.5: secondary ops surface stable validation codes through validate_graph_full.

Prior to Sprint 2.5, filter/derive/limit/sort/unite/generate raised ValidationError
without a `code` argument, so callers received CODES.UNTAGGED.  Each op now passes
its own code so the platform inspector can route the failure to the right field.
"""
from __future__ import annotations

import yaml

from decoy_engine import validate_graph_full, VALIDATION_CODES


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _graph(nodes, edges=None):
    return yaml.safe_dump({
        "mode": "graph",
        "schema_version": 1,
        "nodes": nodes,
        "edges": edges or [],
    })


def _single_node(kind: str, config: dict) -> str:
    return _graph([{"id": "n1", "kind": kind, "config": config}])


def _error_code(yaml_text: str) -> str:
    result = validate_graph_full(yaml_text)
    assert not result.ok, "expected validation failure but result.ok is True"
    return result.errors[0].code


# ---------------------------------------------------------------------------
# filter
# ---------------------------------------------------------------------------

class TestFilterValidationCodes:
    def test_empty_predicate_gives_filter_missing_predicate(self):
        yml = _single_node("filter", {"predicate": ""})
        assert _error_code(yml) == VALIDATION_CODES.FILTER_MISSING_PREDICATE

    def test_missing_predicate_gives_filter_missing_predicate(self):
        yml = _single_node("filter", {})
        assert _error_code(yml) == VALIDATION_CODES.FILTER_MISSING_PREDICATE

    def test_non_string_predicate_gives_filter_missing_predicate(self):
        yml = _single_node("filter", {"predicate": 42})
        assert _error_code(yml) == VALIDATION_CODES.FILTER_MISSING_PREDICATE


# ---------------------------------------------------------------------------
# derive
# ---------------------------------------------------------------------------

class TestDeriveValidationCodes:
    def test_missing_column_gives_derive_missing_column(self):
        yml = _single_node("derive", {"expression": "a + b"})
        assert _error_code(yml) == VALIDATION_CODES.DERIVE_MISSING_COLUMN

    def test_empty_column_gives_derive_missing_column(self):
        yml = _single_node("derive", {"column": "  ", "expression": "a + b"})
        assert _error_code(yml) == VALIDATION_CODES.DERIVE_MISSING_COLUMN

    def test_missing_expression_gives_derive_missing_expression(self):
        yml = _single_node("derive", {"column": "new_col"})
        assert _error_code(yml) == VALIDATION_CODES.DERIVE_MISSING_EXPRESSION

    def test_empty_expression_gives_derive_missing_expression(self):
        yml = _single_node("derive", {"column": "new_col", "expression": ""})
        assert _error_code(yml) == VALIDATION_CODES.DERIVE_MISSING_EXPRESSION


# ---------------------------------------------------------------------------
# limit
# ---------------------------------------------------------------------------

class TestLimitValidationCodes:
    def test_missing_n_gives_limit_bad_n(self):
        yml = _single_node("limit", {})
        assert _error_code(yml) == VALIDATION_CODES.LIMIT_BAD_N

    def test_negative_n_gives_limit_bad_n(self):
        yml = _single_node("limit", {"n": -1})
        assert _error_code(yml) == VALIDATION_CODES.LIMIT_BAD_N

    def test_bool_n_gives_limit_bad_n(self):
        yml = _single_node("limit", {"n": True})
        assert _error_code(yml) == VALIDATION_CODES.LIMIT_BAD_N

    def test_string_n_gives_limit_bad_n(self):
        yml = _single_node("limit", {"n": "10"})
        assert _error_code(yml) == VALIDATION_CODES.LIMIT_BAD_N


# ---------------------------------------------------------------------------
# sort
# ---------------------------------------------------------------------------

class TestSortValidationCodes:
    def test_missing_by_gives_sort_missing_by(self):
        yml = _single_node("sort", {})
        assert _error_code(yml) == VALIDATION_CODES.SORT_MISSING_BY

    def test_empty_by_gives_sort_missing_by(self):
        yml = _single_node("sort", {"by": []})
        assert _error_code(yml) == VALIDATION_CODES.SORT_MISSING_BY

    def test_non_list_by_gives_sort_missing_by(self):
        yml = _single_node("sort", {"by": "name"})
        assert _error_code(yml) == VALIDATION_CODES.SORT_MISSING_BY

    def test_bad_order_string_gives_sort_bad_order(self):
        yml = _single_node("sort", {"by": ["name"], "order": "random"})
        assert _error_code(yml) == VALIDATION_CODES.SORT_BAD_ORDER

    def test_order_length_mismatch_gives_sort_order_length_mismatch(self):
        yml = _single_node("sort", {"by": ["a", "b"], "order": ["asc"]})
        assert _error_code(yml) == VALIDATION_CODES.SORT_ORDER_LENGTH_MISMATCH

    def test_bad_order_list_entry_gives_sort_bad_order(self):
        yml = _single_node("sort", {"by": ["a"], "order": ["descending"]})
        assert _error_code(yml) == VALIDATION_CODES.SORT_BAD_ORDER

    def test_order_non_string_non_list_gives_sort_bad_order(self):
        yml = _single_node("sort", {"by": ["a"], "order": 1})
        assert _error_code(yml) == VALIDATION_CODES.SORT_BAD_ORDER


# ---------------------------------------------------------------------------
# unite
# ---------------------------------------------------------------------------

class TestUniteValidationCodes:
    def test_bad_on_type_gives_unite_bad_on(self):
        yml = _single_node("unite", {"on": "id"})
        assert _error_code(yml) == VALIDATION_CODES.UNITE_BAD_ON

    def test_empty_on_gives_unite_bad_on(self):
        yml = _single_node("unite", {"on": []})
        assert _error_code(yml) == VALIDATION_CODES.UNITE_BAD_ON

    def test_bad_join_type_gives_unite_bad_join_type(self):
        yml = _single_node("unite", {"join_type": "cross"})
        assert _error_code(yml) == VALIDATION_CODES.UNITE_BAD_JOIN_TYPE

    def test_bad_suffixes_gives_unite_bad_suffixes(self):
        yml = _single_node("unite", {"suffixes": ["_only_one"]})
        assert _error_code(yml) == VALIDATION_CODES.UNITE_BAD_SUFFIXES

    def test_non_list_suffixes_gives_unite_bad_suffixes(self):
        yml = _single_node("unite", {"suffixes": "_left"})
        assert _error_code(yml) == VALIDATION_CODES.UNITE_BAD_SUFFIXES


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

class TestGenerateValidationCodes:
    def test_columns_not_mapping_gives_generate_bad_columns_type(self):
        # Override the falsy-fallback: YAML produces a string, not falsy.
        yml = _graph([{
            "id": "gen_1",
            "kind": "generate",
            "config": {"columns": ["col_a"]},
        }])
        assert _error_code(yml) == VALIDATION_CODES.GENERATE_BAD_COLUMNS_TYPE

    def test_bad_row_count_gives_generate_bad_row_count(self):
        yml = _single_node("generate", {"row_count": 0})
        assert _error_code(yml) == VALIDATION_CODES.GENERATE_BAD_ROW_COUNT

    def test_negative_row_count_gives_generate_bad_row_count(self):
        yml = _single_node("generate", {"row_count": -5})
        assert _error_code(yml) == VALIDATION_CODES.GENERATE_BAD_ROW_COUNT

    def test_column_spec_not_mapping_gives_generate_bad_column_spec_type(self):
        yml = _single_node("generate", {
            "row_count": 10,
            "columns": {"name": "not-a-dict"},
        })
        assert _error_code(yml) == VALIDATION_CODES.GENERATE_BAD_COLUMN_SPEC_TYPE

    def test_unknown_strategy_gives_generate_unknown_strategy(self):
        yml = _single_node("generate", {
            "row_count": 10,
            "columns": {"name": {"strategy": "random_walk"}},
        })
        assert _error_code(yml) == VALIDATION_CODES.GENERATE_UNKNOWN_STRATEGY
