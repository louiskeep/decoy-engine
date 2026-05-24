"""Shared constants for the graph validator modules.

Owned by the validators package so the bundled GraphConfigValidator
class can be removed without leaving these as orphan class attributes.
"""

from __future__ import annotations

import re

NODE_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")

SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})

# File-producing source op kinds for format-consistency checks.
FILE_SOURCE_KINDS: frozenset[str] = frozenset(
    {
        "source.file",
        "source.s3",
        "source.gcs",
        "source.sftp",
    }
)

# File-consuming sink op kinds for format-consistency checks.
FILE_TARGET_KINDS: frozenset[str] = frozenset(
    {
        "target.file",
        "target.s3",
        "target.gcs",
        "target.sftp",
    }
)
