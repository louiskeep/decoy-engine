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
"""
from __future__ import annotations

import hashlib
from typing import Any

import numpy as np
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
    for any size: implementation just needs streaming hashing for
    very large frames, which can land later without touching the
    metric contract.

    Privacy note: the row hash is computed locally and never
    serialized: only the aggregate counts + fraction land in the
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


def compute_dcr(
    source: pd.DataFrame,
    output: pd.DataFrame,
    *,
    holdout: pd.DataFrame | None = None,
    subset_columns: list[str] | None = None,
    sample_cap: int = 5000,
) -> dict[str, Any]:
    """Distance to Closest Record sanity check (D7b).

    For each output row, compute the minimum Gower distance to any
    source row. Returns aggregate percentiles of those minimums; the
    raw per-row distances are NOT included (per the security
    requirements, they would leak which source rows the synth is
    closest to).

    When `holdout` is provided, the same computation runs against
    the holdout frame, producing the memorization-vs-generalization
    comparison block. Operators read the comparison this way:

        median(synth_to_source) ~ median(synth_to_holdout)
          -> synth is generalizing (good privacy signal)
        median(synth_to_source) << median(synth_to_holdout)
          -> synth is memorizing the training source (privacy risk)

    Gower distance (Gower 1971):
      - numeric columns: |a - b| / (max - min), the range-normalized
        absolute difference. NaN handling: if either side is NaN the
        per-column distance is 1.0 (worst case) for that column.
      - categorical / object columns: 0 if equal, 1 otherwise.
      - per-row distance is the mean of per-column distances, so it
        lands in [0, 1].

    Cost: O(N_out * N_source * cols). The sample_cap is a defensive
    guard against pathological frames; both sides are independently
    capped to `sample_cap` rows by deterministic sample (head() is
    used so the same call returns the same DCR; np.random would
    inject hidden non-determinism into a privacy metric).

    Returns:

        {
          "metric": "distance_to_closest_record",
          "synth_to_source": {
            "median": float, "p05": ..., "p25": ..., "p75": ..., "p95": ...,
            "rows_sampled": int, "source_rows_sampled": int,
          },
          "synth_to_holdout": {...} | None,
          "comparison": {
            "median_ratio": float | None,   # source / holdout
            "interpretation": "generalizing" | "borderline" | "memorizing" | None,
          },
          "subset_columns": [str, ...],
          "warning": str | None,
        }
    """
    if source is None or output is None:
        return _dcr_unavailable("source or output not provided")

    out_n = len(output)
    src_n = len(source)
    if out_n == 0 or src_n == 0:
        return _dcr_unavailable("source or output is empty")

    if subset_columns is None:
        cols = sorted(set(source.columns) & set(output.columns))
    else:
        cols = [c for c in subset_columns if c in source.columns and c in output.columns]
    if not cols:
        return _dcr_unavailable("no overlapping columns between source and output")

    # Cap deterministically with head() so the same inputs always
    # produce the same DCR. Random sampling here would make the
    # metric non-reproducible, which defeats the purpose of a sanity
    # check (operator can't compare against a previous run).
    src_capped = source.head(sample_cap)
    out_capped = output.head(sample_cap)

    # Build the normalization spec from the source frame ONLY. The
    # output's range is irrelevant: we're measuring how close output
    # rows are to source rows, in the source's own units. Using the
    # holdout's range would also bias the comparison.
    normalizer = _build_gower_normalizer(src_capped, cols)

    synth_to_source = _gower_min_distances(
        out_capped, src_capped, cols, normalizer,
    )
    synth_to_source_block = _summarize_dcr(synth_to_source, len(src_capped))

    synth_to_holdout_block = None
    comparison = {"median_ratio": None, "interpretation": None}
    if holdout is not None and len(holdout) > 0:
        h_cols = [c for c in cols if c in holdout.columns]
        if h_cols:
            holdout_capped = holdout.head(sample_cap)
            synth_to_holdout = _gower_min_distances(
                out_capped, holdout_capped, h_cols, normalizer,
            )
            synth_to_holdout_block = _summarize_dcr(
                synth_to_holdout, len(holdout_capped),
            )
            comparison = _interpret_holdout_comparison(
                synth_to_source_block["median"],
                synth_to_holdout_block["median"],
            )

    return {
        "metric": "distance_to_closest_record",
        "synth_to_source": synth_to_source_block,
        "synth_to_holdout": synth_to_holdout_block,
        "comparison": comparison,
        "subset_columns": cols,
        "warning": (
            "DCR is a sanity check, not a privacy guarantee. "
            "A high DCR does not establish that synthetic rows "
            "cannot be linked back to source records."
        ),
    }


def compute_attack_metrics(
    source: pd.DataFrame,
    output: pd.DataFrame,
    *,
    enable_attacks: bool = False,
    extras_module: str = "decoy_engine_privacy_attacks",
    holdout: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Run optional attack-based privacy metrics (D7c contract).

    Attack-based metrics (Membership Inference Attack, shadow-model
    attacks) require heavy ML dependencies and produce results whose
    interpretation needs care. Per the sprint security requirements:

      "Keep optional metric libraries as extras until approved by
       dependency/security review."

    So this function ONLY runs attack metrics when BOTH:

      1. The caller explicitly passes `enable_attacks=True`
         (gates platform admin policy through the call).
      2. The optional extras package `decoy_engine_privacy_attacks`
         (or `extras_module` if pinned) is importable.

    Either gate failing -> returns an `{available: False, reason: ...}`
    block. The default behavior of this function -- and therefore of
    a `compute_attack_metrics(source, output)` call from the platform
    runner with no opt-in flag -- is to record that attacks were
    skipped, not to silently run them.

    Returned shape when available:

        {
          "metric": "attack_based_metrics",
          "available": True,
          "results": <whatever the extras module returned>,
          "extras_module": str,
        }

    Returned shape when unavailable:

        {
          "metric": "attack_based_metrics",
          "available": False,
          "reason": "extras_not_installed" | "not_enabled_by_caller",
          "extras_module": str,
        }

    The extras package must expose:
      `run_privacy_attacks(source, output, *, holdout=None) -> dict`
    Returned dict shape is the extras module's contract; SynthReport
    persists it under the `results` key without re-shaping. This
    keeps the engine free of any direct ML attack dependency while
    still giving operators a single place to read the results.

    NOTE on the holdout pass-through: some attack metrics (notably
    the MIA family) genuinely require a holdout frame to compute.
    Forwarding it here lets the extras module use it without
    duplicating the holdout-resolution logic the platform already
    does for D7b's DCR.
    """
    if not enable_attacks:
        return _attacks_unavailable("not_enabled_by_caller", extras_module)

    try:
        import importlib

        attacks_pkg = importlib.import_module(extras_module)
    except ImportError:
        return _attacks_unavailable("extras_not_installed", extras_module)

    run_fn = getattr(attacks_pkg, "run_privacy_attacks", None)
    if run_fn is None:
        return _attacks_unavailable(
            "extras_module_missing_entry_point", extras_module,
        )

    try:
        results = run_fn(source, output, holdout=holdout)
    except Exception as exc:  # noqa: BLE001 - third-party can raise anything
        # Defensive: an attack extras module's failure must NOT
        # take down the rest of the SynthReport assembly. Record
        # the failure type for operator visibility, not the raw
        # exception (which could carry sensitive data).
        return {
            "metric": "attack_based_metrics",
            "available": False,
            "reason": "extras_runtime_error",
            "extras_module": extras_module,
            "error_type": type(exc).__name__,
        }

    return {
        "metric": "attack_based_metrics",
        "available": True,
        "results": results,
        "extras_module": extras_module,
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
        attacks is not None
        and isinstance(attacks, dict)
        and attacks.get("available") is not False
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
        synth_to_source = dcr.get("synth_to_source") if isinstance(dcr.get("synth_to_source"), dict) else None
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
        yield hashlib.sha1(composite.encode("utf-8"), usedforsecurity=False).hexdigest()  # noqa: S324


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


# ── DCR helpers (D7b) ─────────────────────────────────────────────────────


def _build_gower_normalizer(source: pd.DataFrame, cols: list[str]) -> dict[str, Any]:
    """Per-column metadata: numeric range or 'categorical' marker.

    Computed from the SOURCE frame's range only. Using the output
    or holdout range to normalize would let an outlier in either
    frame compress the meaningful source range and skew the metric.
    """
    spec: dict[str, Any] = {}
    for col in cols:
        ser = source[col]
        if pd.api.types.is_numeric_dtype(ser):
            arr = pd.to_numeric(ser, errors="coerce").dropna().to_numpy(dtype=float)
            if arr.size == 0:
                spec[col] = {"kind": "categorical"}
                continue
            lo = float(arr.min())
            hi = float(arr.max())
            spec[col] = {
                "kind": "numeric",
                "lo": lo,
                "hi": hi,
                "range": hi - lo if hi > lo else 1.0,
            }
        else:
            spec[col] = {"kind": "categorical"}
    return spec


def _gower_min_distances(
    output: pd.DataFrame,
    reference: pd.DataFrame,
    cols: list[str],
    normalizer: dict[str, Any],
) -> np.ndarray:
    """For each output row, the minimum Gower distance to any
    reference row, returned as a (len(output),) float array.

    Vectorized per column: each iteration builds an (N_out, N_ref)
    distance contribution for one column, sums into a running
    distance accumulator, then takes axis=1 min at the end. Memory:
    O(N_out * N_ref) for the accumulator (one column at a time).
    """
    n_out = len(output)
    n_ref = len(reference)
    if n_out == 0 or n_ref == 0 or not cols:
        return np.zeros(n_out, dtype=float)

    # Distance accumulator: sum of per-column distances; divide by
    # column count at the end for the Gower mean.
    dist_sum = np.zeros((n_out, n_ref), dtype=float)
    contributing_cols = 0

    for col in cols:
        spec = normalizer.get(col)
        if spec is None:
            continue
        if spec["kind"] == "numeric":
            r = spec["range"]
            # Coerce both sides; NaN per-cell gets max distance (1.0)
            # via the post-fill nan_to_num path below.
            out_vals = pd.to_numeric(output[col], errors="coerce").to_numpy(dtype=float)
            ref_vals = pd.to_numeric(reference[col], errors="coerce").to_numpy(dtype=float)
            # Broadcast subtraction: shape (n_out, 1) - shape (1, n_ref)
            # -> (n_out, n_ref). abs + divide by source range -> [0, 1]
            # (can exceed 1 if output has a value beyond source range;
            # clip to 1 so a single outlier doesn't dominate the mean).
            diff = np.abs(out_vals[:, None] - ref_vals[None, :]) / r
            diff = np.where(np.isnan(diff), 1.0, diff)
            np.clip(diff, 0.0, 1.0, out=diff)
            dist_sum += diff
            contributing_cols += 1
        else:
            # Categorical / object. astype(str) for stable equality
            # (handles None as 'None', NaN as 'nan' the same way
            # the row-hash path does).
            out_vals = output[col].astype(str).to_numpy()
            ref_vals = reference[col].astype(str).to_numpy()
            ne = out_vals[:, None] != ref_vals[None, :]
            dist_sum += ne.astype(float)
            contributing_cols += 1

    if contributing_cols == 0:
        return np.zeros(n_out, dtype=float)

    mean_dist = dist_sum / contributing_cols
    return mean_dist.min(axis=1)


def _summarize_dcr(distances: np.ndarray, ref_rows: int) -> dict[str, Any]:
    """Aggregate the per-row minimum distances. NO raw distances
    in the output: only percentiles + counts."""
    if distances.size == 0:
        return {
            "median": None,
            "p05": None, "p25": None, "p75": None, "p95": None,
            "rows_sampled": 0,
            "source_rows_sampled": ref_rows,
        }
    return {
        "median": _round(float(np.median(distances))),
        "p05": _round(float(np.quantile(distances, 0.05))),
        "p25": _round(float(np.quantile(distances, 0.25))),
        "p75": _round(float(np.quantile(distances, 0.75))),
        "p95": _round(float(np.quantile(distances, 0.95))),
        "rows_sampled": int(distances.size),
        "source_rows_sampled": ref_rows,
    }


def _interpret_holdout_comparison(
    median_source: float | None,
    median_holdout: float | None,
) -> dict[str, Any]:
    """Memorization signal from the source/holdout median ratio.

    The reasoning: if the synth was trained on `source` and has
    never seen `holdout`, then for a model that GENERALIZES the
    median DCR against both frames should be similar (the model is
    drawing from the underlying distribution, not the specific
    training rows). For a model that MEMORIZES, the synth rows
    are systematically closer to source rows than to holdout rows
    -> source-median < holdout-median.

    Thresholds:
      ratio >= 0.85   -> generalizing (DCRs roughly equal)
      0.50 <= ratio   -> borderline (some memorization signal)
      ratio < 0.50    -> memorizing  (synth markedly closer to source)

    These match SDV's documented thresholds for the same comparison.
    """
    if median_source is None or median_holdout is None:
        return {"median_ratio": None, "interpretation": None}
    if median_holdout == 0:
        # Holdout perfectly matched somewhere; ratio is undefined.
        # If source is also 0 the synth equally memorizes both,
        # which is its own kind of red flag; if source > 0 the
        # holdout is the leakier side, which means our training
        # data isn't what's leaking.
        return {"median_ratio": None, "interpretation": None}
    ratio = median_source / median_holdout
    if ratio >= 0.85:
        interp = "generalizing"
    elif ratio >= 0.50:
        interp = "borderline"
    else:
        interp = "memorizing"
    return {"median_ratio": _round(ratio), "interpretation": interp}


def _dcr_unavailable(reason: str) -> dict[str, Any]:
    return {
        "metric": "distance_to_closest_record",
        "synth_to_source": None,
        "synth_to_holdout": None,
        "comparison": {"median_ratio": None, "interpretation": None},
        "subset_columns": [],
        "warning": reason,
    }


# ── attacks helpers (D7c) ─────────────────────────────────────────────────


def _attacks_unavailable(reason: str, extras_module: str) -> dict[str, Any]:
    """The default attacks block: 'no attack was attempted'.

    Operators reading the SynthReport must be able to tell the
    difference between 'attacks were tried and the synth survived'
    (results dict) and 'no attack was tried' (this block). The
    explicit reason makes that distinction unambiguous in the
    audit trail.
    """
    return {
        "metric": "attack_based_metrics",
        "available": False,
        "reason": reason,
        "extras_module": extras_module,
    }
