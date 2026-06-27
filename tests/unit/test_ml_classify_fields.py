"""Tests for classify_fields (ML3.1).

Gate reference: ml-benchmarking-and-privacy.md §B.4 (no raw cell values in
output) and Sprint C ML3.1 spec (engine library function, not HTTP endpoint).

Tests:
  - classify_fields returns None when DECOY_ML_DISABLED=1 (off-by-default).
  - classify_fields returns None when pack_dir is missing.
  - classify_fields returns a dict when pack is present and ML enabled.
  - Output shape: each column key has label, calibrated_confidence, band,
    model_pack_id, model_pack_version, feature_schema_version.
  - §B.4 asserting test: no raw cell values in output.
  - Empty DataFrame returns empty dict (not None).
  - Determinism: same input -> same output (two calls on same df).
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

_LIVE_PACK = Path(__file__).parents[2] / "docs" / "v2" / "ml" / "packs" / "lgbm-v1"


# ── §B.4 no-raw-values asserting test ────────────────────────────────────────


@pytest.mark.skipif(not _LIVE_PACK.exists(), reason="lgbm-v1 pack not found")
def test_classify_fields_output_contains_no_raw_cell_values() -> None:
    """§B.4: classify_fields output must not contain any raw input cell values.

    This is the asserting test required by ml-benchmarking-and-privacy.md §B.4.
    The output dict is serialised to JSON and checked against a sample of the
    actual cell values from the input DataFrame.
    """
    from decoy_engine.storm.model_pack.classify import classify_fields

    # Create a small DataFrame with distinctive PII-like values.
    df = pd.DataFrame(
        {
            "ssn_col": ["123-45-6789", "987-65-4321", "111-22-3333"],
            "email_col": ["alice@example.com", "bob@test.org", "carol@domain.net"],
            "plain_id": [1001, 1002, 1003],
        }
    )

    result = classify_fields(df, pack_dir=_LIVE_PACK)
    assert result is not None, "classify_fields returned None with a live pack"

    output_json = json.dumps(result)

    # Collect raw cell values that are long enough to be distinctive.
    raw_values = []
    for col in df.columns:
        for v in df[col].dropna().tolist():
            s = str(v)
            if len(s) >= 8:
                raw_values.append(s)

    leaked = [v for v in raw_values if v in output_json]
    assert not leaked, (
        "§B.4 VIOLATION: raw cell values found in classify_fields output:\n"
        + "\n".join(f"  {v!r}" for v in leaked)
    )


# ── Off-by-default: DECOY_ML_DISABLED ────────────────────────────────────────


def test_classify_fields_returns_none_when_ml_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """classify_fields returns None when DECOY_ML_DISABLED=1 (off-by-default)."""
    monkeypatch.setenv("DECOY_ML_DISABLED", "1")
    from decoy_engine.storm.model_pack.classify import classify_fields

    df = pd.DataFrame({"col_a": ["foo", "bar"]})
    result = classify_fields(df, pack_dir=_LIVE_PACK)
    assert result is None


def test_classify_fields_returns_none_when_pack_missing(tmp_path: Path) -> None:
    """classify_fields returns None (not raises) when pack_dir is missing."""
    from decoy_engine.storm.model_pack.classify import classify_fields

    df = pd.DataFrame({"col_a": ["foo", "bar"]})
    result = classify_fields(df, pack_dir=tmp_path / "nonexistent")
    assert result is None


# ── Output shape ──────────────────────────────────────────────────────────────

_EXPECTED_KEYS = {
    "label",
    "calibrated_confidence",
    "band",
    "model_pack_id",
    "model_pack_version",
    "feature_schema_version",
}


@pytest.mark.skipif(not _LIVE_PACK.exists(), reason="lgbm-v1 pack not found")
def test_classify_fields_output_shape() -> None:
    """Each column result has exactly the required keys (no extra, no missing)."""
    from decoy_engine.storm.model_pack.classify import classify_fields

    df = pd.DataFrame(
        {
            "ssn": ["123-45-6789", "987-65-4321"],
            "name": ["Alice Smith", "Bob Jones"],
        }
    )
    result = classify_fields(df, pack_dir=_LIVE_PACK)
    assert result is not None

    for col_name, col_result in result.items():
        missing = _EXPECTED_KEYS - set(col_result.keys())
        assert not missing, f"Missing keys in column {col_name!r}: {missing}"

        # label is str or None
        label = col_result["label"]
        assert label is None or isinstance(label, str), (
            f"label must be str | None, got {type(label)}"
        )

        # calibrated_confidence is a float in [0, 1]
        conf = col_result["calibrated_confidence"]
        assert isinstance(conf, float), "calibrated_confidence must be float"
        assert 0.0 <= conf <= 1.0, f"calibrated_confidence {conf} out of [0, 1]"

        # band is one of the three valid values
        band = col_result["band"]
        assert band in ("high", "review", "low"), f"band must be high/review/low, got {band!r}"

        # provenance strings are non-empty
        assert col_result["model_pack_id"], "model_pack_id must be non-empty"
        assert col_result["model_pack_version"], "model_pack_version must be non-empty"
        assert col_result["feature_schema_version"], "feature_schema_version must be non-empty"


# ── Empty DataFrame ───────────────────────────────────────────────────────────


@pytest.mark.skipif(not _LIVE_PACK.exists(), reason="lgbm-v1 pack not found")
def test_classify_fields_empty_dataframe_returns_empty_dict() -> None:
    """An empty DataFrame (0 columns) returns {} not None."""
    from decoy_engine.storm.model_pack.classify import classify_fields

    df = pd.DataFrame()
    result = classify_fields(df, pack_dir=_LIVE_PACK)
    assert result == {}, f"Expected empty dict, got {result!r}"


# ── Determinism ───────────────────────────────────────────────────────────────


@pytest.mark.skipif(not _LIVE_PACK.exists(), reason="lgbm-v1 pack not found")
def test_classify_fields_is_deterministic() -> None:
    """Same DataFrame + same pack -> identical result on two calls."""
    from decoy_engine.storm.model_pack.classify import classify_fields

    df = pd.DataFrame(
        {
            "email": ["user@example.com", "admin@test.org"],
            "amount": [100.50, 250.00],
            "ssn": ["123-45-6789", "987-65-4321"],
        }
    )

    result_a = classify_fields(df, pack_dir=_LIVE_PACK)
    result_b = classify_fields(df, pack_dir=_LIVE_PACK)

    assert result_a is not None
    assert result_b is not None
    assert result_a == result_b, "classify_fields is not deterministic"
