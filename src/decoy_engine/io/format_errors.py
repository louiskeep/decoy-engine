"""Coded refusals for the Avro and ORC readers and writers.

Each is a `BoundedReadError`, so a caller that already handles the bounded readers' coded
failures handles these too. The detail carries identifiers, positions, Arrow and Avro type
names and fixed reason words, never a cell value, a default, an enum symbol or a path. They are
raised after any library `except` block has closed, so no library error rides as context.
"""

from __future__ import annotations

from decoy_engine import reasons
from decoy_engine.io.bounded_reader import BoundedReadError


class AvroMalformed(BoundedReadError):  # noqa: N818 - coded condition
    """The Avro container is truncated, corrupt or not an Avro file."""

    code = reasons.runtime.AVRO_CONTAINER_MALFORMED


class AvroSchemaUnsupported(BoundedReadError):  # noqa: N818 - coded condition
    """The Avro writer schema or codec is outside what the engine reads."""

    code = reasons.runtime.AVRO_SCHEMA_UNSUPPORTED


class AvroBlockTooLarge(BoundedReadError):  # noqa: N818 - coded condition
    """One Avro block is larger than the chunk budget or the absolute block cap."""

    code = reasons.runtime.AVRO_BLOCK_TOO_LARGE


class OrcMalformed(BoundedReadError):  # noqa: N818 - coded condition
    """The ORC file is truncated, corrupt or not an ORC file."""

    code = reasons.runtime.ORC_FILE_MALFORMED


class OrcTypeUnsupported(BoundedReadError):  # noqa: N818 - coded condition
    """The ORC file has a column type the engine refuses to read."""

    code = reasons.runtime.ORC_TYPE_UNSUPPORTED


class TargetColumnTypeUnsupported(BoundedReadError):  # noqa: N818 - coded condition
    """A column cannot be written to the target format without changing its values."""

    code = reasons.runtime.TARGET_COLUMN_TYPE_UNSUPPORTED


class TargetSchemaMismatch(BoundedReadError):  # noqa: N818 - coded condition
    """A chunk does not cast to the declared write schema."""

    code = reasons.runtime.TARGET_SCHEMA_MISMATCH


__all__ = [
    "AvroBlockTooLarge",
    "AvroMalformed",
    "AvroSchemaUnsupported",
    "OrcMalformed",
    "OrcTypeUnsupported",
    "TargetColumnTypeUnsupported",
    "TargetSchemaMismatch",
]
