"""PipelineConfig: the top-level model.

Per advisory axis-by-axis ratification:
- `version: Literal[1]` (axis 6 + 3: schema version, single pipeline per file)
- `global_settings: GlobalSettings` required (axis 6: V1 naming convention kept)
- `sources: dict[str, SourceDescriptor]` required (axis 1=A: inline declarations)
- `tables: list[TableConfig]` required, non-empty
- `relationships: list[RelationshipConfig]` (empty list OK for single-table pipelines)
- `targets: dict[str, TargetDescriptor]` required (axis 6: explicit targets analogous to sources)

`extra="forbid"` at every model rejects unknown keys + V1 graph-mode
keys (`nodes`, `edges`, `mode: graph`) per axis 6 (no V1 compat).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from decoy_engine.config._global_settings import GlobalSettings
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
    global_settings: GlobalSettings
    sources: dict[str, SourceDescriptor] = Field(min_length=1)
    tables: list[TableConfig] = Field(min_length=1)
    relationships: list[RelationshipConfig] = Field(default_factory=list)
    targets: dict[str, TargetDescriptor] = Field(min_length=1)
