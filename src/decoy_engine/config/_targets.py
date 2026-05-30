"""TargetDescriptor: discriminated union over output-target variants.

S15-CLOUD-TGT-S3GCS (2026-05-30): structural mirror of SourceDescriptor.
Adds `S3Target` and `GCSTarget` alongside the existing `FileTarget`.
The discriminator is the literal `type` field.

Credentials handling mirrors S14 sources: `credentials_ref` is OPAQUE to
the engine; the platform layer resolves it against its credentials store
before the SDK call. The engine layer is not a secrets boundary
(ISO/IEC 27002 §8.24 / §5.17).

Write semantics (S15): cloud targets write to a temporary key
`<key>.<job_id>.tmp`, head-object verify, then server-side copy_object to
the canonical key and delete the tmp. A partial-write never leaves a
half-written canonical object (Q12 pattern carry from QA review of the
V1 DB connector's no-transaction to_sql). Verification is at the
platform layer (`api/jobs/v2_runner.py:_materialize_output`); this module
is the schema only.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class FileTarget(BaseModel):
    """A local-filesystem CSV or Parquet target."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["file"]
    format: Literal["csv", "parquet"]
    path: str


class S3Target(BaseModel):
    """An S3 (or S3-compatible) CSV / Parquet target.

    Symmetric with `S3Source`. The platform writes via the atomic-move
    pattern (Q12 carry): upload to `<key>.<job_id>.tmp`, server-side
    `copy_object` to the canonical key on verify, then delete the tmp.
    A mid-upload failure leaves the canonical key untouched.
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


class GCSTarget(BaseModel):
    """A Google Cloud Storage CSV / Parquet target.

    Symmetric with `GCSSource`. Atomic-move pattern at the platform layer
    mirrors `S3Target`.
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


TargetDescriptor = Annotated[
    FileTarget | S3Target | GCSTarget,
    Field(discriminator="type"),
]
