"""Dark: the search is reached only by import; V1 surfaces are unchanged."""

from __future__ import annotations

import subprocess
import sys

import pytest

import decoy_engine
import decoy_engine.anonymize.search as search
import decoy_engine.quality as quality
from decoy_engine.execution._strategies import strategy_catalog


def _run(code: str) -> str:
    result = subprocess.run(  # noqa: S603 -- args are test literals
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_engine_import_does_not_load_the_search() -> None:
    code = "import sys, decoy_engine; print('decoy_engine.anonymize.search' in sys.modules)"
    assert _run(code) == "False"


@pytest.mark.parametrize(
    "first",
    ["decoy_engine.quality.anonymity", "decoy_engine.anonymize.search", "decoy_engine.anonymize"],
)
def test_each_package_imports_first_without_a_cycle(first: str) -> None:
    code = (
        f"import importlib; importlib.import_module({first!r}); "
        "import decoy_engine.anonymize.search, decoy_engine.quality.anonymity; print('ok')"
    )
    assert _run(code) == "ok"


def test_no_search_name_in_the_engine_api_or_catalog() -> None:
    ours = set(search.__all__) | {"search", "search_lattice"}
    assert not ours & set(decoy_engine.__all__)
    for row in strategy_catalog():
        assert row.name not in ours
        assert "anonymi" not in row.name and "search" not in row.name


def test_quality_lazy_exports_unchanged() -> None:
    """Captured at bc711b6c: the new names are reached through the sub-package only."""
    lazy = quality._LAZY_EXPORTS
    assert sorted(lazy) == [
        "DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION",
        "QUALITY_DIAGNOSTIC_SCHEMA_VERSION",
        "QUALITY_FIDELITY_SCHEMA_VERSION",
        "QUALITY_POLICY_SCHEMA_VERSION",
        "QUALITY_REPORT_SCHEMA_VERSION",
        "QUALITY_SHAPE_FIDELITY_SCHEMA_VERSION",
        "SYNTH_REPORT_SCHEMA_VERSION",
        "apply_quality_policy",
        "assemble_quality_report",
        "assemble_synth_report",
        "compute_attack_metrics",
        "compute_dcr",
        "compute_diagnostic",
        "compute_distribution_snapshot",
        "compute_fidelity",
        "compute_new_row_synthesis",
        "compute_quality_report",
        "compute_shape_fidelity",
        "score_to_grade",
    ]
