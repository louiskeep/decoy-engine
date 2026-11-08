"""A10: dark. The `quality` surfaces V1 reaches are unchanged, and nothing new is exported above the
copula package. Golden values were captured from engine main `b5b1ad16`, whose `quality` modules
are byte-identical to this branch's."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import decoy_engine
from decoy_engine import quality
from decoy_engine.quality.fidelity import compute_fidelity
from decoy_engine.quality.snapshot import compute_distribution_snapshot
from decoy_engine.quality.synth_report import (
    assemble_synth_report,
    compute_attack_metrics,
    compute_dcr,
    compute_new_row_synthesis,
)

_GOLDEN = Path(__file__).with_name("_dark_golden.json")

_QUALITY_ALL = [
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


def _frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(60)
    source = pd.DataFrame(
        {
            "x": rng.normal(size=200),
            "n": rng.integers(0, 5, 200),
            "c": rng.choice(np.array(["a", "b", None], dtype=object), 200),
        }
    )
    source.loc[:9, "x"] = np.nan
    output = source.sample(frac=1.0, random_state=1).reset_index(drop=True)
    output.loc[:49, "x"] = rng.normal(size=50)
    return source, output


def outputs() -> dict[str, object]:
    source, output = _frames()
    nrs = compute_new_row_synthesis(source, output)
    dcr = compute_dcr(source, output, holdout=source.iloc[:80])
    attacks = compute_attack_metrics(source, output)
    return {
        "new_row_synthesis": nrs,
        "dcr": dcr,
        "fidelity": compute_fidelity(
            compute_distribution_snapshot(source), compute_distribution_snapshot(output)
        ),
        "synth_report": assemble_synth_report(new_row_synthesis=nrs, dcr=dcr, attacks=attacks),
    }


def test_quality_public_names_unchanged() -> None:
    assert quality.__all__ == _QUALITY_ALL
    assert sorted(quality._LAZY_EXPORTS) == sorted(_QUALITY_ALL)


def test_nothing_new_at_the_top_level() -> None:
    assert not {n for n in decoy_engine.__all__ if "copula" in n.lower()}


def test_v1_quality_outputs_equal_main() -> None:
    assert json.loads(json.dumps(outputs(), sort_keys=True)) == json.loads(_GOLDEN.read_text())
