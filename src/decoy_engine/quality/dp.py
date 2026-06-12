"""Differentially private release of distribution snapshots.

`apply_dp_noise` turns an exact `distribution-snapshot/v1` (the `decoy
fit` artifact) into a noisy release: every count in the snapshot gets
independent Laplace noise, exact moments that leak individual values
are removed, and min/max collapse to histogram-edge resolution. The
samplers in `generation/statistical` consume the noisy artifact
unchanged (weights are relative; they normalize).

Methodology (per the established-methodology rule):

- The Laplace mechanism: Dwork, McSherry, Nissim, Smith, "Calibrating
  Noise to Sensitivity in Private Data Analysis" (TCC 2006). Each
  released count gets noise drawn from Laplace(scale = sensitivity /
  epsilon).
- Histogram release pattern: OpenDP / SmartNoise noisy histograms.
  Under add/remove-one-row adjacency, one row changes exactly one bin
  of a disjoint histogram by 1, so per-bin sensitivity is 1 and the
  bins of ONE histogram compose in parallel (the whole histogram costs
  a single epsilon).
- Framing: NIST SP 800-188 (de-identifying government datasets),
  which treats DP releases of aggregate statistics as the formal
  alternative to ad-hoc suppression.

Budget accounting, stated honestly: `epsilon` here is PER RELEASED
HISTOGRAM. A snapshot of k columns releases k histograms plus the
table-level counts, which compose SEQUENTIALLY: the total budget is on
the order of (k + 1) * epsilon, not epsilon. The `dp` metadata block
records `scope: "per-column-histogram"` so downstream consumers cannot
misread the guarantee.

What this release does NOT protect (recorded caveats, see also
docs/what-we-cannot-prove.md):

- Bin EDGES and category LABELS remain data-dependent supports: the
  histogram range comes from the real min/max and `top_values` carries
  real category strings (already gated behind the
  `allow_real_categories` opt-in). SmartNoise mitigates with fixed,
  data-independent bin ranges and thresholded category release; both
  are recorded follow-ups.
- Joint contingency tables: rejected in v1 (`dp_joint_unsupported`).
  Releasing marginals plus joints under one stated epsilon without
  composition accounting would overclaim; refit without `--joint` or
  omit epsilon.

Noise source: fresh OS entropy (`numpy.random.default_rng()`), NEVER
the job seed. Seeding noise from material the config holder owns would
let them subtract the noise exactly and void the guarantee, which is
why OpenDP's release RNG is explicitly non-seedable. The `rng`
parameter exists for tests only. The reproducibility boundary is the
ARTIFACT: generation from a fixed noisy snapshot is fully
deterministic (the samplers are seeded; the snapshot is just weights).

Interplay with the generation-time fidelity warn-gate: DP removes
exact numeric quantiles, so the gate's numeric comparator reports
`no_quantiles` (not comparable) for DP'd columns; categorical and
datetime columns still score (TVD over counts). Lower fidelity against
the noisy artifact is the privacy/utility trade working as intended.
"""

from __future__ import annotations

import copy
import math
from datetime import datetime, timedelta
from typing import Any

import numpy as np


class DpError(Exception):
    """Invalid DP release request. Machine-readable code."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def apply_dp_noise(
    snapshot: dict[str, Any],
    *,
    epsilon: float,
    rng: np.random.Generator | None = None,
) -> dict[str, Any]:
    """Return a NEW snapshot dict with Laplace-noised counts.

    Args:
        snapshot: A `distribution-snapshot/v1` dict (not mutated).
        epsilon: Per-histogram privacy budget; must be a finite float
            greater than zero (an infinite "DP" flag that is a silent
            no-op is a footgun, so it is rejected).
        rng: Noise source override for TESTS ONLY. Production callers
            leave it None for fresh OS entropy; see the module
            docstring for why the job seed must never be used.

    Returns:
        A deep-copied snapshot with noised counts, exact moments
        removed, edge-resolution min/max, and a `dp` metadata block.
        The result stays schema `distribution-snapshot/v1` (the `dp`
        key is additive; loaders ignore unknown keys).

    Raises:
        DpError: ``code='dp_epsilon_invalid'`` when epsilon is not a
            finite positive float; ``code='dp_joint_unsupported'`` when
            the snapshot carries joint contingency tables (no
            composition accounting in v1).
    """
    try:
        epsilon = float(epsilon)
    except (TypeError, ValueError) as exc:
        raise DpError(
            code="dp_epsilon_invalid",
            message=f"epsilon must be a number; got {epsilon!r}.",
        ) from exc
    if not math.isfinite(epsilon) or epsilon <= 0:
        raise DpError(
            code="dp_epsilon_invalid",
            message=(
                f"epsilon must be a finite float > 0; got {epsilon!r}. A "
                "non-positive or infinite epsilon would be a no-op labeled DP."
            ),
        )
    if snapshot.get("joints"):
        raise DpError(
            code="dp_joint_unsupported",
            message=(
                "this snapshot carries joint contingency tables; releasing "
                "marginals plus joints under one epsilon needs sequential "
                "composition accounting, which v1 does not do. Refit without "
                "--joint, or omit --epsilon."
            ),
        )

    generator = rng if rng is not None else np.random.default_rng()
    scale = 1.0 / epsilon

    def _noisy(count: Any) -> int:
        # float() unwraps the numpy scalar so round() returns a python int
        # (round(np.float64) stays numpy and would not JSON-serialize).
        return max(0, round(float(count) + float(generator.laplace(0.0, scale))))

    out = copy.deepcopy(snapshot)
    if "row_count" in out:
        out["row_count"] = _noisy(out["row_count"])
    for col_entry in (out.get("columns") or {}).values():
        for key in ("null_count", "non_null_count", "distinct_count"):
            if key in col_entry:
                col_entry[key] = _noisy(col_entry[key])
        stats = col_entry.get("stats") or {}
        kind = col_entry.get("kind")
        if kind == "numeric" and stats.get("bin_counts"):
            stats["bin_counts"] = [_noisy(c) for c in stats["bin_counts"]]
            # Exact moments leak (a quantile IS a real value's neighborhood;
            # the samplers never read them). Min/max collapse to the
            # histogram edges, which the noised release already exposes.
            stats["quantiles"] = {}
            stats["mean"] = None
            stats["std"] = None
            edges = stats.get("bin_edges") or []
            if len(edges) >= 2:
                stats["min"] = edges[0]
                stats["max"] = edges[-1]
        elif kind == "categorical":
            for item in stats.get("top_values") or []:
                item["count"] = _noisy(item["count"])
            if "other_count" in stats:
                stats["other_count"] = _noisy(stats["other_count"])
        elif kind == "datetime" and stats.get("year_bins"):
            for item in stats["year_bins"]:
                item["count"] = _noisy(item["count"])
            years = [int(b["year"]) for b in stats["year_bins"]]
            # Year-bin resolution: the sampler draws a year then a uniform
            # timestamp within it, so exact min/max only ever clamped the
            # boundary years. Widening them to year bounds removes the two
            # real record timestamps from the artifact.
            stats["min"] = datetime(min(years), 1, 1).isoformat()
            stats["max"] = (datetime(max(years) + 1, 1, 1) - timedelta(seconds=1)).isoformat()
        elif kind == "freetext" and stats.get("length_bin_counts"):
            stats["length_bin_counts"] = [_noisy(c) for c in stats["length_bin_counts"]]
            length = stats.get("length") or {}
            length["mean"] = None
            length["std"] = None
            edges = stats.get("length_bin_edges") or []
            if len(edges) >= 2:
                length["min"] = int(edges[0])
                length["max"] = int(edges[-1])

    out["dp"] = {
        "epsilon": epsilon,
        "mechanism": "laplace",
        "sensitivity": 1,
        "adjacency": "add-remove-one-row",
        "scope": "per-column-histogram",
    }
    return out
