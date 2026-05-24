"""Shape-only fidelity: similarity that ignores value identity.

V2 Phase 3 Distribution Integrity, Sprint D5b.

The D1c `compute_fidelity` scorer compares VALUES: hashed columns
score ~0 even though hash perfectly preserves cardinality and
frequency shape (the hashes are different strings from the source
values, so TVD treats them as disjoint distributions).

This module adds a complementary scorer that compares SHAPE:
sorted-descending normalized count vectors per column / per joint.
A 1:1 transform like hash gets ~1.0 because the source's
{CA: 50, NY: 30, TX: 20} and the hashed {hash1: 50, hash2: 30,
hash3: 20} have identical sorted-frequency vectors.

The two metrics measure different things:
  - value_identity (D1c): "are the actual output values close to
    the source values?" -- the right metric for shuffle, redact,
    and per-column statistical comparisons.
  - shape_only (D5b): "did the distribution shape survive,
    regardless of what values it landed on?" -- the right metric
    for hash, faker, and other deterministic-but-value-changing
    transforms.

Operators / policy can reference either. D4 policy adds
`thresholds.shape.*` keys parallel to the existing `thresholds.*`
keys; D5a's corrected per-strategy defaults stay as the
value-identity expectations, and a future companion table will
hold the shape expectations (where hash sits closer to 0.95).

Schema versioned `quality-shape-fidelity/v1`. The QualityReport
assembly in D1d can embed this dict as an optional `shape_fidelity`
top-level field; existing callers that do not compute shape
fidelity see unchanged report shape.

Method per kind:
  - numeric: sort `bin_counts` descending, normalize, TVD.
  - categorical / bool: sort `top_values` counts + other_count
    descending, normalize, TVD. Pads to equal length with zeros.
  - datetime: sort `year_bins` counts descending, normalize, TVD.
  - freetext: sort `length_bin_counts` descending, normalize, TVD.
  - empty: skipped (comparable=False).

Out of scope:
  - Replacing value_identity. D5b is additive; D1c stays the
    canonical similarity, and the QualityReport's primary
    `overall_score` stays value-identity. Shape is a second
    opinion, not a replacement.
  - Per-strategy shape expectations table. That belongs in a
    D5b follow-up commit on the policy module once operators
    have seen real shape scores on real jobs and can calibrate.
"""

from __future__ import annotations

from typing import Any

QUALITY_SHAPE_FIDELITY_SCHEMA_VERSION = "quality-shape-fidelity/v1"

_SCORE_PRECISION = 6


def compute_shape_fidelity(
    source_snapshot: dict[str, Any],
    output_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Compute shape-only similarity from two snapshots.

    Same input shape as `compute_fidelity`; produces a parallel
    dict with `shape_similarity` per column / joint plus aggregate
    `marginal.shape_score`, `pairwise.shape_score`, and
    `overall_shape_score`.

    Returns:
        Dict matching schema `quality-shape-fidelity/v1`.
    """
    src_cols = source_snapshot.get("columns", {})
    out_cols = output_snapshot.get("columns", {})
    shared_columns = sorted(set(src_cols.keys()) & set(out_cols.keys()))

    column_results: list[dict[str, Any]] = []
    for col in shared_columns:
        result = _column_shape_similarity(src_cols[col], out_cols[col])
        column_results.append({"column": col, **result})

    comparable_cols = [c for c in column_results if c["comparable"]]
    marginal_score = (
        round(
            sum(c["shape_similarity"] for c in comparable_cols) / len(comparable_cols),
            _SCORE_PRECISION,
        )
        if comparable_cols
        else None
    )

    src_joints = source_snapshot.get("joints", [])
    out_joints_by_pair = {tuple(j.get("columns", [])): j for j in output_snapshot.get("joints", [])}
    joint_results: list[dict[str, Any]] = []
    for src_joint in src_joints:
        pair = tuple(src_joint.get("columns", []))
        out_joint = out_joints_by_pair.get(pair)
        if out_joint is None:
            joint_results.append(
                {
                    "columns": list(pair),
                    "shape_similarity": None,
                    "method": "no_output_joint",
                    "comparable": False,
                },
            )
            continue
        result = _joint_shape_similarity(src_joint, out_joint)
        joint_results.append({"columns": list(pair), **result})

    comparable_joints = [j for j in joint_results if j["comparable"]]
    pairwise_score = (
        round(
            sum(j["shape_similarity"] for j in comparable_joints) / len(comparable_joints),
            _SCORE_PRECISION,
        )
        if comparable_joints
        else None
    )

    overall = _compute_overall(marginal_score, pairwise_score)

    return {
        "schema_version": QUALITY_SHAPE_FIDELITY_SCHEMA_VERSION,
        "marginal": {"shape_score": marginal_score, "columns": column_results},
        "pairwise": {"shape_score": pairwise_score, "joints": joint_results},
        "overall_shape_score": overall,
    }


# ── per-kind shape comparators ─────────────────────────────────────────────


def _column_shape_similarity(
    src_col: dict[str, Any],
    out_col: dict[str, Any],
) -> dict[str, Any]:
    src_kind = src_col.get("kind")
    out_kind = out_col.get("kind")
    if src_kind != out_kind:
        return {
            "shape_similarity": None,
            "method": "kind_mismatch",
            "comparable": False,
        }
    if src_kind == "empty":
        return {
            "shape_similarity": None,
            "method": "empty",
            "comparable": False,
        }

    src_stats = src_col.get("stats", {})
    out_stats = out_col.get("stats", {})

    if src_kind == "numeric":
        return _shape_tvd(
            src_stats.get("bin_counts", []),
            out_stats.get("bin_counts", []),
            label="bin_counts_sorted_tvd",
        )
    if src_kind in ("categorical", "bool"):
        src_counts = [int(item["count"]) for item in src_stats.get("top_values", [])]
        src_counts.append(int(src_stats.get("other_count", 0)))
        out_counts = [int(item["count"]) for item in out_stats.get("top_values", [])]
        out_counts.append(int(out_stats.get("other_count", 0)))
        return _shape_tvd(src_counts, out_counts, label="freq_vector_sorted_tvd")
    if src_kind == "datetime":
        src_counts = [int(b["count"]) for b in src_stats.get("year_bins", [])]
        out_counts = [int(b["count"]) for b in out_stats.get("year_bins", [])]
        return _shape_tvd(src_counts, out_counts, label="year_bins_sorted_tvd")
    if src_kind == "freetext":
        return _shape_tvd(
            src_stats.get("length_bin_counts", []),
            out_stats.get("length_bin_counts", []),
            label="length_bins_sorted_tvd",
        )
    return {
        "shape_similarity": None,
        "method": f"unknown_kind:{src_kind}",
        "comparable": False,
    }


def _joint_shape_similarity(
    src_joint: dict[str, Any],
    out_joint: dict[str, Any],
) -> dict[str, Any]:
    src_cells = src_joint.get("cells", [])
    out_cells = out_joint.get("cells", [])
    src_counts = [int(c["count"]) for c in src_cells]
    src_counts.append(int(src_joint.get("other_count", 0)))
    out_counts = [int(c["count"]) for c in out_cells]
    out_counts.append(int(out_joint.get("other_count", 0)))
    return _shape_tvd(src_counts, out_counts, label="joint_cells_sorted_tvd")


# ── shape-vector TVD ───────────────────────────────────────────────────────


def _shape_tvd(
    src_counts: list[int],
    out_counts: list[int],
    *,
    label: str,
) -> dict[str, Any]:
    """Sorted-descending normalized TVD on two count vectors.

    Pad to equal length with zeros so vectors of different sizes
    can compare without truncating either side.
    """
    src_sorted = sorted((int(c) for c in src_counts if int(c) > 0), reverse=True)
    out_sorted = sorted((int(c) for c in out_counts if int(c) > 0), reverse=True)
    if not src_sorted and not out_sorted:
        return {
            "shape_similarity": None,
            "method": "no_data",
            "comparable": False,
        }
    n = max(len(src_sorted), len(out_sorted))
    src_padded = src_sorted + [0] * (n - len(src_sorted))
    out_padded = out_sorted + [0] * (n - len(out_sorted))
    src_total = sum(src_padded) or 1
    out_total = sum(out_padded) or 1
    src_norm = [c / src_total for c in src_padded]
    out_norm = [c / out_total for c in out_padded]
    tvd = 0.5 * sum(abs(s - o) for s, o in zip(src_norm, out_norm, strict=True))
    similarity = max(0.0, 1.0 - tvd)
    return {
        "shape_similarity": round(similarity, _SCORE_PRECISION),
        "method": label,
        "comparable": True,
    }


# ── aggregation ────────────────────────────────────────────────────────────


def _compute_overall(
    marginal: float | None,
    pairwise: float | None,
) -> float | None:
    if marginal is None and pairwise is None:
        return None
    if pairwise is None:
        return marginal
    if marginal is None:
        return pairwise
    return round((marginal + pairwise) / 2.0, _SCORE_PRECISION)
