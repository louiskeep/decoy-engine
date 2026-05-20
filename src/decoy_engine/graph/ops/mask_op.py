"""mask — apply per-column masking strategies.

Config:
    columns:
      <column_name>:
        strategy: 'faker' | 'hash' | 'redact' | 'categorical' | 'shuffle' | 'passthrough'
                | 'date_shift' | 'formula' | 'reference' | 'truncate' | 'bucketize' | 'fpe'
        # ...strategy-specific keys, mirroring the existing per-column shape
    seed: int  (optional, default 42)

The op reuses the existing transforms registry — no logic duplication.
"""

from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "mask"
# Mask transforms are per-row Python (Faker, scipy, custom callbacks) — kept
# on pandas by design. The runner converts Arrow → pandas at this op's boundary.
NATIVE_ENGINE = "pandas"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"

# Mirror MaskerConfigValidator.SUPPORTED_MASKING_STRATEGIES -- the graph-mode
# allowlist must track the legacy validator's list when new transforms ship.
_VALID_STRATEGIES = {
    "faker", "hash", "redact", "categorical", "shuffle",
    "passthrough", "date_shift", "formula",
    "reference", "truncate", "bucketize", "fpe",
}


def validate_config(config: dict[str, Any]) -> None:
    from decoy_engine.validation_result import CODES

    # Empty / missing `columns` is valid: it means every input column
    # passthroughs unchanged. The UI treats passthrough as the per-column
    # default and the serializer only emits non-passthrough picks, so a
    # mask node with no user-set strategies legitimately serializes to an
    # empty `columns: {}` mapping. Reject only structural issues.
    columns = config.get("columns") or {}
    if not isinstance(columns, dict):
        raise ValidationError(
            "'columns' must be a mapping", "config.columns",
            code=CODES.MASK_BAD_COLUMNS_TYPE,
        )
    for col_name, spec in columns.items():
        if not isinstance(spec, dict):
            raise ValidationError(
                f"column {col_name!r} spec must be a mapping",
                f"config.columns.{col_name}",
                code=CODES.MASK_BAD_COLUMN_SPEC_TYPE,
            )
        strategy = spec.get("strategy")
        if strategy not in _VALID_STRATEGIES:
            raise ValidationError(
                f"unsupported strategy {strategy!r} (one of {sorted(_VALID_STRATEGIES)})",
                f"config.columns.{col_name}.strategy",
                code=CODES.MASK_UNKNOWN_STRATEGY,
            )
        # Strategy-specific required-field gates: move the check to
        # validate-time so the UI sees the failure at the right column key
        # rather than as a runtime exception.
        if strategy == "formula" and not (spec.get("formula") or "").strip():
            raise ValidationError(
                f"column {col_name!r} uses strategy 'formula' but no "
                f"'formula' expression is set",
                f"config.columns.{col_name}.formula",
                code=CODES.MASK_FORMULA_MISSING,
            )
        if strategy == "reference" and not (spec.get("reference") or "").strip():
            raise ValidationError(
                f"column {col_name!r} uses strategy 'reference' but no "
                f"'reference' dataset path is set",
                f"config.columns.{col_name}.reference",
                code=CODES.MASK_REFERENCE_MISSING,
            )
        if strategy == "categorical":
            categories = spec.get("categories")
            if not isinstance(categories, list) or not categories:
                raise ValidationError(
                    f"column {col_name!r} uses strategy 'categorical' but no "
                    f"non-empty 'categories' list is set",
                    f"config.columns.{col_name}.categories",
                    code=CODES.MASK_BAD_COLUMN_SPEC_TYPE,
                )
            weights = spec.get("weights")
            if weights is not None:
                if not isinstance(weights, list) or len(weights) != len(categories):
                    raise ValidationError(
                        f"column {col_name!r} categorical weights must match categories",
                        f"config.columns.{col_name}.weights",
                        code=CODES.MASK_BAD_COLUMN_SPEC_TYPE,
                    )
                try:
                    numeric_weights = [float(w) for w in weights if not isinstance(w, bool)]
                except (TypeError, ValueError):
                    raise ValidationError(
                        f"column {col_name!r} categorical weights must be numeric",
                        f"config.columns.{col_name}.weights",
                        code=CODES.MASK_BAD_COLUMN_SPEC_TYPE,
                    )
                if len(numeric_weights) != len(weights) or any(w < 0 for w in numeric_weights) or sum(numeric_weights) <= 0:
                    raise ValidationError(
                        f"column {col_name!r} categorical weights must be non-negative with at least one positive value",
                        f"config.columns.{col_name}.weights",
                        code=CODES.MASK_BAD_COLUMN_SPEC_TYPE,
                    )
            null_probability = spec.get("null_probability")
            if null_probability is not None:
                if isinstance(null_probability, bool):
                    raise ValidationError(
                        f"column {col_name!r} categorical null_probability must be a number between 0 and 1",
                        f"config.columns.{col_name}.null_probability",
                        code=CODES.MASK_BAD_COLUMN_SPEC_TYPE,
                    )
                try:
                    p = float(null_probability)
                except (TypeError, ValueError):
                    raise ValidationError(
                        f"column {col_name!r} categorical null_probability must be a number between 0 and 1",
                        f"config.columns.{col_name}.null_probability",
                        code=CODES.MASK_BAD_COLUMN_SPEC_TYPE,
                    )
                if p < 0 or p > 1:
                    raise ValidationError(
                        f"column {col_name!r} categorical null_probability must be between 0 and 1",
                        f"config.columns.{col_name}.null_probability",
                        code=CODES.MASK_BAD_COLUMN_SPEC_TYPE,
                    )


def apply(inputs, config, ctx) -> pd.DataFrame:
    df = inputs[0].copy()
    seed = int(config.get("seed", 42))
    # Tolerate missing/empty columns — see validate_config. Empty rules list
    # makes `apply_masking_rules` a no-op, so the df flows through unchanged.
    columns = config.get("columns") or {}

    rules = _columns_to_rules(columns)
    logger = ctx.logger if ctx is not None else None
    derive_key = getattr(ctx, "derive_key", None) if ctx is not None else None

    # Instance-wide default Faker locale (platform-supplied via
    # AppSettings.default_faker_locale on ctx). Fill it onto any faker
    # rule that doesn't set its own locale. Per-column locale still
    # overrides — this only affects rules that didn't pick one.
    instance_locale = (
        getattr(ctx, "instance_default_locale", None) if ctx is not None else None
    )
    if instance_locale:
        for rule in rules:
            if rule.get("type") == "faker" and not rule.get("locale"):
                rule["locale"] = instance_locale

    try:
        from decoy_engine.transforms.registry import StrategyManager

        manager = StrategyManager(seed=seed, logger=logger, derive_key=derive_key)
        result = manager.apply_masking_rules(df, rules)
    except Exception as exc:
        raise OpError(f"mask op failed: {exc}") from exc

    if ctx is not None and hasattr(ctx, "export"):
        ctx.export("rows_processed", int(len(result)))
        # Strategies that touched the data — distinct values, sorted for
        # stable downstream comparisons.
        strategies_applied = sorted({
            (spec.get("strategy") or "")
            for spec in columns.values()
            if spec.get("strategy") and spec.get("strategy") != "passthrough"
        })
        ctx.export("strategies_applied", strategies_applied)
        # Total null cells across the columns the user actually masked.
        # Passes through unchanged when the source value was None — useful
        # signal for "PII not masked because it was null" audit trails.
        masked_col_names = [c for c in columns if c in result.columns]
        if masked_col_names:
            null_count = int(result[masked_col_names].isna().sum().sum())
        else:
            null_count = 0
        ctx.export("null_passthrough_count", null_count)

    return result


def _columns_to_rules(columns: dict[str, dict]) -> list[dict]:
    """Convert graph column-mapping form to legacy rules list.

    Graph form:    {name: {strategy: ..., ..., _why?: str}}
    Rules form:    [{column: name, type: ..., ...}, ...]

    Underscored keys (e.g. `_why`, the FORECAST chooser's per-column
    rationale string preserved by build_from_forecast) are stripped
    before the rule reaches the legacy strategies registry. They live
    on the YAML for evidence / report rendering, not for the masker.
    """
    rules = []
    for col_name, spec in columns.items():
        rule = {k: v for k, v in spec.items() if not k.startswith("_")}
        rule["column"] = col_name
        rule["type"] = rule.pop("strategy")
        rules.append(rule)
    return rules
