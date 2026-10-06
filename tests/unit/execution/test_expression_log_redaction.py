"""Logs never carry user expression text: a predicate can embed literal values (PII)."""

from __future__ import annotations

import logging
import warnings

import pandas as pd

from decoy_engine.execution._expression_fingerprint import expression_fingerprint
from decoy_engine.execution._transforms import FilterOp, apply_transforms
from decoy_engine.execution._when_gate import _eval_predicate

_SECRET = "bob@example.com"


def _nullable_frame() -> pd.DataFrame:
    # Extension dtypes make pandas fall back from numexpr, which triggers the log line.
    return pd.DataFrame(
        {
            "email": pd.array([_SECRET, "x@y.z", None], dtype="string"),
            "age": pd.array([10, 20, 30], dtype="Int64"),
        }
    )


def test_fingerprint_is_stable_and_short() -> None:
    assert expression_fingerprint("a == 'b'") == expression_fingerprint("a == 'b'")
    assert expression_fingerprint("a == 'b'") != expression_fingerprint("a == 'c'")
    assert len(expression_fingerprint("a == 'b'")) == 12


def test_when_fallback_log_omits_expression_literals(caplog) -> None:
    expr = f"email == '{_SECRET}' and age >= 18"
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with caplog.at_level(logging.WARNING, logger="decoy_engine.execution._when_gate"):
            _eval_predicate(_nullable_frame(), expr, "hash")
    records = [r for r in caplog.records if "fell back" in r.getMessage()]
    assert records, "numexpr fallback was not surfaced through the logger"
    for record in records:
        assert _SECRET not in record.getMessage()
        assert expression_fingerprint(expr) in record.getMessage()


def test_transform_fallback_log_omits_expression_literals(caplog) -> None:
    expr = f"email != '{_SECRET}' and age >= 18"
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with caplog.at_level(logging.WARNING, logger="decoy_engine.execution._transforms"):
            apply_transforms(_nullable_frame(), [FilterOp(op="filter", expression=expr)])
    records = [r for r in caplog.records if "fell back" in r.getMessage()]
    assert records, "numexpr fallback was not surfaced through the logger"
    for record in records:
        assert _SECRET not in record.getMessage()
        assert expression_fingerprint(expr) in record.getMessage()
