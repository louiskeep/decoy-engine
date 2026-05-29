"""TargetDescriptor: discriminated union over output-target variants.

Structural mirror of SourceDescriptor (same Pydantic 2 discriminated-
union pattern). V1 ships only `FileTarget`.
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


TargetDescriptor = Annotated[
    FileTarget,
    Field(discriminator="type"),
]
