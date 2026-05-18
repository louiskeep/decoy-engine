"""R2.2: mask op validation contract tests.

The mask op's validate_config now emits stable codes from
:mod:`decoy_engine.validation_result.CODES` on every failure path,
including the strategy-specific required-field gates (formula needs
``formula``, reference needs ``reference``) that the legacy runtime
validator used to raise at apply time.

The codes round-trip through the graph-level validator
(:func:`decoy_engine.validate_graph_full`) so the platform layer
can route each failure to the right inspector field.
"""
from __future__ import annotations

import pytest
import yaml

from decoy_engine import validate_graph_full, VALIDATION_CODES
from decoy_engine.graph.ops.mask_op import validate_config
from decoy_engine.internal.validator import ValidationError


def _wrap_graph(mask_columns):
    """Build a minimal src -> mask -> tgt graph with the given mask columns."""
    return yaml.safe_dump({
        "mode": "graph",
        "schema_version": 1,
        "nodes": [
            {"id": "src_1", "kind": "source.file",
             "config": {"path": "uploads/x.csv", "format": "csv"}},
            {"id": "mask_1", "kind": "mask",
             "config": {"columns": mask_columns}},
            {"id": "tgt_1", "kind": "target.file",
             "config": {"output_filename": "out.csv", "format": "csv"}},
        ],
        "edges": [
            {"from": "src_1", "to": "mask_1"},
            {"from": "mask_1", "to": "tgt_1"},
        ],
    })


class TestMaskValidateConfig:
    """Direct unit tests on validate_config - the gates raise with
    a stable code so the graph-level converter can preserve it."""

    def test_empty_columns_is_valid(self):
        # passthrough-everything is a legitimate state.
        validate_config({"columns": {}})
        validate_config({})

    def test_columns_not_mapping_raises(self):
        with pytest.raises(ValidationError) as exc:
            validate_config({"columns": ["a", "b"]})
        assert exc.value.code == VALIDATION_CODES.MASK_BAD_COLUMNS_TYPE

    def test_spec_not_mapping_raises(self):
        with pytest.raises(ValidationError) as exc:
            validate_config({"columns": {"a": "passthrough"}})
        assert exc.value.code == VALIDATION_CODES.MASK_BAD_COLUMN_SPEC_TYPE

    def test_unknown_strategy_raises(self):
        with pytest.raises(ValidationError) as exc:
            validate_config({"columns": {"a": {"strategy": "bogus"}}})
        assert exc.value.code == VALIDATION_CODES.MASK_UNKNOWN_STRATEGY

    def test_map_strategy_raises(self):
        with pytest.raises(ValidationError) as exc:
            validate_config({"columns": {"a": {"strategy": "map"}}})
        assert exc.value.code == VALIDATION_CODES.MASK_UNKNOWN_STRATEGY

    def test_formula_requires_formula_field(self):
        with pytest.raises(ValidationError) as exc:
            validate_config({"columns": {"a": {"strategy": "formula"}}})
        assert exc.value.code == VALIDATION_CODES.MASK_FORMULA_MISSING

    def test_formula_with_blank_string_raises(self):
        with pytest.raises(ValidationError) as exc:
            validate_config({"columns": {"a": {"strategy": "formula", "formula": "   "}}})
        assert exc.value.code == VALIDATION_CODES.MASK_FORMULA_MISSING

    def test_formula_with_expression_passes(self):
        validate_config({
            "columns": {"a": {"strategy": "formula", "formula": "value.upper()"}}
        })

    def test_reference_requires_reference_path(self):
        with pytest.raises(ValidationError) as exc:
            validate_config({"columns": {"a": {"strategy": "reference"}}})
        assert exc.value.code == VALIDATION_CODES.MASK_REFERENCE_MISSING

    def test_reference_with_dataset_passes(self):
        validate_config({
            "columns": {"a": {"strategy": "reference", "reference": "refs/names.csv"}}
        })


class TestMaskCodesRoundTripThroughGraph:
    """validate_graph_full preserves the code through the per-node
    validator path so platform consumers see the stable code, not the
    generic UNTAGGED fallback."""

    def test_unknown_strategy_code_survives(self):
        yaml_text = _wrap_graph({"a": {"strategy": "bogus"}})
        result = validate_graph_full(yaml_text)
        assert not result.ok
        assert result.errors[0].code == VALIDATION_CODES.MASK_UNKNOWN_STRATEGY

    def test_map_strategy_code_survives(self):
        yaml_text = _wrap_graph({"a": {"strategy": "map"}})
        result = validate_graph_full(yaml_text)
        assert not result.ok
        assert result.errors[0].code == VALIDATION_CODES.MASK_UNKNOWN_STRATEGY

    def test_formula_missing_code_survives(self):
        yaml_text = _wrap_graph({"col_a": {"strategy": "formula"}})
        result = validate_graph_full(yaml_text)
        assert not result.ok
        assert result.errors[0].code == VALIDATION_CODES.MASK_FORMULA_MISSING

    def test_reference_missing_code_survives(self):
        yaml_text = _wrap_graph({"col_b": {"strategy": "reference"}})
        result = validate_graph_full(yaml_text)
        assert not result.ok
        assert result.errors[0].code == VALIDATION_CODES.MASK_REFERENCE_MISSING
