"""Public `column_schema` handling for the DP fit (freeze + parse).

Split out of `quality/dp.py` on a size-cap crossing (CLAUDE.md's ~600-LOC cap)
when the schema-freeze hardening landed. These are pandas/pyarrow-free pure
functions over a caller's public `column_schema` -- they read only the declared
schema, never a private value, so `fit_dp_snapshot` runs them BEFORE the proof
stack gate and before any cell is fetched. `quality/dp.py` is their sole caller.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.quality.carriers import CarrierError, _validate_bound

# The closed release-kind x carrier table (guide section 3.3). `kind` is
# OPTIONAL in `column_schema` (the carrier alone drives the codec and the
# mechanism), but when a caller supplies a `kind` it is validated against this
# table so an impossible pair (categorical + number, which OpenDP has no float
# `make_count_by` for) fails loud rather than silently mis-releasing. Kept in
# sync with `carrier_adapter._KIND_TO_CARRIERS` so the DataFrame and direct
# paths accept exactly the same schema.
_KIND_TO_CARRIERS: dict[str, tuple[str, ...]] = {
    "numeric": ("number",),
    "categorical": ("text", "flag"),
}


def freeze_column_schema(column_schema: Any) -> Any:
    """Return a deep-frozen copy of a caller's `column_schema` so a mutable
    mapping cannot drift between the fit's several reads (parse -> routing,
    adapter/sanitize -> values, then the recorded artifact metadata). A schema
    whose iteration yields a different carrier -- or whose nested `bounds` list
    is mutated -- on a later read could release under one mechanism or domain
    while the artifact declares another, which the verifier trusts. Freeze all
    three levels: the outer mapping, each per-column spec dict, and the `bounds`
    sequence (-> an immutable tuple). Non-dict schemas or non-dict specs pass
    through unfrozen for `_parse_column_schema`/`_validate_column_schema` to
    reject with the normal coded error."""
    if not isinstance(column_schema, dict):
        return column_schema
    frozen: dict[Any, Any] = {}
    for key, spec in column_schema.items():
        if isinstance(spec, dict):
            spec_copy = dict(spec)
            bounds = spec_copy.get("bounds")
            if isinstance(bounds, (list, tuple)):
                spec_copy["bounds"] = tuple(bounds)
            frozen[key] = spec_copy
        else:
            frozen[key] = spec
    return frozen


def parse_column_schema(
    column_schema: Any,
) -> tuple[dict[str, tuple[float, float]], dict[str, str]]:
    """Split a validated `column_schema` into numeric bounds and categorical
    carriers for schedule construction.

    Reads only the public schema, never a value, so it runs BEFORE the proof
    stack gate and BEFORE any private cell is fetched. Structural problems (not
    a dict, a bad carrier, a malformed/misordered number bound, an impossible
    kind x carrier pair) fail loud with the SAME coded `CarrierError` the
    carrier layer raises, so the DataFrame and direct paths reject an identical
    schema identically. The authoritative per-cell FFI-safety validation is
    still `sanitize_carrier_table`'s (run on every input); this is only what the
    schedule needs to know a column's mechanism domain and bin edges."""
    if not isinstance(column_schema, dict):
        raise CarrierError(
            code="dp_schema_type",
            message=f"column_schema must be a dict, got {type(column_schema).__name__}",
        )
    numeric_bounds: dict[str, tuple[float, float]] = {}
    categorical_carriers: dict[str, str] = {}
    for name, spec in column_schema.items():
        if not isinstance(spec, dict):
            raise CarrierError(
                code="dp_schema_column_type",
                message=f"column {name!r}: schema entry must be a dict, got {type(spec).__name__}",
            )
        carrier = spec.get("carrier")
        if not isinstance(carrier, str) or carrier not in ("number", "flag", "text"):
            raise CarrierError(
                code="dp_carrier_unknown",
                message=(
                    f"column {name!r}: unknown carrier {carrier!r}, expected one of "
                    "('number', 'flag', 'text')"
                ),
            )
        kind = spec.get("kind")
        if kind is not None:
            if not isinstance(kind, str) or kind not in _KIND_TO_CARRIERS:
                raise CarrierError(
                    code="dp_kind_unknown",
                    message=(
                        f"column {name!r}: unknown kind {kind!r}, expected one of "
                        f"{tuple(_KIND_TO_CARRIERS)}"
                    ),
                )
            allowed = _KIND_TO_CARRIERS[kind]
            if carrier not in allowed:
                raise CarrierError(
                    code="dp_kind_carrier_mismatch",
                    message=(
                        f"column {name!r}: kind {kind!r} does not allow carrier {carrier!r} "
                        f"(allowed: {allowed})"
                    ),
                )
        if carrier == "number":
            bounds = spec.get("bounds")
            if not isinstance(bounds, (tuple, list)) or len(bounds) != 2:
                raise CarrierError(
                    code="dp_carrier_bounds_missing",
                    message=f"column {name!r}: a 'number' carrier requires (lower, upper) bounds",
                )
            lower = _validate_bound(name, "lower", bounds[0])
            upper = _validate_bound(name, "upper", bounds[1])
            if not lower < upper:
                raise CarrierError(
                    code="dp_carrier_bounds_order",
                    message=f"column {name!r}: bounds must satisfy lower < upper, got ({lower}, {upper})",
                )
            numeric_bounds[name] = (lower, upper)
        else:
            categorical_carriers[name] = carrier
    return numeric_bounds, categorical_carriers
