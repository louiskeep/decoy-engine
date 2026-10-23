"""Multi-part parquet dataset staging primitive (warehouse export reader, S1a).

A cloud warehouse's native export (Redshift ``UNLOAD``, Snowflake ``COPY INTO``)
writes a table as MANY parquet part-files under one prefix. This primitive
merges that set of part-files into ONE consistent staged parquet file, which the
engine's existing single-file source/profile path then consumes unchanged. It is
the file-set analogue of ``connectors/postgres_snapshot.snapshot_tables_to_parquet``
(read a source, stage one ordered parquet, atomic publish, fail closed), and it
follows that module's structure deliberately.

The library does not know its callers: it takes a list of already-local parquet
paths. Listing a bucket prefix and staging the parts locally is the platform's
job (it holds the cloud credentials); this primitive only merges local files.

Pinned guarantees (each has a test in ``tests/connectors/test_parquet_dataset*``):

* One consistent dataset. Every part's Arrow schema must equal the first part's
  schema exactly (names, types, nullability, field metadata). Any disagreement
  fails closed with ``schema_mismatch`` -- no silent upcast or union-by-name.
  pyarrow is used (not DuckDB's multi-file read) precisely so each column's type
  is preserved exactly rather than unified across files.
* Deterministic output. Parts are read in sorted-path order and each part's row
  order is preserved, so equal inputs yield an order-stable staged dataset
  regardless of the input list's order. (This is about row order/content, not
  the parquet file's writer-metadata bytes.)
* Type fidelity. Column types are exactly what pyarrow reads from the parts; no
  coercion. Downstream MASKING fidelity is the separate S0 matrix.
* Atomic publish. Parts stream into a staging directory; the staged file is
  ``os.replace``d into place only after every part is written. Any failure or
  interrupt publishes nothing and removes the staging directory.
* Bounded memory. Each part streams row-group by row-group into one
  ``ParquetWriter``; the whole dataset is never materialized at once.
* No leaks. Errors carry a code and a part BASENAME (never a full path, value,
  or credential) plus the exception type.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.sdk import PermanentError

__all__ = ["ParquetDatasetError", "stage_parquet_parts"]

_log = logging.getLogger(__name__)

# pyarrow's default row-group batch; keeps per-part streaming bounded.
_DEFAULT_BATCH_ROWS = 122_880


class ParquetDatasetError(PermanentError):
    """Coded, redacted multi-part staging failure.

    ``code`` is a stable machine-readable token. ``location`` is a part file
    BASENAME (never a full path or value). The message is built only from those
    parts plus an exception type name.
    """

    def __init__(self, code: str, *, location: str = "", cause_type: str = "") -> None:
        self.code = code
        self.location = location
        self.cause_type = cause_type
        parts = [f"parquet_dataset[{code}]"]
        if location:
            parts.append(f"at {location}")
        if cause_type:
            parts.append(f"cause={cause_type}")
        super().__init__(" ".join(parts))


def _open_part(path: Path) -> pq.ParquetFile:
    try:
        return pq.ParquetFile(path)
    except Exception as exc:  # pragma: no cover - exercised via the error test
        raise ParquetDatasetError(
            "part_unreadable", location=path.name, cause_type=type(exc).__name__
        ) from None


def stage_parquet_parts(
    part_paths: Sequence[str | Path],
    dest_dir: str | Path,
    *,
    batch_rows: int = _DEFAULT_BATCH_ROWS,
    preserve_order: bool = False,
) -> Path:
    """Merge local parquet part-files into one staged parquet; return its path.

    By default parts are read in sorted-path order (deterministic for a caller
    that has no authoritative order of its own). Each part's row order is
    preserved. The first part's Arrow schema is the dataset schema; every other
    part must match it exactly or the call fails closed (``schema_mismatch``) and
    publishes nothing. On any failure, timeout, or interrupt the staging
    directory is removed and nothing is published.

    Args:
        part_paths: local parquet files to merge (at least one). The platform
            resolves a warehouse export prefix to this list; this primitive does
            no bucket/credential work.
        dest_dir: directory the staged file is published into.
        batch_rows: per-part streaming batch size (bounds memory).
        preserve_order: when True, merge in the GIVEN ``part_paths`` order instead
            of sorting by path. Use it when the caller already holds the
            authoritative order (e.g. the platform staged a warehouse export's
            parts under UUID-bearing local names but knows the part-KEY order);
            relying on the local-path sort would then be non-deterministic.

    Raises:
        ParquetDatasetError: ``no_parts`` (empty input), ``part_unreadable``
            (missing/corrupt part), ``schema_mismatch`` (a part disagrees with
            the first part's schema), ``stage_failed`` (write/publish failure).
    """
    if not part_paths:
        raise ParquetDatasetError("no_parts")

    # Order: the caller's given order when it is authoritative, else a
    # deterministic sort by resolved path string. Within-part row order is
    # preserved either way by streaming each part's batches in file order.
    paths = [Path(p) for p in part_paths]
    ordered = paths if preserve_order else sorted(paths, key=lambda p: str(p))

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    stage_dir = dest_dir / f".stage-{run_id}"
    stage_dir.mkdir(mode=0o700)
    staged_file = stage_dir / "dataset.parquet"
    final_file = dest_dir / f"dataset-{run_id}.parquet"

    published = False
    writer: pq.ParquetWriter | None = None
    try:
        first = _open_part(ordered[0])
        schema = first.schema_arrow
        writer = pq.ParquetWriter(staged_file, schema)
        for part in ordered:
            pf = first if part is ordered[0] else _open_part(part)
            # Strict schema identity: names, types, nullability, field metadata.
            # No upcast or union-by-name -- a warehouse export's parts share one
            # schema, and a disagreement means the set is not one table.
            if not pf.schema_arrow.equals(schema, check_metadata=True):
                raise ParquetDatasetError("schema_mismatch", location=part.name)
            for batch in pf.iter_batches(batch_size=batch_rows):
                writer.write_batch(batch)
        writer.close()
        writer = None
        os.replace(staged_file, final_file)
        published = True
        _log.info("parquet_dataset staged parts=%d", len(ordered))
        return final_file
    except ParquetDatasetError:
        raise
    except Exception as exc:
        raise ParquetDatasetError("stage_failed", cause_type=type(exc).__name__) from None
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        shutil.rmtree(stage_dir, ignore_errors=True)
        if not published:
            try:
                final_file.unlink()
            except OSError:
                pass


def read_schema(part_path: str | Path) -> pa.Schema:
    """The Arrow schema of one part (for callers validating a set before staging)."""
    return _open_part(Path(part_path)).schema_arrow
