"""A5: the engine's existing synthetic-data checks, run on canonical copula output."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation.copula._quality_inputs import canonical
from decoy_engine.quality.synth_report import (
    assemble_synth_report,
    compute_attack_metrics,
    compute_new_row_synthesis,
)

from ._helpers import fit, report, sample

_EXTRAS = "fake_attacks_for_copula_quality"


def _real() -> pd.DataFrame:
    rng = np.random.default_rng(20)
    c = rng.choice(np.array(["a", "b", "c"], dtype=object), 300)
    frame = pd.DataFrame({"x": rng.normal(size=300), "c": c})
    frame.loc[:29, "c"] = np.nan  # pandas' usual object null; the copula emits None
    return frame


def test_copied_rows_with_object_nulls_match() -> None:
    real = _real()
    model = fit(real)
    synth = sample(model)
    synth.loc[:9, "x"] = real.loc[:9, "x"].to_numpy()
    synth.loc[:9, "c"] = None
    # Fail-before: on the raw frames NaN hashes as "nan" and None as "None".
    assert compute_new_row_synthesis(real, synth)["matched_rows"] == 0
    result = report(real, model, synth)
    assert result.synth_report["new_row_synthesis"]["matched_rows"] == 10


def test_attacks_off_by_default() -> None:
    real = _real()
    attacks = report(real, fit(real)).synth_report["attacks"]
    assert attacks["available"] is False and attacks["reason"] == "not_enabled_by_caller"


def _with_extras(monkeypatch: pytest.MonkeyPatch, run: Callable[..., Any]) -> dict[str, Any]:
    module = types.ModuleType(_EXTRAS)
    module.run_privacy_attacks = run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _EXTRAS, module)
    real = _real()
    return dict(report(real, fit(real), attack_extras=_EXTRAS).synth_report["attacks"])


def test_scores_in_unit_interval_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    attacks = _with_extras(
        monkeypatch, lambda *a, **k: {"mia_auc": 0.42371, "flag": True, "x": None}
    )
    assert attacks["available"] is True
    assert dict(attacks["results"]) == {"mia_auc": 0.424, "flag": True, "x": None}


@pytest.mark.parametrize(
    "results",
    [
        {"label": "SENTINEL_LABEL"},
        {"nested": {"a": 0.5}},
        {"count": 1234.0},
        {"shift": -0.5},
        {"Bad-Key": 0.5},
        ["not", "a", "mapping"],
        {"nan": float("nan")},
    ],
)
def test_other_results_are_withheld(monkeypatch: pytest.MonkeyPatch, results: Any) -> None:
    attacks = _with_extras(monkeypatch, lambda *a, **k: results)
    assert "results" not in attacks
    assert attacks["results_status"] == "withheld_not_value_free"
    assert "SENTINEL" not in repr(attacks)


def test_extras_failure_records_the_type_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("SENTINEL_FAILURE")

    attacks = _with_extras(monkeypatch, boom)
    assert attacks["reason"] == "extras_runtime_error" and attacks["error_type"] == "RuntimeError"
    assert "SENTINEL" not in repr(attacks)


def test_synth_report_is_embedded_unchanged() -> None:
    real = _real()
    model = fit(real)
    synth = sample(model)
    result = report(real, model, synth)
    expected = assemble_synth_report(
        new_row_synthesis=compute_new_row_synthesis(
            canonical(real, model), canonical(synth, model)
        ),
        dcr=None,
        attacks=compute_attack_metrics(real, synth),
    )
    assert result.to_dict()["synth_report"] == expected
    assert expected["schema_version"] == "synth-report/v1" and expected["disclaimers"]


def test_copied_rows_with_nullable_ints_match() -> None:
    rng = np.random.default_rng(21)
    real = pd.DataFrame(
        {"n": pd.array(rng.integers(0, 1000, 300), dtype="Int64"), "x": rng.normal(size=300)}
    )
    real.loc[:29, "n"] = pd.NA
    model = fit(real)
    synth = sample(model)
    # The synthetic table as float64 (e.g. read back from a file): 5.0 for 5, NaN for <NA>.
    synth["n"] = synth["n"].astype("Float64").astype("float64")
    synth.loc[:9, "n"] = real.loc[20:29, "n"].astype("Float64").astype("float64").to_numpy()
    synth.loc[:9, "x"] = real.loc[20:29, "x"].to_numpy()
    synth.loc[10:14, "n"] = real.loc[30:34, "n"].astype("Float64").astype("float64").to_numpy()
    synth.loc[10:14, "x"] = real.loc[30:34, "x"].to_numpy()
    # Fail-before: raw frames hash "<NA>" against "nan" and "5" against "5.0".
    assert compute_new_row_synthesis(real, synth)["matched_rows"] == 0
    result = report(real, model, synth)
    assert result.synth_report["new_row_synthesis"]["matched_rows"] == 15


def test_extras_editing_its_inputs_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    real = _real()
    model = fit(real)
    synth = sample(model)
    holdout = real.iloc[:150].copy()
    baseline = report(real, model, synth, holdout=holdout).to_dict()
    saved = [f.copy(deep=True) for f in (real, synth, holdout)]

    def vandal(source: pd.DataFrame, output: pd.DataFrame, *, holdout: Any = None) -> Any:
        for frame in (source, output, holdout):
            frame["x"] = 0.0
            frame["c"] = "z"
        return {"auc": 0.5}

    module = types.ModuleType(_EXTRAS)
    module.run_privacy_attacks = vandal  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _EXTRAS, module)
    result = report(real, model, synth, holdout=holdout, attack_extras=_EXTRAS).to_dict()
    for frame, before in zip((real, synth, holdout), saved, strict=True):
        pd.testing.assert_frame_equal(frame, before, check_exact=True)
    for key in ("marginal", "pairs", "pairwise", "dcr"):
        assert result[key] == baseline[key]
    assert (
        result["synth_report"]["new_row_synthesis"] == baseline["synth_report"]["new_row_synthesis"]
    )
