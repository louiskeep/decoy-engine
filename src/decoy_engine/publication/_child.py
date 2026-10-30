"""Versioned child-preparation document: the only thing an isolated child hands its parent.

The child seals a directory; it never publishes. The parent reconstructs the distinct
`ChildPreparationEnvelope` from this document, requires its path to equal the staging
directory the PARENT assigned (re-measuring files does not establish attempt ownership),
and only then promotes it. Anything else is refused and left untouched.
"""

from __future__ import annotations

import os
import stat
import struct
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.errors import DecoyError
from decoy_engine.publication._local_directory import _refuse, gate_request
from decoy_engine.publication._types import ChildPreparationEnvelope, PublicationRequest

CHILD_SCHEMA = "decoy-child-preparation/v1"
_KEYS = {"schema", "staging_dir", "tables", "row_counts"}

# The child is untrusted, so the parent parses its footer under a fixed budget. A footer is
# O(row groups x columns); 64 MiB is orders of magnitude above any real output and far below
# what a hostile length field could make the parent allocate. Thrift limits bound the
# decoder's own expansion (Arrow's ParquetFile exposes both; Parquet format spec, "File Format").
MAX_FOOTER_BYTES = 64 * 1024 * 1024
MAX_THRIFT_STRING_BYTES = 16 * 1024 * 1024
MAX_THRIFT_CONTAINER_ITEMS = 1_000_000
_TRAILER_BYTES = 8  # 4-byte little-endian footer length + the "PAR1" magic
_MAGIC = b"PAR1"
_MIN_FILE_BYTES = len(_MAGIC) + _TRAILER_BYTES  # leading magic + trailer


class ChildEnvelopeError(DecoyError):
    """The child's preparation document is missing, malformed, or names a foreign path.

    The message is a stable token; the child controls every value in the document, so none
    of it (table names, paths) is ever echoed."""


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
        raise ChildEnvelopeError("child_table_name_unsafe")
    return name


def parse_child_envelope(doc: object, assigned: Path) -> ChildPreparationEnvelope:
    """Validate `doc` and bind it to the parent-assigned staging directory."""
    if not isinstance(doc, dict) or set(doc) != _KEYS or doc.get("schema") != CHILD_SCHEMA:
        raise ChildEnvelopeError("child_document_invalid")
    tables_raw, counts_raw = doc["tables"], doc["row_counts"]
    if not isinstance(tables_raw, list) or not isinstance(counts_raw, dict):
        raise ChildEnvelopeError("child_document_shape")
    tables = tuple(_table_name(t) for t in tables_raw)
    if len(set(tables)) != len(tables):
        raise ChildEnvelopeError("child_table_duplicate")
    counts: list[tuple[str, int]] = []
    for name, rows in counts_raw.items():
        if _table_name(name) not in tables:
            raise ChildEnvelopeError("child_row_count_undeclared")
        if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
            raise ChildEnvelopeError("child_row_count_invalid")
        counts.append((name, rows))
    staging = doc["staging_dir"]
    if not isinstance(staging, str) or Path(staging).resolve() != Path(assigned).resolve():
        raise ChildEnvelopeError("child_staging_foreign")
    if Path(assigned).is_symlink() or not Path(assigned).is_dir():
        raise ChildEnvelopeError("child_staging_not_plain")
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
            rows = _footer_rows(root / f"{table}.parquet")
        except (OSError, pa.ArrowException, ValueError, OverflowError, MemoryError) as exc:
            raise invalid() from exc
        if rows != expected_rows:
            raise invalid()


def _footer_rows(path: Path) -> int:
    """Row count from the footer alone, refusing before the parse when the declared footer
    cannot be legitimate: the 8-byte trailer is read first and its length is capped."""
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        if size < _MIN_FILE_BYTES:
            raise ValueError("file too short for a Parquet trailer")
        fh.seek(-_TRAILER_BYTES, os.SEEK_END)
        trailer = fh.read(_TRAILER_BYTES)
        (footer_len,) = struct.unpack("<I", trailer[:4])
        if trailer[4:] != _MAGIC or footer_len > MAX_FOOTER_BYTES:
            raise ValueError("trailer is not a Parquet trailer within budget")
        if footer_len > size - _MIN_FILE_BYTES:
            raise ValueError("declared footer is longer than the file")
        with pq.ParquetFile(
            fh,
            thrift_string_size_limit=MAX_THRIFT_STRING_BYTES,
            thrift_container_size_limit=MAX_THRIFT_CONTAINER_ITEMS,
        ) as parquet:
            return int(parquet.metadata.num_rows)
