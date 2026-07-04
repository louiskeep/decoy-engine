"""SubsetConfig: the `subset:` block on `PipelineConfig` (Sprint G, SS5).

Mirrors `_relationships.py`'s `extra="forbid"` discipline. This is the
INPUT-side (config-layer) shape; `decoy_engine.subset._api.subset_inputs_from_config`
converts a validated dump of it into the frozen `SeedSpec` / `FanOutPolicy`
types `run_subset` consumes.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PredicateOpLiteral = Literal["eq", "ne", "lt", "le", "gt", "ge", "in", "is_null", "is_not_null"]
SeedModeLiteral = Literal["sample", "filter", "keys"]
EdgeDirectionLiteral = Literal["both", "downward", "upward", "none"]


class SubsetPredicateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str
    op: PredicateOpLiteral
    value: Any | None = None


class SubsetSeedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: str
    mode: SeedModeLiteral
    key_columns: list[str] = Field(default_factory=list)
    fraction: float | None = Field(default=None, gt=0, le=1)
    count: int | None = Field(default=None, ge=1)
    predicates: list[SubsetPredicateConfig] = Field(default_factory=list)
    keys: list[list[Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _mode_consistency(self) -> SubsetSeedConfig:
        # Mirrors `SeedSpec.__post_init__` (decoy_engine.subset._types) exactly:
        # the config-layer and frozen-dataclass-layer rules must never drift.
        if self.mode == "sample":
            if not self.key_columns:
                raise ValueError(
                    f"subset seed for table {self.table!r}: mode='sample' requires "
                    "non-empty key_columns"
                )
            has_fraction = self.fraction is not None
            has_count = self.count is not None
            if has_fraction == has_count:
                raise ValueError(
                    f"subset seed for table {self.table!r}: mode='sample' requires exactly "
                    "one of fraction/count"
                )
        elif self.mode == "filter":
            if not self.predicates:
                raise ValueError(
                    f"subset seed for table {self.table!r}: mode='filter' requires "
                    "non-empty predicates"
                )
        elif self.mode == "keys":
            if not self.key_columns or not self.keys:
                raise ValueError(
                    f"subset seed for table {self.table!r}: mode='keys' requires non-empty "
                    "key_columns and keys"
                )
            width = len(self.key_columns)
            for tup in self.keys:
                if len(tup) != width:
                    raise ValueError(
                        f"subset seed for table {self.table!r}: every key tuple must have "
                        f"length {width} (== len(key_columns))"
                    )
        return self


class SubsetBudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_total_rows: int | None = Field(default=None, ge=1)
    max_table_seed_multiple: float | None = Field(default=None, gt=0)


class SubsetConfig(BaseModel):
    """The `subset:` block. Requires at least one seed (an empty subset job is not meaningful)."""

    model_config = ConfigDict(extra="forbid")

    seeds: list[SubsetSeedConfig] = Field(min_length=1)
    budget: SubsetBudgetConfig = Field(default_factory=SubsetBudgetConfig)
    edge_directions: dict[str, EdgeDirectionLiteral] = Field(default_factory=dict)
    allow_dangling: bool = False
