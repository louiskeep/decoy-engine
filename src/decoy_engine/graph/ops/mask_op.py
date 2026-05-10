"""mask — apply per-column masking strategies.

Config:
    columns:
      <column_name>:
        strategy: 'faker' | 'hash' | 'redact' | 'map' | 'shuffle' | 'passthrough'
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
# on pandas by design per the polars-duckdb hybrid plan. The runner converts
# Arrow → pandas at this op's boundary.
NATIVE_ENGINE = "pandas"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"

# Mirror MaskerConfigValidator.SUPPORTED_MASKING_STRATEGIES — the graph-mode
# allowlist has to track the legacy validator's whenever transforms ship.
# Sprint A added truncate/bucketize/reference; Sprint B added fpe.
_VALID_STRATEGIES = {
    "faker", "hash", "redact", "map", "shuffle",
    "passthrough", "date_shift", "formula",
    "reference", "truncate", "bucketize", "fpe",
}


def validate_config(config: dict[str, Any]) -> None:
    # Empty / missing `columns` is valid: it means every input column
    # passthroughs unchanged. The UI treats passthrough as the per-column
    # default and the serializer only emits non-passthrough picks, so a
    # mask node with no user-set strategies legitimately serializes to an
    # empty `columns: {}` mapping. Reject only structural issues.
    columns = config.get("columns") or {}
    if not isinstance(columns, dict):
        raise ValidationError(
            "'columns' must be a mapping", "config.columns"
        )
    for col_name, spec in columns.items():
        if not isinstance(spec, dict):
            raise ValidationError(
                f"column {col_name!r} spec must be a mapping",
                f"config.columns.{col_name}",
            )
        strategy = spec.get("strategy")
        if strategy not in _VALID_STRATEGIES:
            raise ValidationError(
                f"unsupported strategy {strategy!r} (one of {sorted(_VALID_STRATEGIES)})",
                f"config.columns.{col_name}.strategy",
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

    try:
        from decoy_engine.transforms.registry import StrategyManager

        manager = StrategyManager(seed=seed, logger=logger, derive_key=derive_key)
        return manager.apply_masking_rules(df, rules)
    except Exception as exc:
        raise OpError(f"mask op failed: {exc}") from exc


def _columns_to_rules(columns: dict[str, dict]) -> list[dict]:
    """Convert graph column-mapping form to legacy rules list.

    Graph form:    {name: {strategy: ..., ...}}
    Rules form:    [{column: name, type: ..., ...}, ...]
    """
    rules = []
    for col_name, spec in columns.items():
        rule = dict(spec)
        rule["column"] = col_name
        rule["type"] = rule.pop("strategy")
        rules.append(rule)
    return rules
