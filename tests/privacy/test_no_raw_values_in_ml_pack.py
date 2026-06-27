"""Privacy gate: no raw cell values appear in the ML model-pack artifact (§B.4).

The model pack (manifest.json + model.joblib) and the eval report
(lightgbm-report.json) must contain ONLY aggregate column statistics
and structural metadata.  They must NEVER contain actual cell values
from the training corpus: SSNs, PANs, email addresses, IBANs, etc.

Gate reference: ml-benchmarking-and-privacy.md §B.4:
    "NO raw cell values in model artifact, metric report, feature vectors,
    or logs. Add a test asserting no raw sampled value appears in the
    serialized report."

The test:
1. Extracts known cell values from the extended fixture corpus.
2. Checks that no extracted value appears in manifest.json as a string.
3. Checks that no extracted value appears in lightgbm-report.json.
4. Verifies that the model.joblib weights file (binary) does not embed
   any long-enough distinctive value as a plain UTF-8 substring.
5. Verifies flatten_features() output contains no raw cell values.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_PACK_DIR = Path(__file__).parents[2] / "docs" / "v2" / "ml" / "packs" / "lgbm-v1"
_REPORT_PATH = Path(__file__).parents[2] / "docs" / "v2" / "ml" / "lightgbm-report.json"
_MIN_VALUE_LEN = 8  # short strings (e.g. "1234") are too common to be probes


def _sample_raw_values(n_per_column: int = 5) -> list[str]:
    """Extract structurally distinctive PII strings from the extended corpus.

    Only values >= _MIN_VALUE_LEN characters that are not plain integers are
    returned.  Short plain numerics ("300", "1234") appear inside floating-
    point statistics and would cause false positives.
    """
    from decoy_engine.storm.eval.fixtures import build_extended_fixtures

    fixtures = build_extended_fixtures()
    sampled: list[str] = []
    for fx in fixtures:
        for col in fx.df.columns:
            series = fx.df[col].dropna()
            vals = series.head(n_per_column).tolist()
            for v in vals:
                s = str(v)
                if len(s) >= _MIN_VALUE_LEN and not s.isdigit():
                    sampled.append(s)
    return sampled


@pytest.fixture(scope="module")
def raw_values() -> list[str]:
    vals = _sample_raw_values()
    assert vals, "No raw values extracted -- extended corpus is empty"
    return vals


@pytest.fixture(scope="module")
def manifest_text() -> str:
    if not _PACK_DIR.exists():
        pytest.skip("lgbm-v1 pack not found; run train_and_evaluate first")
    return (_PACK_DIR / "manifest.json").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def report_text() -> str:
    if not _REPORT_PATH.exists():
        pytest.skip("lightgbm-report.json not found; run train_and_evaluate first")
    return _REPORT_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def weights_bytes() -> bytes:
    if not _PACK_DIR.exists():
        pytest.skip("lgbm-v1 pack not found")
    return (_PACK_DIR / "model.joblib").read_bytes()


def test_manifest_contains_no_raw_cell_values(
    raw_values: list[str], manifest_text: str
) -> None:
    """manifest.json must not embed any training cell value as a string."""
    leaks = [v for v in raw_values if v in manifest_text]
    assert not leaks, (
        f"Raw cell values found in manifest.json ({len(leaks)} values):\n"
        + "\n".join(f"  {v!r}" for v in leaks[:10])
    )


def test_report_contains_no_raw_cell_values(
    raw_values: list[str], report_text: str
) -> None:
    """lightgbm-report.json must not embed any training cell value."""
    leaks = [v for v in raw_values if v in report_text]
    assert not leaks, (
        f"Raw cell values leaked into lightgbm-report.json ({len(leaks)} values):\n"
        + "\n".join(f"  {v!r}" for v in leaks[:10])
    )


def test_weights_file_contains_no_raw_cell_values(
    raw_values: list[str], weights_bytes: bytes
) -> None:
    """model.joblib must not embed cell values as plain UTF-8 substrings.

    The model weights are a serialised DictVectorizer + CalibratedClassifierCV.
    The DictVectorizer vocabulary contains feature NAMES ('hdr_ssn', 'char_digit',
    etc.), never actual cell values.  If any cell value appears in the binary,
    it signals that the featurizer leaked raw data into the vocabulary.
    """
    # Decode leniently: we only search for ASCII-printable substrings.
    try:
        weights_text = weights_bytes.decode("latin-1")
    except Exception:
        weights_text = ""

    leaks = [v for v in raw_values if v in weights_text]
    assert not leaks, (
        f"Raw cell values embedded in model.joblib ({len(leaks)} values):\n"
        + "\n".join(f"  {v!r}" for v in leaks[:10])
    )


def test_flatten_features_contains_no_raw_cell_values(raw_values: list[str]) -> None:
    """flatten_features() output must contain only aggregate statistics.

    Verifies the §B.4 privacy invariant directly at the featurizer boundary:
    the flat feature dict passed to DictVectorizer must never include a raw
    cell value, even indirectly.
    """
    from decoy_engine.storm.eval.fixtures import build_extended_fixtures
    from decoy_engine.storm.eval.split import make_split_inputs
    from decoy_engine.storm.model_pack.featurizer import flatten_features

    fixtures = build_extended_fixtures()
    # make_split_inputs returns ColumnFeatures dicts; re-flatten them
    X_raw, _, _ = make_split_inputs(fixtures)  # list of ColumnFeatures dicts
    flat_blobs = [json.dumps(flatten_features(f), default=str) for f in X_raw]
    combined = " ".join(flat_blobs)

    leaks = [v for v in raw_values if v in combined]
    assert not leaks, (
        f"Raw cell values leaked into flatten_features() output ({len(leaks)}):\n"
        + "\n".join(f"  {v!r}" for v in leaks[:10])
    )
