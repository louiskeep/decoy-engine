"""Statistical column spec: validate config + snapshot into a sampler plan.

The spec layer owns every "is this config + artifact coherent?" question
so `_sample.py` can be pure draw logic and the plan compiler's
`check_statistical_columns` can reuse the same validation (one set of
error codes, raised here, surfaced either at compile time or at
generation time for unvalidated-dict callers).

Privacy gate: a categorical snapshot's `top_values` contain REAL values
from the source frame. Emitting them is a deliberate disclosure the
operator must opt into with `allow_real_categories: true`; without it
the spec refuses to load (`statistical_real_categories_not_allowed`).
Differential privacy is out of scope for v1 (capability-gaps plan,
decision 3, 2026-06-12).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from decoy_engine.quality.snapshot import DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION

_SUPPORTED_KINDS = ("numeric", "categorical", "datetime")
_OTHER_MODES = ("redistribute", "emit")

# The placeholder emitted for tail mass under other_mode="emit".
OTHER_TOKEN = "__other__"  # noqa: S105 -- a column placeholder value, not a credential


class StatisticalSpecError(Exception):
    """Config/artifact mismatch for a statistical column. Machine-readable code."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


@dataclass(frozen=True)
class StatisticalSpec:
    """Everything `sample_column` needs, validated."""

    column: str
    source_column: str
    kind: str  # numeric | categorical | datetime
    dtype: str
    stats: dict[str, Any]
    other_mode: str
    condition_on: str | None
    joint: dict[str, Any] | None  # the snapshot joint entry when condition_on
    parent_first: bool  # joint key order: True when condition_on is key[0]


# Snapshot files are read once per path per process; configs commonly
# point many columns at one artifact.
_SNAPSHOT_CACHE: dict[str, dict[str, Any]] = {}


def _load_snapshot(path: str) -> dict[str, Any]:
    cached = _SNAPSHOT_CACHE.get(path)
    if cached is not None:
        return cached
    try:
        with open(path, encoding="utf-8") as fh:
            snap = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise StatisticalSpecError(
            code="statistical_snapshot_unreadable",
            message=f"snapshot_file {path!r} could not be read: {exc}",
        ) from exc
    version = snap.get("schema_version")
    if version != DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION:
        raise StatisticalSpecError(
            code="statistical_snapshot_schema_mismatch",
            message=(
                f"snapshot_file {path!r} declares schema {version!r}; this engine "
                f"consumes {DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION!r}."
            ),
        )
    _SNAPSHOT_CACHE[path] = snap
    return snap


def load_spec(col_cfg: dict[str, Any]) -> StatisticalSpec:
    """Validate a `type: statistical` generate column into a sampler spec.

    Raises StatisticalSpecError with a stable code on every mismatch;
    the plan compiler surfaces the same codes at validate time.
    """
    name = col_cfg.get("name", "<unnamed>")
    snapshot_file = col_cfg.get("snapshot_file")
    if not snapshot_file:
        raise StatisticalSpecError(
            code="statistical_snapshot_file_required",
            message=f"statistical column {name!r} requires `snapshot_file`.",
        )
    snap = _load_snapshot(str(snapshot_file))

    source_column = str(col_cfg.get("source_column") or name)
    col_entry = (snap.get("columns") or {}).get(source_column)
    if col_entry is None:
        available = sorted((snap.get("columns") or {}).keys())
        raise StatisticalSpecError(
            code="statistical_column_not_in_snapshot",
            message=(
                f"statistical column {name!r}: source column {source_column!r} is "
                f"not in the snapshot (available: {available})."
            ),
        )
    kind = col_entry.get("kind")
    if kind not in _SUPPORTED_KINDS:
        raise StatisticalSpecError(
            code="statistical_kind_unsupported",
            message=(
                f"statistical column {name!r}: snapshot kind {kind!r} has no sampler "
                f"(supported: {', '.join(_SUPPORTED_KINDS)}). Freetext columns belong "
                f"on the faker path."
            ),
        )

    if kind == "categorical" and not bool(col_cfg.get("allow_real_categories")):
        raise StatisticalSpecError(
            code="statistical_real_categories_not_allowed",
            message=(
                f"statistical column {name!r}: the snapshot's top_values contain REAL "
                f"source values; emitting them requires `allow_real_categories: true` "
                f"on the column (explicit disclosure opt-in)."
            ),
        )

    other_mode = str(col_cfg.get("other_mode") or "redistribute")
    if other_mode not in _OTHER_MODES:
        raise StatisticalSpecError(
            code="statistical_other_mode_invalid",
            message=(
                f"statistical column {name!r}: other_mode {other_mode!r} is not one "
                f"of {_OTHER_MODES}."
            ),
        )

    condition_on = col_cfg.get("condition_on")
    joint: dict[str, Any] | None = None
    parent_first = False
    if condition_on is not None:
        condition_on = str(condition_on)
        if kind != "categorical":
            raise StatisticalSpecError(
                code="statistical_condition_kind_invalid",
                message=(
                    f"statistical column {name!r}: condition_on is supported for "
                    f"categorical columns only (snapshot kind here: {kind!r})."
                ),
            )
        pair = sorted((condition_on, source_column))
        for entry in snap.get("joints") or []:
            if list(entry.get("columns") or []) == pair:
                joint = entry
                break
        if joint is None:
            raise StatisticalSpecError(
                code="statistical_joint_missing",
                message=(
                    f"statistical column {name!r}: the snapshot has no joint "
                    f"contingency for ({condition_on!r}, {source_column!r}). "
                    f"Re-fit with `decoy fit --joint {condition_on},{source_column}`."
                ),
            )
        parent_first = joint["columns"][0] == condition_on

    return StatisticalSpec(
        column=str(name),
        source_column=source_column,
        kind=str(kind),
        dtype=str(col_entry.get("dtype") or ""),
        stats=dict(col_entry.get("stats") or {}),
        other_mode=other_mode,
        condition_on=condition_on,
        joint=joint,
        parent_first=parent_first,
    )
