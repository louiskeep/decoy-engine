"""AT-E2-5, AT-E2-6: a formula reference to a missing cell substitutes "" and counts it."""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import pytest

from decoy_engine.generators.columns import ColumnGenerator


def _fill(cells: list[Any]) -> tuple[list[Any], str]:
    cg = ColumnGenerator(seed=42)
    out = pd.DataFrame({"a": pd.Series(cells, dtype=object)})
    return cg.fill_referenced_formula_column("f", "f'<{a}>'", ["a"], out, "t").tolist(), ""


def _counts(caplog: pytest.LogCaptureFixture) -> str:
    return " ".join(r.getMessage() for r in caplog.records if "substituted" in r.getMessage())


@pytest.mark.parametrize("missing", [pd.NaT, pd.NA], ids=["NaT", "NA"])
def test_missing_reference_becomes_empty_and_is_counted(
    missing: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        values, _ = _fill([missing])
    assert values == ["<>"]
    assert "{'a': 1}" in _counts(caplog)


def test_text_nat_and_empty_string_are_values_not_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        values, _ = _fill(["NaT", ""])
    assert values == ["<NaT>", "<>"]
    assert _counts(caplog) == ""
