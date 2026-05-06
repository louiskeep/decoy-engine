"""mask — apply per-column masking strategies.

Config:
    columns:
      <column_name>:
        strategy: 'faker' | 'hash' | 'redact' | 'map' | 'shuffle' | 'passthrough' | 'date_shift' | 'formula'
        # ...strategy-specific keys, mirroring the existing per-column shape
    seed: int  (optional, default 42)

The op reuses the existing transforms registry — no logic duplication.
"""

from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "mask"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"

_VALID_STRATEGIES = {
    "faker", "hash", "redact", "map", "shuffle",
    "passthrough", "date_shift", "formula",
}


def validate_config(config: dict[str, Any]) -> None:
    columns = config.get("columns")
    if not isinstance(columns, dict) or not columns:
        raise ValidationError(
            "'columns' must be a non-empty mapping", "config.columns"
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
    columns = config["columns"]

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
