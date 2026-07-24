"""Pure carrier/shape evidence helpers for the DP generate verifier.

Split out of `plan/_checks_dp.py` on a size-cap crossing (CLAUDE.md's ~600-LOC
orchestration cap) when the L-1 per-column identity check landed. These are
data-independent pure functions over a `dps-marginal/v3` artifact's own `dp`
and `columns` blocks -- `verify_dp_snapshots` (in `_checks_dp.py`) is their sole
caller. Keeping them here leaves the verifier's orchestration under the cap
without trimming any of the invariant text these checks depend on.
"""

from __future__ import annotations

import math
from typing import Any

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.quality.dp_schema import (
    DP_ADJACENCY,
    DP_BOUNDARY_VALUES,
    DP_CODEC_ID,
    DP_CODEC_VERSION,
    DP_RELEASE_SCOPE,
)


def carrier_aware_query_count(dp_block: dict[str, Any]) -> int | None:
    """Reconstruct the artifact's own `query_count` from its recorded
    `column_schema`, carrier-aware (guide sections 3.4/3.9): a `number` carrier
    is one numeric query, a `text` OR `flag` carrier is a categorical PAIR (the
    flag column's bool-domain pair is the same DP guarantee as the text path, so
    it reconstructs to the same count). Returns `1 + numeric + 2*categorical`,
    or `None` when the recorded schema is missing/malformed or carries an
    unknown carrier (the caller then rejects the artifact)."""
    column_schema = dp_block.get("column_schema")
    if not isinstance(column_schema, dict):
        return None
    numeric = 0
    categorical = 0
    for spec in column_schema.values():
        if not isinstance(spec, dict):
            return None
        carrier = spec.get("carrier")
        if carrier == "number":
            numeric += 1
        elif carrier in ("text", "flag"):
            categorical += 1
        else:
            return None
    return 1 + numeric + 2 * categorical


def schema_matches_legacy(
    column_schema: dict[str, Any],
    categorical_cols: list[Any],
    numeric_domains: dict[str, Any],
) -> bool:
    """MEDIUM-5: the v3 `column_schema` must agree with the artifact's own legacy
    `categorical_columns`/`numeric_domains` declaration per column in name,
    carrier, KIND, and (for a `number` carrier) the numeric bounds -- not merely
    in carrier and set membership. A reconstructed count agreeing is necessary
    but not sufficient (a name-disjoint or carrier-transposed schema can match
    the count), and an artifact that records a contradictory `kind` or different
    `bounds` while keeping a valid `numeric_domains` would otherwise verify
    because the numeric path trusts `numeric_domains`. Any mismatch is a
    corrupted or hand-edited artifact, not a genuine release."""
    if set(column_schema) != set(categorical_cols) | set(numeric_domains):
        return False
    for name, spec in column_schema.items():
        if not isinstance(spec, dict):
            return False
        carrier = spec.get("carrier")
        kind = spec.get("kind")
        if carrier == "number":
            if name not in numeric_domains or name in categorical_cols:
                return False
            if kind != "numeric":
                return False
            bounds = spec.get("bounds")
            if not isinstance(bounds, (list, tuple)) or tuple(bounds) != tuple(
                numeric_domains[name]
            ):
                return False
        elif carrier in ("text", "flag"):
            if name not in categorical_cols or name in numeric_domains:
                return False
            if kind != "categorical":
                return False
        else:
            return False
    return True


def column_block_matches_schema(
    col_snap: dict[str, Any], column_schema: dict[str, Any], source_column: str
) -> bool:
    """HIGH-2: the per-column `columns` block's own `carrier`/`kind` -- which
    `load_spec` (`_spec.py`) reads onto `StatisticalSpec.carrier` and the
    sampler dispatches on -- must equal the `dp.column_schema` entry for the
    same column. If they disagree, a `flag` release could be relabelled
    `text` in the `columns` block (while `dp.column_schema` still declares
    `flag`), passing verification and reaching the sampler with the wrong
    carrier: the bool "true"/"false" tokens would decode through the legacy
    str() categorical path instead of the flag bool decode. Pin agreement
    between the two recorded carriers at the verify gate."""
    spec = column_schema.get(source_column)
    if not isinstance(spec, dict):
        return False
    return col_snap.get("carrier") == spec.get("carrier") and col_snap.get("kind") == spec.get(
        "kind"
    )


def _flag_tokens_are_canonical(col_snap: dict[str, Any]) -> bool:
    """Guide section 3.4 shape guard: a `flag` column's `top_values[].value`
    entries must be exactly the canonical `"true"`/`"false"` tokens the fit
    path (`quality/dp.py::_flag_token`) always emits. It is data-independent
    (checks the shape of the recorded tokens, not any private value), so it
    catches a corrupted or hand-edited artifact carrying a bogus token
    (`"1"`, `"True"`, `"maybe"`, ...) that the phase-6 sampler's canonical
    decode (`generation/statistical/_sample.py::_decode_flag_token`) would
    otherwise refuse only at generate time, well after verification passed."""
    stats = col_snap.get("stats")
    if not isinstance(stats, dict):
        return False
    top_values = stats.get("top_values")
    if not isinstance(top_values, list):
        return False
    return all(
        isinstance(entry, dict) and entry.get("value") in ("true", "false") for entry in top_values
    )


def check_flag_tokens_canonical(
    col_snap: dict[str, Any],
    *,
    kind: Any,
    col_carrier: Any,
    table_name: Any,
    col_name: Any,
    source_column: str,
) -> None:
    """Raising wrapper around `_flag_tokens_are_canonical`, kept here (not in
    `_checks_dp.py`) so `verify_dp_snapshots`'s own call site stays a single
    expression -- that module is the orchestrator CLAUDE.md's ~600-LOC cap
    applies to. A no-op for anything but a `flag` categorical column."""
    if kind != "categorical" or col_carrier != "flag" or _flag_tokens_are_canonical(col_snap):
        return
    raise PlanCompileError(
        code="dp_flag_token_invalid",
        path=f"tables.{table_name}.generate_columns.{col_name}.snapshot_file",
        message=(
            f"statistical column {col_name!r} in table {table_name!r}: source "
            f"column {source_column!r} is a flag release with a top_values "
            "token other than the canonical 'true'/'false'."
        ),
    )


def numeric_shape_matches_a_dp_release(
    col_snap: dict[str, Any], *, lower: float, upper: float, numeric_bins: int
) -> bool:
    """BLOCKER 2 item 3 (cheap shape evidence): an exact `compute_
    distribution_snapshot` numeric column carries real `min`/`max`/`mean`/
    `std`/`quantiles` (`quality/snapshot.py:566-575`); a genuine
    `fit_dp_snapshot` numeric column NEVER does (guide section 4.2.1: `min`/
    `max` are the declared domain bounds, `mean`/`std` are always `None`,
    `quantiles` is always `{}`, and `bin_counts` always has exactly
    `numeric_bins` entries).

    This is a guard against copy-paste, not against an adversary, and the
    security review corrected the page that used to claim otherwise. It
    stops exactly one case: an ordinary EXACT snapshot with a fabricated
    `dp` block bolted on, whose numeric `stats` still carries the real
    min/max/mean/quantiles untouched. It stops nothing else. All four
    fields read below are attacker-writable, and defeating the check
    needs no DP knowledge and no reproduction of the artifact format --
    null `mean`, `std` and `quantiles`, declare `numeric_bins` as the
    length of the histogram already present, and declare the domain as
    the observed min and max. The resulting artifact compiles
    DP-verified while `bin_counts` are still the exact unnoised
    histogram. Do not read the categorical path's lack of an equivalent
    check as this path being defended; the asymmetry exists only for the
    copy-paste case. See `docs/what-we-cannot-prove.md`, which is the
    authority on this wording and is pinned by
    `tests/unit/test_dp_claim_copy.py`."""
    stats = col_snap.get("stats")
    if not isinstance(stats, dict):
        return False
    bin_counts = stats.get("bin_counts")
    return (
        stats.get("min") == lower
        and stats.get("max") == upper
        and stats.get("mean") is None
        and stats.get("std") is None
        and stats.get("quantiles") == {}
        and isinstance(bin_counts, list)
        and len(bin_counts) == numeric_bins
    )


def _is_finite_real(value: Any) -> bool:
    """A real number the numeric domain may use as a bound: an `int` or
    `float` (NOT a `bool` -- `True`/`False` are `int` subclasses that must not
    stand in for a domain edge) that is finite."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _canonical_bin_edges(lower: float, upper: float, numeric_bins: int) -> list[float]:
    """The bin edges a genuine `fit_dp_snapshot` release ALWAYS carries for a
    `[lower, upper]` domain split into `numeric_bins` bins: the two bounds plus
    the `numeric_bins - 1` public interior cut points, derived from the public
    domain alone (guide section 4.4). Byte-for-byte the fit's own arithmetic
    (`quality/dp.py::_interior_edges`), so a genuine artifact matches exactly and
    a tampered one -- edges pushed outside the domain, which the sampler would
    then draw from and emit out-of-domain values -- does not."""
    interior = [lower + (upper - lower) * i / numeric_bins for i in range(1, numeric_bins)]
    return [lower, *interior, upper]


def check_numeric_release_shape(
    col_snap: dict[str, Any],
    dp_block: dict[str, Any],
    *,
    source_column: str,
    numeric_domains: dict[str, Any],
    table_name: Any,
    col_name: Any,
) -> None:
    """Raising numeric-release gate (guide section 4.4), lifted whole out of
    `verify_dp_snapshots` to keep that orchestrator under the ~600-LOC cap.

    Fails closed unless: the declared domain is a `[lower, upper]` pair of
    finite, non-bool reals with `lower < upper` and `numeric_bins` a positive
    non-bool int; the stats block has the copy-paste-evidence shape
    (`numeric_shape_matches_a_dp_release`); AND `stats.bin_edges` are exactly the
    canonical edges derived from the public domain. The edge check is the one the
    old inline block omitted (Codex whole-feature HIGH): the sampler draws from
    `bin_edges`, so edges pushed outside `[lower, upper]` would emit values
    outside the declared domain the DP release promised."""
    domain = numeric_domains.get(source_column)
    numeric_bins = dp_block.get("numeric_bins")
    shape_path = f"tables.{table_name}.generate_columns.{col_name}.snapshot_file"
    shape_msg = (
        f"statistical column {col_name!r} in table {table_name!r}: source "
        f"column {source_column!r} is declared numeric, but its declared domain "
        "or stats block does not have the shape a genuine fit_dp_snapshot "
        "release always has (a finite lower<upper domain, a positive integer "
        "numeric_bins, min/max equal to the declared bounds, mean/std null, "
        "quantiles empty, and len(bin_counts) == numeric_bins). This is the "
        "shape of an exact, non-DP snapshot with a copied or hand-written dp "
        "block, not a DP release for this column."
    )
    if not (
        isinstance(domain, list)
        and len(domain) == 2
        and _is_finite_real(domain[0])
        and _is_finite_real(domain[1])
        and isinstance(numeric_bins, int)
        and not isinstance(numeric_bins, bool)
        and numeric_bins >= 1
    ):
        raise PlanCompileError(
            code="dp_snapshot_numeric_shape_mismatch", path=shape_path, message=shape_msg
        )
    # The conjunction above proved these; assert to narrow domain->list and
    # numeric_bins->int for the arithmetic below (mypy cannot narrow through the
    # negated conjunction on its own).
    assert isinstance(domain, list)  # noqa: S101 - type-narrowing invariant
    assert isinstance(numeric_bins, int)  # noqa: S101 - type-narrowing invariant
    lower, upper = float(domain[0]), float(domain[1])
    if not (
        lower < upper
        and numeric_shape_matches_a_dp_release(
            col_snap, lower=lower, upper=upper, numeric_bins=numeric_bins
        )
    ):
        raise PlanCompileError(
            code="dp_snapshot_numeric_shape_mismatch", path=shape_path, message=shape_msg
        )
    canonical = _canonical_bin_edges(lower, upper, numeric_bins)
    edges = col_snap.get("stats", {}).get("bin_edges")
    edges_ok = (
        isinstance(edges, list)
        and len(edges) == len(canonical)
        and all(_is_finite_real(e) for e in edges)
        and [float(e) for e in edges] == canonical
    )
    if not edges_ok:
        raise PlanCompileError(
            code="dp_snapshot_numeric_edges_mismatch",
            path=f"tables.{table_name}.generate_columns.{col_name}.snapshot_file",
            message=(
                f"statistical column {col_name!r} in table {table_name!r}: source "
                f"column {source_column!r} records bin_edges that are not the canonical "
                "edges derived from its declared domain and numeric_bins. A genuine "
                "fit_dp_snapshot release always carries the public evenly-spaced edges; "
                "edges pushed outside the declared domain would make the sampler emit "
                "values outside that domain, so a mismatch fails closed."
            ),
        )


def check_release_compatibility(
    dp_block: dict[str, Any], *, table_name: Any, col_name: Any
) -> None:
    """Raising release-shape-identity gate (guide sections 3.9/4.4): the artifact
    must record the EXACT codec, scope, adjacency and boundary this build's
    stability-1 argument is made for. `verify_dp_snapshots` checks the schema
    version but not these (Codex whole-feature BLOCKER): an artifact fit under a
    future codec version, or recording a different scope/adjacency, would
    otherwise be accepted as DP-verified while the receipt hardcodes
    'single-column-marginals' -- a silent guarantee mismatch. Semantic
    validation, independent of the deferred artifact-authentication MAC."""
    codec = dp_block.get("codec")
    if not (
        isinstance(codec, dict)
        and codec.get("id") == DP_CODEC_ID
        and codec.get("version") == DP_CODEC_VERSION
    ):
        raise PlanCompileError(
            code="dp_snapshot_codec_unsupported",
            path=f"tables.{table_name}.generate_columns.{col_name}.snapshot_file",
            message=(
                f"statistical column {col_name!r} in table {table_name!r}: the snapshot "
                f"records codec {codec!r}, not the {DP_CODEC_ID!r} version "
                f"{DP_CODEC_VERSION!r} this build implements. The stability-1 guarantee "
                "is specific to this codec generation; an artifact from another is not "
                "one this build may accept."
            ),
        )
    scope = dp_block.get("scope")
    adjacency = dp_block.get("adjacency")
    boundary = dp_block.get("boundary")
    if scope != DP_RELEASE_SCOPE or adjacency != DP_ADJACENCY or boundary not in DP_BOUNDARY_VALUES:
        raise PlanCompileError(
            code="dp_snapshot_release_shape_unsupported",
            path=f"tables.{table_name}.generate_columns.{col_name}.snapshot_file",
            message=(
                f"statistical column {col_name!r} in table {table_name!r}: the snapshot "
                f"records scope={scope!r}, adjacency={adjacency!r}, boundary={boundary!r}, "
                f"which must be exactly scope={DP_RELEASE_SCOPE!r}, "
                f"adjacency={DP_ADJACENCY!r}, and a boundary in {DP_BOUNDARY_VALUES!r}. "
                "The stability-1 argument is made for this release shape only."
            ),
        )
