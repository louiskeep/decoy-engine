"""MG-6 D1 (2026-05-31): distribution-behavior classification.

A second per-strategy metadata field (alongside MG-1 S1's
`technique_class`) that describes what each strategy does to the
SOURCE COLUMN's value distribution. Drives the FE drift-badge
threshold logic in MG-6 D2: low drift on a `preserves_all` column is
success; low drift on a `destroys_frequency` column is a problem
(the masking didn't actually mask).

Six values cover the V1 strategy set:

- `preserves_all`: same marginal distribution preserved. Includes
  the identity transform (passthrough), shuffle (same marginals
  but broken row identity), and categorical-with-source-weights.
- `preserves_cardinality_only`: distinct count preserved but
  individual values destroyed. Includes hash + FPE (reversible).
- `destroys_frequency`: the source distribution does not propagate.
  Includes faker (synthetic per row), composite_* (bundle synth),
  and uniform categorical (collapses to fixed weights).
- `coarsens`: many-to-one mapping that retains some signal.
  Includes truncate (drops chars) + bucketize (numeric bands).
- `collapses`: many-to-one to a constant or near-constant.
  Includes redact (whole-cell constant) + text_redact (per-span
  collapse).
- `varies_shape`: marginal shape preserved but values shifted.
  Includes date_shift (per-row offset within a bound).
- `mixed`: cannot be classified statically; depends on the
  config. Includes formula (per the operator's expression).

Two non-static cases are resolved at plan-compile time:

- `categorical`: `preserves_all` when `from_profile: true` or
  `weights` is configured; `destroys_frequency` otherwise.
- `nested` (MG-3 wrapper): inherits the CHILD strategy's behavior;
  carries `inherits` as a sentinel and the manifest layer surfaces
  the child's value.

Privacy note (PO directive 2026-05-31): D1/D2 are OBSERVATIONAL.
Active distribution-controlled masking (MG-7-style auto-tuning) is
deferred for the statistical-fingerprint leakage risk. Operators
see whether their masking preserves/destroys distribution where
intended; the engine does not auto-tune to match.

Industry-standard naming references: this maps closely to NIST
SP 800-188 "De-Identification of Personal Information" Table 1
(value-only vs distribution-preserving transforms) and the SDV
Tonic taxonomy of "synthesis modes."
"""

from __future__ import annotations

from typing import Any, Literal


DistributionBehavior = Literal[
    "preserves_all",
    "preserves_cardinality_only",
    "destroys_frequency",
    "coarsens",
    "collapses",
    "varies_shape",
    "mixed",
    "inherits",
]


# Static per-strategy assignment. The categorical row is the
# uniform-default value; the function below resolves it dynamically.
# nested is `inherits` -- the manifest carries the child's actual
# behavior after plan-compile resolves it.
_STATIC_BEHAVIOR: dict[str, DistributionBehavior] = {
    "passthrough": "preserves_all",
    "shuffle": "preserves_all",
    "hash": "preserves_cardinality_only",
    "fpe": "preserves_cardinality_only",
    "faker": "destroys_frequency",
    "categorical": "destroys_frequency",  # overridden by the dynamic resolver
    "bucketize": "coarsens",
    "truncate": "coarsens",
    "redact": "collapses",
    "text_redact": "collapses",
    "date_shift": "varies_shape",
    "formula": "mixed",
    "nested": "inherits",
}


def distribution_behavior_for(
    strategy: str | None,
    provider_config: tuple[tuple[str, Any], ...] | None = None,
) -> DistributionBehavior | None:
    """Resolve the distribution-behavior label for a strategy.

    Returns `None` when the strategy is unknown. `provider_config`
    is consulted ONLY for `categorical`, where the source-weighted
    + from_profile cases flip the static `destroys_frequency` to
    `preserves_all`.

    For `nested`, returns the sentinel `"inherits"`; the manifest
    layer is responsible for substituting the child's behavior.
    Composite providers (faker strategy with a `composite_*`
    provider) get `destroys_frequency` because the bundle is
    synthesized; the per-strategy value covers the composite case.
    """
    if not strategy:
        return None
    if strategy == "categorical":
        cfg = dict(provider_config or ())
        if cfg.get("from_profile") is True:
            return "preserves_all"
        weights = cfg.get("weights")
        if isinstance(weights, (list, tuple)) and len(weights) > 0:
            return "preserves_all"
        return "destroys_frequency"
    return _STATIC_BEHAVIOR.get(strategy)
