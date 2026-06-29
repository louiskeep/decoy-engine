"""Relabel-invariant correlation check for the test-flight suite (Phase 3c).

Implements check_correlation_through_masking: a harness-side Cramers V check
that measures categorical association via chi-square over contingency COUNTS.

Why Cramers V is relabel-invariant
-----------------------------------
The chi-square statistic and Cramers V depend only on the COUNTS in the
contingency table (pd.crosstab), not on the value labels themselves::

    V = sqrt(chi2 / (n * min(r - 1, c - 1)))

where n is the total row count, r and c are the numbers of distinct values in
each column, and chi2 is the Pearson chi-square statistic computed from the
observed contingency table vs the expected counts under independence.

An FPE bijection remaps every unique value in column A to a different unique
output value (and similarly for column B), but because FPE is a bijection
(no collisions, no merges) the COUNT of every (A_val, B_val) pair is
preserved exactly. The contingency table of the masked output has the same
cell counts as the source -- just different labels. Therefore Cramers V of
the output equals Cramers V of the source.

Contrast with the engine's crosstab-TVD metric: it compares value-LABELED
cells. Source cell (A="X", B="Y") has count 100; output cell (A="X", B="Y")
has count 0 (because FPE relabeled X and Y to different strings). The TVD is
1.0 and similarity is 0.0, even when the correlation structure is perfectly
preserved. A faithfully FPE-masked pair scored 0.0 under that metric, worse
than a genuinely decorrelated pair at around 0.34.

Reference: Cramer (1946), "Mathematical Methods of Statistics," section 21.4;
Pearson (1900) chi-square test of independence. Implementation uses only
numpy and pandas (no scipy dependency): chi-square is computed from observed
vs row/col marginal expected counts as sum((O - E)^2 / E) over all cells
with E > 0.

Degenerate-input guard
-----------------------
When one column has only one unique value, min(r-1, c-1) = 0 and Cramers V
is undefined (0 / 0). The internal helper _cramers_v returns None in that
case. check_correlation_through_masking then returns a result dict with
v_src=None or v_out=None and does NOT assert anything (no divide-by-zero,
no false failure). Callers that need to assert against degenerate data should
validate their fixtures independently.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _cramers_v(col_a: pd.Series, col_b: pd.Series) -> float | None:
    """Compute Cramers V association between two categorical Series.

    Uses the Pearson chi-square statistic over the (col_a, col_b) contingency
    table. The chi-square is computed without scipy: expected counts are the
    outer product of row and column marginal sums divided by n.

    Args:
        col_a: First categorical column.
        col_b: Second categorical column (must be same length as col_a).

    Returns:
        Cramers V in [0.0, 1.0], or None when V is undefined (fewer than 2
        unique non-null values in either column, or n == 0).
    """
    # Align non-null pairs only.
    mask = col_a.notna() & col_b.notna()
    a = col_a[mask]
    b = col_b[mask]
    n = len(a)
    if n == 0:
        return None

    # Build contingency table of COUNTS (relabel-invariant: only counts matter).
    ct = pd.crosstab(a, b)
    r, c = ct.shape
    if r < 2 or c < 2:
        # Degenerate: at least one column has only one unique (non-null) value.
        return None

    observed = ct.to_numpy(dtype=float)
    row_sums = observed.sum(axis=1, keepdims=True)  # shape (r, 1)
    col_sums = observed.sum(axis=0, keepdims=True)  # shape (1, c)
    expected = row_sums * col_sums / float(n)  # outer-product expected counts

    # chi2 = sum over cells where E > 0 of (O - E)^2 / E.
    with np.errstate(divide="ignore", invalid="ignore"):
        chi2_cells = np.where(expected > 0.0, (observed - expected) ** 2 / expected, 0.0)
    chi2 = float(chi2_cells.sum())

    denom = float(n) * float(min(r - 1, c - 1))
    if denom <= 0.0:
        return None

    v = float(np.sqrt(chi2 / denom))
    # Clamp to [0, 1] to guard against floating-point overshoot.
    return min(1.0, max(0.0, v))


# ---------------------------------------------------------------------------
# Public invariant
# ---------------------------------------------------------------------------


def check_correlation_through_masking(
    job_name: str,
    table: str,
    col_a: str,
    col_b: str,
    source_df: pd.DataFrame,
    output_df: pd.DataFrame,
    *,
    tol: float = 0.10,
    min_assoc: float = 0.0,
    strategy_a: str | None = None,
    strategy_b: str | None = None,
) -> dict[str, Any]:
    """Assert that Cramers V association is preserved through value-changing masking.

    Computes Cramers V on (source[col_a], source[col_b]) and on
    (output[col_a], output[col_b]), then asserts::

        abs(v_out - v_src) <= tol

    Because Cramers V depends on contingency COUNTS (not value labels), an FPE
    bijection on both columns yields v_out == v_src exactly (to floating-point
    precision). This closes the carry-forward that the engine crosstab-TVD
    metric cannot handle: an FPE-masked pair scores 0.0 under TVD (disjoint
    label sets) even when the association structure is perfectly preserved.

    Degenerate guard: if either column has fewer than 2 unique non-null values
    in source_df or output_df, Cramers V is undefined. The function returns a
    result dict with v_src=None or v_out=None and does NOT assert. Callers that
    want to enforce non-degeneracy should validate their fixture data before
    calling this function.

    Args:
        job_name: Job name for error messages.
        table: Table name for error messages.
        col_a: First column name (must exist in source_df and output_df).
        col_b: Second column name (must exist in source_df and output_df).
        source_df: Pre-mask pandas DataFrame.
        output_df: Post-mask pandas DataFrame.
        tol: Maximum allowed absolute difference |v_out - v_src|. Default 0.10.
            For FPE bijections the theoretical diff is 0.0; tol provides a
            small budget for floating-point and degenerate-row edge cases.
        min_assoc: Optional floor: source Cramers V must be >= min_assoc.
            Non-zero means the source columns are required to be meaningfully
            associated so the check is non-vacuous (a near-zero v_src means
            any output passes and the tooth has no bite).
        strategy_a: Strategy name for col_a (included in error messages only).
        strategy_b: Strategy name for col_b (included in error messages only).

    Returns:
        Dict with keys v_src, v_out, diff. diff is None when either V is
        undefined (degenerate input).

    Raises:
        ValueError: If a declared column is absent from source_df or output_df.
        AssertionError: If v_src < min_assoc, or if abs(v_out - v_src) > tol.
    """
    # Validate column presence (fail-closed: a typo in the spec must error,
    # not silently disable the check).
    for col in (col_a, col_b):
        if col not in source_df.columns:
            raise ValueError(
                f"[{job_name}/{table}] masked_correlation: column {col!r} absent "
                f"from source_df. Check the manifest masked_correlations entry."
            )
        if col not in output_df.columns:
            raise ValueError(
                f"[{job_name}/{table}] masked_correlation: column {col!r} absent "
                f"from output_df. Check that the pipeline produced this column."
            )

    v_src = _cramers_v(source_df[col_a], source_df[col_b])
    v_out = _cramers_v(output_df[col_a], output_df[col_b])

    # Degenerate guard: undefined V -> skip assertion, return None markers.
    if v_src is None or v_out is None:
        return {"v_src": v_src, "v_out": v_out, "diff": None}

    # Non-vacuity floor: source association must be meaningful.
    if min_assoc > 0.0:
        assert v_src >= min_assoc, (
            f"[{job_name}/{table}] masked_correlation ({col_a!r}, {col_b!r}): "
            f"v_src={v_src:.4f} < min_assoc={min_assoc}. "
            f"Source columns do not have sufficient association; the check would "
            f"be vacuous (a Cramers V floor near 0 cannot catch association "
            f"destruction). Strengthen the fixture correlation or lower min_assoc."
        )

    diff = abs(v_out - v_src)
    strat_label = f"strategies=({strategy_a!r}, {strategy_b!r}) " if strategy_a else ""
    assert diff <= tol, (
        f"[{job_name}/{table}] masked_correlation ({col_a!r}, {col_b!r}): "
        f"Cramers V drift = {diff:.4f} > tol={tol}. "
        f"v_src={v_src:.4f} v_out={v_out:.4f}. {strat_label}"
        f"The joint association structure was NOT preserved through masking. "
        f"For FPE bijections, drift should be ~0 because the bijection preserves "
        f"contingency counts exactly. A non-zero drift indicates the masking "
        f"strategy altered the joint count structure (e.g. one column was "
        f"independently shuffled, destroying the pairing)."
    )

    return {"v_src": float(v_src), "v_out": float(v_out), "diff": float(diff)}
