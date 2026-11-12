"""Dark: the report is reached only by import; V1 surfaces are unchanged."""

from __future__ import annotations

import subprocess
import sys

import pytest

import decoy_engine
import decoy_engine.anonymize.report as report
import decoy_engine.quality as quality
import decoy_engine.run_result as run_result
from decoy_engine.execution._strategies import strategy_catalog
from tests.unit.anonymize.search._captured import CATALOG_AT_BASE, ENGINE_ALL_AT_BASE

RUN_RESULT_ALL_AT_BASE = ("RUN_RESULT_SCHEMA", "RunResult", "RunResultRead", "derive_outcome")


def _run(code: str) -> str:
    result = subprocess.run(  # noqa: S603 -- args are test literals
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_engine_import_does_not_load_the_report() -> None:
    code = "import sys, decoy_engine; print('decoy_engine.anonymize.report' in sys.modules)"
    assert _run(code) == "False"


@pytest.mark.parametrize(
    "first",
    ["decoy_engine.anonymize.report", "decoy_engine.quality.anonymity", "decoy_engine.run_result"],
)
def test_each_package_imports_first_without_a_cycle(first: str) -> None:
    code = (
        f"import importlib; importlib.import_module({first!r}); "
        "import decoy_engine.anonymize.report, decoy_engine.quality.anonymity; print('ok')"
    )
    assert _run(code) == "ok"


def test_engine_api_and_catalog_unchanged() -> None:
    assert tuple(decoy_engine.__all__) == ENGINE_ALL_AT_BASE
    assert tuple(row.name for row in strategy_catalog()) == CATALOG_AT_BASE
    assert not set(report.__all__) & set(decoy_engine.__all__)
    assert "measure_reidentification_risk" not in quality._LAZY_EXPORTS


def test_run_result_gains_exactly_the_two_names() -> None:
    assert set(run_result.__all__) == {*RUN_RESULT_ALL_AT_BASE, "ValueFree", "derive_read_model"}
    assert "derive_read_model" not in decoy_engine.__all__
    assert "ValueFree" not in decoy_engine.__all__
