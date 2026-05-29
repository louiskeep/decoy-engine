"""SourceDescriptor: discriminated union over source-type variants.

V1 ships only `FileSource` (`type: "file"`, `format: csv | parquet`).
S3 / GCS / SFTP variants are V2+ extensions; build them when there's
a real customer reading from those backends. Per
`decoy_release_1_scope` memory: V1 is file upload + cloud object storage
only, and DB connectors are deferred.

Pydantic 2 discriminated-union pattern per
https://docs.pydantic.dev/latest/concepts/unions/#discriminated-unions.
Single-variant union is a deliberate forward-compat shape: callers
parsing the YAML will reject `type: s3` etc. with a clean "type=ftp not
in discriminator values" error, and the union grows by adding a new
variant + extending the Union.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class FileSource(BaseModel):
    """A local-filesystem CSV or Parquet source."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["file"]
    format: Literal["csv", "parquet"]
    path: str


# V1 union: just FileSource. Add S3Source / GCSSource / SFTPSource in V2.
SourceDescriptor = Annotated[
    FileSource,
    Field(discriminator="type"),
]
