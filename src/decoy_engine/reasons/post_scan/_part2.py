"""Catalog of the ``post_scan`` namespace, part 2 (declared key checks and the verdict)."""

from __future__ import annotations

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import UNRELEASED, RetryClass

__all__ = [
    "FINDING_BLOCKED",
    "FOREIGN_KEY_ORPHANS_INTRODUCED",
    "KEY_COLUMN_MISSING",
    "KEY_TYPE_MISMATCH",
    "PRIMARY_KEY_DUPLICATES_INTRODUCED",
    "PRIMARY_KEY_NULLS_INTRODUCED",
]

_d = _catalog.post_scan("decoy_engine.verification.postmask._keys")
_verdict = _catalog.post_scan("decoy_engine.verification.postmask._blocked")
_repair = RetryClass.CONFIGURATION_REPAIR

PRIMARY_KEY_DUPLICATES_INTRODUCED = _d(
    "primary_key_duplicates_introduced",
    "The run left more duplicate primary keys in the output than its inputs held.",
    retry=_repair,
    since=UNRELEASED,
)
PRIMARY_KEY_NULLS_INTRODUCED = _d(
    "primary_key_nulls_introduced",
    "The run left more null primary keys in the output than its inputs held.",
    retry=_repair,
    since=UNRELEASED,
)
FOREIGN_KEY_ORPHANS_INTRODUCED = _d(
    "foreign_key_orphans_introduced",
    "The run left more foreign keys without a parent in the output than its inputs held.",
    retry=_repair,
    since=UNRELEASED,
)
KEY_COLUMN_MISSING = _d(
    "key_column_missing",
    "A declared relationship key column is absent from the output.",
    retry=_repair,
    since=UNRELEASED,
)
KEY_TYPE_MISMATCH = _d(
    "key_type_mismatch",
    "The output parent and child key columns of a relationship have types that cannot be compared.",
    retry=_repair,
    since=UNRELEASED,
)
FINDING_BLOCKED = _verdict(
    "finding_blocked",
    "A post-scan column check found a failing finding in the output, so the run is withheld.",
    retry=_repair,
    since=UNRELEASED,
)
