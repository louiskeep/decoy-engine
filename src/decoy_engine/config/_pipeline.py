"""PipelineConfig: the top-level model.

Per advisory axis-by-axis ratification:
- `version: Literal[1]` (axis 6 + 3: schema version, single pipeline per file)
- `global_settings: GlobalSettings` required (axis 6: V1 naming convention kept)
- `sources: dict[str, SourceDescriptor]` required (axis 1=A: inline declarations)
- `tables: list[TableConfig]` required, non-empty
- `relationships: list[RelationshipConfig]` (empty list OK for single-table pipelines)
- `targets: dict[str, TargetDescriptor]` required (axis 6: explicit targets analogous to sources)
- `namespaces: dict[str, NamespaceConfig]` optional (the engine reads a top-level
  `namespaces` block via `config.get("namespaces", {})`; empty default is fine)

`extra="forbid"` at every model rejects unknown keys + V1 graph-mode
keys (`nodes`, `edges`, `mode: graph`) per axis 6 (no V1 compat).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from decoy_engine.config._global_settings import GlobalSettings
from decoy_engine.config._namespaces import NamespaceConfig
from decoy_engine.config._relationships import RelationshipConfig
from decoy_engine.config._sources import SourceDescriptor
from decoy_engine.config._tables import TableConfig
from decoy_engine.config._targets import TargetDescriptor


class PipelineConfig(BaseModel):
    """Strict, validated pipeline configuration.

    Callers do:

        cfg_dict = PipelineConfig.model_validate(parsed_yaml).model_dump()

    and hand `cfg_dict` to `profile_source` and `compile_plan`. The
    engine functions do not re-validate. Validation is a one-time event
    at the choke-point.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    # S6 (generation): mask vs generate. Mask configs OMIT it (default "mask"), so
    # existing stored configs + the run dispatch (`cfg.get("mode","mask")`) are
    # unchanged; a generate submission sets `mode: generate`.
    mode: Literal["mask", "generate"] = "mask"
    global_settings: GlobalSettings
    # Relaxed from min_length=1: a pure-generate config has NO sources. The
    # validator below keeps MASK mode requiring at least one source, so the mask
    # contract is unchanged.
    sources: dict[str, SourceDescriptor] = Field(default_factory=dict)
    tables: list[TableConfig] = Field(min_length=1)
    relationships: list[RelationshipConfig] = Field(default_factory=list)
    targets: dict[str, TargetDescriptor] = Field(min_length=1)
    namespaces: dict[str, NamespaceConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _mode_consistency(self) -> "PipelineConfig":
        """Keep `mode` consistent with the tables + sources.

        Mask mode requires a source and mask tables; generate mode requires
        generate tables (no mask columns). This preserves the mask contract
        (sources required) while admitting a no-source generate config.
        """
        if self.mode == "mask":
            if not self.sources:
                raise ValueError("mask mode requires at least one source")
            if any(t.generate_columns for t in self.tables):
                raise ValueError(
                    "mask mode tables must not declare generate_columns "
                    "(use mode: generate)"
                )
        else:  # generate
            if any(t.columns for t in self.tables):
                raise ValueError(
                    "generate mode tables must use generate_columns, not mask columns"
                )
        return self
