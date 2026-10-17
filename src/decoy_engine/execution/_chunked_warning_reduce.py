"""Cross-chunk fold of per-chunk `QualityWarning`s.

Most warnings are the same object in every chunk (the chunks run one plan), so the fold keeps the
first and drops equal repeats. Two codes carry counts over the rows of the frame they were
computed on, so each chunk's copy holds a chunk-local count and an equality dedup would keep
several partial warnings where the full-frame run emits one global warning. For those codes the
fold sums the counts into one warning at the position of the first.

The summed codes are the ones the operator registry declares as `reduce_warning:<code>`
(`fpe_partial_plaintext_disclosure`, `text_mask_sub_floor_span_handled`); a test pins the table
below to the registry so a newly declared reducible code cannot be forgotten. The sum is exact
because both counts are plain per-row tallies. A warning whose detail does not have the expected
shape falls back to the equality dedup, so an unexpected payload is never silently altered.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

__all__ = ["CHUNK_STABLE_WARNING_CODES", "REDUCED_WARNING_CODES", "fold_warning"]


def _sum_counts(
    keys: tuple[str, ...],
) -> Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None]:
    def merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any] | None:
        if set(a) != set(b):
            return None
        out = dict(a)
        for key in keys:
            if not (isinstance(a.get(key), int) and isinstance(b.get(key), int)):
                return None
            out[key] = a[key] + b[key]
        if any(a[k] != b[k] for k in a if k not in keys):
            return None  # a non-count field differs: not the same warning, do not fuse
        return out

    return merge


def _text_mask_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any] | None:
    by_a, by_b = a.get("by_detector"), b.get("by_detector")
    if not (isinstance(by_a, dict) and isinstance(by_b, dict)):
        return None
    if not (isinstance(a.get("total"), int) and isinstance(b.get("total"), int)):
        return None
    if set(a) != set(b) or any(a[k] != b[k] for k in a if k not in ("by_detector", "total")):
        return None
    merged: dict[str, Any] = dict(by_a)
    for key, count in by_b.items():
        if not (isinstance(count, int) and isinstance(merged.get(key, 0), int)):
            return None
        merged[key] = merged.get(key, 0) + count
    return {**a, "by_detector": merged, "total": a["total"] + b["total"]}


_REDUCERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None]] = {
    "fpe_partial_plaintext_disclosure": _sum_counts(("affected_values", "total_values")),
    "text_mask_sub_floor_span_handled": _text_mask_merge,
}
REDUCED_WARNING_CODES = frozenset(_REDUCERS)
# Codes whose detail is fixed by the column config, so every chunk emits an equal warning and the
# equality dedup already yields the full-frame result.
_CONFIG_CONSTANT_CODES = frozenset({"fpe_join_group_active"})
# Every code that is identical to the full-frame warning after the fold. Admission uses this.
CHUNK_STABLE_WARNING_CODES = REDUCED_WARNING_CODES | _CONFIG_CONSTANT_CODES


def fold_warning(seen: list[Any], warning: Any) -> None:
    """Fold `warning` into `seen`: sum a reducible code's counts, else equality-dedup."""
    reducer = _REDUCERS.get(str(getattr(warning, "code", "")))
    if reducer is not None:
        for i, kept in enumerate(seen):
            if (kept.code, kept.provider, kept.column) == (
                warning.code,
                warning.provider,
                warning.column,
            ):
                detail = reducer(kept.detail, warning.detail)
                if detail is not None:
                    seen[i] = dataclasses.replace(kept, detail=detail)
                    return
    if warning not in seen:
        seen.append(warning)
