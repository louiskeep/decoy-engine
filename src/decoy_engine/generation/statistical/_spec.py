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
Differential privacy lives at FIT time (`quality.dp.fit_dp_snapshot`);
the spec layer consumes exact and DP-fit snapshots identically EXCEPT
for one exemption: a categorical column whose snapshot the COMPILER has
already verified as an OpenDP-certified `dps-marginal/v3` release
(`dp_verified=True`, threaded in by the caller -- never inferred by
reading the snapshot's own `dp` key, guide section 5) does not need the
`allow_real_categories` consent gate, because there is no real vocabulary
in a verified DP artifact for it to release. `load_spec` never reopens a
snapshot path itself (guide section 4.7): the caller passes the already
pinned/parsed snapshot mapping.

`high_cardinality: true` (HC-5) opts a column into the FULL retained
vocabulary a fit step produced with `compute_distribution_snapshot(...,
high_cardinality_columns=...)` (quality/snapshot.py) -- it carries no
weight here beyond validating the config is coherent with that intent:
the flag requires `allow_real_categories: true` (retaining every
category is a LARGER disclosure than the top-K default, so it needs the
same consent gate, checked first for a specific error) and only makes
sense against a categorical snapshot kind. The sampler itself
(_sample.py) needs no high_cardinality-specific code: it already draws
over whatever `top_values` list the snapshot carries.

Freetext (deferred follow-up 4, 2026-06-12) is LENGTH-ONLY: the sampler
draws a target length from the fitted length histogram and fills with
deterministic lorem tokens. No source tokens are stored in the snapshot
(only length stats), so freetext needs no disclosure gate; a word-level
Markov mode that would store real n-grams is a recorded follow-up
behind its own opt-in.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from decoy_engine.quality.snapshot import DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION

_SUPPORTED_KINDS = ("numeric", "categorical", "datetime", "freetext")
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
    # The pinned snapshot's SHA-256 hex digest (guide section 4.7's read-once
    # pass), or None when this spec was built outside that pass (a direct/
    # unvalidated-dict caller with no Plan). Generation-time seed derivation
    # (`generators.derivation.strategy_config_fingerprint`) needs a content
    # digest for its `snapshot_file` fingerprint input; without this field it
    # had no pinned digest to reuse and fell back to re-opening
    # `col_cfg["snapshot_file"]` from disk at generate time -- a real TOCTOU
    # reopen of a path the Plan already pinned (guide section 4.7/4.8, defect
    # F4). Carrying the digest here lets the statistical generator branch
    # reuse it instead, closing that reopen without touching the sampler.
    snapshot_digest: str | None = None


def spec_to_dict(spec: StatisticalSpec) -> dict[str, Any]:
    """Plain-dict form of a validated `StatisticalSpec`, for embedding into
    `PinnedStatisticalSpec.spec` (guide section 4.7). `dataclasses.asdict`
    is exact here because every field is already a JSON-shaped value
    (str/dict/bool/None)."""
    return asdict(spec)


def spec_from_dict(data: Mapping[str, Any]) -> StatisticalSpec:
    """Inverse of `spec_to_dict`: reconstruct a `StatisticalSpec` from its
    plain-dict (or thawed frozen-mapping) form."""
    return StatisticalSpec(
        column=str(data["column"]),
        source_column=str(data["source_column"]),
        kind=str(data["kind"]),
        dtype=str(data["dtype"]),
        stats=dict(data["stats"]),
        other_mode=str(data["other_mode"]),
        condition_on=data["condition_on"],
        joint=dict(data["joint"]) if data.get("joint") is not None else None,
        parent_first=bool(data["parent_first"]),
        snapshot_digest=data.get("snapshot_digest"),
    )


# `_load_snapshot` is the sole `open()` site for a snapshot artifact in
# this package (guide section 4.7 item 3): the process-global content-
# keyed cache that used to live here is GONE. It stopped being a
# correctness mechanism once the compiler started reading every path
# exactly once, up front, into an immutable pinned mapping
# (`plan._generation.read_and_pin_snapshots`) before any check or
# `load_spec` call runs -- caching a second time here would only risk
# serving a DIFFERENT read's bytes to a caller expecting the compiler's
# pinned bytes. `load_spec` below never calls this function itself; it
# takes the already-parsed snapshot mapping as an explicit argument. This
# function remains for compile-time callers that still need to read a
# path (the compiler's read-once pass) and for tests building fixtures.
def _load_snapshot(path: str) -> tuple[str, dict[str, Any]]:
    """Read + parse a snapshot artifact. Returns `(content_sha256, parsed)`.
    No caching: every call is a fresh read + hash + parse."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise StatisticalSpecError(
            code="statistical_snapshot_unreadable",
            message=f"snapshot_file {path!r} could not be read: {exc}",
        ) from exc
    digest = hashlib.sha256(raw).hexdigest()
    try:
        snap = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StatisticalSpecError(
            code="statistical_snapshot_unreadable",
            message=f"snapshot_file {path!r} could not be read: {exc}",
        ) from exc
    if not isinstance(snap, dict):
        raise StatisticalSpecError(
            code="statistical_snapshot_schema_mismatch",
            message=f"snapshot_file {path!r} does not contain a JSON object at the top level.",
        )
    version = snap.get("schema_version")
    if version != DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION:
        raise StatisticalSpecError(
            code="statistical_snapshot_schema_mismatch",
            message=(
                f"snapshot_file {path!r} declares schema {version!r}; this engine "
                f"consumes {DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION!r}."
            ),
        )
    return digest, snap


def load_spec(
    col_cfg: dict[str, Any],
    *,
    snapshot: Mapping[str, Any],
    dp_verified: bool = False,
    snapshot_digest: str | None = None,
) -> StatisticalSpec:
    """Validate a `type: statistical` generate column into a sampler spec.

    Args:
        col_cfg: The generate-column config entry (name, source_column,
            other_mode, condition_on, allow_real_categories,
            high_cardinality).
        snapshot: The ALREADY parsed, already pinned snapshot artifact
            (guide section 4.7) -- this function never opens
            `col_cfg["snapshot_file"]` itself.
        dp_verified: Whether the PLAN COMPILER has verified this exact
            snapshot as an OpenDP-certified `dps-marginal/v3` release for
            this column (guide section 5). Never derived by reading
            `snapshot["dp"]` here -- an unverified `dp`-shaped key in an
            otherwise ordinary snapshot must not exempt anything; only the
            compiler's own verdict does.
        snapshot_digest: The pinned snapshot's SHA-256 hex digest, when the
            caller read it through the compile-time read-once pass
            (`plan._generation.read_and_pin_snapshots`). Carried onto the
            returned spec so generation-time seed derivation can reuse it
            instead of reopening `snapshot_file` (see `StatisticalSpec.
            snapshot_digest`).

    Raises StatisticalSpecError with a stable code on every mismatch;
    the plan compiler surfaces the same codes at validate time.
    """
    name = col_cfg.get("name", "<unnamed>")
    snap = snapshot
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
                f"(supported: {', '.join(_SUPPORTED_KINDS)})."
            ),
        )

    high_cardinality = col_cfg.get("high_cardinality")
    if high_cardinality is not None and not isinstance(high_cardinality, bool):
        raise StatisticalSpecError(
            code="statistical_high_cardinality_invalid_type",
            message=(
                f"statistical column {name!r}: high_cardinality must be a bool, got "
                f"{high_cardinality!r}."
            ),
        )
    high_cardinality = bool(high_cardinality)

    # HIGH-1 (gate remediation): `bool("false")` is True, so a truthy
    # non-bool string/int would otherwise sail through the consent gates
    # below. Validate the type up front (same shape as high_cardinality
    # above) and gate on `is True` identity, never `bool(...)` coercion.
    allow_real = col_cfg.get("allow_real_categories")
    if allow_real is not None and not isinstance(allow_real, bool):
        raise StatisticalSpecError(
            code="statistical_allow_real_categories_invalid_type",
            message=(
                f"statistical column {name!r}: allow_real_categories must be a "
                f"bool, got {allow_real!r}."
            ),
        )

    if high_cardinality and col_cfg.get("allow_real_categories") is not True:
        raise StatisticalSpecError(
            code="statistical_high_cardinality_requires_real_categories",
            message=(
                f"statistical column {name!r}: high_cardinality retains the FULL "
                f"observed category vocabulary (no top-K collapse), a larger "
                f"disclosure than the default; it requires `allow_real_categories: "
                f"true` on the same column (explicit disclosure opt-in)."
            ),
        )

    if (
        kind == "categorical"
        and not dp_verified
        and col_cfg.get("allow_real_categories") is not True
    ):
        raise StatisticalSpecError(
            code="statistical_real_categories_not_allowed",
            message=(
                f"statistical column {name!r}: the snapshot's top_values contain REAL "
                f"source values; emitting them requires `allow_real_categories: true` "
                f"on the column (explicit disclosure opt-in), unless the compiler has "
                f"verified this snapshot as an OpenDP-certified DP release."
            ),
        )

    if high_cardinality and kind != "categorical":
        raise StatisticalSpecError(
            code="statistical_high_cardinality_kind_invalid",
            message=(
                f"statistical column {name!r}: high_cardinality requires a "
                f"categorical snapshot kind (got {kind!r}). Re-fit the source "
                f"column with high_cardinality set so the fit step forces it "
                f"categorical."
            ),
        )

    # HIGH-2 (gate remediation): `high_cardinality: true` on the generate
    # side is a promise that the FIT step retained the full vocabulary
    # (quality/snapshot.py::_high_cardinality_categorical_stats stamps a
    # `high_cardinality: true` marker on the column stats for exactly this
    # reason). A snapshot fit WITHOUT that flag -- ordinary top-K collapse,
    # `other_count` possibly > 0 -- must not be silently accepted here: the
    # tail is already gone from the artifact, so redistribution would
    # permanently drop it. Provenance marker, not `other_count`, is the
    # source of truth (DP snapshots deep-copy the marker through noising).
    col_stats = col_entry.get("stats") if isinstance(col_entry.get("stats"), dict) else {}
    if high_cardinality and col_stats.get("high_cardinality") is not True:
        raise StatisticalSpecError(
            code="statistical_high_cardinality_snapshot_mismatch",
            message=(
                f"statistical column {name!r}: high_cardinality is set but the "
                f"snapshot column {source_column!r} was not fit with full-vocabulary "
                f"retention (no `high_cardinality` marker on the column stats). "
                f"Re-fit the source column with "
                f"`compute_distribution_snapshot(..., high_cardinality_columns=[...])` "
                f"before setting high_cardinality: true here."
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
        snapshot_digest=snapshot_digest,
    )
