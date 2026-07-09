"""Transactional sink protocol and reference file-based implementation.

A TransactionalSink lets run_sequential publish tables all-or-nothing: each
table is written during the run, and either committed (atomically on success)
or aborted (everything discarded) on any exception.

The file-based reference implementation (ParquetTransactionalSink) uses a
single atomic directory rename: Parquet files are staged to a private
temporary directory during write(), and commit() publishes the entire staging
directory to the target with one os.replace call (POSIX rename(2), which is
atomic within one filesystem, per POSIX.1-2008). Either every file lands at
once or nothing is published. On abort(), the staging directory is removed
before any data reaches the target.

Commit-time failure guarantee: if os.replace raises (disk full, permissions
denied, or the target already exists and is non-empty), nothing is published
and the staging directory remains intact for a subsequent abort(). The target
path is never touched until the single rename succeeds.

If the target directory already exists and is non-empty when commit() is
called, os.replace raises OSError (POSIX rename(2) ENOTEMPTY) and commit
fails closed with nothing published.

Staging always lives in target.parent so staging and target share a
filesystem, ensuring the rename cannot fail with EXDEV.

See GNU Coreutils mv(1), POSIX.1-2008 rename(2), and Python os.replace docs.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

import pyarrow as pa
import pyarrow.parquet as pq


# NOTE: isinstance(obj, TransactionalSink) matches write/write_batches/commit/
# abort by name only, not by signature -- a consequence of runtime_checkable
# Protocol. Because isinstance checks ALL protocol members, a sink object that
# implements only write/commit/abort (the pre-SC1 three-method shape) no
# longer matches this Protocol now that write_batches is a member; see
# `_sequential.run_sequential`'s dispatch comment. Both shipped
# implementations below (`_CallableSinkAdapter`, `ParquetTransactionalSink`)
# implement all four, so existing callers of those two classes are unaffected.
@runtime_checkable
class TransactionalSink(Protocol):
    """All-or-nothing table sink protocol.

    run_sequential calls write() for each masked table in FK-topological order,
    then calls commit() on success or abort() on any exception. Implementations
    must be safe to call abort() even when no write() calls were made (e.g. the
    loader itself raises before the first table is processed).

    abort() MUST be best-effort and must not raise. If cleanup inside abort()
    fails, the error must be suppressed so that the caller can re-raise the
    original run exception unmasked.
    """

    def write(self, table: str, data: pa.Table) -> None:
        """Accept one masked table; called in FK-topological order."""
        ...

    def write_batches(
        self, table: str, batches: Iterable[pa.RecordBatch], *, schema: pa.Schema
    ) -> None:
        """Stream-append one masked table as record batches.

        Used by the out-of-core runner (SC1), which produces each table as a
        batch stream so the whole table is never resident. Must honor the
        same atomic commit/abort contract as write(): nothing staged here is
        visible until commit() runs.
        """
        ...

    def commit(self) -> None:
        """Finalize all written tables; called only on a successful run."""
        ...

    def abort(self) -> None:
        """Discard all written tables; called on any exception during the run.

        Must be best-effort and must not raise.
        """
        ...


class _CallableSinkAdapter:
    """Wraps a plain Callable[[str, pa.Table], None] as a TransactionalSink.

    Preserves the pre-existing non-transactional contract: write() calls the
    callable immediately (so partial output on abort is the documented
    behavior). commit() and abort() are no-ops.
    """

    def __init__(self, fn: object) -> None:
        self._fn = fn

    def write(self, table: str, data: pa.Table) -> None:
        self._fn(table, data)  # type: ignore[operator]

    def write_batches(
        self, table: str, batches: Iterable[pa.RecordBatch], *, schema: pa.Schema
    ) -> None:
        """Non-streaming fallback: this callable's contract is one whole
        pa.Table per call, so the batch stream is materialized once here
        before the single legacy call. The out-of-core streaming path is
        ParquetTransactionalSink.write_batches, not this adapter."""
        data = pa.Table.from_batches(list(batches), schema=schema)
        self._fn(table, data)  # type: ignore[operator]

    def commit(self) -> None:
        pass

    def abort(self) -> None:
        pass


class ParquetTransactionalSink:
    """File-based transactional sink using a single atomic POSIX directory rename.

    On write(), each masked table is serialized as Parquet to a private staging
    directory (a sibling of the target directory, sharing a filesystem). On
    commit(), the staging directory is atomically published as the target via a
    single os.replace call (POSIX rename within one filesystem): either the
    entire set of tables lands at once or nothing is published. On abort(), the
    staging directory is removed so nothing partial reaches the target.

    Commit-time failure: if os.replace raises (disk full, permissions denied,
    or the target already exists and is non-empty), nothing is published and the
    staging directory is intact for a subsequent abort().

    If the target directory already exists and is non-empty when commit() is
    called, os.replace raises OSError and commit fails closed.

    The staging directory name starts with ``_decoy_stage_`` so callers can
    identify and clean up orphaned directories if needed.

    Args:
        target_dir: Directory where committed Parquet files are published via
            atomic rename from the staging directory.
    """

    def __init__(self, target_dir: Path) -> None:
        self._target = target_dir
        self._staging: Path | None = None

    def write(self, table: str, data: pa.Table) -> None:
        """Stage one masked table as Parquet in the private staging directory.

        Args:
            table: Table name used as the Parquet file stem. Must be a single
                path component containing no path separators and no ``..``.
            data: Arrow table to serialize.

        Raises:
            ValueError: If ``table`` contains a path separator, ``..``, or is
                not a single valid path component.
            OSError: If the staging directory cannot be created or the Parquet
                file cannot be written.
            pa.lib.ArrowInvalid: If ``data`` cannot be serialized as Parquet.
        """
        self._validate_table_name(table)
        dest = self._stage_dest(table)
        pq.write_table(data, dest)

    def write_batches(
        self, table: str, batches: Iterable[pa.RecordBatch], *, schema: pa.Schema
    ) -> None:
        """Stream-append one masked table as record batches via ParquetWriter.

        Used by the out-of-core runner (SC1) so the whole table is never
        resident: each batch is written to the staged Parquet file as it
        arrives. This staged file lands in the same staging directory, under
        the same table-name validation and same-filesystem invariant, as
        write(); the commit()/abort() atomic-rename contract is unchanged.
        Follows the pyarrow.parquet.ParquetWriter incremental-write pattern
        (Apache Arrow docs: "Reading and Writing Single Files").

        Args:
            table: Table name used as the Parquet file stem. Same validation
                as write(): must be a single path component, no separators
                or ``..``.
            batches: Record batches to append, in order. Each batch becomes one
                Parquet row group, so callers own row-group sizing by choosing
                their batch size (the out-of-core runner uses a bounded batch of
                tens of thousands of rows). An empty iterable still produces a
                valid, readable zero-row Parquet file with ``schema``.
            schema: Arrow schema for the staged file. Must match the schema
                of every batch in ``batches``.

        Raises:
            ValueError: If ``table`` is not a single valid path component.
            OSError: If the staging directory or file cannot be created or
                written.
            pa.lib.ArrowInvalid: If a batch does not match ``schema``.

        If the batch iterator raises partway through, the in-flight exception
        is preserved (a close() failure while unwinding is suppressed so it
        cannot mask it) and the partially staged file is never published: it
        exists only under the staging directory, which abort() removes
        wholesale. On the success path a close() failure (for example a footer
        flush hitting ENOSPC) is a real write error and does surface.
        """
        self._validate_table_name(table)
        dest = self._stage_dest(table)
        writer = pq.ParquetWriter(dest, schema)
        try:
            for batch in batches:
                writer.write_batch(batch)
        except BaseException:
            # Close best-effort so a close error cannot mask the original.
            try:
                writer.close()
            except Exception:
                pass
            raise
        writer.close()

    def _validate_table_name(self, table: str) -> None:
        if (
            os.sep in table
            or (os.altsep is not None and os.altsep in table)
            or ".." in table
            or table != Path(table).name
        ):
            raise ValueError(
                f"table name {table!r} is not a single path component; "
                "must not contain path separators or '..'"
            )

    def _stage_dest(self, table: str) -> Path:
        if self._staging is None:
            # Staging lives in target.parent to guarantee same-filesystem rename.
            self._target.parent.mkdir(parents=True, exist_ok=True)
            self._staging = Path(tempfile.mkdtemp(prefix="_decoy_stage_", dir=self._target.parent))
        return self._staging / f"{table}.parquet"

    def commit(self) -> None:
        """Atomically publish the staging directory as the target.

        A single os.replace (POSIX rename) makes the entire set of Parquet
        files visible at once or not at all. The target path is not created
        or touched until the rename succeeds. Exception: if no write() calls
        were made before commit(), the staging directory was never created and
        an empty target directory is created directly (no rename).

        If the target directory already exists and is non-empty, os.replace
        raises OSError and nothing is published; the staging directory remains
        intact for a subsequent abort().

        Raises:
            OSError: If the rename fails (target non-empty, disk full, or
                permission denied). Nothing is published on failure; staging
                is intact for abort().
        """
        if self._staging is None:
            # Nothing was written; create an empty target directory directly.
            self._target.parent.mkdir(parents=True, exist_ok=True)
            self._target.mkdir(exist_ok=True)
            return
        # Ensure target's parent exists so the rename can land.
        self._target.parent.mkdir(parents=True, exist_ok=True)
        # Single atomic rename: staging becomes target.
        os.replace(self._staging, self._target)
        # Rename succeeded: staging is now the target directory; clear state.
        self._staging = None

    def abort(self) -> None:
        """Remove the staging directory so nothing partial reaches the target.

        Best-effort: must not raise even if cleanup fails.
        """
        self._cleanup_staging()

    def _cleanup_staging(self) -> None:
        if self._staging is not None:
            shutil.rmtree(self._staging, ignore_errors=True)
        self._staging = None
