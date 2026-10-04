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
    variant. An unseeded categorical draws a whole-column vector that is not
    reproducible, so it declines here rather than silently running the
    always-deterministic native operator. Non-string categories decline (the oracle's
    data-dependent output type for them is not this operator's to reproduce). A
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


def prepare_chunked_categoricals(
    col_seed_by_name: Mapping[str, Any], first_schema: pa.Schema
) -> dict[str, PreparedCategorical]:
    """The prepared artifact for every categorical column that is native-admissible:
    config-admissible AND a `string` first-chunk source. This is the one predicate the
    string output-type pin and the native chunk call both consume, so a column is
    pinned exactly when the native operator could run it, on either chunked leg."""
    prepared: dict[str, PreparedCategorical] = {}
    for name, seed in col_seed_by_name.items():
        if seed.strategy != "categorical" or name not in first_schema.names:
            continue
        if first_schema.field(name).type != pa.string():
            continue
        artifact, _reason = prepare_categorical(
            name,
            deterministic=bool(seed.deterministic),
            namespace=seed.namespace,
            provider_config=provider_config_to_dict(seed.provider_config),
        )
        if artifact is not None:
            prepared[name] = artifact
    return prepared
