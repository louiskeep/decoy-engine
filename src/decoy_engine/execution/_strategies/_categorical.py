"""categorical strategy (engine-v2 S9): remap values onto a category pool.

Re-keyed onto S3/S5 (S9 spec §4 row 8): the replacement set is a pool of
`provider_config["categories"]`; deterministic mode maps each source value to a
category via `derive_index(job_seed, namespace, _canonicalize_source(value),
pool_size=len(categories))` (same source -> same category within a namespace).
Non-deterministic mode is reproducible but NOT source-keyed: the draw for the non-null
row at ordinal `g = ctx.row_offset + local_index` is
`derive_index(mask_key, namespace, encode_int(g), pool_size=...)`, so the same job seed
gives the same output and the output does not depend on the source value (no join
preservation). `g` is the ordinal within the frame this handler receives: the physical row
for a plain whole-frame table, the match ordinal under `when:`, the synthetic-frame
ordinal under FK orphan remapping. The key is the public `decoy_engine.kernel.encode_int`,
the same encoding the native batch kernel applies to an integer column, so a later batch
path reproduces these bytes. Null positions preserved (a null still consumes its ordinal).

MG-1 S5 extension (2026-06-01): `weights` and `from_profile`.
- ``cfg["weights"]``: list of floats matching ``categories`` (must be
  same length, non-negative, at least one > 0). Normalized + routed
  through a CDF so picks follow the configured distribution. When
  unset, the uniform path (V1 byte identity) is preserved.
- ``cfg["from_profile"]``: True signals that the plan compiler should
  pull (labels, data) from the column's ``FieldStats.distribution``
  and emit them as ``categories + weights`` on the seed. By the time
  the runtime sees the plan, ``from_profile`` is informational and
  the actual ``categories`` + ``weights`` are already set; the
  plan-compile change lives in ``decoy_engine.plan._compile``.

Sprint 13 / coercion-13 S3 (2026-07-03, GATE-1 Q4 sibling of the truncate
fail-closed fix): ``categories = list(cfg.get("categories", []))`` used to
silently iterate the CHARACTERS of a plain-string ``categories`` value
(e.g. a Studio free-text field submitted with no coercion), corrupting
the output to single resampled characters instead of masking with the
intended category set. `run` now raises `StrategyError` when
``categories`` is present and not a list/tuple (unless ``from_profile``
is set, per D5). `check_categorical_categories`
(plan/_checks_categorical.py) rejects the same shape at compile time;
this is the defense-in-depth backstop.

The deterministic + weighted path uses a CDF over a fixed integer
resolution so ``derive_index(..., pool_size=_WEIGHTED_CDF_RES)`` picks
a uniform integer that maps through the CDF to a weighted category.
Same source value + same namespace + same weights => same category.
"""

from __future__ import annotations

import bisect
import math
import sys
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

from decoy_engine import reasons
from decoy_engine.determinism import derive_index
from decoy_engine.execution._adapter import (
    StrategyContext,
    StrategyOption,
    option,
    provider_config_to_dict,
)
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._positional_keys import row_positions
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.kernel import encode_int
from decoy_engine.plan._types import ColumnSeed

# Resolution for the deterministic-weighted CDF. 1_000_000 supports
# weights down to 1e-6 with the precision the CDF rounding allows.
_WEIGHTED_CDF_RES = 1_000_000

_POSITION_DOMAIN_CODE = reasons.runtime.CATEGORICAL_POSITION_OUT_OF_DOMAIN


# Exact weight types, decided on `type(w)` alone so a subclass (bool, a float subclass with
# a lying `__float__`) is refused rather than trusted. Integer members are built from C type
# codes because named numpy integer aliases differ by platform.
_EXACT_INT_TYPES: frozenset[type] = frozenset({int} | {np.dtype(c).type for c in "bBhHiIlLqQ"})
_EXACT_FLOAT_TYPES: frozenset[type] = frozenset({float, np.float16, np.float32, np.float64})
_MAX_DOUBLE_RATIO = float.as_integer_ratio(sys.float_info.max)


def _admit_weights(weights: Sequence[Any]) -> list[Any]:
    """The weights unchanged, as a list, once every one is an exact admitted type.

    The single type admission for categorical weights: every route that builds a CDF
    (resident handler, out-of-core group B, native prepared artifact) passes the raw
    configured weights here, never a coerced copy, and `_build_cdf` calls it again."""
    for i, w in enumerate(weights):
        if type(w) not in _EXACT_INT_TYPES and type(w) not in _EXACT_FLOAT_TYPES:
            raise StrategyError(
                code=reasons.runtime.CATEGORICAL_WEIGHTS_NONPOSITIVE,
                strategy="categorical",
                message=f"categorical weight at index {i:d} is not a number; cannot normalize.",
            )
    return list(weights)


def _exact_ratios(weights: Sequence[Any]) -> list[tuple[int, int]]:
    """Each weight as an exact `(numerator, power-of-two denominator)`; refusals run as
    whole-vector passes (type, then non-finite) and report the lowest index only."""
    weights = _admit_weights(weights)
    ratios: list[tuple[int, int]] = []
    for i, w in enumerate(weights):
        if type(w) in _EXACT_INT_TYPES:
            ratios.append((int(w), 1))
            continue
        x = float(w)
        if not math.isfinite(x):
            raise StrategyError(
                code=reasons.runtime.CATEGORICAL_WEIGHTS_NONPOSITIVE,
                strategy="categorical",
                message=f"categorical weight at index {i:d} is NaN or infinite; cannot normalize.",
            )
        ratios.append(float.as_integer_ratio(x))
    return ratios


def _build_cdf(weights: Sequence[Any]) -> list[int]:
    """Cumulative integer thresholds over ``_WEIGHTED_CDF_RES``: entry i is the exclusive
    upper bound for category i, so ``bisect_right(cdf, x)`` maps a uniform ``x`` in
    ``[0, _WEIGHTED_CDF_RES)`` to its category.

    Exact arithmetic, so the result does not depend on the interpreter's float summation
    (CPython 3.12 made ``sum()`` of floats compensated). Every finite binary64 value is a
    dyadic rational (IEEE 754-2019 section 3.3; ``float.as_integer_ratio``), so scaling to
    the largest power-of-two denominator makes each weight, prefix sum ``C_i`` and total
    ``T`` an exact ``int``. Thresholds are ``(2 * C_i * RES + T) // (2 * T)`` (round half
    up): the integer-frequency normalization of range coding and ANS (Witten, Neal, Cleary
    1987, CACM 30(6); Duda 2013, arXiv:1311.2540).

    Refusals, each a whole-vector pass reporting the lowest index and no weight value:
    not a number, NaN or infinite, negative, total above the largest double or zero, and
    a positive weight whose slot rounds to zero width (QA-3 F9: such a category would
    never be selected)."""
    ratios = _exact_ratios(weights)
    for i, (num, _) in enumerate(ratios):
        if num < 0:
            raise StrategyError(
                code=reasons.runtime.CATEGORICAL_WEIGHTS_NEGATIVE,
                strategy="categorical",
                message=f"categorical weight at index {i:d} is negative.",
            )
    denom = max((d for _, d in ratios), default=1)
    scaled = [num * (denom // d) for num, d in ratios]
    total = 0
    for v in scaled:
        total += v
    max_num, max_den = _MAX_DOUBLE_RATIO
    if total * max_den > max_num * denom:
        raise StrategyError(
            code=reasons.runtime.CATEGORICAL_WEIGHTS_NONPOSITIVE,
            strategy="categorical",
            message="categorical weights sum above the largest finite double; cannot normalize.",
        )
    if total == 0:
        raise StrategyError(
            code=reasons.runtime.CATEGORICAL_WEIGHTS_NONPOSITIVE,
            strategy="categorical",
            message="categorical weights sum to 0; cannot normalize.",
        )
    cdf: list[int] = []
    running = 0
    prev_threshold = 0
    for i, v in enumerate(scaled):
        running += v
        threshold = (2 * running * _WEIGHTED_CDF_RES + total) // (2 * total)
        if v > 0 and threshold == prev_threshold:
            raise StrategyError(
                code=reasons.runtime.CATEGORICAL_WEIGHT_BELOW_RESOLUTION,
                strategy="categorical",
                message=(
                    f"categorical weight at index {i:d} is below the CDF resolution "
                    f"(1 / {_WEIGHTED_CDF_RES:d} of total). The category would never be "
                    "selected; remove the category or raise its weight."
                ),
            )
        prev_threshold = threshold
        cdf.append(threshold)
    return cdf


class CategoricalStrategyHandler:
    """Remap a column onto a fixed category pool (derive_index-keyed)."""

    name: str = "categorical"
    namespace_required: bool = True
    join_safe: bool = False
    wizard_options: tuple[StrategyOption, ...] = (
        option("categories", "str_list", ("gold", "silver", "bronze"), required=True),
        option("weights", "float_list", (0.5, 0.3, 0.2)),
        option("deterministic", "bool", True, location="column"),
    )
    offer_in_wizard: bool = True

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)
        raw_categories = cfg.get("categories")
        if (
            not cfg.get("from_profile")
            and raw_categories is not None
            and not isinstance(raw_categories, (list, tuple))
        ):
            # Invalid shape: fail closed (Sprint 13 GATE-1 Q4). A string
            # value would iterate as characters below, silently corrupting
            # the output instead of masking with the intended categories.
            raise StrategyError(
                code=reasons.runtime.CATEGORICAL_CATEGORIES_NOT_LIST,
                strategy="categorical",
                message=(
                    f"column {column!r} uses categorical with "
                    f"categories={raw_categories!r} ({type(raw_categories).__name__}), "
                    "which is not a list. A string value iterates as individual "
                    "characters at runtime; provide categories as a list."
                ),
            )
        categories = list(cfg.get("categories", []))
        if not categories:
            raise StrategyError(
                code=reasons.runtime.CATEGORICAL_REQUIRES_CATEGORIES,
                strategy="categorical",
                message=f"column {column!r} uses categorical but provided no categories.",
            )
        # MG-1 S5: optional weights. None = uniform (V1 path).
        weights_raw = cfg.get("weights")
        weights: list[Any] | None = None
        if weights_raw is not None:
            if not isinstance(weights_raw, (list, tuple)) or len(weights_raw) != len(categories):
                raise StrategyError(
                    code=reasons.runtime.CATEGORICAL_WEIGHTS_SHAPE,
                    strategy="categorical",
                    message=(
                        f"column {column!r}: weights must be a list with the same "
                        f"length as categories ({len(categories)}); got "
                        f"{type(weights_raw).__name__} "
                        f"len={len(weights_raw) if hasattr(weights_raw, '__len__') else 'n/a'}."
                    ),
                )
            weights = _admit_weights(weights_raw)

        source = df[column]
        na_mask = source.isna().to_numpy()

        if plan.namespace is None:
            mode = "deterministic" if plan.deterministic else "non-deterministic"
            raise StrategyError(
                code=reasons.runtime.CATEGORICAL_REQUIRES_NAMESPACE,
                strategy="categorical",
                message=f"column {column!r} uses {mode} categorical but has no namespace.",
            )
        cdf = _build_cdf(weights) if weights is not None else None
        # getattr: handlers are also driven with hand-built contexts that predate this field.
        # Admission accepts only the deterministic subset; a declined column still passes through
        # so the run counts the rows Python masked. No residual-warning phase exists here, so a
        # native return is the whole result.
        seq = getattr(ctx, "seq_native", None)
        if seq is not None:
            tokens = seq.categorical_column(
                ctx,
                df,
                column,
                namespace=plan.namespace,
                mask_key=ctx.mask_key,
                categories=categories,
                cdf=cdf,
            )
            if tokens is not None:
                df[column] = tokens
                return df, []
        # Deterministic keys on the canonical source value; non-deterministic keys on the
        # full-table row number, so a `when:`-selected row draws the same as in an ungated run
        # (C8-iii-d). `row_positions` is the single owner of that numbering; convert each scalar
        # to a Python int because `encode_int` needs int operations a numpy uint64 lacks.
        positions = (
            None
            if plan.deterministic
            else row_positions(
                ctx.row_offset,
                len(source),
                getattr(ctx, "gate_positions", None),
                code=_POSITION_DOMAIN_CODE,
            )
        )
        out: list[object] = []
        for i, value in enumerate(source):
            if na_mask[i]:
                out.append(None)
                continue
            key = (
                _canonicalize_source(value) if plan.deterministic else encode_int(int(positions[i]))  # type: ignore[index]
            )
            if cdf is None:
                # Uniform path -- V1 byte identity for the deterministic mode.
                out.append(
                    categories[
                        derive_index(ctx.mask_key, plan.namespace, key, pool_size=len(categories))
                    ]
                )
                continue
            bucket = derive_index(ctx.mask_key, plan.namespace, key, pool_size=_WEIGHTED_CDF_RES)
            cat_idx = bisect.bisect_right(cdf, bucket)
            # Defensive: if rounding ever pushes bucket past cdf[-1]
            # (= _WEIGHTED_CDF_RES), clamp to the last category.
            out.append(categories[min(cat_idx, len(categories) - 1)])

        df[column] = out
        return df, []
