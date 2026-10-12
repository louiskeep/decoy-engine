"""Phase D / D1c log hygiene: no sentinel literal, date or cell text in logs, errors or metrics."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from decoy_engine.execution._errors import StrategyError
from tests.parity._d1c_data import (
    IN_RANGE,
    OPERATORS,
    OUT_OF_RANGE,
    operator_config,
    run_oracle,
    strings,
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "decoy_engine"
_NEEDLES = ("2099", "9999", "31/12", "SECRET-CELL-TEXT")


@pytest.mark.parametrize("strategy", sorted(OPERATORS))
def test_a_run_with_sentinels_logs_and_reports_no_value(
    strategy: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    config = operator_config(strategy, preserve_values=[IN_RANGE, OUT_OF_RANGE])
    got = run_oracle(
        strategy, config, strings(IN_RANGE, OUT_OF_RANGE, "2024-03-05", "SECRET-CELL-TEXT")
    )
    emitted = "\n".join(r.getMessage() for r in caplog.records)
    blob = emitted + repr(got.metrics) + repr(got.errors)
    for needle in _NEEDLES:
        assert needle not in blob, needle


@pytest.mark.parametrize("strategy", sorted(OPERATORS))
@pytest.mark.parametrize("bad", ["2099-13-45", "not-a-date", "31/12/9999"])
def test_config_errors_never_echo_a_literal(strategy: str, bad: str) -> None:
    with pytest.raises(StrategyError) as caught:
        run_oracle(
            strategy, operator_config(strategy, preserve_values=[bad]), strings("2024-03-05")
        )
    assert bad not in str(caught.value)


def test_the_preserve_modules_do_not_log() -> None:
    for rel in ("transforms/date_preserve.py",):
        text = (_SRC / rel).read_text()
        assert "logging" not in text and "_LOG" not in text
