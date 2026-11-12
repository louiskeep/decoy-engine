"""GEN-UNIQUE E2: the registered `unique` option and the `unique_together` field (AT-8, AT-13)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from decoy_engine import reasons
from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.config._tables import GENERATE_TYPES, GenerateColumnConfig, TableConfig
from tests.unit.generation.unique._gu_e2_support import (
    error_types,
    seq_parent,
    table,
    validate,
)

UNSUPPORTED = str(reasons.config.GENERATE_UNIQUE_UNSUPPORTED)
OPTION_TYPE = str(reasons.config.GENERATE_OPTION_TYPE)

# One valid column per generate type, so `unique` is the only thing under test.
VALID = {
    "formula": {"formula": "1 + 1"},
    "statistical": {"snapshot_file": "x.json"},
    "derived": {"expression": "a + 1"},
    "derived_aggregate": {"op": "sum", "column": "a"},
    "grouped_series": {"group_by": "a", "order_by": "b"},
    "windowed_date": {"anchor": "a", "max_days": 5},
    "group_key": {"group_by": "a"},
    "constant": {"value": 1},
    "distribution": {"family": "uniform", "low": 0, "high": 3},
    "lookup": {"file": "x.csv"},
}
OTHER_TEN = sorted(VALID)


def _col(type_: str, **extra):
    return GenerateColumnConfig(name="c", type=type_, **VALID[type_], **extra)


class TestAT13UnsupportedTypes:
    def test_the_other_ten_types_are_exactly_the_registry_remainder(self):
        assert set(OTHER_TEN) == GENERATE_TYPES - {
            "sequence",
            "categorical",
            "faker",
            "reference",
        }

    @pytest.mark.parametrize("type_", OTHER_TEN)
    def test_unique_true_is_refused_by_name(self, type_):
        with pytest.raises(ValidationError) as exc:
            _col(type_, unique=True)
        assert UNSUPPORTED in error_types(exc)

    @pytest.mark.parametrize("type_", OTHER_TEN)
    def test_unique_false_is_accepted_and_changes_nothing(self, type_):
        assert _col(type_, unique=False).type == type_

    @pytest.mark.parametrize(
        ("type_", "extra"),
        [
            ("sequence", {"start": 1}),
            ("categorical", {"categories": ["a"]}),
            ("faker", {"faker_type": "word"}),
            ("reference", {"reference_table": "p", "reference_column": "id"}),
        ],
    )
    def test_supported_types_accept_unique(self, type_, extra):
        assert GenerateColumnConfig(name="c", type=type_, unique=True, **extra)


class TestAT8StrictValues:
    @pytest.mark.parametrize("value", ["true", "false", 1, 0, "yes"])
    def test_non_bool_values_are_refused(self, value):
        # M6 (accept a string bool) turns this red.
        with pytest.raises(ValidationError) as exc:
            GenerateColumnConfig(name="c", type="sequence", start=1, unique=value)
        assert OPTION_TYPE in error_types(exc)

    def test_null_is_accepted_as_absent(self):
        col = GenerateColumnConfig(name="c", type="sequence", start=1, unique=None)
        assert col.model_extra["unique"] is None

    def test_unique_false_and_absent_dump_the_same_generation_bytes(self):
        from tests.unit.generation.unique._gu_e2_support import col_values, run

        base = {"name": "c", "type": "categorical", "categories": ["a", "b", "c"]}
        a = run([table("t", 30, [base])])
        b = run([table("t", 30, [{**base, "unique": False}])])
        c = run([table("t", 30, [{**base, "unique": None}])])
        assert col_values(a, "t", "c") == col_values(b, "t", "c") == col_values(c, "t", "c")

    def test_a_table_without_pairs_dumps_without_the_key(self):
        # M9 (dump `unique_together: []`) turns this red: every old config_json would change.
        dumped = TableConfig.model_validate(seq_parent("p", 3)).model_dump()
        assert "unique_together" not in dumped
        assert (
            "unique_together"
            not in json.loads(validate([seq_parent("p", 3)]).model_dump_json())["tables"][0]
        )

    def test_a_table_with_pairs_dumps_and_revalidates(self):
        tables = [
            seq_parent("pa", 5),
            seq_parent("pb", 5),
            table(
                "j",
                5,
                [
                    {
                        "name": "a",
                        "type": "reference",
                        "reference_table": "pa",
                        "reference_column": "id",
                    },
                    {
                        "name": "b",
                        "type": "reference",
                        "reference_table": "pb",
                        "reference_column": "id",
                    },
                ],
                unique_together=[["a", "b"]],
            ),
        ]
        first = validate(tables)
        again = PipelineConfig.model_validate(first.model_dump())
        assert again.tables[2].unique_together == [["a", "b"]]

    def test_serialization_schema_still_lists_table_fields(self):
        schema = TableConfig.model_json_schema(mode="serialization")
        assert {"name", "row_count", "generate_columns", "unique_together"} <= set(
            schema["properties"]
        )

    def test_schema_lists_unique_for_every_generate_type(self):
        schema = GenerateColumnConfig.model_json_schema()
        for branch in schema["allOf"]:
            assert "unique" in branch["then"]["properties"]

    def test_a_mask_table_cannot_carry_unique_together(self):
        with pytest.raises(ValidationError):
            TableConfig.model_validate(
                {
                    "name": "m",
                    "columns": [{"name": "x", "strategy": {"type": "hash"}}],
                    "unique_together": [["x", "y"]],
                }
            )
