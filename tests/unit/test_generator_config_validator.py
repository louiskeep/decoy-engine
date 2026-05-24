# tests/unit/test_generator_config_validator.py
"""Tests for GeneratorConfigValidator — covers both the basic structural pass
and the deep per-table / per-column / relationship validation that was previously
dead code (wired in as of FINDING-S9-01 fix, 2026-05-23).
"""

import logging

import pytest

from decoy_engine.internal.validator import GeneratorConfigValidator, ValidationError


@pytest.fixture
def logger():
    return logging.getLogger("test_generator_validator")


@pytest.fixture
def validator(logger):
    return GeneratorConfigValidator(logger)


def _minimal_table(name="users", rows=10):
    return {
        "name": name,
        "rows": rows,
        "columns": [
            {"name": "id", "type": "sequence", "start": 1},
            {"name": "email", "type": "faker", "faker_type": "email"},
        ],
    }


class TestBasicStructuralChecks:
    def test_missing_tables_section(self, validator):
        with pytest.raises(ValidationError, match="Missing required section 'tables'"):
            validator.validate({})

    def test_missing_table_name(self, validator):
        config = {"tables": [{"columns": [{"name": "id", "type": "sequence"}]}]}
        with pytest.raises(ValidationError, match="missing a 'name'"):
            validator.validate(config)

    def test_missing_columns_section(self, validator):
        config = {"tables": [{"name": "users"}]}
        with pytest.raises(ValidationError, match="missing 'columns'"):
            validator.validate(config)

    def test_invalid_output_type(self, validator):
        table = _minimal_table()
        table["output_type"] = "json"
        with pytest.raises(ValidationError, match="Unsupported output_type"):
            validator.validate({"tables": [table]})

    def test_fixed_width_missing_definition_path(self, validator):
        table = _minimal_table()
        table["output_type"] = "fixed_width"
        table["fixed_width_options"] = {}
        with pytest.raises(ValidationError, match="definition_path"):
            validator.validate({"tables": [table]})

    def test_valid_config_passes(self, validator):
        config = {"tables": [_minimal_table()]}
        # must not raise
        validator.validate(config)


class TestDeepTableValidation:
    """Previously dead code — _validate_tables wired into validate()."""

    def test_duplicate_table_names_caught(self, validator):
        config = {"tables": [_minimal_table("users"), _minimal_table("users")]}
        with pytest.raises(ValidationError, match="Duplicate table name"):
            validator.validate(config)

    def test_rows_must_be_positive_int(self, validator):
        table = _minimal_table(rows=0)
        with pytest.raises(ValidationError, match="positive integer"):
            validator.validate({"tables": [table]})

    def test_rows_must_not_be_negative(self, validator):
        table = _minimal_table(rows=-5)
        with pytest.raises(ValidationError, match="positive integer"):
            validator.validate({"tables": [table]})

    def test_rows_defaulted_when_missing(self, validator):
        table = {"name": "users", "columns": [{"name": "id", "type": "sequence"}]}
        config = {"tables": [table]}
        validator.validate(config)
        assert table["rows"] == 1000


class TestDeepColumnValidation:
    """Previously dead code — _validate_columns / _validate_column_type_specific."""

    def test_duplicate_column_names_caught(self, validator):
        table = {
            "name": "users",
            "rows": 5,
            "columns": [
                {"name": "id", "type": "sequence"},
                {"name": "id", "type": "faker", "faker_type": "name"},
            ],
        }
        with pytest.raises(ValidationError, match="Duplicate column name"):
            validator.validate({"tables": [table]})

    def test_unsupported_column_type_caught(self, validator):
        table = {
            "name": "users",
            "rows": 5,
            "columns": [{"name": "id", "type": "uuid"}],
        }
        with pytest.raises(ValidationError, match="Unsupported column type"):
            validator.validate({"tables": [table]})

    def test_null_probability_out_of_range(self, validator):
        table = {
            "name": "users",
            "rows": 5,
            "columns": [{"name": "id", "type": "sequence", "null_probability": 1.5}],
        }
        with pytest.raises(ValidationError, match="null_probability"):
            validator.validate({"tables": [table]})

    def test_categorical_missing_categories(self, validator):
        table = {
            "name": "users",
            "rows": 5,
            "columns": [{"name": "status", "type": "categorical"}],
        }
        with pytest.raises(ValidationError, match="'categories'"):
            validator.validate({"tables": [table]})

    def test_categorical_weight_length_mismatch(self, validator):
        table = {
            "name": "users",
            "rows": 5,
            "columns": [
                {
                    "name": "status",
                    "type": "categorical",
                    "categories": ["a", "b"],
                    "weights": [0.5, 0.3, 0.2],
                }
            ],
        }
        with pytest.raises(ValidationError, match="'weights'"):
            validator.validate({"tables": [table]})

    def test_reference_missing_reference_table(self, validator):
        table = {
            "name": "orders",
            "rows": 5,
            "columns": [{"name": "user_id", "type": "reference", "reference_column": "id"}],
        }
        with pytest.raises(ValidationError, match="reference_table"):
            validator.validate({"tables": [table]})

    def test_formula_missing_formula_field(self, validator):
        table = {
            "name": "users",
            "rows": 5,
            "columns": [{"name": "full_name", "type": "formula"}],
        }
        with pytest.raises(ValidationError, match="'formula'"):
            validator.validate({"tables": [table]})

    def test_formula_references_must_be_list(self, validator):
        table = {
            "name": "users",
            "rows": 5,
            "columns": [
                {
                    "name": "full_name",
                    "type": "formula",
                    "formula": "f'{first} {last}'",
                    "references": "not-a-list",
                }
            ],
        }
        with pytest.raises(ValidationError, match=r"references.*list"):
            validator.validate({"tables": [table]})

    def test_faker_type_defaulted(self, validator):
        col = {"name": "note", "type": "faker"}
        table = {"name": "users", "rows": 5, "columns": [col]}
        validator.validate({"tables": [table]})
        assert col["faker_type"] == "word"


class TestRelationshipValidation:
    """Previously dead code — _validate_relationships wired into validate()."""

    def test_missing_relationship_name(self, validator):
        config = {
            "tables": [_minimal_table("users")],
            "relationships": [
                {
                    "type": "foreign_key",
                    "source_table": "users",
                    "source_column": "id",
                    "target_table": "users",
                    "target_column": "id",
                }
            ],
        }
        with pytest.raises(ValidationError, match="'name'"):
            validator.validate(config)

    def test_unsupported_relationship_type(self, validator):
        config = {
            "tables": [_minimal_table("users")],
            "relationships": [{"name": "r1", "type": "unknown_type"}],
        }
        with pytest.raises(ValidationError, match="Unsupported relationship type"):
            validator.validate(config)

    def test_foreign_key_missing_target_table(self, validator):
        config = {
            "tables": [_minimal_table("orders")],
            "relationships": [
                {
                    "name": "fk_user",
                    "type": "foreign_key",
                    "source_table": "orders",
                    "source_column": "user_id",
                    "target_table": "nonexistent_users",
                    "target_column": "id",
                }
            ],
        }
        with pytest.raises(ValidationError, match="nonexistent_users"):
            validator.validate(config)

    def test_valid_foreign_key_passes(self, validator):
        users = _minimal_table("users")
        orders = {
            "name": "orders",
            "rows": 5,
            "columns": [
                {"name": "order_id", "type": "sequence"},
                {
                    "name": "user_id",
                    "type": "reference",
                    "reference_table": "users",
                    "reference_column": "id",
                },
            ],
        }
        config = {
            "tables": [users, orders],
            "relationships": [
                {
                    "name": "fk_orders_users",
                    "type": "foreign_key",
                    "source_table": "orders",
                    "source_column": "user_id",
                    "target_table": "users",
                    "target_column": "id",
                }
            ],
        }
        # must not raise
        validator.validate(config)


class TestExceptionType:
    """FINDING-S9-02: validate() must raise ValidationError, not ValueError."""

    def test_missing_tables_raises_validation_error(self, validator):
        with pytest.raises(ValidationError):
            validator.validate({})

    def test_missing_tables_does_not_raise_value_error(self, validator):
        with pytest.raises(Exception) as exc_info:
            validator.validate({})
        assert not isinstance(exc_info.value, ValueError), (
            "validate() must raise ValidationError, not ValueError"
        )
