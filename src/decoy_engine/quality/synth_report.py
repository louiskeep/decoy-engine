"""V2 Distribution Integrity, Sprints D7a + D7b: SynthReport foundation.

QualityReport (D1-D5) measures how WELL the output matches the source's
distribution: did the synth preserve marginals, joints, shape? But
high fidelity does NOT mean low privacy risk: a perfect copy of the
source scores 1.0 on every fidelity metric AND leaks every source row.
We need a sibling report that separates "looks right" (utility) from
"doesn't memorize the source" (privacy).

D7a shipped:

  - `SynthReport` dict shape, schema `synth-report/v1`
  - `compute_new_row_synthesis` metric: fraction of output rows that
    do NOT exactly match any source row. Direct measure of memorization:
    exact-copy synth -> 0.0; independent generation -> ~1.0.

D7b adds:

  - `compute_dcr` (Distance to Closest Record) sanity check.
    For each output row, find the minimum Gower-style distance to
    any source row. Returns aggregate percentiles, NOT per-row
    distances (per-row would leak which source rows the synth is
    closest to). Optionally accepts a holdout frame for the
    memorization-vs-generalization comparison:

      median(DCR_synth_to_source) ~= median(DCR_synth_to_holdout)
        -> synth is generalizing
      median(DCR_synth_to_source) << median(DCR_synth_to_holdout)
        -> synth is memorizing the training source

D7c adds:

  - `compute_attack_metrics`: contract entry point that returns the
    attacks block when the optional `decoy_engine_privacy_attacks`
    extras package is installed AND the call site explicitly opts
    in. Returns an `{available: False, reason: ...}` block otherwise.
    The extras package implements MIA / shadow-model attacks; D7c
    only defines the contract so the platform layer can wire the
    opt-in flag through admin policy.

D7c does NOT install the attack extras automatically. Per the
sprint security requirements: optional metric libraries stay as
extras until approved by dependency/security review. The default
SynthReport behavior is to record that attacks were skipped, not
to silently run them.

Method citation:
  - New-row synthesis fraction: matches SDV's `NewRowSynthesis`
    metric (sdv-dev/SDMetrics). The naming is intentionally aligned
    so operators reading both projects' docs see the same term mean
    the same thing.
  - DCR + holdout comparison: SDV's `NumericalRadiusNearestNeighbors`
    and `CategoricalCAP` privacy metrics use the same approach
    (sdv-dev/SDMetrics). Gower distance for the mixed-type case
    is Gower 1971 ("A General Coefficient of Similarity and Some
    of its Properties"), the standard cross-type distance.
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

Layout: this module computes new-row synthesis and assembles the report.
`compute_dcr` lives in `_dcr.py` and `compute_attack_metrics` in
`_attacks.py`; both are re-exported here.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pandas as pd

# Re-exported: callers import every SynthReport metric from this module.
from decoy_engine.quality._attacks import compute_attack_metrics  # noqa: F401
from decoy_engine.quality._dcr import _round, compute_dcr  # noqa: F401

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
    for any size - implementation just needs streaming hashing for
    very large frames, which can land later without touching the
    metric contract.

    Privacy note: the row hash is computed locally and never
    serialized - only the aggregate counts + fraction land in the
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
    dcr: dict[str, Any] | None = None,
    attacks: dict[str, Any] | None = None,
    job_id: int | None = None,
) -> dict[str, Any]:
    """Bundle the privacy metrics into a single JSON-serializable report.

    D7a populates `new_row_synthesis`. D7b populates `dcr`. D7c
    populates `attacks` ONLY when the optional extras package is
    installed AND the call site explicitly opts in - otherwise the
    attacks block records that attacks were skipped (not silently
    omitted).

    The report always includes the disclaimer block. This is required
    by the sprint acceptance criteria and survives schema evolution:
    operators reading the report at any time must see the explicit
    statement that DCR is not a privacy guarantee and that no
    differential-privacy claim is made.
    """
    # QA-10 F13 (2026-06-01): the "attacks were skipped" disclaimer
    # only applies when `attacks is None` (i.e., no attack metrics
    # in the report). When attacks WERE run + recorded, surfacing
    # that disclaimer is misleading: it tells operators "no attack
    # was attempted" while attack results sit in the same payload.
    # The other three disclaimers are unconditional (DCR caveat +
    # no-DP claim + high-fidelity-not-privacy) and survive every
    # report shape.
    disclaimers = [
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
    ]
    # QA-10 F13 refined (2026-06-01): the disclaimer applies when no
    # attack actually ran. That includes BOTH attacks=None AND the
    # _attacks_unavailable shape (available=False) that the platform
    # hook supplies by default. Pre-refinement the check was just
    # `attacks is None`; the platform hook always passes a dict so the
    # disclaimer dropped from every production report, contradicting
    # the intent. Now we keep the disclaimer whenever no attack ran.
    _attack_actually_attempted = (
        attacks is not None and isinstance(attacks, dict) and attacks.get("available") is not False
    )
    if not _attack_actually_attempted:
        disclaimers.append(
            "Attack-based metrics (Membership Inference, shadow-model) "
            "are OFF by default and only run when the operator "
            "explicitly opts in AND the privacy-attacks extras "
            "package is installed. Absence of attack results does "
            "NOT mean the synth survived an attack; it means no "
            "attack was attempted."
        )
    # QA-10 P1 severity-field plumbing (2026-06-01, PO Q1 lock per
    # qa-10-quality-report-hardening.md): the overall report severity
    # is the max-band across sub-reports. Ordering:
    #   ok < info < low < medium < high < critical
    # "info" routes things-that-did-not-run (attacks unavailable;
    # under-sample TVD per P2 F5) so they surface but do not escalate
    # the report's overall severity to medium. "critical" is reserved
    # for fail-open signals only the DCR-zero-distance path triggers
    # today (P3 F10 closure).
    overall_severity = _max_band(
        new_row_synthesis,
        dcr,
        attacks,
        attack_actually_attempted=_attack_actually_attempted,
    )
    return {
        "schema_version": SYNTH_REPORT_SCHEMA_VERSION,
        "job_id": job_id,
        "new_row_synthesis": new_row_synthesis,
        "dcr": dcr,
        "attacks": attacks,
        "severity": overall_severity,
        "disclaimers": disclaimers,
    }


# QA-10 P1 severity-field plumbing helpers (2026-06-01) ────────────────


# Ordering of the report's severity band. Lower index = less severe.
# Index in this tuple IS the comparable rank.
_SEVERITY_BANDS = ("ok", "info", "low", "medium", "high", "critical")


def _band_rank(band: str | None) -> int:
    """Numerical rank of a severity band. Unknown values map to 'info'
    (do not escalate, but surface to the operator)."""
    if band in _SEVERITY_BANDS:
        return _SEVERITY_BANDS.index(band)
    return _SEVERITY_BANDS.index("info")


def _max_band(
    new_row_synthesis: dict[str, Any] | None,
    dcr: dict[str, Any] | None,
    attacks: dict[str, Any] | None,
    *,
    attack_actually_attempted: bool,
) -> str:
    """Compute the report's overall severity as the max-band across
    sub-reports.

    Inputs:
      - new_row_synthesis.band (set by `compute_new_row_synthesis`):
        "low" / "medium" / "high" depending on fraction_new.
      - dcr: severity routing is "ok" when median > 0 + comparison
        clean; "critical" when DCR median == 0 (a synth row exactly
        copied a source row).
      - attacks: "ok" when an attack ran + survived; "info" when no
        attack ran (the _attacks_unavailable shape).

    Empty inputs (everything None) route to "info" since nothing
    actually ran."""
    ranks = []

    if isinstance(new_row_synthesis, dict):
        band = new_row_synthesis.get("band")
        if band is not None:
            ranks.append(_band_rank(band))

    if isinstance(dcr, dict):
        # DCR-zero-distance is the critical signal: a synth row equals
        # a source row exactly. compute_dcr surfaces this either as
        # `synth_to_source.median == 0` or as an explicit `band:
        # "critical"` field once P3 F10 closure ships. Pre-P3 we read
        # the median directly.
        synth_to_source = (
            dcr.get("synth_to_source") if isinstance(dcr.get("synth_to_source"), dict) else None
        )
        if synth_to_source is not None:
            median = synth_to_source.get("median")
            if isinstance(median, (int, float)) and median == 0:
                ranks.append(_band_rank("critical"))
            else:
                ranks.append(_band_rank("ok"))
        # Also accept an explicit band field if the caller set one.
        if isinstance(dcr.get("band"), str):
            ranks.append(_band_rank(dcr["band"]))

    if attacks is not None:
        if attack_actually_attempted:
            # Attack ran. Read the band; default ok if absent.
            ranks.append(_band_rank(attacks.get("band", "ok")))
        else:
            # Attack did not run; info-band so the operator sees the
            # gap but the overall severity does not escalate.
            ranks.append(_band_rank("info"))

    if not ranks:
        return "info"
    return _SEVERITY_BANDS[max(ranks)]


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
        # QA-10 F3 (2026-06-01, HIGH): pass usedforsecurity=False so
        # this call does not raise on FIPS-hardened OpenSSL builds.
        # Python 3.9+ requires the flag for SHA-1 in FIPS mode.
        # The hash here is a row-fingerprint collision-detector, not a
        # cryptographic primitive; SHA-1's preimage strength is
        # irrelevant. Healthcare + federal deployments (NPI / ICD-10 /
        # NDC providers) commonly run FIPS hosts.
        yield hashlib.sha1(composite.encode("utf-8"), usedforsecurity=False).hexdigest()


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
            "is a memorization signal - review the synth strategy "
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
