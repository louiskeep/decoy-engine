"""Parent-side publication and result-envelope IO for the isolated-run primitive.

Split out of `_isolated_run.py` (which owns spawn/classify orchestration) to stay under the
~600 LOC orchestration cap: filesystem and publication helpers with no subprocess lifecycle of
their own, shared by the isolated (subprocess) path and the `isolate=False` fallback so both
publish through F22's `publish_unit` with one discipline (plan 5.7).

The isolated parent opens a `publication` stage beside `output_dir` and hands its path to the
child, which stages plain Parquet files there and nothing else. The child is an untrusted
producer (class 6), so before anything is measured the parent establishes the staged set
itself: the envelope's table names must each be one path component, and the stage, listed
through the file-access policy without following links, must hold exactly those files, each a
regular file. Anything else (a child-written `_decoy_publication.json`, an extra file, a
directory, a link, a missing table) is refused with nothing bound. Each admitted entry is made
owner-only (0600, the stage 0700) before it is read, since the child's umask decided its mode
(V1-b). `publish_unit` then measures the bytes (size, SHA-256, footer rows) and writes the
descriptor; the child's word is never taken for any of them. Pattern: Git's receiving side
(`git index-pack --strict`) recomputes every object id from the received bytes and checks the
pack's contents rather than trusting the sender's claims.

The fallback stages its in-memory tables, each as `<table>.parquet`, into its own `publication`
stage and binds them with `publish_unit`: what `publish_tables` does, with the stage held here
so the cleanup of a refused stage is confirmed by looking (F29).
"""

from __future__ import annotations

import errno
import json
import os
import signal
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine import fileaccess, reasons
from decoy_engine.errors import DecoyError
from decoy_engine.execution._isolated_common import IsolatedRunResult, commit_error_text
from decoy_engine.execution._isolated_reason import commit_reason
from decoy_engine.execution._row_errors import RowErrorRecord
from decoy_engine.execution._transactional_sink import _validate_table_name
from decoy_engine.fileaccess import FileRequest, FileRole, OutputDir, OutputTxn
from decoy_engine.fileaccess._component import is_valid_component
from decoy_engine.fileaccess._local import _LocalStageDir
from decoy_engine.fileaccess._output import _call, _close
from decoy_engine.fileaccess.adapters import arrow as arrow_adapter
from decoy_engine.publication import (
    CommitReceipt,
    PublicationRequest,
    publish_unit,
    raise_for_outcome,
)
from decoy_engine.reason_adapter import reason_from_exception
from decoy_engine.reasons import (
    CleanupState,
    Operation,
    Phase,
    PublicationState,
    ReasonCode,
    ReasonContext,
)

__all__ = [
    "COMMITTED",
    "CommitError",
    "Publication",
    "begin_publication",
    "check_request",
    "collect",
    "failed_publication",
    "publish_resident",
    "read_envelope",
    "row_errors_of",
    "signal_name",
    "stage_sources",
]

COMMITTED = frozenset({"published", "already_committed"})
_FIELD_KEY = "output_dir"


class CommitError(DecoyError):
    """A staged isolated output that could not be committed (F29: `publication_failed`).

    Nothing in the route raises it since the route publishes through `publish_unit`, whose
    receipts carry the outcome; the class stays registered with the reason adapter."""


@dataclass(frozen=True)
class Publication:
    """The parent's publication of one isolated run: capability, open stage and request."""

    out: OutputDir
    txn: OutputTxn
    request: PublicationRequest
    stage: Path
    target: Path


def check_request(request: PublicationRequest | None) -> None:
    """A request is a `PublicationRequest` without a vault: the route never binds one (B0)."""
    if request is not None and (type(request) is not PublicationRequest or request.vault):
        raise ValueError("request must be a PublicationRequest without a vault")


def _request(request: PublicationRequest | None) -> PublicationRequest:
    return request if request is not None else PublicationRequest(uuid.uuid4().hex)


def _output(output_dir: Path) -> OutputDir:
    req = FileRequest(FileRole.TARGET_OUTPUT, "write", str(output_dir), _FIELD_KEY)
    return fileaccess.capture().output_dir(req)


def _failed(
    request: PublicationRequest,
    code: ReasonCode = reasons.decoy.PUBLICATION_FAILED,
    *,
    os_errno: int | None = None,
) -> CommitReceipt:
    """A `failed` receipt for a refusal before `publish_unit` (nothing measured or bound)."""
    return CommitReceipt(
        outcome="failed",
        operation_id=request.operation_id,
        reason_code=str(code),
        result_id=None,
        descriptor_digest=None,
        vault_digest=None,
        artifacts=(),
        visible=False,
        durable=False,
        os_error=None if os_errno is None else errno.errorcode.get(os_errno, "EIO"),
    )


def begin_publication(
    output_dir: Path, request: PublicationRequest | None
) -> Publication | CommitReceipt:
    """Open the `publication` stage beside `output_dir` that the child stages into."""
    request = _request(request)
    out = _output(output_dir)
    try:
        txn = out.begin("dir", "exclusive", label="publication")
    except OSError as exc:  # errno only: a parent that cannot be made or opened
        return _failed(request, os_errno=exc.errno)
    return Publication(out, txn, request, Path(txn.local_stage_path()), output_dir)


def publish_resident(
    output_dir: Path, outputs: dict[str, pa.Table], request: PublicationRequest | None
) -> tuple[CommitReceipt, Path | None]:
    """The fallback's publication: stage every table as `<table>.parquet` in a `publication`
    stage beside `output_dir` and bind it with `publish_unit`, as `publish_tables` does, but
    with the stage held here so a refusal's cleanup is confirmed by looking. Returns the
    receipt and the stage path (already discarded unless bound)."""
    pub = begin_publication(output_dir, request)
    if isinstance(pub, CommitReceipt):
        return pub, None
    try:
        if not _names_ok(list(outputs)):
            return _failed(pub.request), pub.stage
        for name, data in outputs.items():
            arrow_adapter.write_table(data, pub.txn, f"{name}.parquet")
        components = [f"{name}.parquet" for name in outputs]
        return publish_unit(pub.out, pub.txn, pub.request, tables=components), pub.stage
    except OSError as exc:  # errno only: a staging write failed, nothing is bound
        return _failed(pub.request, os_errno=exc.errno), pub.stage
    finally:
        pub.txn.abort()  # a no-op once bound


def _names_ok(names: object) -> TypeGuard[list[str]]:
    if type(names) is not list or any(type(n) is not str for n in names):
        return False
    try:
        for name in names:
            _validate_table_name(name)
    except ValueError:
        return False
    components = [f"{name}.parquet" for name in names]
    return len(set(components)) == len(components) and all(map(is_valid_component, components))


def _own(path: Path, mode: int, *, directory: bool = False) -> None:
    """Set `mode` on a file or directory through its own descriptor, opened read-only without
    following a link: the repair a narrowing umask makes necessary (V1-b). Anything but the
    expected kind, or an entry its owner cannot read, raises with the errno only."""
    fd = _call(os.open, path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        kind = os.fstat(fd).st_mode
        if not (stat.S_ISDIR(kind) if directory else stat.S_ISREG(kind)):
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
        os.fchmod(fd, mode)
    finally:
        _close(fd)


def stage_sources(work_root: Path, sources: dict[str, pa.Table]) -> dict[str, str]:
    """Write each source table into the private work root, owner-only from creation, so the
    child can read it whatever the umask; returns the manifest the payload carries."""
    stage = _LocalStageDir(work_root, "isolated.sources", private=True)
    manifest: dict[str, str] = {}
    for index, (name, table) in enumerate(sources.items()):
        component = f"s{index:04d}.parquet"  # opaque: a name can hold "/" or ".."
        arrow_adapter.write_table(table, stage, component)
        manifest[name] = str(work_root / component)
    return manifest


def _admit(stage: Path, names: list[str]) -> bool:
    """True when the stage holds exactly `<name>.parquet` for each name, each a regular file,
    made owner-only (the stage 0700, each file 0600) before anything reads it: the child's
    umask decided its modes. A link or a directory under a table's name raises."""
    posix = sys.platform != "win32"
    if posix:
        _own(stage, 0o700, directory=True)
    listing = FileRequest(FileRole.TARGET_OUTPUT, "read_dir", str(stage), _FIELD_KEY)
    with fileaccess.capture().open_dir(listing) as handle:
        found = sorted(entry.name for entry in handle.entries())
    expected = sorted(f"{name}.parquet" for name in names)
    if found != expected:
        return False
    for component in expected if posix else ():
        _own(stage / component, 0o600)
    return True


def collect(
    envelope: dict[str, Any], staging: Path, pub: Publication | None
) -> tuple[dict[str, pa.Table], CommitReceipt | None] | CommitReceipt:
    """Admit and read back the child's staged tables, then publish them when there is an
    `output_dir`. Returns the tables and the receipt, or the receipt of a refusal or of a
    publication that did not commit; a refused stage is already discarded."""
    request = _request(pub.request if pub is not None else None)
    names = envelope.get("staged_tables")
    code: int | None = None
    outputs: dict[str, pa.Table] | None = None
    try:
        if _names_ok(names) and _admit(staging, names):
            outputs = {n: pq.read_table(staging / f"{n}.parquet") for n in names}  # type: ignore[no-untyped-call, unused-ignore]
    except (OSError, pa.ArrowException) as exc:
        code = exc.errno if isinstance(exc, OSError) else None
    if outputs is None:
        if pub is not None:
            pub.txn.abort()
        return _failed(request, os_errno=code)
    if pub is None:
        return outputs, None
    components = [f"{name}.parquet" for name in outputs]
    receipt = publish_unit(pub.out, pub.txn, request, tables=components)
    return (outputs, receipt) if receipt.outcome in COMMITTED else receipt


def _typed_error(receipt: CommitReceipt) -> BaseException:
    """The typed error a caller raises for this receipt's outcome (`raise_for_outcome`)."""
    try:
        raise_for_outcome(receipt)
    except Exception as exc:
        return exc
    raise ValueError("a committed receipt is not a failure")


def failed_publication(
    receipt: CommitReceipt, staging: Path | None, **fields: Any
) -> IsolatedRunResult:
    """A classified `crashed` result for a publication that did not commit: safe text, the F29
    reason and the receipt. `staging` is the stage to look at for cleanup evidence, if any."""
    error = _typed_error(receipt)
    if receipt.visible or staging is None:
        state = PublicationState.UNKNOWN if receipt.visible else PublicationState.NOT_PUBLISHED
        cleanup = CleanupState.NOT_NEEDED if receipt.visible else CleanupState.ATTEMPTED
        context = ReasonContext.build(
            operation=Operation.COMMIT,
            storage_ref="output",
            publication_state=state,
            cleanup_state=cleanup,
        )
        reason = reason_from_exception(error, phase=Phase.PUBLISH, context=context)
    else:
        reason = commit_reason(error, staging)
    return IsolatedRunResult(
        outcome="crashed",
        outputs=None,
        quality_metrics={},
        table_kinds={},
        signal_number=None,
        error=commit_error_text(receipt),
        reason=reason,
        publication_receipt=receipt,
        **fields,
    )


def row_errors_of(envelope: dict[str, Any]) -> tuple[RowErrorRecord, ...]:
    """The row errors the worker carried in its envelope (dennis review MED-4).

    Absent (the common case: no row errors this run) or malformed both degrade to an empty
    tuple rather than failing the run: row_errors is additive telemetry, not load-bearing."""
    try:
        return tuple(RowErrorRecord(**item) for item in envelope.get("row_errors") or ())
    except TypeError:
        return ()


def read_envelope(result_path: Path) -> dict[str, Any] | None:
    """Read the worker's result envelope from `result_path` (dennis review HIGH-1).

    Missing (the child died before writing it -- a harder rlimit trip, an
    external SIGKILL) or corrupt (a partial write raced with a kill) both
    return `None` rather than raising, so the caller falls through to
    `classify_abnormal_exit`'s outside classification exactly as it did for
    the old "no parseable last stdout line" case (MED-5's missing/corrupt
    envelope coverage).
    """
    try:
        text = result_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        return None
    return envelope if isinstance(envelope, dict) else None


def signal_name(signal_number: int | None) -> str | None:
    """`signal.Signals(n).name`, guarded (dennis review LOW-3).

    Real-time signal NUMBERS (e.g. SIGRTMIN+5) are valid `os.kill` targets
    but most have no individual `signal.Signals` enum member -- the
    constructor raises `ValueError` for them. That must not turn a clean
    abnormal-exit classification into an unhandled driver exception; fall
    back to a bare numeric label instead.
    """
    if not signal_number:
        return None
    try:
        return signal.Signals(signal_number).name
    except ValueError:
        return f"signal {signal_number}"
