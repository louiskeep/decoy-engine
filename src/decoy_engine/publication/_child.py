"""Versioned child-preparation document: the only thing an isolated child hands its parent.

The child seals a directory; it never publishes. The parent reconstructs the distinct
`ChildPreparationEnvelope` from this document, requires its path to equal the staging
directory the PARENT assigned (re-measuring files does not establish attempt ownership),
and only then promotes it. Anything else is refused and left untouched.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.errors import DecoyError
from decoy_engine.publication._local_directory import _refuse, gate_request
from decoy_engine.publication._types import ChildPreparationEnvelope, PublicationRequest

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


def verify_child_envelope(envelope: ChildPreparationEnvelope, request: PublicationRequest) -> None:
    """Structural check of a child's sealed directory BEFORE any Parquet read or measure.

    Raises `child_envelope_invalid` (touching nothing) unless the directory is a real
    directory, every declared table is a regular `<table>.parquet`, the row-count keys
    equal the declared tables exactly, and no undeclared `.parquet` file is present.
    """
    gate_request(request)  # admission + required-check rejection before ANY filesystem read

    def invalid() -> Exception:
        return _refuse(request, "rejected", "child_envelope_invalid")

    root = envelope.staging_dir
    if root.is_symlink() or not root.is_dir():
        raise invalid()
    declared = set(envelope.tables)
    if len(declared) != len(envelope.tables):
        raise invalid()
    if {name for name, _ in envelope.row_counts} != declared or len(envelope.row_counts) != len(
        declared
    ):
        raise invalid()
    for table in envelope.tables:
        try:
            if not stat.S_ISREG(os.lstat(root / f"{table}.parquet").st_mode):
                raise invalid()
        except OSError as exc:
            raise invalid() from exc
    try:
        present = {p.name for p in root.iterdir() if p.suffix == ".parquet"}
    except OSError as exc:
        raise invalid() from exc
    if present != {f"{t}.parquet" for t in declared}:
        raise invalid()
    if request.expected_tables is not None and set(request.expected_tables) != declared:
        raise invalid()
    # Footers only: the claimed row counts are checked without deserializing any payload.
    for table, expected_rows in envelope.row_counts:
        try:
            meta = pq.read_metadata(root / f"{table}.parquet")
        except (OSError, pa.ArrowException) as exc:
            raise invalid() from exc
        if meta.num_rows != expected_rows:
            raise invalid()
