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

Commit-time failure guarantee: if the publication is refused or the rename
fails (disk full, permissions denied, a different result already occupying the
target), nothing new is published and the staging directory remains intact for a
subsequent abort(). The target path is never touched until the single rename
succeeds.

F22: commit() delegates to `decoy_engine.publication`, which stages a complete
unit (tables + evidence core + the descriptor file), judges the destination
under a stable advisory lock, and returns a `CommitReceipt`. A verified identical
committed result is reported as `already_committed` instead of failing on the
non-empty target; any other occupant is a typed refusal.

Staging always lives in target.parent so staging and target share a
filesystem, ensuring the rename cannot fail with EXDEV.

Concurrency (F22 r5): the public API is callable from several threads on ONE sink. Every
state read and transition holds ``self._lock`` (re-entrant: a cancel hook may call back into
the sink); the lock is never held across long I/O. Work that is slow instead runs in a named
in-flight state (WRITING, COMMITTING) that the table below makes every other call respect.
The flags in the code are the state: OPEN has none set; WRITING is ``_writes > 0``;
COMMITTING is ``_committing``; SEALED is ``_sealed`` or a receipt; UNCERTAIN is
``_uncertain``; REFUSED is ``_reason``; FAILED is ``_failed``; ABORTED is ``_aborted``.

  state       write/sidecar   record_validation  commit                  abort
  OPEN        ok              ok                 -> COMMITTING           -> ABORTED
  WRITING     ok              ok                 refuse write_in_progress  deferred to idle
  COMMITTING  refuse          refuse             refuse commit_in_progress deferred to resolution
  SEALED      refuse          refuse             same receipt            no-op
  UNCERTAIN   refuse          refuse             refuse (reconcile)      no-op
  REFUSED     refuse          refuse             refuse attempt_rejected -> ABORTED
  FAILED      refuse          refuse             refuse attempt_invalid  -> ABORTED
  ABORTED     refuse          refuse             refuse attempt_aborted  no-op

A deferred abort runs when the in-flight work settles, and only if that work did not bind a
result: a commit that published or went uncertain leaves the sink SEALED/UNCERTAIN, so an
abort that raced the bind can never poison a later ``reconcile()``. ``lookup_committed`` and
``reconcile`` are refused while COMMITTING (``commit_in_progress``). Refusals for busy state
are not latched: the in-flight call still decides the attempt.

See GNU Coreutils mv(1), POSIX.1-2008 rename(2), and Python os.replace docs.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine import publication as _pub
from decoy_engine.publication import _protocol as _proto
from decoy_engine.publication import _retention as _ret


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

    def commit(self) -> object:
        """Finalize all written tables; called only on a successful run.

        The reference sink returns a `CommitReceipt`; legacy sinks return None."""
        ...

    def abort(self) -> None:
        """Discard all written tables; called on any exception during the run.

        Must be best-effort and must not raise.
        """
        ...


class _CallableSinkAdapter:
    protocol_declaration = _proto.NON_TRANSACTIONAL

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


class _ParquetStager:
    """Shared staging half of the Parquet sinks: validated table names, one private
    sibling staging directory, and a terminal failed-write latch.

    Staging lives in target.parent so staging and target share a filesystem and the
    rename can never be an EXDEV copy. The staging directory name starts with
    ``_decoy_stage_`` so orphans are identifiable.
    """

    # Capability marker for bounded generation: write_batches appends one row group per batch and
    # never materializes the stream. The callable adapter lacks it, so it cannot take a streamed run.
    bounded_write_batches = True

    def __init__(self, target_dir: Path) -> None:
        self._target = target_dir
        self._lock = threading.RLock()
        self._staging: Path | None = None
        self._rows: dict[str, int] = {}
        self._failed = False
        self._sealed = False
        self._aborted = False
        self._uncertain = False
        self._writes = 0  # in-flight write()/write_batches()/stage_sidecar() calls
        self._committing = False
        self._abort_requested = False
        self._reason: str | None = None  # set by a refusal; never cleared
        self.receipt: _pub.CommitReceipt | None = None
        self.abort_report: _ret.AbortReport | None = None

    def _operation_id(self) -> str:
        return ""

    def _busy(self, reason: str) -> _pub.PublicationRefusedError:
        """A refusal for a call that raced in-flight work; it never latches the attempt."""
        return _pub.PublicationRefusedError(
            f"the sink is busy ({reason})",
            _pub.CommitReceipt("rejected", self._operation_id(), reason),
        )

    def _require_open(self) -> None:
        """Writes, sidecars, validation changes and the child seal are legal only while the
        attempt is OPEN. The machine is OPEN -> SEALED | REFUSED | FAILED | ABORTED, one way."""
        with self._lock:
            if (
                self._failed
                or self._sealed
                or self._aborted
                or self._committing
                or self._reason is not None
                or self.receipt is not None
            ):
                raise _pub.PublicationRefusedError(
                    "sink is no longer open (a write failed, it was refused, aborted, or sealed)"
                )

    @contextlib.contextmanager
    def _writing(self) -> Iterator[None]:
        """Mark a write in flight so commit refuses and abort defers until it settles."""
        with self._lock:
            self._require_open()
            self._writes += 1
        try:
            yield
        finally:
            with self._lock:
                self._writes -= 1
            self._settle()

    def _mark_failed(self) -> None:
        with self._lock:
            self._failed = True

    def _begin_commit(self) -> None:
        """OPEN -> COMMITTING (caller holds the lock). Busy callers are refused, not latched."""
        if self._committing:
            raise self._busy("commit_in_progress")
        if self._writes:
            raise self._busy("write_in_progress")
        self._committing = True

    def _end_commit(self) -> None:
        with self._lock:
            self._committing = False
        self._settle()

    def write(self, table: str, data: pa.Table) -> None:
        """Stage one masked table as Parquet in the private staging directory.

        A failed write permanently invalidates this attempt: a caller that catches the
        error cannot later commit a readable but truncated prefix.

        Raises:
            ValueError: If ``table`` is not a single valid path component.
            OSError: If the staging directory or file cannot be written.
            pa.lib.ArrowInvalid: If ``data`` cannot be serialized as Parquet.
        """
        with self._writing():
            self._validate_table_name(table)
            dest = self._stage_dest(table)
            try:
                pq.write_table(data, dest)
            except BaseException:
                self._mark_failed()
                raise
            with self._lock:
                self._rows[table] = data.num_rows

    def write_batches(
        self, table: str, batches: Iterable[pa.RecordBatch], *, schema: pa.Schema
    ) -> None:
        """Stream-append one masked table as record batches via ParquetWriter.

        Each batch becomes one Parquet row group (callers own row-group sizing); an empty
        iterable still produces a valid zero-row file. Follows the
        pyarrow.parquet.ParquetWriter incremental-write pattern. If the batch iterator
        raises, the in-flight exception is preserved (a close() failure while unwinding is
        suppressed), the attempt is permanently invalidated, and the partial file exists
        only under staging, which abort() removes wholesale.

        Raises:
            ValueError: If ``table`` is not a single valid path component.
            OSError: If the staging directory or file cannot be created or written.
            pa.lib.ArrowInvalid: If a batch does not match ``schema``.
        """
        with self._writing():
            self._validate_table_name(table)
            dest = self._stage_dest(table)
            rows = 0
            try:
                writer = pq.ParquetWriter(dest, schema)
            except BaseException:
                self._mark_failed()
                raise
            try:
                for batch in batches:
                    writer.write_batch(batch)
                    rows += batch.num_rows
            except BaseException:
                self._mark_failed()
                try:
                    writer.close()
                except Exception:
                    pass
                raise
            try:
                writer.close()
            except BaseException:
                self._mark_failed()
                raise
            with self._lock:
                self._rows[table] = rows

    @property
    def row_counts(self) -> dict[str, int]:
        """Rows the producer handed this sink per table (reconciled against footers)."""
        with self._lock:
            return dict(self._rows)

    @property
    def spill_parent(self) -> Path:
        """The directory a streamed run's schema hold-back may spill into: the target's
        parent, the same filesystem as the `_decoy_stage_*` staging directory. Not part of
        the `TransactionalSink` protocol; a sink without it cannot take a spill."""
        return self._target.parent

    def _validate_table_name(self, table: str) -> None:
        if (
            not table
            or os.sep in table
            or (os.altsep is not None and os.altsep in table)
            or ".." in table
            or table != Path(table).name
            or table.startswith(".")
        ):
            raise ValueError(
                f"table name {table!r} is not a single path component; "
                "must not contain path separators or '..'"
            )

    def _stage_dest(self, table: str) -> Path:
        return self._ensure_staging() / f"{table}.parquet"

    def _ensure_staging(self) -> Path:
        # One mkdir + mkdtemp under the lock (short metadata work): two first writers must not
        # each create a directory and orphan one.
        with self._lock:
            if self._staging is None:
                self._before_first_stage()
                parent = self._staging_parent()
                parent.mkdir(parents=True, exist_ok=True)
                self._staging = Path(tempfile.mkdtemp(prefix="_decoy_stage_", dir=parent))
            return self._staging

    def _staging_parent(self) -> Path:
        return self._target.parent

    def _before_first_stage(self) -> None:
        """Hook: admission checks that must refuse before any staging byte is written."""

    def abort(self) -> None:
        """Remove the staging directory so nothing partial reaches the target.

        Best-effort: must not raise even if cleanup fails. A no-op once the staging
        directory has been renamed into place (it no longer exists at the staged path).
        The typed outcome is left in ``abort_report``; an aborted sink can never commit.
        With a write or commit in flight the abort is deferred and runs when it settles,
        unless that work bound a result (then the sink stays SEALED/UNCERTAIN).
        """
        with self._lock:
            self._abort_requested = True
        self._settle()

    def _settle(self) -> None:
        """Run a deferred abort once nothing is in flight."""
        with self._lock:
            if not self._abort_requested or self._committing or self._writes:
                return
            self._abort_requested = False
            if self.receipt is not None or self._uncertain:
                return  # a committed or possibly-committed attempt stays SEALED
            self._aborted = True
            staging, self._staging = self._staging, None
        if staging is None:
            return
        try:
            report = self._dispose_staging(staging)
        except Exception:
            report = _ret.AbortReport("failed", "cleanup_error", resource=staging)
        with self._lock:
            self.abort_report = report

    def _dispose_staging(self, staging: Path) -> _ret.AbortReport:
        return _ret.discard(staging, "aborted")


class ParquetPreparationSink(_ParquetStager):
    protocol_declaration = _proto.PREPARATION_ONLY

    """Child-side sink: stages and seals a directory but never publishes a result.

    `commit()` renames the private staging directory to its (private, intermediate)
    target and returns a `ChildPreparationEnvelope`. No descriptor is written, so
    nothing here can be mistaken for a committed output; only the parent's publication
    backend re-measures and promotes the directory.
    """

    def commit(self) -> _pub.ChildPreparationEnvelope:
        with self._lock:
            self._require_open()
            self._begin_commit()
            staging = self._staging
        try:
            if staging is None:
                self._target.mkdir(parents=True, exist_ok=True)
            else:
                self._target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, self._target)
            with self._lock:
                self._staging = None
                self._sealed = True
                rows = dict(self._rows)
        finally:
            self._end_commit()
        return _pub.ChildPreparationEnvelope(
            staging_dir=self._target,
            tables=tuple(sorted(rows)),
            row_counts=tuple(sorted(rows.items())),
        )


class _PublishingSink(_ParquetStager):
    """Shared F22 publication half: request, validation binding, idempotent commit.

    Tables are staged by write()/write_batches(); commit() prepares the unit (inventory,
    evidence core, descriptor), judges the destination under a stable lock, and binds
    the staging directory to the target with a single ``os.replace``. Either every file
    lands at once or nothing is published; an identical committed result is reported as
    ``already_committed``.

    Args:
        target_dir: Directory where the unit is published.
        request: Fixed-before-first-write publication request (operation id, expected
            tables, checks, overwrite policy). Defaults to a one-shot request with
            generated identifiers; retry callers must pass a stable ``operation_id``.
    """

    _unit = _pub.UNIT_IMMUTABLE_DIRECTORY
    protocol_declaration = _proto.IMMUTABLE_DIRECTORY

    def __init__(self, target_dir: Path, *, request: _pub.PublicationRequest | None = None) -> None:
        super().__init__(target_dir)
        self._request = request or _pub.PublicationRequest(destination=target_dir, unit=self._unit)
        if self._request.destination != target_dir:
            raise ValueError("request.destination must equal target_dir")
        if self._request.unit != self._unit:
            raise ValueError(
                f"{type(self).__name__} publishes {self._unit}, not {self._request.unit}"
            )

    def _operation_id(self) -> str:
        return self._request.operation_id

    def _dispose_staging(self, staging: Path) -> _ret.AbortReport:
        with self._lock:
            reason = self._reason or ("write_failed" if self._failed else "aborted")
            request = self._request
        return _ret.dispose_staging(
            staging,
            request.retention,
            operation_id=request.operation_id,
            attempt_id=request.attempt_id,
            reason=reason,
        )

    @property
    def request(self) -> _pub.PublicationRequest:
        with self._lock:
            return self._request

    @property
    def sidecars_in_unit(self) -> bool:
        return self.request.sidecars_in_unit

    def stage_sidecar(self, name: str, data: bytes) -> None:
        """Stage a required sidecar inside the unit (bound by the descriptor, mode 0600).

        Raises:
            ValueError: ``name`` is not a plain file name or would alias a table artifact.
        """
        with self._writing():
            if name.endswith(".parquet") or name != Path(name).name or name.startswith("."):
                raise ValueError(f"sidecar name {name!r} is not a plain non-table file name")
            dest = self._ensure_staging() / name
            try:
                _pub.write_private_file(dest, data)
            except BaseException:
                self._mark_failed()
                raise

    def record_validation(
        self, checks: Iterable[_pub.CheckResult], *, policy_fingerprint: str | None = None
    ) -> None:
        """Bind completed validation to the unit. A required fail/inconclusive check
        makes commit() refuse; there is no silent downgrade. Only legal while OPEN."""
        from dataclasses import replace

        with self._lock:
            self._require_open()
            self._request = replace(
                self._request,
                checks=tuple(checks),
                validation_policy_fingerprint=policy_fingerprint,
            )

    def _enter_publication(self) -> None:
        """The one entry gate for lookup, reconcile, staging and commit: admission through
        the publication layer's single authority plus the acceptance gate, latching the
        refusal so every path leaves the attempt in the same terminal state. A failed,
        aborted or refused attempt is refused here, so none of the four entry points can
        emit a receipt (cached or fresh) from a dead attempt."""
        with self._lock:
            self._terminal_guard()
            try:
                _proto.admit_request(self._request, self)
                _pub.reject_failed_checks(self._request)
            except _pub.PublicationRefusedError as exc:
                self._reason = exc.receipt.reason_code if exc.receipt else "refused"
                raise

    def _terminal_guard(self) -> None:
        for dead, reason in (
            (self._aborted, "attempt_aborted"),
            (self._failed, "attempt_invalid"),
            (self._reason is not None, "attempt_rejected"),
        ):
            if dead:
                raise _pub.PublicationRefusedError(
                    f"this attempt is terminal ({reason}) and can never publish or report",
                    _pub.CommitReceipt("rejected", self._request.operation_id, reason),
                )

    def _before_first_stage(self) -> None:
        self._enter_publication()
        _pub.validate_request(self.request)

    def lookup_committed(self) -> _pub.CommitReceipt | None:
        """Receipt for an already committed result this request names, else None."""
        return self._recovery_read(_pub.lookup_committed)

    def reconcile(self) -> _pub.CommitReceipt | None:
        """Resolve a `commit_uncertain` outcome to the authoritative committed state."""
        return self._recovery_read(_pub.reconcile)

    def _recovery_read(
        self, op: Callable[[_pub.PublicationRequest], _pub.CommitReceipt | None]
    ) -> _pub.CommitReceipt | None:
        with self._lock:
            self._enter_publication()
            if self._committing:
                raise self._busy("commit_in_progress")
            request = self._request
        return self._latched(op, request)

    def _latched(
        self,
        op: Callable[[_pub.PublicationRequest], _pub.CommitReceipt | None],
        request: _pub.PublicationRequest,
    ) -> _pub.CommitReceipt | None:
        try:
            return op(request)
        except _pub.PublicationRefusedError as exc:
            with self._lock:
                self._reason = exc.receipt.reason_code if exc.receipt else "refused"
            raise

    def commit(self) -> _pub.CommitReceipt:
        """Publish the staged unit and return the receipt.

        Raises:
            PublicationRefusedError: conflict / unsupported / rejected / cancelled; nothing new
                is bound and staging is intact for abort(). Also raised, without latching the
                attempt, while another commit or a write is in flight.
            CommitUncertainError: the binding may be visible but sync/reread failed; the
                possible committed generation is preserved, never deleted.
        """
        with self._lock:
            self._enter_publication()  # admission + acceptance + terminal latch, ALWAYS first
            if self.receipt is not None:
                return self.receipt
            if self._uncertain:
                raise _pub.PublicationRefusedError(
                    "the commit is uncertain: reconcile() instead of committing again",
                    _pub.CommitReceipt("rejected", self._request.operation_id, "commit_uncertain"),
                )
            self._begin_commit()
            self._sealed = True  # every resolution below is a decision; nothing may mutate after
            request, staged, rows = self._request, self._staging, dict(self._rows)
        try:
            if staged is None and request.expected_result_id is not None:
                found = self._latched(_pub.lookup_committed, request)
                if found is not None:
                    with self._lock:
                        self.receipt = found
                    return found
            stage = self._ensure_staging()
            try:
                prepared = _pub.prepare_directory(stage, request, producer_rows=rows)
                receipt = _pub.commit_prepared(prepared)
            except _pub.CommitUncertainError:
                with self._lock:
                    self._staging = None  # renamed into place: never delete a possible commit
                    self._uncertain = True
                raise
            except _pub.PublicationRefusedError as exc:
                with self._lock:
                    self._reason = exc.receipt.reason_code if exc.receipt else "refused"
                raise
            if receipt.outcome == "already_committed":
                receipt = _pub.with_cleanup(receipt, _pub.cleanup_staging(stage))
            with self._lock:
                self._staging = None
                self.receipt = receipt
            return receipt
        finally:
            self._end_commit()  # clears COMMITTING, then runs any abort that raced it


class ParquetTransactionalSink(_PublishingSink):
    """`immutable_directory/v1`: tables land as ``<target>/<table>.parquet`` in one rename.

    The default Parquet unit; a different committed result is never replaced here (use
    `ManagedDatasetSink` for replacement)."""

    def committed_table_path(self, table: str) -> Path:
        """Return the path `table` will be published to once commit() succeeds.

        Public so a caller can check a side artifact's path is not aliasing a table
        this sink owns (`quarantine.guard_quarantine_not_aliasing_committed_table`). A
        pure function of `table` and the target; valid before or after commit.
        """
        self._validate_table_name(table)
        return self._target / f"{table}.parquet"


class ManagedDatasetSink(_PublishingSink):
    """`managed_dataset/v1`: immutable generations under ``root`` bound by a ``CURRENT`` pointer.

    Supports typed replacement (``Replace`` with an expected predecessor and optional
    protected backup). Readers must resolve the current generation once with
    ``decoy_engine.publication.resolve_current``; a bare directory listing of the root is
    not the reader protocol, so this sink exposes no ``committed_table_path``.
    """

    _unit = _pub.UNIT_MANAGED_DATASET
    protocol_declaration = _proto.MANAGED_DATASET

    def _staging_parent(self) -> Path:
        return self._target / ".staging"

    @property
    def spill_parent(self) -> Path:
        return self._target / ".staging"


def publish_tables(
    outputs: Mapping[str, pa.Table], request: _pub.PublicationRequest
) -> _pub.CommitReceipt:
    """Publish already-resident tables through the same contract as a streamed run.

    A resident run publishes nothing by itself (no sink session opens); a caller that
    wants the output committed passes it here and gets the same descriptor and receipt.
    """
    sink_cls = (
        ManagedDatasetSink
        if request.unit == _pub.UNIT_MANAGED_DATASET
        else ParquetTransactionalSink
    )
    sink = sink_cls(request.destination, request=request)
    try:
        for name, table in outputs.items():
            sink.write(name, table)
        return sink.commit()
    except BaseException:
        sink.abort()
        raise
