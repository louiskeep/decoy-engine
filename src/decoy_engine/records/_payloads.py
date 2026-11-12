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
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from decoy_engine._value_free import Count
from decoy_engine.run_result import SafeName, Sha256

__all__ = ["EvidencePayload", "ExportLayout", "SampleRunPayload"]

Number = StrictInt | StrictFloat
# The `cli-local-1` manifest predates the shared value-free types, so its names and paths
# keep their wider shape: a manifest already on disk must still read. New payloads use SafeName,
# Sha256 and the other shared types.
_PLAIN = r"^[^\x00-\x1f\x7f]+$"
Name = Annotated[StrictStr, StringConstraints(min_length=1, max_length=256, pattern=_PLAIN)]
RelPath = Annotated[StrictStr, StringConstraints(min_length=1, max_length=1024, pattern=_PLAIN)]
# Sample-run paths are machine paths, reduced to file names by export, so they stay loose too.
PathText = RelPath


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class FileFingerprint(_Model):
    path: RelPath
    fingerprint: Sha256 | None
    fingerprint_method: Literal["full"] | None
    size_bytes: Count | None


class LayoutColumn(_Model):
    name: SafeName
    start: Count
    width: Annotated[StrictInt, Field(ge=1)]
    type: Literal["str", "int", "float"] = "str"
    pad: Annotated[StrictStr, StringConstraints(min_length=1, max_length=1)] = " "
    align: Literal["left", "right"] = "left"


class ExportLayout(_Model):
    """The structural shape a fixed-width layout must have to leave in a default export: names,
    byte offsets and enum words only. ``OutputFingerprint.layout`` is a loose dict so an older
    manifest still reads; export proves a layout against this model instead."""

    columns: Annotated[tuple[LayoutColumn, ...], Field(min_length=1)]
    exact_record_width: StrictBool = False


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
    pipeline_fingerprint: Sha256
    input_fingerprints: dict[Name, FileFingerprint]
    output_fingerprints: dict[Name, OutputFingerprint]
    row_counts: dict[Name, Count]
    key_label: Name | None
    warnings: tuple[Name | EvidenceWarning, ...]
    timings: tuple[Timing, ...]
    strategies: tuple[Strategy, ...]
    manifest_hash: Sha256
    # Additive to cli-local-1 (CLI #200): written together inside a decoy.yaml project, else absent.
    # Absent must stay absent for the manifest_hash, so these are never given a stored default.
    path_base: Literal["project"] | None = None
    project_manifest: RelPath | None = None

    @field_validator("path_base", mode="after")
    @classmethod
    def _path_base_not_null(cls, value: str | None) -> str | None:
        # Runs only when the key is present, so an explicit null is the one case left to refuse.
        if value is None:
            raise PydanticCustomError("path_base_null", "path_base is not null")
        return value

    @model_validator(mode="before")
    @classmethod
    def _binding_pair(cls, data: Any) -> Any:
        if isinstance(data, dict) and ("path_base" in data) != ("project_manifest" in data):
            missing = "project_manifest" if "path_base" in data else "path_base"
            detail = {
                "type": PydanticCustomError("binding_pair", "path_base and project_manifest pair"),
                "loc": (missing,),
                "input": None,
            }
            raise ValidationError.from_exception_data(cls.__name__, [detail])  # type: ignore[list-item]
        return data


class ColumnRef(_Model):
    table: SafeName
    column: SafeName


class SampleOutput(_Model):
    table: SafeName
    path: PathText
    sha256: Sha256


class SampleRunPayload(_Model):
    """Metadata of one ``--sample N`` run. Sample rows are scratch and never part of a record."""

    sample_size: Annotated[StrictInt, Field(ge=1)]
    root_table: SafeName
    table_rows: dict[SafeName, Count]
    subset_seed_counts: dict[SafeName, Count]
    approximate_columns: tuple[ColumnRef, ...]
    scratch_dir: PathText
    outputs: tuple[SampleOutput, ...]
    ttl_seconds: Annotated[StrictInt, Field(ge=1)]
