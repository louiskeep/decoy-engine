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

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class GenerateColumnConfig(BaseModel):
    """A synthesis (generation) column declaration (S6).

    Mirrors V1's generate column shape (decoy_engine.generators.columns): ``type``
    selects the generator (``faker`` / ``sequence`` / ``categorical`` / ``formula`` --
    the V1 ``ColumnGenerator.generators`` keys), and the per-type params
    (``faker_type``, ``start``, ``step``, ``values``, ``expr``, ...) ride FLAT on
    the column, exactly as V1 reads them (``column_config.get("faker_type")``).
    ``extra="allow"`` carries those flat params so the front-end emit and the V1
    parity oracle line up (Reading B). S6-ENG-1 ships the spine + ``sequence``;
    S6-ENG-2 adds the remaining parity-frozen generators.

    NOTE (Dennis S6-ENG-1 gate, Q-S6-1): ``extra="allow"`` here is the deliberate
    mirror-V1-flat choice over a stricter nested ``config: dict`` under
    ``extra="forbid"`` -- flagged for the gate.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    type: str


class TableConfig(BaseModel):
    """Per-table declaration.

    A table is EITHER a MASK table (``columns`` of mask ColumnConfig, fed by a
    source) OR a GENERATE table (``generate_columns`` + ``row_count``, no source),
    enforced by the validator below. ``columns`` was ``min_length=1``; it is now
    validated CONDITIONALLY so a generate table can omit it. The mask path is
    unchanged when no generation fields are set.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    columns: list[ColumnConfig] = Field(default_factory=list)
    # Generation (S6): row_count is V1's per-table `rows`; generate_columns are the
    # synthesis column specs. Both unset => a mask table.
    row_count: int | None = None
    generate_columns: list[GenerateColumnConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _mask_xor_generate(self) -> "TableConfig":
        if self.generate_columns:
            if self.columns:
                raise ValueError(
                    f"table {self.name!r}: a generate table (generate_columns) must "
                    f"not also declare mask columns"
                )
            if self.row_count is None or self.row_count < 0:
                raise ValueError(
                    f"table {self.name!r}: a generate table requires a non-negative "
                    f"row_count"
                )
        else:
            if not self.columns:
                raise ValueError(
                    f"table {self.name!r}: a mask table requires at least one column "
                    f"(or use generate_columns + row_count to generate)"
                )
            if self.row_count is not None:
                raise ValueError(
                    f"table {self.name!r}: row_count is only valid on a generate table "
                    f"(with generate_columns)"
                )
        return self
