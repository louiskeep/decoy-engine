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
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import pyarrow as pa

from decoy_engine import fileaccess
from decoy_engine.execution._publication_observer import (
    PublicationEvidence,
    capture_publication_failure,
)
from decoy_engine.fileaccess import (
    FileAccess,
    FileAccessUnavailable,
    FileRequest,
    FileRole,
    OutputDir,
    OutputTxn,
)
from decoy_engine.fileaccess.adapters import arrow as arrow_adapter
from decoy_engine.progress import get_active_channel
from decoy_engine.reasons import CleanupState, Operation, PublicationState


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
        # The callable publishes as it goes, so a failure leaves the output state unknowable.
        self.publication_evidence = PublicationEvidence()

    def _written(self, table: str, call: Any) -> None:
        evidence = self.publication_evidence
        evidence.last_operation = Operation.WRITE
        try:
            call()
        except BaseException as exc:
            evidence.state = PublicationState.UNKNOWN
            evidence.cleanup = CleanupState.UNKNOWN
            capture_publication_failure(exc, self, Operation.WRITE, table=table)
            raise
        evidence.state = PublicationState.PARTIAL
        evidence.cleanup = CleanupState.UNKNOWN

    def write(self, table: str, data: pa.Table) -> None:
        self._written(table, lambda: self._fn(table, data))  # type: ignore[operator]

    def write_batches(
        self, table: str, batches: Iterable[pa.RecordBatch], *, schema: pa.Schema
    ) -> None:
        """Non-streaming fallback: this callable's contract is one whole
        pa.Table per call, so the batch stream is materialized once here
        before the single legacy call. The out-of-core streaming path is
        ParquetTransactionalSink.write_batches, not this adapter."""
        data = pa.Table.from_batches(list(batches), schema=schema)
        self._written(table, lambda: self._fn(table, data))  # type: ignore[operator]

    def commit(self) -> None:
        self.publication_evidence.state = PublicationState.PUBLISHED
        self.publication_evidence.cleanup = CleanupState.NOT_NEEDED

    def abort(self) -> None:
        # Nothing is undone: whatever the callable already published stays, and cleanup is not
        # claimed. `unknown` until a write is known not to have happened.
        pass


class ParquetTransactionalSink:
    """File-based transactional sink using a single atomic POSIX directory rename.

    On write(), each masked table is serialized as Parquet to a private staging
    directory (a sibling of the target directory, sharing a filesystem). On
    commit(), the staging directory is atomically published as the target via a
    single rename (POSIX rename within one filesystem): either the
    entire set of tables lands at once or nothing is published. On abort(), the
    staging directory is removed so nothing partial reaches the target.

    Commit-time failure: if the rename raises (disk full, permissions denied,
    or the target already exists and is non-empty), nothing is published and the
    staging directory is intact for a subsequent abort().

    If the target directory already exists and is non-empty when commit() is
    called, the rename raises OSError and commit fails closed.

    The staging directory name starts with ``_decoy_stage_`` so callers can
    identify and clean up orphaned directories if needed.

    The stage is a policy-issued output transaction: the sink holds the output capability it
    was built with and uses it for every later write, commit and abort, so a sink built under
    one policy is judged by that policy however it is driven afterwards.

    Args:
        target_dir: Directory where committed Parquet files are published via atomic rename
            from the staging directory, or an `OutputDir` the policy already issued for it.
        file_access: The policy that decides where output may go (default: ambient CLI).
        field_key: The config position named in a denial.
    """

    # Capability marker for bounded generation: write_batches appends one row group per batch and
    # never materializes the stream. The callable adapter lacks it, so it cannot take a streamed run.
    bounded_write_batches = True

    def __init__(
        self,
        target_dir: Path | str | OutputDir,
        *,
        file_access: FileAccess | None = None,
        field_key: str = "targets.path",
    ) -> None:
        if isinstance(target_dir, (str, Path)):
            policy = fileaccess.capture(file_access)
            self._out: OutputDir = policy.output_dir(
                FileRequest(FileRole.TARGET_OUTPUT, "write", str(target_dir), field_key)
            )
            self._target = Path(target_dir)
        else:
            self._out = target_dir
            self._target = Path(target_dir.locator)
        self._txn: OutputTxn | None = None
        self.publication_evidence = PublicationEvidence()

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
        txn = self._stage()
        component = f"{table}.parquet"
        self._replace_staged(txn, component)
        self.publication_evidence.last_operation = Operation.WRITE
        try:
            arrow_adapter.write_table(data, txn, component)
        except BaseException as exc:
            # Staged only: nothing is visible at the target until the single rename.
            capture_publication_failure(exc, self, Operation.WRITE, table=table)
            raise
        self._stored(table, txn, component)

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
        txn = self._stage()
        component = f"{table}.parquet"
        self._replace_staged(txn, component)
        evidence = self.publication_evidence
        evidence.last_operation = Operation.WRITE
        writer = arrow_adapter.parquet_writer(txn, component, schema)
        try:
            for batch in batches:
                writer.write_batch(batch)
        except BaseException as exc:
            capture_publication_failure(exc, self, Operation.WRITE, table=table)
            # Close best-effort so a close error cannot mask the original.
            try:
                writer.close()
            except Exception:
                pass
            raise
        evidence.last_operation = Operation.CLOSE
        try:
            writer.close()
        except BaseException as exc:
            capture_publication_failure(exc, self, Operation.CLOSE, table=table)
            raise
        self._stored(table, txn, component)

    def _stored(self, table: str, txn: OutputTxn, component: str) -> None:
        """Record and report the serialized size of a closed staged file (`stored_output`).

        The size comes from the staged component's own descriptor: the stage has no path under
        a strict policy, and a name-based stat would race the publish rename."""
        try:
            fd = txn.open_file(component)
        except OSError:
            return
        try:
            size = os.fstat(fd).st_size
        except OSError:
            return
        finally:
            os.close(fd)
        self.publication_evidence.stored_bytes[table] = size
        channel = get_active_channel()
        if channel.active:
            channel.publish_stored(table, size)

    @property
    def spill_parent(self) -> Path:
        """The directory a streamed run's schema hold-back may spill into: the target's
        parent, the same filesystem as the `_decoy_stage_*` staging directory. Not part of
        the `TransactionalSink` protocol; a sink without it cannot take a spill."""
        return self._target.parent

    def committed_table_path(self, table: str) -> Path:
        """Return the path `table` will be published to once commit() succeeds.

        Exposed (public, non-underscore) so a caller can check a side
        artifact's path is not aliasing a table this sink owns without
        reaching into private state -- e.g. `quarantine.guard_quarantine_
        not_aliasing_committed_table`, called by `run_sequential` after
        commit() to refuse a `quarantine.output_path` that aliases a table
        artifact this same run just published (DE-08 re-gate HIGH #1). A pure
        function of `table` and `self._target`; valid before or after commit.
        """
        self._validate_table_name(table)
        return self._target / f"{table}.parquet"

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

    def _stage(self) -> OutputTxn:
        if self._txn is None:
            # The stage lives beside the target (same filesystem) so publication is one rename.
            self._txn = self._out.begin("dir", "exclusive", label="table_sink")
            # Staged files exist and nothing has cleaned them yet.
            self.publication_evidence.cleanup = CleanupState.UNKNOWN
        return self._txn

    @staticmethod
    def _replace_staged(txn: OutputTxn, component: str) -> None:
        """Writing a table again replaces its staged file, as the path write always did."""
        try:
            txn.remove(component)
        except FileNotFoundError:
            pass

    def commit(self) -> None:
        """Atomically publish the staging directory as the target.

        A single rename makes the entire set of Parquet
        files visible at once or not at all. The target path is not created
        or touched until the rename succeeds. Exception: if no write() calls
        were made before commit(), the staging directory was never created and
        an empty target directory is created directly (no rename).

        If the target directory already exists and is non-empty, the rename
        raises OSError and nothing is published; the staging directory remains
        intact for a subsequent abort().

        Raises:
            OSError: If the rename fails (target non-empty, disk full, or
                permission denied). Nothing is published on failure; staging
                is intact for abort().
        """
        evidence = self.publication_evidence
        evidence.last_operation = Operation.COMMIT
        try:
            if self._txn is None:
                # Nothing was written; create an empty target directory directly.
                self._out.ensure_dir()
                evidence.state = PublicationState.PUBLISHED
                return
            self._txn.publish()
        except BaseException as exc:
            # The rename is atomic: a failure leaves nothing published and staging intact.
            evidence.state = PublicationState.NOT_PUBLISHED
            evidence.cleanup = CleanupState.NOT_NEEDED
            capture_publication_failure(exc, self, Operation.COMMIT)
            raise
        # Rename succeeded: staging is now the target directory; clear state.
        self._txn = None
        evidence.state = PublicationState.PUBLISHED
        evidence.cleanup = CleanupState.NOT_NEEDED

    def abort(self) -> None:
        """Remove the staging directory so nothing partial reaches the target.

        Best-effort: must not raise even if cleanup fails.
        """
        self._cleanup_staging()

    def _cleanup_staging(self) -> None:
        evidence = self.publication_evidence
        txn, self._txn = self._txn, None
        if txn is None:
            return
        # Confirm by looking where there is a path to look at (the ambient policy); a strict
        # stage has none and its discard swallows removal errors, so its state stays unknown.
        try:
            stage: str | None = txn.local_stage_path()
        except FileAccessUnavailable:
            stage = None
        try:
            txn.abort()
        except Exception:
            evidence.cleanup = CleanupState.FAILED
            return
        if stage is None:
            evidence.cleanup = CleanupState.UNKNOWN
        else:
            evidence.cleanup = (
                CleanupState.FAILED if Path(stage).exists() else CleanupState.CONFIRMED
            )
