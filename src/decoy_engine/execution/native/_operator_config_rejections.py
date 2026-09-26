"""Per-operator config/type admission gates for the native planning boundary.

The categorical / bucket_perturb / group_key / date_shift
``*_config_rejection`` resolvers plus the small constants and helpers they own.
Each returns the coded reason a column cannot run on its native operator, or
None when it can. Both native admission boundaries (the compiler's
``_config_gate_rejection`` and the config-only ``native_route_eligibility``
query) call these SAME functions so they can never reach a different verdict for
the same column.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._categorical import _build_cdf
from decoy_engine.execution.native._requirements import resolve_input_arrow_type

# Mirrors the oracle's `transforms.bucket_perturb._VALID_BUCKETS`; kept local to
# avoid importing the transforms module into the planning boundary.
_VALID_BUCKET_PERTURB_BUCKETS = frozenset({"week", "month", "quarter"})


def is_deterministic_categorical(resolved_config: Any) -> bool:
    """Whether a categorical column's resolved config selects the deterministic
    (source-keyed, row-local) path -- the SINGLE source of truth the native
    determinism gate consults at the config-only boundary.

    Reproduces the seed envelope's own determinism computation for a column
    (`plan/_seed_envelope.py`): the first-class `deterministic: bool` field,
    OR the `allow_collisions: true` alias that forces deterministic reuse. So
    `ColumnSeed.deterministic == is_deterministic_categorical(col_config)` by
    construction (pinned by a test), letting the config-only native-route query
    decide determinism without a compiled `ColumnSeed`.
    """
    get = resolved_config.get if hasattr(resolved_config, "get") else (lambda _k, _d=None: _d)
    return bool(get("deterministic", False)) or bool(get("allow_collisions", False))


def categorical_config_rejection(
    name: str,
    *,
    deterministic: bool,
    namespace: str | None,
    provider_config: dict[str, Any],
) -> str | None:
    """The coded reason a `categorical` column cannot run on the native
    operator, or None when it can (Phase 5 Track B).

    v1 admits ONLY the deterministic, namespaced, STRING-category variant. An
    unseeded (non-deterministic) categorical draws a whole-column vector that
    is not reproducible, so it declines to the oracle here rather than silently
    running the always-deterministic native operator. Non-string categories
    decline (the oracle's data-dependent output-type reconciliation for them is
    a later slice). A weighted config whose CDF the oracle's `_build_cdf` would
    reject (nonpositive total, a below-resolution weight) declines here too, so
    the whole table routes to the oracle, which raises the identical error --
    never a native-side compile failure the oracle would not produce.
    """
    if not deterministic:
        return f"categorical_not_deterministic:{name}"
    if not namespace:
        return f"categorical_requires_namespace:{name}"
    categories = provider_config.get("categories")
    if not isinstance(categories, (list, tuple)) or not categories:
        return f"categorical_categories_not_nonempty_list:{name}"
    if not all(isinstance(c, str) for c in categories):
        return f"categorical_categories_not_all_string:{name}"
    weights = provider_config.get("weights")
    if weights is not None:
        if not isinstance(weights, (list, tuple)) or len(weights) != len(categories):
            return f"categorical_weights_shape:{name}"
        if any(isinstance(w, bool) or not isinstance(w, (int, float)) for w in weights):
            return f"categorical_weights_not_numeric:{name}"
        if any(w < 0 for w in weights):
            return f"categorical_weights_negative:{name}"
        try:
            _build_cdf([float(w) for w in weights])
        except StrategyError:
            return f"categorical_weights_unbuildable_cdf:{name}"
    return None


def bucket_perturb_config_rejection(
    name: str,
    table: str,
    profile: Any | None,
    *,
    namespace: str | None,
    provider_config: dict[str, Any],
) -> str | None:
    """The coded reason a `bucket_perturb` column cannot run natively, or None.

    v1 admits ONLY the string-source, explicit-`date_format`, tz-free,
    valid-bucket, namespaced variant; everything else declines to the oracle. The
    oracle defaults a missing bucket to "month" (`_bucket_perturb.py:54`) and
    treats a missing/empty/non-string `date_format` as autodetect (an
    order-dependent parity hazard). A non-string source declines (`astype(str)`
    is an identity only for strings, which keeps canonicalization byte-parity-
    safe). Both native boundaries call this ONE resolver so they never diverge.
    """
    if not namespace:
        return f"bucket_perturb_requires_namespace:{name}"
    bucket = str(provider_config.get("bucket", "month"))
    if bucket not in _VALID_BUCKET_PERTURB_BUCKETS:
        return f"bucket_perturb_unsupported_bucket:{name}"
    date_format = provider_config.get("date_format")
    if not isinstance(date_format, str) or not date_format:
        return f"bucket_perturb_requires_date_format:{name}"
    # A tz directive (%z/%Z) declines: the oracle reduces each value to a naive
    # `datetime.date` before strftime (dropping time AND tz) while the native
    # kernel keeps tz-aware Timestamps. Imported lazily so the pandas-bearing
    # kernel module stays off the planning boundary's module-load path.
    from decoy_engine.execution.native._bucket_perturb_ext import has_timezone_directive

    if has_timezone_directive(date_format):
        return f"bucket_perturb_timezone_directive:{name}"
    # An unresolved profile leaves the input type unknowable; defer to the
    # unified-slice resident-type gate (matches hash). A RESOLVED non-string
    # type is rejected here, early.
    if profile is not None:
        resolved = resolve_input_arrow_type(table, name, profile)
        if resolved is not None and resolved != pa.string():
            return f"bucket_perturb_source_not_string:{name}:{resolved!s}"
    return None


# pandas `format=` values that are NOT strptime directives: "mixed" infers a
# format per element and "ISO8601" accepts any ISO shape. Neither is an explicit
# format in the v1 sense, so both decline.
_PANDAS_SPECIAL_DATE_FORMATS = frozenset({"mixed", "ISO8601"})


# Distinguishes an ABSENT bound (the oracle applies its default) from one set
# explicitly to null (the oracle's `int(None)` raises), which `.get()` conflates.
_ABSENT = object()


def _date_shift_bound_rejection(name: str, key: str, value: Any) -> str | None:
    if value is _ABSENT:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return f"date_shift_{key}_not_int:{name}"
    # Imported lazily: the kernel module pulls in pandas, which the planning
    # boundary keeps off its module-load path.
    from decoy_engine.execution.native._date_shift_ext import MAX_ABS_SHIFT_DAYS

    if abs(value) > MAX_ABS_SHIFT_DAYS:
        return f"date_shift_{key}_out_of_range:{name}"
    return None


def date_shift_config_rejection(
    name: str,
    table: str,
    profile: Any | None,
    *,
    namespace: str | None,
    provider_config: dict[str, Any],
) -> str | None:
    """The coded reason a `date_shift` column cannot run natively, or None.

    v1 admits ONLY the namespaced, string-source, explicit-`date_format`,
    tz-free, no-`group_by` variant with integer day bounds. The oracle treats a
    missing/empty format as whole-column autodetect (`_detect_format`, a
    `format_detect` prepass) and a truthy `group_by` as a pre-mask sibling
    anchor; both stay on the oracle. `min_days`/`max_days` pass through the
    oracle's `int()`, which also accepts floats and numeric strings; v1 narrows
    to real ints so the native bound resolution cannot diverge from it.
    """
    if not namespace:
        return f"date_shift_requires_namespace:{name}"
    if provider_config.get("group_by"):
        return f"date_shift_group_by_not_native:{name}"
    date_format = provider_config.get("date_format")
    if not isinstance(date_format, str) or not date_format:
        return f"date_shift_requires_date_format:{name}"
    if date_format in _PANDAS_SPECIAL_DATE_FORMATS:
        return f"date_shift_special_date_format:{name}"
    from decoy_engine.execution.native._bucket_perturb_ext import has_timezone_directive

    if has_timezone_directive(date_format):
        return f"date_shift_timezone_directive:{name}"
    for key in ("min_days", "max_days"):
        reason = _date_shift_bound_rejection(name, key, provider_config.get(key, _ABSENT))
        if reason is not None:
            return reason
    # An unresolved profile defers to the unified-slice resident-type gate
    # (matches hash/bucket_perturb); a RESOLVED non-string type rejects here.
    if profile is not None:
        resolved = resolve_input_arrow_type(table, name, profile)
        if resolved is not None and resolved != pa.string():
            return f"date_shift_source_not_string:{name}:{resolved!s}"
    return None


# The native group_key route admits ONLY these sibling resident types in v1.
# The full stringify-safe set is larger (`_chunked_group_key.group_by_type_is_safe`:
# integer, bool, string, large_string, date, timestamp), and the operator itself
# masks all of them byte-identically -- but the sibling MUST be an unmasked
# passthrough node, and passthrough's own production resident set is exactly
# {string, int64, bool} (`_unified_slice_admission._ADMITTED_RESIDENT_TYPES`).
# So a large_string/int32/uint64/date/timestamp sibling could never activate
# end-to-end anyway; admitting it here would over-advertise. Narrow to the
# intersection so admission is honest; float/decimal/dictionary stay excluded
# (str()/collision + exact-type-map reasons). Extending passthrough's resident
# set + end-to-end coverage for the wider set is a later slice.
_NATIVE_GROUP_KEY_SIBLING_TYPES = frozenset({pa.string(), pa.int64(), pa.bool_()})


def group_key_sibling_type_admitted(arrow_type: pa.DataType) -> bool:
    """Whether a group_by sibling's Arrow type is admitted to the native
    group_key route in v1: exactly `{string, int64, bool}` (see
    `_NATIVE_GROUP_KEY_SIBLING_TYPES`). Everything else -- including the wider
    stringify-safe types the operator supports but production passthrough cannot
    yet carry -- declines to the oracle."""
    return arrow_type in _NATIVE_GROUP_KEY_SIBLING_TYPES


def group_key_config_rejection(
    name: str,
    table: str,
    profile: Any | None,
    *,
    provider_config: dict[str, Any],
) -> str | None:
    """The coded reason a `group_key` column cannot run natively, or None.

    v1 admits ONLY a column whose `group_by` sibling is present and resolves to
    a safe Arrow type (integer/bool/string/large_string/date/timestamp;
    float/decimal/dictionary excluded), with a valid even `length` in `[8, 64]`.
    `group_by` and `length` are already validated at plan-compile
    (`GroupKeyConfig.from_dict`), so those checks are defensive; the load-bearing
    gate here is the SIBLING type. An unresolved profile leaves the sibling type
    unknowable, so this defers to the unified-slice resident-type gate (matching
    hash) rather than guessing. The order-dependence decline (the sibling must
    not itself be masked by another node) needs cross-node visibility the config
    boundary lacks and is enforced at `resident_contract_admission`, the
    full-visibility shadow-vs-oracle arbiter."""
    group_by = provider_config.get("group_by")
    if not isinstance(group_by, str) or not group_by:
        return f"group_key_requires_group_by:{name}"
    length = provider_config.get("length", 16)
    if isinstance(length, bool) or not isinstance(length, int):
        return f"group_key_length_not_int:{name}"
    if length % 2 != 0 or length < 8 or length > 64:
        return f"group_key_length_out_of_range:{name}"
    if profile is not None:
        resolved = resolve_input_arrow_type(table, group_by, profile)
        if resolved is not None and not group_key_sibling_type_admitted(resolved):
            return f"group_key_group_by_type_not_native:{name}:{group_by}:{resolved!s}"
    return None


__all__ = [
    "bucket_perturb_config_rejection",
    "categorical_config_rejection",
    "date_shift_config_rejection",
    "group_key_config_rejection",
    "group_key_sibling_type_admitted",
    "is_deterministic_categorical",
]
