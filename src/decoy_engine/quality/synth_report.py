"""V2 Distribution Integrity, Sprint D7a: SynthReport foundation.

QualityReport (D1-D5) measures how WELL the output matches the source's
distribution: did the synth preserve marginals, joints, shape? But
high fidelity does NOT mean low privacy risk: a perfect copy of the
source scores 1.0 on every fidelity metric AND leaks every source row.
We need a sibling report that separates "looks right" (utility) from
"doesn't memorize the source" (privacy).

D7a ships the foundation:

  - `SynthReport` dict shape, schema `synth-report/v1`
  - `compute_new_row_synthesis` metric: fraction of output rows that
    do NOT exactly match any source row. Direct measure of memorization:
    exact-copy synth -> 0.0; independent generation -> ~1.0.

D7b adds holdout DCR (Distance to Closest Record), D7c gates
attack-based metrics behind an `extras` import (only loaded when the
operator explicitly opts in to the privacy-attack dependencies).

Method citation:
  - New-row synthesis fraction: matches SDV's `NewRowSynthesis`
    metric (sdv-dev/SDMetrics). The naming is intentionally aligned
    so operators reading both projects' docs see the same term mean
    the same thing.
  - The "high fidelity does not imply low privacy" warning: this is
    a direct restatement of NIST IR 8336 Section 5.3 ("Synthetic
    data must be evaluated on BOTH fidelity AND disclosure risk")
    and SDV's NewRowSynthesis documentation rationale.

Scope per the implementation guide:
  - DCR is NOT a privacy guarantee (sanity check only).
  - No differential privacy claim is made (D7 deliberately stops
    short of DP; that's a future sprint).
  - Aggregate metrics only. No raw rows in the report.
  - High-fidelity warnings explicit: high marginal/joint similarity
    can preserve outliers and therefore can increase privacy risk.

The acceptance criteria (from the sprint plan):
  - Exact-copy synthetic output reports POOR new-row synthesis.
  - Independent synthetic sample reports STRONGER new-row synthesis.
  - Report states that DCR is not a privacy guarantee.
  - No differential privacy claim is made.
"""
from __future__ import annotations

import hashlib
from typing import Any

import pandas as pd

SYNTH_REPORT_SCHEMA_VERSION = "synth-report/v1"

# Threshold the report uses to classify new-row-synthesis as "low" /
# "moderate" / "high". Boundaries match SDV NewRowSynthesis's
# documented thresholds so operators comparing across tools see the
# same grade for the same fraction. Strictly informational; the
# numeric fraction is the load-bearing field.
_LOW_BAND = 0.50
_HIGH_BAND = 0.90


def compute_new_row_synthesis(
    source: pd.DataFrame,
    output: pd.DataFrame,
    *,
    subset_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Fraction of output rows that do not exactly match any source row.

    Algorithm:
      1. Hash each row to a stable digest using only `subset_columns`
         (default: the intersection of source and output column names).
      2. Build the set of source row hashes.
      3. Count output rows whose hash is NOT in that set; divide by
         output row count.

    Returns a dict shaped:

        {
          "metric": "new_row_synthesis",
          "fraction_new": float | None,  # None when output empty
          "matched_rows": int,           # output rows that DID match source
          "new_rows": int,               # output rows that did NOT match
          "output_row_count": int,
          "source_row_count": int,
          "subset_columns": [str, ...],  # columns actually compared
          "band": "low" | "moderate" | "high" | "unavailable",
          "warning": str | None,         # explainer when band == low
        }

    Hashing rationale: comparing object-dtype frames row-by-row in
    pandas is O(N*M); pre-hashing reduces to O(N+M) with a single
    membership lookup per output row. The hash domain is a stable
    UTF-8 representation of the row's values so two frames with the
    same logical content hash identically across runs.

    Memory: source row hashes are held in a Python set. For a 50M-row
    source this is ~3 GB; D7a leaves that as a "future-tier"
    limitation (the V2.0 platform caps source rows at 1M for the
    quality pipeline). The fraction is mathematically well-defined
    for any size — implementation just needs streaming hashing for
    very large frames, which can land later without touching the
    metric contract.

    Privacy note: the row hash is computed locally and never
    serialized — only the aggregate counts + fraction land in the
    returned dict. Per the security requirements, no raw source rows
    or output rows are stored.
    """
    if source is None or output is None:
        return {
            "metric": "new_row_synthesis",
            "fraction_new": None,
            "matched_rows": 0,
            "new_rows": 0,
            "output_row_count": 0,
            "source_row_count": 0,
            "subset_columns": [],
            "band": "unavailable",
            "warning": "source or output not provided",
        }

    out_n = len(output)
    src_n = len(source)
    if out_n == 0:
        return {
            "metric": "new_row_synthesis",
            "fraction_new": None,
            "matched_rows": 0,
            "new_rows": 0,
            "output_row_count": 0,
            "source_row_count": src_n,
            "subset_columns": [],
            "band": "unavailable",
            "warning": "output is empty - new-row synthesis undefined",
        }

    if subset_columns is None:
        # Intersect column names so two frames that share most but not
        # all columns can still be compared. Sorting keeps the hash
        # stable across runs even when one of the frames was built
        # in a different column order.
        cols = sorted(set(source.columns) & set(output.columns))
    else:
        cols = [c for c in subset_columns if c in source.columns and c in output.columns]

    if not cols:
        return {
            "metric": "new_row_synthesis",
            "fraction_new": None,
            "matched_rows": 0,
            "new_rows": 0,
            "output_row_count": out_n,
            "source_row_count": src_n,
            "subset_columns": [],
            "band": "unavailable",
            "warning": "no overlapping columns between source and output",
        }

    src_hashes = _row_hash_set(source, cols)
    out_hashes_iter = _row_hash_iter(output, cols)

    matched = 0
    new_rows = 0
    for h in out_hashes_iter:
        if h in src_hashes:
            matched += 1
        else:
            new_rows += 1
    fraction = new_rows / out_n
    band = _band_for(fraction)

    return {
        "metric": "new_row_synthesis",
        "fraction_new": _round(fraction),
        "matched_rows": matched,
        "new_rows": new_rows,
        "output_row_count": out_n,
        "source_row_count": src_n,
        "subset_columns": cols,
        "band": band,
        "warning": _warning_for(band, fraction),
    }


def assemble_synth_report(
    *,
    new_row_synthesis: dict[str, Any] | None,
    job_id: int | None = None,
) -> dict[str, Any]:
    """Bundle the privacy metrics into a single JSON-serializable report.

    Currently carries only the new-row-synthesis block (D7a). D7b
    appends `dcr` (Distance to Closest Record) under its own key;
    D7c appends `attacks` only when the optional extra is installed
    AND the platform-side admin policy permits attack-based metrics.

    The report always includes the disclaimer block. This is required
    by the sprint acceptance criteria and survives schema evolution:
    operators reading the report at any time must see the explicit
    statement that DCR is not a privacy guarantee and that no
    differential-privacy claim is made.
    """
    return {
        "schema_version": SYNTH_REPORT_SCHEMA_VERSION,
        "job_id": job_id,
        "new_row_synthesis": new_row_synthesis,
        "dcr": None,        # populated in D7b
        "attacks": None,    # populated in D7c, only when opted-in
        "disclaimers": [
            "DCR (Distance to Closest Record) is a sanity check, not a "
            "privacy guarantee. A high DCR does not establish that the "
            "synthetic data cannot be linked back to source records.",
            "Decoy does NOT make a differential-privacy claim. Synthetic "
            "data generated by Decoy is not differentially private and "
            "should not be treated as such for compliance purposes.",
            "High fidelity does NOT imply low privacy risk: high "
            "marginal or joint similarity can preserve outlier rows "
            "verbatim, which may re-identify individuals. Use this "
            "report ALONGSIDE the QualityReport, not as a substitute.",
        ],
    }


# ── helpers ────────────────────────────────────────────────────────────────


def _row_hash_set(df: pd.DataFrame, cols: list[str]) -> set[str]:
    """Build a set of stable per-row digests for membership lookup."""
    return set(_row_hash_iter(df, cols))


def _row_hash_iter(df: pd.DataFrame, cols: list[str]):
    """Yield a stable SHA-1 digest per row.

    SHA-1 (not -256) is intentional: this is NOT a security hash, just
    a row fingerprint for set-membership equality. SHA-1 is faster and
    its 160-bit output makes collisions vanishingly unlikely at any
    realistic frame size (~2^80 row pairs before expected collision).
    """
    sub = df[cols]
    # Convert to a list-of-tuples once. Pandas' .itertuples is the
    # fastest cross-version path; .to_records sometimes drops object
    # dtypes' Python repr in surprising ways.
    for row in sub.itertuples(index=False, name=None):
        # str(v) on None / NaN / pd.NaT produces stable distinct
        # strings ('None', 'nan', 'NaT'). Two frames with the same
        # null in the same position therefore hash identically. The
        # separator byte (ASCII Unit Separator, same idiom as D5c
        # hash strategy) avoids "ab|c" / "a|bc" collisions on
        # adjacent string fields.
        composite = "\x1f".join(str(v) for v in row)
        yield hashlib.sha1(composite.encode("utf-8")).hexdigest()


def _band_for(fraction: float) -> str:
    if fraction < _LOW_BAND:
        return "low"
    if fraction < _HIGH_BAND:
        return "moderate"
    return "high"


def _warning_for(band: str, fraction: float) -> str | None:
    if band == "low":
        return (
            f"new-row synthesis is LOW ({fraction:.2%}): the output "
            "contains many rows that exactly match source rows. This "
            "is a memorization signal — review the synth strategy "
            "before releasing the output."
        )
    if band == "moderate":
        return (
            f"new-row synthesis is moderate ({fraction:.2%}): some "
            "output rows match source rows exactly. This may be "
            "acceptable depending on the source's natural duplication "
            "rate; investigate if the source has low duplication."
        )
    return None


def _round(x: float) -> float:
    """Stable JSON-friendly rounding to 4 places."""
    return round(float(x), 4)
