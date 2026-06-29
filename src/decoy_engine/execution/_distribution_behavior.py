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
    # text_mask (SP-07): per-span dispatch means behavior varies by config
    # (FPE preserves cardinality; redact collapses; faker destroys frequency).
    # Cannot be resolved statically without inspecting per_detector_strategy.
    "text_mask": "mixed",
    "date_shift": "varies_shape",
    "formula": "mixed",
    "nested": "inherits",
    # joint_mask (SP-08): replaces multiple columns with a reference-table row.
    # The output is drawn from the reference set -- cardinality is bounded by
    # the table size. Coarsens: many source key values map to the same row.
    "joint_mask": "coarsens",
    # geo_generalize (SP-08): ZIP cascade with k-threshold. Many-to-one
    # mapping (zip5 -> zip3 -> state). Coarsens geographic granularity.
    "geo_generalize": "coarsens",
    # code_set (SP-09b): HMAC-keyed or seeded selection from a bounded
    # code corpus. Many source values map to the same output code (N
    # source codes map to N-1 candidates from the corpus). Coarsens:
    # the output distribution is bounded by the corpus size.
    "code_set": "coarsens",
    # derived (SP-10): closed-grammar expression over same-row columns.
    # Output distribution depends entirely on the expression: it could
    # preserve, destroy, coarsen, or shift the source distribution.
    # Cannot be resolved statically without inspecting the expression.
    "derived": "mixed",
    # derived_aggregate (SP-10b): intra-table scalar aggregate fill.
    # Collapses the source column's distribution to a single scalar (sum,
    # mean, min, max, or count) and writes it to every row of the target.
    # This is many-to-one: all source values map to the same output, which
    # coarsens the target column's distribution (same class as bucketize and
    # geo_generalize, which are also many-to-one mappings).
    "derived_aggregate": "coarsens",
    # bucket_perturb (SP-08b): coarse time-bucket datetime generalization.
    # Snaps each date to a random position within its ISO week/month/quarter.
    # The distribution is coarsened: exact dates are lost; the bucket-level
    # distribution (month, quarter, year counts) is fully preserved. Same
    # class as bucketize (numeric coarsening) and geo_generalize (geographic
    # coarsening).
    "bucket_perturb": "coarsens",
    # grouped_series (SP-10c): per-group series (cumcount or monotone_walk).
    # The output is a position or accumulated walk within each group; the
    # source column's value distribution is not preserved. Destroys frequency:
    # same class as faker (the output is generated from structure, not source
    # distribution).
    "grouped_series": "destroys_frequency",
    # windowed_date (SP-10c): date within a bounded window relative to an anchor
    # column. The distribution is shifted and coarsened (the anchor date
    # determines the output range, but the exact date is randomly sampled
    # within the window). Varies_shape: similar to date_shift (per-row offset
    # within a bound).
    "windowed_date": "varies_shape",
    # group_key (SP-10c): HKDF-SHA256 + HMAC-SHA256 keyed per-group identifier.
    # All rows sharing a group_by value collapse to the same key; all distinct
    # groups get distinct keys (collision probability negligible under 32-byte
    # HMAC output). Preserves_cardinality_only: distinct count of the group_by
    # column is preserved in the output; individual values are replaced.
    "group_key": "preserves_cardinality_only",
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
        # QA-3 F15 (2026-05-31): accept either Python True or the
        # integer 1 (PyYAML normalizes `yes` / `true` to bool True,
        # but an explicit `from_profile: 1` in YAML parses as int 1
        # and would have fallen through to `destroys_frequency`).
        # Metadata-only impact (the masking strategy itself is
        # unaffected) but the Storm preservation badge would have
        # been wrong.
        from_profile = cfg.get("from_profile")
        if from_profile is True or from_profile == 1:
            return "preserves_all"
        weights = cfg.get("weights")
        if isinstance(weights, (list, tuple)) and len(weights) > 0:
            return "preserves_all"
        return "destroys_frequency"
    return _STATIC_BEHAVIOR.get(strategy)
