"""Golden characterization of the SynthReport metric outputs.

Each case's full output dict is canonicalized to JSON and pinned by sha256,
captured from the code before the DCR and attack metrics moved out of
`synth_report.py`. A refactor that changes any field, rounding or key fails
here with the full dict in the assertion message.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd
import pytest

from decoy_engine.quality.synth_report import (
    assemble_synth_report,
    compute_attack_metrics,
    compute_dcr,
    compute_new_row_synthesis,
)


def _frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    source = pd.DataFrame(
        {
            "age": [23, 35, 47, 51, 62, 29, None, 44],
            "income": [31000.0, 52000.5, 78000.0, np.nan, 91000.25, 40500.0, 61000.0, 55000.0],
            "city": ["Austin", "Boston", "Denver", "Austin", None, "Chicago", "Boston", "Denver"],
            "flag": [True, False, True, True, False, False, True, False],
        }
    )
    output = pd.DataFrame(
        {
            "age": [23, 36, 47, 80, 29, None],
            "income": [31000.0, 50000.0, 78000.0, 120000.0, 40500.0, 61000.0],
            "city": ["Austin", "Boston", "Denver", "Miami", "Chicago", "Boston"],
            "flag": [True, False, True, False, False, True],
            "extra": [1, 2, 3, 4, 5, 6],
        }
    )
    holdout = pd.DataFrame(
        {
            "age": [30, 41, 58, 25],
            "income": [45000.0, 66000.0, 88000.0, 33000.0],
            "city": ["Denver", "Austin", "Boston", "Seattle"],
        }
    )
    return source, output, holdout


def _report() -> dict[str, Any]:
    s, o, h = _frames()
    return assemble_synth_report(
        new_row_synthesis=compute_new_row_synthesis(s, o),
        dcr=compute_dcr(s, o, holdout=h),
        attacks=compute_attack_metrics(s, o),
        job_id=3,
    )


_S, _O, _H = _frames()

_CASES: dict[str, tuple[Callable[[], dict[str, Any]], str]] = {
    "new_row_synthesis": (lambda: compute_new_row_synthesis(_S, _O), "NRS"),
    "new_row_synthesis_subset": (
        lambda: compute_new_row_synthesis(_S, _O, subset_columns=["city", "flag", "missing"]),
        "NRS_SUBSET",
    ),
    "dcr": (lambda: compute_dcr(_S, _O), "DCR"),
    "dcr_holdout": (lambda: compute_dcr(_S, _O, holdout=_H), "DCR_HOLDOUT"),
    "dcr_capped_subset": (
        lambda: compute_dcr(_S, _O, holdout=_H, sample_cap=3, subset_columns=["income", "city"]),
        "DCR_CAPPED",
    ),
    "dcr_empty_output": (lambda: compute_dcr(_S, _O.iloc[0:0]), "DCR_EMPTY"),
    "attacks_default_off": (lambda: compute_attack_metrics(_S, _O), "ATTACKS_DEFAULT"),
    "attacks_extras_missing": (
        lambda: compute_attack_metrics(
            _S, _O, enable_attacks=True, extras_module="decoy_engine_no_such_attacks_pkg"
        ),
        "ATTACKS_MISSING",
    ),
    "assembled_report": (_report, "REPORT"),
}


def _digest(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("name", list(_CASES))
def test_output_matches_golden_digest(name: str) -> None:
    build, golden_key = _CASES[name]
    value = build()
    assert _digest(value) == _GOLDEN[golden_key], json.dumps(value, sort_keys=True, indent=1)


def test_readable_spot_checks() -> None:
    dcr = compute_dcr(_S, _O, holdout=_H)
    assert dcr["synth_to_source"]["median"] == 0.0074
    assert dcr["comparison"] == {"interpretation": "memorizing", "median_ratio": 0.0193}
    nrs = compute_new_row_synthesis(_S, _O)
    assert (nrs["matched_rows"], nrs["new_rows"], nrs["band"]) == (4, 2, "low")
    assert _report()["severity"] == "low"


_GOLDEN: dict[str, str] = {
    "NRS": "ac26721e6944d90f862a7efddc05f10e656c338ef87d3d30db96febced08ab4a",
    "NRS_SUBSET": "ab895cea2e3fd68f06dedaf1a6d0f4fec22fbcb7997ce139e770b605c4994c40",
    "DCR": "c0cae0355dd92c13e7c1e0513a97126e8dae8918fb7fb1c8a22cb8d201188396",
    "DCR_HOLDOUT": "0c97186936ff4e58b8239aeb22586175b819df6c856963018f8e4885e9e228a8",
    "DCR_CAPPED": "b30b59961ee8fe6fb2f9f41577c9cc03da20c19bf6b3f5eb7a8ad8643ea8bd12",
    "DCR_EMPTY": "172576ae52ad3e8b26408672e466c8bd772d3b4a6ba44a91b771c6b4c6d687c2",
    "ATTACKS_DEFAULT": "3aa29c706c2a30e81b0ff1728da4a7ed3eba90340f6b60566b4d07a1d46d967b",
    "ATTACKS_MISSING": "3b3853dee790e76f61c4d4068a24a7d745d8f9f9d61f63e0d47161e9ad624bb6",
    "REPORT": "f3532b6f66f342cd873b4dfe669d013939d5805a81222a167a850007e997b321",
}
