"""The prepared, validated categorical mapping shared by admission and execution.

Deterministic categorical needs two resolved inputs that never change within a run:
the string categories and, for the weighted variant, the integer CDF the oracle's
`_build_cdf` produces. `prepare_categorical` validates the config and builds both in
one place, so the native config gate (`categorical_config_rejection`), the chunked
output-type rule and the per-chunk kernel call all read the same artifact and cannot
disagree on what is admissible or what the weights resolve to. The chunked entry
builds it once per run and hands it to every chunk; nothing here runs per chunk.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._categorical import _build_cdf


@dataclass(frozen=True)
class PreparedCategorical:
    categories: tuple[str, ...]
    # None selects the uniform path; otherwise the oracle's `_build_cdf` output.
    cdf: tuple[int, ...] | None
    # The seeded non-deterministic variant: keyed by global row position, not source value.
    positional: bool = False


def prepare_categorical(
    name: str,
    *,
    deterministic: bool,
    namespace: str | None,
    provider_config: Mapping[str, Any],
) -> tuple[PreparedCategorical | None, str | None]:
    """`(artifact, None)` when the column can run on the native operator, else
    `(None, coded_reason)`.

    The native operator admits ONLY the deterministic, namespaced, STRING-category
    variant. A non-deterministic categorical is position-keyed, which this
    source-keyed operator does not implement, so it declines here rather than silently
    running the always-deterministic native operator. Non-string categories decline
    (the oracle's data-dependent output type for them is not this operator's to reproduce). A
    weighted config whose CDF the oracle's `_build_cdf` would reject (nonpositive
    total, a below-resolution weight) declines too, so the table routes to the
    oracle, which raises the identical error instead of a native-side failure the
    oracle would not produce.
    """
    if not deterministic:
        return None, f"categorical_not_deterministic:{name}"
    if not namespace:
        return None, f"categorical_requires_namespace:{name}"
    categories = provider_config.get("categories")
    if not isinstance(categories, (list, tuple)) or not categories:
        return None, f"categorical_categories_not_nonempty_list:{name}"
    if not all(isinstance(c, str) for c in categories):
        return None, f"categorical_categories_not_all_string:{name}"
    weights = provider_config.get("weights")
    cdf: tuple[int, ...] | None = None
    if weights is not None:
        if not isinstance(weights, (list, tuple)) or len(weights) != len(categories):
            return None, f"categorical_weights_shape:{name}"
        if any(isinstance(w, bool) or not isinstance(w, (int, float)) for w in weights):
            return None, f"categorical_weights_not_numeric:{name}"
        if any(w < 0 for w in weights):
            return None, f"categorical_weights_negative:{name}"
        try:
            cdf = tuple(_build_cdf([float(w) for w in weights]))
        except StrategyError:
            return None, f"categorical_weights_unbuildable_cdf:{name}"
    return PreparedCategorical(tuple(categories), cdf), None


def prepare_positional_categorical(
    name: str, *, namespace: str | None, provider_config: Mapping[str, Any]
) -> tuple[PreparedCategorical | None, str | None]:
    """Stage A of chunked admission for the SEEDED non-deterministic variant, from config
    alone: namespace, explicit all-string categories (no `from_profile`) and a CDF the
    oracle's `_build_cdf` can build. The compat veto, the static route decision, the
    evidence planner and `prepare_chunked_categoricals` all read this one verdict, so a
    config that fails it is refused at the veto and never reaches the oracle route.

    The validation is `prepare_categorical`'s own (called as the deterministic variant,
    which only affects its determinism gate) plus the two things its callers never needed:
    `from_profile`, and weights whose finiteness the oracle's CDF arithmetic does not
    check (a NaN, an infinity or an overflowing sum would escape as a bare ValueError)."""
    if provider_config.get("from_profile"):
        return None, f"categorical_from_profile_not_chunk_safe:{name}"
    weights = provider_config.get("weights")
    if isinstance(weights, (list, tuple)):
        for w in weights:
            if isinstance(w, bool) or not isinstance(w, (int, float)):
                continue
            try:
                finite = math.isfinite(w)
            except OverflowError:
                finite = False
            if not finite:
                return None, f"categorical_weights_not_finite:{name}"
    try:
        artifact, reason = prepare_categorical(
            name, deterministic=True, namespace=namespace, provider_config=provider_config
        )
    except (ValueError, OverflowError):
        return None, f"categorical_weights_unbuildable_cdf:{name}"
    if artifact is None:
        return None, reason
    return PreparedCategorical(artifact.categories, artifact.cdf, positional=True), None


def source_is_string(schema: pa.Schema, name: str) -> bool:
    """Stage B: the compiled index kernel's admitted categorical source is exactly `string`."""
    return bool(name in schema.names and schema.field(name).type == pa.string())


def prepare_chunked_categoricals(
    col_seed_by_name: Mapping[str, Any], first_schema: pa.Schema
) -> dict[str, PreparedCategorical]:
    """The prepared artifact for every categorical column that is native-admissible:
    config-admissible (stage A) AND a `string` first-chunk source (stage B). This is the
    one predicate the string output-type pin and the native chunk call both consume, so a
    column is pinned exactly when the native operator could run it, on either chunked leg.
    A non-deterministic column is admitted only as the positional variant."""
    prepared: dict[str, PreparedCategorical] = {}
    for name, seed in col_seed_by_name.items():
        if seed.strategy != "categorical" or not source_is_string(first_schema, name):
            continue
        cfg = provider_config_to_dict(seed.provider_config)
        if seed.deterministic:
            artifact, _reason = prepare_categorical(
                name, deterministic=True, namespace=seed.namespace, provider_config=cfg
            )
        else:
            artifact, _reason = prepare_positional_categorical(
                name, namespace=seed.namespace, provider_config=cfg
            )
        if artifact is not None:
            prepared[name] = artifact
    return prepared
