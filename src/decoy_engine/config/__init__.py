"""Strict Pydantic adapter for the V2 pipeline-config dict.

PipelineConfig is the choke-point: every caller (the `decoy plan` CLI,
the platform job runner, future SDK consumers) validates the parsed
YAML through `PipelineConfig.model_validate(...).model_dump()` before
handing the dict to `compile_plan` or `profile_source`. The engine
functions stay `config: dict`-typed per S1 spec; the adapter is what
guarantees the dict is well-formed.

Per the PO-ratified six axes (advisory 2026-05-27):
- Strict validation (extra="forbid" at every level).
- Closed-Literal pins on `orphan_policy` (preserve | remap | warn | fail)
  and source/target `format` (csv | parquet).
- Single pipeline per file (no `pipelines: [...]` top-level).
- File-only `SourceDescriptor` / `TargetDescriptor` discriminated unions
  in V1; S3 / GCS / SFTP variants land in V2+.
- Inline declarations only (no separate `pools:` or top-level
  `namespaces:` registry blocks; planner builds those from column-level
  declarations).
- No V1 YAML compatibility (`nodes` / `edges` / graph-mode rejected by
  extra="forbid").

Source patterns: shape draws from dbt's manifest.json (strict schema
validation at the package boundary) and the Pydantic 2 discriminated-
union pattern (https://docs.pydantic.dev/latest/concepts/unions/#discriminated-unions).

`override_sources(config, sources=...)` is the job-time API the platform
runner uses to swap the source binding without rewriting the rest of
the pipeline (per advisory axis 2: source is a job-time parameter; the
pipeline ships a default binding for CLI / dev workflows, the runner
ships the real binding).
"""

from __future__ import annotations

from decoy_engine.config._errors import PipelineConfigError
from decoy_engine.config._global_settings import GlobalSettings
from decoy_engine.config._override import override_sources
from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.config._relationships import (
    OrphanPolicyLiteral,
    RelationshipConfig,
    RelationshipEnd,
)
from decoy_engine.config._sources import FileSource, SourceDescriptor
from decoy_engine.config._tables import ColumnConfig, TableConfig
from decoy_engine.config._targets import FileTarget, TargetDescriptor

__all__ = [
    "ColumnConfig",
    "FileSource",
    "FileTarget",
    "GlobalSettings",
    "OrphanPolicyLiteral",
    "PipelineConfig",
    "PipelineConfigError",
    "RelationshipConfig",
    "RelationshipEnd",
    "SourceDescriptor",
    "TableConfig",
    "TargetDescriptor",
    "override_sources",
]
