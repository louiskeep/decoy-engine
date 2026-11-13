"""Transactional sink protocol and reference file-based implementation.

A TransactionalSink lets run_sequential publish tables all-or-nothing: each
table is written during the run, and either committed (atomically on success)
or aborted (everything discarded) on any exception.

The file-based reference implementation (ParquetTransactionalSink) stages
Parquet files in a private `publication` stage beside the target and commits
through `publish_unit` (F22): under the destination lock the staged unit is
measured, judged against what the target already holds, and bound by one
durable directory rename (POSIX rename(2), atomic within one filesystem), with
the `_decoy_publication.json` descriptor inside. Either every file lands at
once or nothing is published; a target that holds a different result is never
replaced, and a retry of the same result is `already_committed`. On abort(),
the stage is removed before any data reaches the target.

See POSIX.1-2008 rename(2) and `docs/publication.md`.

The sink is two parts: `StagedParquetWriter`, the write half that only stages Parquet files, and
the sink itself, which owns the stage and moves every call through the publication sink state
table (`decoy_engine.publication._sink_state`).
"""

from __future__ import annotations

import errno
import os
import uuid
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

import pyarrow as pa

from decoy_engine import fileaccess, reasons
from decoy_engine.execution._publication_observer import (
    PublicationEvidence,
    capture_publication_failure,
    project_sink_state,
)
from decoy_engine.fileaccess import (
    FileAccess,
    FileAccessDenied,
    FileAccessUnavailable,
    FileRequest,
    FileRole,
    LeafTooLarge,
    OutputDir,
    OutputTxn,
)
from decoy_engine.fileaccess.adapters import arrow as arrow_adapter
from decoy_engine.progress import get_active_channel
from decoy_engine.publication import (
    DESCRIPTOR_NAME,
    MAX_DESCRIPTOR_BYTES,
    CommitReceipt,
    PublicationRequest,
    decode_descriptor,
    descriptor_digest,
    publish_unit,
    raise_for_outcome,
)
from decoy_engine.publication._sink_state import Act, Exit, SinkState, SinkStateMachine
from decoy_engine.reasons import CleanupState, Operation, PublicationState

if TYPE_CHECKING:
    from decoy_engine.reasons import ReasonCode


# NOTE: isinstance(obj, TransactionalSink) matches the four members by name only (runtime
# Protocol), so a pre-SC1 write/commit/abort-only sink does not match; see
# `_sequential.run_sequential`'s dispatch comment. Both shipped implementations have all four.
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

    def commit(self) -> object:
        """Finalize all written tables; called only on a successful run. The reference sink
        returns its `CommitReceipt`; a caller that needs none ignores the value."""
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


def _validate_table_name(table: str) -> None:
    """A table name is one path component: no separator, no ``..``."""
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


class StagedParquetWriter:
    """The write half of the reference sink: stages masked tables as Parquet files in an output
    stage and never publishes, locks or names a descriptor.

    The stage comes from ``stage()``, called once per write, so the owner decides when it is
    begun and who publishes it. Each table is ``<table>.parquet`` directly in the stage; writing
    a table again replaces its staged file. Failures are recorded on ``publication_evidence``
    at the boundary that failed (write or close), and the stored size of every closed file is
    recorded and reported on the progress channel (``stored_output``).
    """

    def __init__(
        self, stage: Callable[[], OutputTxn], evidence: PublicationEvidence | None = None
    ) -> None:
        self._stage = stage
        self.publication_evidence = evidence if evidence is not None else PublicationEvidence()
        # What this writer has staged, so a publisher measures exactly these files.
        self.staged: dict[str, None] = {}

    def write(self, table: str, data: pa.Table) -> None:
        """Stage one masked table as Parquet.

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
        _validate_table_name(table)
        txn = self._stage()
        component = f"{table}.parquet"
        _replace_staged(txn, component, self.staged)
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
        arrives, in the same stage and under the same table-name validation
        as write(). Follows the pyarrow.parquet.ParquetWriter incremental-write
        pattern (Apache Arrow docs: "Reading and Writing Single Files").

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
        exists only in the stage, which the owner's abort removes wholesale.
        On the success path a close() failure (for example a footer flush
        hitting ENOSPC) is a real write error and does surface.
        """
        _validate_table_name(table)
        txn = self._stage()
        component = f"{table}.parquet"
        _replace_staged(txn, component, self.staged)
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
        """Record a closed staged file and report its serialized size (`stored_output`).

        The size comes from the staged component's own descriptor: the stage has no path under
        a strict policy, and a name-based stat would race the publish rename."""
        self.staged[component] = None
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


def _replace_staged(txn: OutputTxn, component: str, staged: dict[str, None]) -> None:
    """Writing a table again replaces its staged file, as the path write always did."""
    staged.pop(component, None)
    try:
        txn.remove(component)
    except FileNotFoundError:
        pass


class ParquetTransactionalSink:
    """File-based transactional sink that commits through `publish_unit` (F22).

    On write(), each masked table is serialized as Parquet into a private `publication` stage
    beside the target by the sink's write half, `StagedParquetWriter`. On commit(), the stage
    and exactly the tables it staged go to `publish_unit`, which judges them against the target
    under its lock and binds the unit, descriptor included, with one durable rename. On abort(),
    the stage is removed so nothing partial reaches the target.

    commit() returns the `CommitReceipt` (also kept on `receipt`) or raises its typed error.

    The sink holds the output capability it was built with for every later call, so a sink
    built under one policy is judged by that policy however it is driven afterwards.

    Every call moves through the sink state table (`publication/_sink_state.py`, F22 plan 5.6)
    under one lock, with the I/O outside it: after a commit the sink is sealed (a second commit
    returns the same receipt, a write is refused `publication_attempt_sealed`); a call that
    arrives while another is in flight is refused busy; an abort that arrives in flight is
    applied when that call ends; after a failure or an abort every call but abort is refused.
    An uncertain sink is sealed by `reconcile()` once the bound unit is its own result and its
    chain is fsynced (D4). The state is projected onto `publication_evidence` (F29).

    Args:
        target_dir: Directory where committed Parquet files are published via atomic rename
            from the staging directory, or an `OutputDir` the policy already issued for it.
        file_access: The policy that decides where output may go (default: ambient CLI).
        field_key: The config position named in a denial.
        request: The publication request; a retry passes the same `operation_id`. Without one
            the sink is its own attempt (a fresh uuid4 `operation_id`). A request carrying a
            vault is refused: an engine sink never binds a vault (B-6 rule B0).
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
        request: PublicationRequest | None = None,
    ) -> None:
        if request is None:
            request = PublicationRequest(uuid.uuid4().hex)
        elif type(request) is not PublicationRequest or request.vault is not None:
            raise ValueError("a sink request must be a PublicationRequest without a vault")
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
        self._stage_dir: str | None = None  # a path to look at, where the policy has one
        self._request = request
        self.receipt: CommitReceipt | None = None
        self.publication_evidence = PublicationEvidence()
        self._writer = StagedParquetWriter(self._stage, self.publication_evidence)
        self._machine = SinkStateMachine(request.operation_id)

    @property
    def state(self) -> SinkState:
        """Where this attempt is in the sink state table."""
        return self._machine.state

    def write(self, table: str, data: pa.Table) -> None:
        """Stage one masked table as Parquet (`StagedParquetWriter.write`).

        Raises:
            ValueError: If ``table`` is not a single path component (the state is unchanged).
            PublicationRefused: If the sink is busy, sealed, failed or aborted.
            OSError, pa.lib.ArrowInvalid: As `StagedParquetWriter.write`; the attempt fails.
        """
        _validate_table_name(table)
        self._writing(lambda: self._writer.write(table, data))

    def write_batches(
        self, table: str, batches: Iterable[pa.RecordBatch], *, schema: pa.Schema
    ) -> None:
        """Stream-append one masked table (`StagedParquetWriter.write_batches`); the same
        state rules as write(), and the same atomic commit/abort contract."""
        _validate_table_name(table)
        self._writing(lambda: self._writer.write_batches(table, batches, schema=schema))

    def _writing(self, write: Callable[[], None]) -> None:
        self._machine.arrive("write")
        try:
            write()
        except BaseException:
            self._leave(Exit.ERROR)
            raise
        self._leave(Exit.OK)

    def _leave(self, exit_: Exit, receipt: CommitReceipt | None = None) -> None:
        code = receipt.reason_code if receipt is not None else None
        aborted = self._machine.leave(exit_, result=receipt, code=_code(code))
        if aborted:  # a pending abort applied on this exit
            self._cleanup_staging()
        project_sink_state(self.publication_evidence, self._machine.state)

    @property
    def spill_parent(self) -> Path:
        """The directory a streamed run's schema hold-back may spill into: the target's
        parent, the same filesystem as the publication stage. Not part of the
        `TransactionalSink` protocol; a sink without it cannot take a spill."""
        return self._target.parent

    def committed_table_path(self, table: str) -> Path:
        """Return the path `table` will be published to once commit() succeeds, so a caller
        (`quarantine.guard_quarantine_not_aliasing_committed_table`, DE-08) can refuse a side
        artifact aliasing a table this sink owns. A pure function of `table` and the target."""
        _validate_table_name(table)
        return self._target / f"{table}.parquet"

    def staged_tables(self) -> list[str]:
        """Every file this sink staged: the one source of what its commit binds and describes."""
        return list(self._writer.staged)

    def _stage(self) -> OutputTxn:
        if self._txn is None:
            # The stage lives beside the target (same filesystem) so publication is one rename.
            self._txn = self._out.begin("dir", "exclusive", label="publication")
            self._stage_dir = _stage_path(self._txn)
            # Staged files exist and nothing has cleaned them yet.
            self.publication_evidence.cleanup = CleanupState.UNKNOWN
        return self._txn

    def commit(self) -> CommitReceipt:
        """Publish the staged tables as one unit through `publish_unit`; return the receipt
        for `published` and `already_committed` (a restart of the same result).

        With no write() the unit holds only its descriptor, so an empty commit is a unit too
        and is never silently replaced. A sealed sink returns its receipt with no I/O.

        Raises:
            PublicationRefused: The target holds a different result (`refused`,
                `operation_conflict`), or the sink is busy, failed or aborted.
            PublicationFailed: Staging, measuring or the rename failed; nothing is bound.
            CommitUncertain: The unit is bound but its chain fsync failed (`reconcile()`).
            OSError: The stage could not be begun.
        """
        arrival = self._machine.arrive("commit")
        if arrival.act is Act.RESULT:
            return arrival.result  # type: ignore[return-value]
        evidence = self.publication_evidence
        evidence.last_operation = Operation.COMMIT
        try:
            tables = self.staged_tables()
            receipt = publish_unit(self._out, self._stage(), self._request, tables=tables)
        except BaseException as exc:
            self._cleanup_staging()  # `publish_unit` aborted it; confirm where we can look
            capture_publication_failure(exc, self, Operation.COMMIT)
            self._leave(Exit.ERROR)
            raise
        self.receipt = receipt
        if receipt.outcome in _BOUND:
            self._txn = None  # renamed into the unit, or a bound result made it redundant
        else:
            self._cleanup_staging()
        self._leave(_EXIT[receipt.outcome], receipt)
        return _raise_observed(self, receipt, Operation.COMMIT)

    def reconcile(self) -> CommitReceipt | None:
        """Seal an uncertain attempt once its result is bound and durable (plan 5.6, D4).

        OPEN returns `None` (a matching `operation_id` alone is never adopted). UNCERTAIN reads
        the bound descriptor: this sink's `result_id` there with the unit tree and chain fsynced
        seals it (`already_committed`); an fsync failure stays uncertain; an absent or different
        unit is `publication_commit_not_visible`. No lock: a bound unit is never replaced.
        """
        arrival = self._machine.arrive("reconcile")
        if arrival.act is Act.RESULT:
            return arrival.result  # type: ignore[return-value]
        if arrival.act is Act.NOTHING:
            return None
        prior = cast(CommitReceipt, self.receipt)  # UNCERTAIN is only reached with one
        try:
            exit_, receipt = _recover(self._out, prior)
        except BaseException:
            self._leave(Exit.FOUND_NOT_DURABLE, prior)
            raise
        self.receipt = receipt
        self._leave(exit_, receipt)
        return _raise_observed(self, receipt, Operation.COMMIT)

    def abort(self) -> None:
        """Remove the staging directory so nothing partial reaches the target.

        Best-effort: must not raise even if cleanup fails. While a write or commit is in
        flight the abort is recorded and applied when that call ends.
        """
        if self._machine.arrive("abort").act is Act.ABORT:
            self._cleanup_staging()
            project_sink_state(self.publication_evidence, self._machine.state)

    def _cleanup_staging(self) -> None:
        evidence = self.publication_evidence
        txn, self._txn = self._txn, None
        if txn is None:
            return
        # Confirm by looking where there is a path to look at (the ambient policy). A strict
        # stage has none and its discard swallows removal errors: the discard ran but cannot be
        # confirmed, which is F29's `attempted`. Abort is a no-op on a finished stage.
        try:
            txn.abort()
        except Exception:
            evidence.cleanup = CleanupState.FAILED
            return
        if self._stage_dir is None:
            evidence.cleanup = CleanupState.ATTEMPTED
        else:
            failed = Path(self._stage_dir).exists()
            evidence.cleanup = CleanupState.FAILED if failed else CleanupState.CONFIRMED


_BOUND = frozenset({"published", "already_committed", "commit_uncertain"})
_EXIT = {
    "published": Exit.PUBLISHED,
    "already_committed": Exit.ALREADY_COMMITTED,
    "operation_conflict": Exit.OPERATION_CONFLICT,
    "refused": Exit.REFUSED,
    "commit_uncertain": Exit.UNCERTAIN,
    "failed": Exit.ERROR,
}


def _code(code: str | None) -> ReasonCode | None:
    """A receipt's code as its registry symbol (a receipt holds registered codes only)."""
    spec = reasons.find("decoy", code)
    return None if spec is None else getattr(reasons.decoy, spec.symbol)


def _stage_path(txn: OutputTxn) -> str | None:
    try:
        return txn.local_stage_path()
    except FileAccessUnavailable:
        return None


def _raise_observed(sink: Any, receipt: CommitReceipt, operation: Operation) -> CommitReceipt:
    """The receipt for a sealed outcome; otherwise its typed error, recorded with the sink's
    evidence (F29) and raised outside any handler."""
    try:
        return raise_for_outcome(receipt)
    except BaseException as exc:
        if receipt.outcome == "failed" and receipt.os_error is not None:
            # The errno alone (no path, no OS text), so F29 still sees e.g. a full disk.
            exc.__cause__ = OSError(getattr(errno, receipt.os_error), receipt.os_error)
        capture_publication_failure(exc, sink, operation)
        raise


def _recover(out: OutputDir, prior: CommitReceipt) -> tuple[Exit, CommitReceipt]:
    """RECOVERING's I/O: this result bound, then D4 (D2 only: a sink has no vault, rule B0)."""
    found = data = None
    try:
        data = out.read_member(DESCRIPTOR_NAME, limit=MAX_DESCRIPTOR_BYTES)
        found = None if data is None else decode_descriptor(data)
    except OSError as exc:  # cannot tell what is bound: still uncertain
        return Exit.FOUND_NOT_DURABLE, replace(prior, os_error=_errno_name(exc))
    except (ValueError, LeafTooLarge, FileAccessDenied):  # never this sink's descriptor
        found = None
    if found is None or found.result_id != prior.result_id:
        code = str(reasons.decoy.PUBLICATION_COMMIT_NOT_VISIBLE)
        return Exit.NOT_VISIBLE, replace(prior, outcome="refused", reason_code=code, visible=False)
    try:
        out.sync_chain()
    except OSError as exc:
        return Exit.FOUND_NOT_DURABLE, replace(prior, os_error=_errno_name(exc))
    digest = descriptor_digest(data)  # type: ignore[arg-type]
    sealed = replace(prior, outcome="already_committed", reason_code=None, os_error=None)
    return Exit.FOUND, replace(sealed, descriptor_digest=digest, visible=True, durable=True)


def _errno_name(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno or errno.EIO, "EIO")
