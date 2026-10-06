"""Logs never carry user expression text or data values: both can hold PII."""

from __future__ import annotations

import logging
import warnings

import pandas as pd

from decoy_engine.execution._transforms import FilterOp, apply_transforms
from decoy_engine.execution._when_gate import _eval_predicate
from decoy_engine.transforms.date_shift import DateShiftStrategy
from tests.unit._dps_helpers import compile_and_generate

_SECRET = "bob@example.com"


def _nullable_frame() -> pd.DataFrame:
    # Extension dtypes make pandas fall back from numexpr, which triggers the log line.
    return pd.DataFrame(
        {
            "email": pd.array([_SECRET, "x@y.z", None], dtype="string"),
            "age": pd.array([10, 20, 30], dtype="Int64"),
        }
    )


def _fallback_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "fell back" in r.getMessage()]


def test_when_fallback_log_names_the_column_not_the_expression(caplog) -> None:
    expr = f"email == '{_SECRET}' and age >= 18"
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with caplog.at_level(logging.WARNING, logger="decoy_engine.execution._when_gate"):
            _eval_predicate(_nullable_frame(), expr, "hash", "email")
    records = _fallback_records(caplog)
    assert records, "numexpr fallback was not surfaced through the logger"
    for record in records:
        message = record.getMessage()
        assert _SECRET not in message
        assert "'email'" in message and "hash" in message


def test_transform_fallback_log_names_the_op_not_the_expression(caplog) -> None:
    expr = f"email != '{_SECRET}' and age >= 18"
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with caplog.at_level(logging.WARNING, logger="decoy_engine.execution._transforms"):
            apply_transforms(_nullable_frame(), [FilterOp(op="filter", expression=expr)])
    records = _fallback_records(caplog)
    assert records, "numexpr fallback was not surfaced through the logger"
    for record in records:
        assert _SECRET not in record.getMessage()
        assert "filter" in record.getMessage()


def test_date_shift_unparseable_values_are_counted_not_logged(caplog) -> None:
    column = pd.Series(["2024-01-01", _SECRET, "2024-02-02", _SECRET])
    with caplog.at_level(logging.DEBUG):
        DateShiftStrategy(seed=7).apply(
            column, {"column": "dob", "date_format": "%Y-%m-%d", "min_days": 1, "max_days": 5}
        )
    assert _SECRET not in caplog.text
    assert "2 value(s) in column 'dob' could not be parsed" in caplog.text


def test_formula_errors_are_one_line_per_column_without_cell_values(caplog) -> None:
    cfg = {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {},
        "tables": [
            {
                "name": "t",
                "row_count": 5,
                "generate_columns": [
                    {"name": "first_name", "type": "faker", "faker_type": "first_name"},
                    {
                        "name": "bad",
                        "type": "formula",
                        "references": ["first_name"],
                        # int() of a name raises ValueError whose text quotes the cell.
                        "formula": "int(first_name)",
                    },
                ],
            }
        ],
    }
    with caplog.at_level(logging.DEBUG):
        out = compile_and_generate(cfg)["t"]
    names = out.column("first_name").to_pylist()
    failures = [r for r in caplog.records if "failed to evaluate" in r.getMessage()]
    assert len(failures) == 1
    assert "ValueError" in failures[0].getMessage()
    for name in names:
        assert repr(name) not in caplog.text
