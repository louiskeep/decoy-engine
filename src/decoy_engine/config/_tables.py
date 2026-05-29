"""TableConfig + ColumnConfig: per-table masking configuration.

S1 spec lines 154-165 verbatim:
  - name (str)
  - columns: list of ColumnConfig with name + strategy + provider +
    optional namespace + optional cardinality_mode + optional
    provider_config (free-form dict, shape per S4 provider registry) +
    optional coherent_with (composite-aware).

Strategy and provider strings are NOT closed Literals here. Reasons:
- `strategy` (e.g. "preserve_format_id", "synthetic_email",
  "replace_with_synthetic", "from_parent") is the strategy table from
  the operating model; it grows as new strategies land. Closing it now
  would require updating this file every time S4-S13 add a strategy.
- `provider` is closed-checked by the planner against `S1_STUB_REGISTRY`
  (S4 swaps the real registry behind the same check). That check fires
  with `code=unknown_provider` per S1 spec §2; the adapter does NOT
  duplicate it. Single source of truth lives at the planner.

`cardinality_mode` IS a closed Literal here because the set of valid
modes is locked by the operating model and S1 spec §2 enumerates them
exhaustively. Per the R6 reshape (S5), `deterministic_map` was removed
from the mode set; the deterministic-vs-random axis is now the separate
first-class `deterministic: bool` field, composed orthogonally with
`cardinality_mode`. The adapter rejects a `deterministic_map` mode here
(the engine also raises a migration error on the raw-dict path).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CardinalityModeLiteral = Literal[
    "reuse",
    "unique",
    "match_source_cardinality",
    "scale_source_cardinality",
]


class ColumnConfig(BaseModel):
    """Per-column masking declaration.

    `name` is the column name in the source data. `strategy` names the
    masking strategy (open string; S4+ catalogs the values). `provider`
    names the semantic provider; the planner validates it against
    S1_STUB_REGISTRY at compile time.

    `from_parent` strategy: the column is wired from a FK parent via the
    relationship coordinator (S2). When `strategy: from_parent`, the
    relationship coordinator owns the namespace + provider; ColumnConfig
    declares only the strategy + name.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    strategy: str
    provider: str | None = None
    namespace: str | None = None
    cardinality_mode: CardinalityModeLiteral | None = None
    # R6 (S5): deterministic-vs-random is a first-class field, orthogonal to
    # cardinality_mode (the 2x4 matrix in S5 spec §6). The engine reads it via
    # `col_entry.get("deterministic", False)`; defaults False when omitted.
    deterministic: bool = False
    # Read by pool/_validate.py (`float(col_entry.get("scale", 2.0))`) under
    # cardinality_mode == "scale_source_cardinality"; the planner owns the
    # scale-vs-mode interaction, so the adapter just carries the value.
    scale: float | None = None
    # provider_config is a free-form dict; S4's real provider registry
    # validates the shape per-backend. Adapter just enforces it's a dict.
    provider_config: dict[str, Any] = Field(default_factory=dict)
    coherent_with: list[str] = Field(default_factory=list)
    backend_type: Literal["faker", "mimesis", "pool", "decoy_native"] | None = None
    backend_version: str | None = None
    # Capacity hint for the planner's basic_uniqueness_pre_flight check
    # (S1 spec §2 #4). Only meaningful when cardinality_mode == "unique"
    # and backend_type == "pool"; the planner reads this directly from
    # the column dict, so the adapter must allow it. S5 ships the full
    # pool_capacity_pre_flight check.
    pool_size: int | None = None


class TableConfig(BaseModel):
    """Per-table column-list declaration."""

    model_config = ConfigDict(extra="forbid")

    name: str
    columns: list[ColumnConfig] = Field(min_length=1)
