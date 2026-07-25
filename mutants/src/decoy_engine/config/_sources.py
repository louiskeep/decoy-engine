"""SourceDescriptor: discriminated union over source-type variants.

S14-CLOUD-SRC-S3GCS (2026-05-30): V2 grows cloud source variants per the
R1 marketed scope (`decoy_release_1_scope` memory: file + cloud object
storage). Adds `S3Source` and `GCSSource` next to the existing
`FileSource`; the discriminator is the literal `type` field.

S4-FIXED-WIDTH (finish-open-ended program): `FileSource.format` grows a
`fixed_width` variant alongside `csv` / `parquet`, backed by the
declarative `FixedWidthLayout` column-spec (`config._fixed_width`).
Additive-only change (compatibility-contract §5: new Literal member,
new optional field with a default) -- existing `csv` / `parquet`
configs are unaffected. `layout` is required iff `format=="fixed_width"`
and forbidden otherwise, enforced below.

Credentials: `credentials_ref` is OPAQUE to the engine. The engine treats
it as a string identifier the platform layer resolves against its
credentials store at run time before issuing the SDK call. The engine
never sees raw secrets in the config (ISO/IEC 27002 §8.24 / §5.17 -- key
management isolation; the engine layer is not a secrets boundary).

DB connectors are still V2.1 per `decoy_release_1_scope`; SFTP rides
along on `connectors/sftp.py` and ships in a separate S18 sprint.

Pydantic 2 discriminated-union pattern per
https://docs.pydantic.dev/latest/concepts/unions/#discriminated-unions.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from decoy_engine.config._fixed_width import FixedWidthLayout


class FileSource(BaseModel):
    """A local-filesystem CSV, Parquet, or fixed-width source.

    `layout` carries the column-spec for `format=="fixed_width"` (see
    `FixedWidthLayout` for the schema contract); it is required for
    that format and forbidden for `csv` / `parquet`.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["file"]
    format: Literal["csv", "parquet", "fixed_width"]
    path: str
    layout: FixedWidthLayout | None = Field(
        default=None,
        description=(
            "Column-spec for format=='fixed_width'; required for that "
            "format, forbidden (must be omitted/None) for csv/parquet."
        ),
    )

    @model_validator(mode="after")
    def _validate_layout_matches_format(self) -> FileSource:
        if self.format == "fixed_width" and self.layout is None:
            raise ValueError("FileSource: format='fixed_width' requires a `layout` column-spec")
        if self.format != "fixed_width" and self.layout is not None:
            raise ValueError(
                "FileSource: `layout` is only valid when format='fixed_width' "
                f"(got format={self.format!r})"
            )
        return self


class S3Source(BaseModel):
    """An S3 (or S3-compatible) CSV / Parquet source.

    Compatible with real AWS S3 plus any S3-compatible service that accepts
    a custom `endpoint_url` (MinIO, Cloudflare R2, Wasabi, B2). The engine
    fetches the object once into a buffer + reads into Arrow; no streaming
    in the V1 spec because the masked output is in-memory anyway.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["s3"]
    format: Literal["csv", "parquet"]
    bucket: str = Field(..., min_length=1, max_length=255)
    key: str = Field(..., min_length=1)
    region: str | None = None
    endpoint_url: str | None = Field(
        default=None,
        description=(
            "Override the AWS endpoint (e.g. http://localhost:9000 for MinIO, "
            "https://<account>.r2.cloudflarestorage.com for Cloudflare R2). "
            "Leave None for real AWS S3."
        ),
    )
    credentials_ref: str | None = Field(
        default=None,
        description=(
            "Opaque credentials identifier the platform resolves at run time. "
            "The engine never sees raw secrets; when None the SDK walks its "
            "default credential chain (env vars / instance profile / etc.)."
        ),
    )


class GCSSource(BaseModel):
    """A Google Cloud Storage CSV / Parquet source.

    Structural mirror of S3Source. GCS uses `object` instead of `key` to
    match the GCS API surface (`bucket.object_name` vs S3 `bucket.key`).
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["gcs"]
    format: Literal["csv", "parquet"]
    bucket: str = Field(..., min_length=1, max_length=255)
    object: str = Field(..., min_length=1)
    credentials_ref: str | None = Field(
        default=None,
        description=(
            "Opaque credentials identifier the platform resolves at run time. "
            "The engine never sees raw secrets; when None the SDK uses GCP "
            "ADC (Application Default Credentials)."
        ),
    )


SourceDescriptor = Annotated[
    FileSource | S3Source | GCSSource,
    Field(discriminator="type"),
]
