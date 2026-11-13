"""``publish_unit``: judge and bind one ``immutable_directory/v1`` unit under the destination lock.

Pattern: Delta Lake idempotent transaction identifiers under an exclusive destination lock (Delta Lake protocol; PostgreSQL SyncDataDirectory). See: https://github.com/delta-io/delta/blob/master/PROTOCOL.md#transaction-identifiers.

One exclusive lock on the destination is held from reading what is there to binding (never an
``exists()`` check and then a write): the bound descriptor is read, this attempt's staged tables
are measured, the two are judged, and only a bind verdict writes the descriptor and renames the
stage. A retry is recognized by its measured ``result_id`` (Delta Lake records a transaction
identifier and treats a repeat as already committed; here equality of content decides and an
``operation_id`` alone never does). State found on disk is fsynced along its whole chain before
it is reported, as PostgreSQL's ``SyncDataDirectory`` does at startup (F22 rule D, D2).

Every exit is a ``CommitReceipt`` (plan 4.1): only a bind can leave anything new at the
destination, and every other exit aborts the stage. Receipts hold closed tokens, digests, counts
and errno names; an exception text is never read. Contract: ``docs/publication.md``.
"""

from __future__ import annotations

import errno
import hashlib
import mmap
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine import reasons
from decoy_engine.fileaccess._types import (
    CommitUncertain,
    FileAccessDenied,
    LeafTooLarge,
    LockTimeout,
    LockUnavailable,
)
from decoy_engine.publication._descriptor import (
    DESCRIPTOR_NAME,
    MAX_DESCRIPTOR_BYTES,
    Artifact,
    Descriptor,
    build_descriptor,
    decode_descriptor,
    descriptor_digest,
    encode_descriptor,
)
from decoy_engine.publication._types import CommitReceipt, Outcome, PublicationRequest

if TYPE_CHECKING:
    from decoy_engine.fileaccess import OutputDir, OutputTxn
    from decoy_engine.reasons import ReasonCode

__all__ = ["publish_tables", "publish_unit"]

_OCCUPIED = reasons.decoy.PUBLICATION_TARGET_OCCUPIED


def _after_judge() -> None:
    """Test hook: runs under the lock after a bind verdict, before any bind I/O (plan 7.1)."""


@dataclass(frozen=True)
class _Stored:
    """What the destination holds: a valid descriptor, nothing, or something else."""

    state: Literal["empty", "descriptor", "occupied"]
    descriptor: Descriptor | None = None
    digest: str | None = None
    os_error: str | None = None


@dataclass(frozen=True)
class _Measured:
    descriptor: Descriptor
    data: bytes


def _errno_name(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno or errno.EIO, "EIO")


def _receipt(
    request: PublicationRequest,
    outcome: Outcome,
    code: ReasonCode | None,
    *,
    descriptor: Descriptor | None = None,
    digest: str | None = None,
    visible: bool = False,
    durable: bool = False,
    os_error: str | None = None,
) -> CommitReceipt:
    vault = descriptor.vault if descriptor is not None else None
    return CommitReceipt(
        outcome=outcome,
        operation_id=request.operation_id,
        reason_code=None if code is None else str(code),
        result_id=descriptor.result_id if descriptor is not None else None,
        descriptor_digest=digest,
        vault_digest=vault.sha256 if vault is not None else None,
        artifacts=tuple(a.summary() for a in descriptor.artifacts) if descriptor else (),
        visible=visible,
        durable=durable,
        os_error=os_error,
    )


def _read_stored(out: OutputDir) -> _Stored:
    """The destination's state; anything unreadable or malformed is occupied, never adopted."""
    code: str | None = None
    data: bytes | None = None
    try:
        data = out.read_member(DESCRIPTOR_NAME, limit=MAX_DESCRIPTOR_BYTES)
        if data is None:
            return _Stored("empty" if out.is_empty_or_absent() else "occupied")
    except LeafTooLarge:
        return _Stored("occupied", os_error="EFBIG")
    except OSError as exc:  # a file at the leaf, a symlink, an unreadable member
        code = _errno_name(exc)
    except FileAccessDenied:  # the strict policies refuse an unopenable leaf this way
        return _Stored("occupied")
    if data is None:
        return _Stored("occupied", os_error=code)
    refused = False
    try:
        stored = decode_descriptor(data)
    except ValueError:  # descriptor_refused:<check>: a malformed record never drives a judge
        refused = True
    if refused:
        return _Stored("occupied")
    return _Stored("descriptor", stored, descriptor_digest(data))


def _measure_one(txn: OutputTxn, component: str) -> Artifact:
    """Size, SHA-256 and Parquet footer row count of one staged file, read through the stage.

    The file is mapped read-only and hashed in one call, so a large table costs page cache, not
    a heap copy."""
    fd = txn.open_file(component)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size == 0:  # an empty file is no Parquet table
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
        with mmap.mmap(fd, 0, access=mmap.ACCESS_READ) as mapped:
            sha256 = hashlib.sha256(mapped).hexdigest()
        size = st.st_size
        with os.fdopen(os.dup(fd), "rb") as fh:
            rows = pq.ParquetFile(fh).metadata.num_rows
    finally:
        os.close(fd)
    return Artifact(path=component, role="table", rows=rows, sha256=sha256, size=size)


def _measure(txn: OutputTxn, tables: Sequence[str], request: PublicationRequest) -> _Measured:
    artifacts = [_measure_one(txn, component) for component in tables]
    descriptor = build_descriptor(
        operation_id=request.operation_id, artifacts=artifacts, vault=None
    )
    return _Measured(descriptor, encode_descriptor(descriptor))


def _check_request(request: PublicationRequest, tables: Sequence[str]) -> list[str]:
    if type(request) is not PublicationRequest:
        raise TypeError("request must be a PublicationRequest")
    if request.vault is not None:  # binding without its vault would break rule V
        raise NotImplementedError("a vault-bound publication needs vault ordering")
    names = list(tables)
    if any(type(n) is not str for n in names) or len(set(names)) != len(names):
        raise ValueError("invalid tables")
    if DESCRIPTOR_NAME in names:
        raise ValueError("invalid tables")
    return names


def publish_unit(
    out: OutputDir,
    txn: OutputTxn,
    request: PublicationRequest,
    *,
    tables: Sequence[str] = (),
) -> CommitReceipt:
    """Bind the staged unit in ``txn`` (a ``publication`` directory stage of ``out``) whose
    files are exactly the Parquet tables named in ``tables``, or judge it against what the
    destination already holds. The stage is aborted on every exit but a bind."""
    try:
        names = _check_request(request, tables)
        return _locked(out, txn, request, names)
    finally:
        txn.abort()  # a no-op after publish


def _locked(
    out: OutputDir, txn: OutputTxn, request: PublicationRequest, tables: list[str]
) -> CommitReceipt:
    try:
        with out.lock(request.lock_timeout_s):
            return _judge_and_bind(out, txn, request, tables)
    except LockTimeout as exc:
        return _receipt(
            request, "refused", reasons.decoy.PUBLICATION_LOCK_TIMEOUT, os_error=exc.errno_name
        )
    except LockUnavailable as exc:
        return _receipt(
            request, "refused", reasons.decoy.PUBLICATION_LOCK_UNAVAILABLE, os_error=exc.errno_name
        )


def _judge_and_bind(
    out: OutputDir, txn: OutputTxn, request: PublicationRequest, tables: list[str]
) -> CommitReceipt:
    stored = _read_stored(out)
    failure: str | None = None
    try:
        new = _measure(txn, tables, request)
    except (OSError, pa.ArrowException) as exc:  # unreadable or not a Parquet table
        failure = _errno_name(exc) if isinstance(exc, OSError) else None
    else:
        return _judge(out, txn, request, stored, new)
    return _receipt(request, "failed", reasons.decoy.PUBLICATION_FAILED, os_error=failure)


def _judge(
    out: OutputDir,
    txn: OutputTxn,
    request: PublicationRequest,
    stored: _Stored,
    new: _Measured,
) -> CommitReceipt:
    mine = new.descriptor
    if stored.state == "occupied":
        return _receipt(request, "refused", _OCCUPIED, descriptor=mine, os_error=stored.os_error)
    bound = stored.descriptor
    if bound is not None:
        if bound.result_id == mine.result_id:
            return _already(out, request, bound, stored.digest)
        if bound.operation_id == request.operation_id:
            return _receipt(
                request,
                "operation_conflict",
                reasons.decoy.PUBLICATION_OPERATION_CONFLICT,
                descriptor=mine,
            )
        return _receipt(request, "refused", _OCCUPIED, descriptor=mine)
    _after_judge()
    return _bind(txn, request, new)


def _already(
    out: OutputDir, request: PublicationRequest, bound: Descriptor, digest: str | None
) -> CommitReceipt:
    if bound.vault is not None:  # this request carries no vault (a3), so the units differ
        return _receipt(
            request, "refused", reasons.decoy.PUBLICATION_VAULT_MISMATCH, descriptor=bound
        )
    code: str | None = None
    try:
        out.sync_chain()  # D2: the unit tree and its chain to the anchor, before the report
    except OSError as exc:
        code = _errno_name(exc)
    if code is not None:
        return _receipt(
            request,
            "commit_uncertain",
            reasons.decoy.PUBLICATION_COMMIT_UNCERTAIN,
            descriptor=bound,
            digest=digest,
            visible=True,
            os_error=code,
        )
    return _receipt(
        request,
        "already_committed",
        None,
        descriptor=bound,
        digest=digest,
        visible=True,
        durable=True,
    )


def _bind(txn: OutputTxn, request: PublicationRequest, new: _Measured) -> CommitReceipt:
    mine, digest = new.descriptor, descriptor_digest(new.data)
    code: str | None = None
    uncertain = False
    try:
        fd = txn.create_file(DESCRIPTOR_NAME)
        with os.fdopen(fd, "wb") as fh:
            fh.write(new.data)
        txn.publish()  # 5.1: stage fsync walk, rename, then the chain to the anchor
    except CommitUncertain as exc:
        code, uncertain = exc.errno_name, True
    except OSError as exc:
        code = _errno_name(exc)
    if uncertain:
        return _receipt(
            request,
            "commit_uncertain",
            reasons.decoy.PUBLICATION_COMMIT_UNCERTAIN,
            descriptor=mine,
            digest=digest,
            visible=True,
            os_error=code,
        )
    if code is not None:
        return _receipt(
            request, "failed", reasons.decoy.PUBLICATION_FAILED, descriptor=mine, os_error=code
        )
    return _receipt(
        request, "published", None, descriptor=mine, digest=digest, visible=True, durable=True
    )


def publish_tables(
    out: OutputDir, tables: Mapping[str, pa.Table], request: PublicationRequest
) -> CommitReceipt:
    """The resident entry: stage each table as Parquet into a ``publication`` stage of ``out``
    under its name, then ``publish_unit``. A staging failure aborts the stage."""
    from decoy_engine.fileaccess.adapters import arrow as arrow_adapter

    names = _check_request(request, list(tables))
    failure: str | None = None
    try:
        txn = out.begin("dir", "exclusive", label="publication")
    except OSError as exc:
        failure = _errno_name(exc)
    else:
        try:
            for name in names:
                arrow_adapter.write_table(tables[name], txn, name)
        except OSError as exc:
            failure = _errno_name(exc)
            txn.abort()
        except BaseException:
            txn.abort()
            raise
        else:
            return publish_unit(out, txn, request, tables=names)
    return _receipt(request, "failed", reasons.decoy.PUBLICATION_FAILED, os_error=failure)
