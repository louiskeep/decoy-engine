# decoy_engine/strategies/categorical.py
"""Categorical masking strategy.

Replaces non-null source values with values from a configured category list.
Determinism is derived from the source value, column name, policy, and
seed/key material. No source-to-output mapping file is created or read.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pandas as pd

from decoy_engine.internal.helpers import deterministic_hash, hmac_hex
from decoy_engine.transforms.base import BaseMaskingStrategy


class CategoricalStrategy(BaseMaskingStrategy):
    """Mask values by selecting from a configured categorical vocabulary."""

    def apply(self, column: pd.Series, rule: Dict[str, Any]) -> pd.Series:
        self.validate_rule(rule)

        column_name = rule.get("column", "unnamed")
        seed = int(rule.get("seed", self.seed))
        categories = list(rule["categories"])
        weights = self._normalize_weights(rule.get("weights"), categories)
        null_probability = self._normalize_null_probability(
            rule.get("null_probability", 0.0)
        )
        policy = json.dumps(
            {
                "categories": categories,
                "weights": weights,
                "null_probability": null_probability,
            },
            sort_keys=True,
            default=str,
        )
        key = self._mask_key()

        def digest(label: str) -> str:
            message = f"{column_name}:{policy}:{label}"
            if key is not None:
                return hmac_hex(key, message)
            return deterministic_hash(message, seed)

        def mask_value(value):
            if value is None:
                return value
            try:
                if pd.isna(value):
                    return value
            except (TypeError, ValueError):
                pass
            value_text = str(value)
            if null_probability > 0:
                null_roll = self._fraction(digest(f"null:{value_text}"))
                if null_roll < null_probability:
                    return None
            category_roll = self._fraction(digest(f"category:{value_text}"))
            return self._pick_category(category_roll, categories, weights)

        result = column.apply(mask_value)
        self._log_stats(column, result, rule)
        return result

    def validate_rule(self, rule: Dict[str, Any]) -> None:
        super().validate_rule(rule)
        categories = rule.get("categories")
        if not isinstance(categories, list) or not categories:
            raise ValueError("categorical strategy requires non-empty 'categories'")
        self._normalize_weights(rule.get("weights"), categories)
        self._normalize_null_probability(rule.get("null_probability", 0.0))

    def _normalize_weights(
        self,
        raw_weights: Any,
        categories: list[Any],
    ) -> list[float]:
        if raw_weights is None:
            return [1.0 for _ in categories]
        if not isinstance(raw_weights, list) or len(raw_weights) != len(categories):
            raise ValueError(
                "categorical 'weights' must be a list with the same length as 'categories'"
            )
        weights: list[float] = []
        for raw in raw_weights:
            if isinstance(raw, bool):
                raise ValueError("categorical 'weights' must be numeric")
            try:
                weight = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("categorical 'weights' must be numeric") from exc
            if weight < 0:
                raise ValueError("categorical 'weights' must be non-negative")
            weights.append(weight)
        if sum(weights) <= 0:
            raise ValueError("categorical 'weights' must contain at least one positive value")
        return weights

    def _normalize_null_probability(self, raw_probability: Any) -> float:
        if isinstance(raw_probability, bool):
            raise ValueError("categorical 'null_probability' must be numeric")
        try:
            probability = float(raw_probability)
        except (TypeError, ValueError) as exc:
            raise ValueError("categorical 'null_probability' must be numeric") from exc
        if probability < 0 or probability > 1:
            raise ValueError("categorical 'null_probability' must be between 0 and 1")
        return probability

    def _pick_category(
        self,
        roll: float,
        categories: list[Any],
        weights: list[float],
    ) -> Any:
        total = sum(weights)
        threshold = roll * total
        cumulative = 0.0
        for category, weight in zip(categories, weights):
            cumulative += weight
            if threshold < cumulative:
                return category
        return categories[-1]

    def _fraction(self, digest: str) -> float:
        return int(digest[:16], 16) / float(16**16)

    def _mask_key(self) -> bytes | None:
        if self.derive_key is None:
            return None
        try:
            return self.derive_key("mask")
        except Exception as exc:
            self.logger.warning(
                f"derive_key failed for categorical mask ({exc}); falling back to seed"
            )
            return None
