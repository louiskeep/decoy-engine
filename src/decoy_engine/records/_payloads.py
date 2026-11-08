"""Payload models the records package owns: local evidence and sample-run.

``EvidencePayload`` mirrors the manifest ``decoy run --evidence-out`` writes (schema
``cli-local-1``); the CLI still builds the manifest, and the platform importer decodes it through
this model. Every field is typed: names, relative paths, versions, hashes, counts and enum-like
words, no free text and no cell value.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
)

from decoy_engine.records._envelope import Name, RelPath

__all__ = ["EvidencePayload", "SampleRunPayload"]

Sha = Annotated[StrictStr, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
Number = StrictInt | StrictFloat
Count = Annotated[StrictInt, Field(ge=0)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class FileFingerprint(_Model):
    path: RelPath
    fingerprint: Sha | None
    fingerprint_method: Literal["full"] | None
    size_bytes: Count | None


class OutputFingerprint(FileFingerprint):
    # Additive to cli-local-1: older manifests have neither key.
    format: Literal["csv", "parquet", "fixed_width"] | None = Field(default=None)
    layout: dict[str, Any] | None = Field(default=None)


class EvidenceWarning(_Model):
    code: Name
    provider: Name | None
    column: Name | None
    detail: dict[str, StrictStr | StrictInt | StrictFloat | StrictBool | None]


class Timing(_Model):
    strategy_type: Name
    column: Name
    elapsed_ms: Number
    peak_memory_delta_kb: Number | None


class Strategy(_Model):
    table: Name
    column: Name
    strategy: Name


class EvidencePayload(_Model):
    schema_version: Literal["cli-local-1"]
    producer: Literal["decoy-cli"]
    run_id: Name
    run_timestamp: Name
    cli_version: Name
    engine_version: Name
    pipeline_path: RelPath
    pipeline_fingerprint: Sha
    input_fingerprints: dict[Name, FileFingerprint]
    output_fingerprints: dict[Name, OutputFingerprint]
    row_counts: dict[Name, Count]
    key_label: Name | None
    warnings: tuple[Name | EvidenceWarning, ...]
    timings: tuple[Timing, ...]
    strategies: tuple[Strategy, ...]
    manifest_hash: Sha


class ColumnRef(_Model):
    table: Name
    column: Name


class SampleOutput(_Model):
    table: Name
    path: RelPath
    sha256: Sha


class SampleRunPayload(_Model):
    """Metadata of one ``--sample N`` run. Sample rows are scratch and never part of a record."""

    sample_size: Annotated[StrictInt, Field(ge=1)]
    root_table: Name
    table_rows: dict[Name, Count]
    subset_seed_counts: dict[Name, Count]
    approximate_columns: tuple[ColumnRef, ...]
    scratch_dir: RelPath
    outputs: tuple[SampleOutput, ...]
    ttl_seconds: Annotated[StrictInt, Field(ge=1)]
