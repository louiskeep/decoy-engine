"""Privacy gate: no raw PII cell values appear in the baseline report (§B.4).

The harness report must contain only aggregate column statistics (precision,
recall, F2, support counts, match rates) and structural identifiers (fixture
names, column names, detector IDs). It must NEVER contain actual cell values
from the input data -- SSNs, PANs, email addresses, IBANs, NPIs, etc.

Gate reference: ml-benchmarking-and-privacy.md §B.4:
    "MUST keep features as aggregate column statistics ... never raw cell
    values in feature vectors, eval reports, model artifacts, or logs. Add
    a test asserting no raw sampled value appears in the serialized report
    (BF2's security-canary pattern)."

The test:
1. Extracts known cell values from the five fixture DataFrames.
2. Serialises the HarnessReport to canonical JSON.
3. Asserts no extracted cell value appears in the serialised blob.
"""

from __future__ import annotations

import json

from decoy_engine.storm.eval import build_fixtures, run_baseline


def _sample_raw_values(n_per_column: int = 5) -> list[str]:
    """Return structurally distinctive PII cell values from the fixture corpus.

    Only includes values long enough and structurally distinctive enough that
    they cannot appear as substrings of floating-point statistics:
    - Minimum 8 characters (rules out short numerics like CVV codes that
      could be substrings of Shannon entropy or match-rate floats).
    - Excludes plain-integer columns (claim_amount, numeric business IDs)
      whose values are entirely likely to be embedded in count fields.
    """
    fixtures = build_fixtures()
    sampled: list[str] = []
    for fx in fixtures:
        for col in fx.df.columns:
            series = fx.df[col].dropna()
            vals = series.head(n_per_column).tolist()
            for v in vals:
                s = str(v)
                # Only probe values with distinctive PII structure: at least
                # 8 characters, and not a bare integer (those can appear inside
                # count fields like row_count or support).
                if len(s) >= 8 and not s.isdigit():
                    sampled.append(s)
    return sampled


def test_baseline_report_contains_no_raw_cell_values() -> None:
    """Serialised HarnessReport must not contain any fixture cell value."""
    raw_values = _sample_raw_values(n_per_column=5)
    assert raw_values, "No raw values extracted -- fixture corpus is empty"

    rep = run_baseline()
    blob = json.dumps(rep.to_dict(), sort_keys=True, default=str)

    leaks = [v for v in raw_values if v in blob]
    assert not leaks, (
        f"Raw cell values leaked into baseline report ({len(leaks)} values):\n"
        + "\n".join(f"  {v!r}" for v in leaks[:10])
    )


def test_column_features_contain_no_raw_cell_values() -> None:
    """ColumnFeatures dicts (X from make_split_inputs) must be aggregate-only."""
    from decoy_engine.storm.eval.split import make_split_inputs

    fixtures = build_fixtures()
    raw_values = _sample_raw_values(n_per_column=5)

    X, _, _ = make_split_inputs(fixtures)
    blob = json.dumps(X, sort_keys=True, default=str)

    leaks = [v for v in raw_values if v in blob]
    assert not leaks, (
        f"Raw cell values leaked into column feature dicts ({len(leaks)} values):\n"
        + "\n".join(f"  {v!r}" for v in leaks[:10])
    )
