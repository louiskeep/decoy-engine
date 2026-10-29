"""Versioned child-preparation document: the only thing an isolated child hands its parent.

The child seals a directory; it never publishes. The parent reconstructs the distinct
`ChildPreparationEnvelope` from this document, requires its path to equal the staging
directory the PARENT assigned (re-measuring files does not establish attempt ownership),
and only then promotes it. Anything else is refused and left untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from decoy_engine.errors import DecoyError
from decoy_engine.publication._types import ChildPreparationEnvelope

CHILD_SCHEMA = "decoy-child-preparation/v1"
_KEYS = {"schema", "staging_dir", "tables", "row_counts"}


class ChildEnvelopeError(DecoyError):
    """The child's preparation document is missing, malformed, or names a foreign path."""


def child_envelope_to_doc(envelope: ChildPreparationEnvelope) -> dict[str, Any]:
    return {
        "schema": CHILD_SCHEMA,
        "staging_dir": str(envelope.staging_dir),
        "tables": list(envelope.tables),
        "row_counts": dict(envelope.row_counts),
    }


def _table_name(name: object) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name != Path(name).name
        or name.startswith(".")
        or "\\" in name
        or "\x00" in name
    ):
        raise ChildEnvelopeError(f"unsafe table name {name!r} in child preparation")
    return name


def parse_child_envelope(doc: object, assigned: Path) -> ChildPreparationEnvelope:
    """Validate `doc` and bind it to the parent-assigned staging directory."""
    if not isinstance(doc, dict) or set(doc) != _KEYS or doc.get("schema") != CHILD_SCHEMA:
        raise ChildEnvelopeError("child preparation document is missing or has an unknown version")
    tables_raw, counts_raw = doc["tables"], doc["row_counts"]
    if not isinstance(tables_raw, list) or not isinstance(counts_raw, dict):
        raise ChildEnvelopeError("child preparation tables/row_counts have the wrong shape")
    tables = tuple(_table_name(t) for t in tables_raw)
    if len(set(tables)) != len(tables):
        raise ChildEnvelopeError("duplicate table in child preparation")
    counts: list[tuple[str, int]] = []
    for name, rows in counts_raw.items():
        if _table_name(name) not in tables:
            raise ChildEnvelopeError(f"row count for undeclared table {name!r}")
        if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
            raise ChildEnvelopeError(f"invalid row count for {name!r}")
        counts.append((name, rows))
    staging = doc["staging_dir"]
    if not isinstance(staging, str) or Path(staging).resolve() != Path(assigned).resolve():
        raise ChildEnvelopeError("child preparation names a directory the parent did not assign")
    if Path(assigned).is_symlink() or not Path(assigned).is_dir():
        raise ChildEnvelopeError("assigned staging directory is not a plain directory")
    return ChildPreparationEnvelope(Path(assigned), tables, tuple(sorted(counts)))
