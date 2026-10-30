"""Staging, atomic-commit, and result-envelope IO for the isolated-run primitive.

Split out of `_isolated_run.py` (which owns spawn/classify orchestration) to
stay under the ~600 LOC orchestration cap (CLAUDE.md, `graph/runner.py`
threshold) -- these are pure IO/filesystem helpers with no subprocess
lifecycle logic of their own, shared by both the isolated (subprocess) path
and the `isolate=False` in-process fallback so the two modes commit with
identical discipline (dennis review HIGH-2).

F22: the parent is the only publisher. The child returns a prepared directory
(`ParquetPreparationSink`); `atomic_commit` re-measures it, builds the descriptor and
binds it through the one `decoy_engine.publication` backend, so this path and
`ParquetTransactionalSink.commit` share one commit implementation.
"""

from __future__ import annotations

import json
import signal
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine import publication as _pub
from decoy_engine.execution._row_errors import RowErrorRecord
from decoy_engine.publication import CommitError

__all__ = [
    "CommitError",
    "PublishContext",
    "atomic_commit",
    "read_envelope",
    "read_staged_row_errors",
    "resolve_staging_output_dir",
    "sibling_staging_dir",
    "signal_name",
    "stage_tables",
]

_STAGING_DIR_PREFIX = "_decoy_isolated_stage_"


@dataclass(frozen=True)
class PublishContext:
    """Caller-fixed publication request for the parent's one publication (stable across
    retries). `request_for` is the ONLY place the parent builds a request, so the child
    gate and the promotion can never see different checks, tables, unit or overwrite."""

    operation_id: str | None = None
    cancelled: Callable[[], bool] | None = None
    checks: tuple[_pub.CheckResult, ...] = ()
    validation_policy_fingerprint: str | None = None
    expected_tables: tuple[str, ...] | None = None
    unit: str = _pub.UNIT_IMMUTABLE_DIRECTORY
    overwrite: _pub.OverwritePolicy = _pub.ErrorIfDifferent()  # noqa: RUF009

    def __post_init__(self) -> None:
        # Minted once, so the child gate and the promotion name the same operation.
        if self.operation_id is None:
            object.__setattr__(self, "operation_id", uuid.uuid4().hex)

    def request_for(self, destination: Path) -> _pub.PublicationRequest:
        return _pub.PublicationRequest(
            destination=destination,
            operation_id=self.operation_id or "",
            unit=self.unit,
            cancelled=self.cancelled,
            checks=self.checks,
            validation_policy_fingerprint=self.validation_policy_fingerprint,
            expected_tables=self.expected_tables,
            overwrite=self.overwrite,
        )


def sibling_staging_dir(output_dir: Path) -> Path:
    """A scratch dir in `output_dir`'s own parent (same filesystem), so the
    commit's `os.replace` is a single atomic rename, never a cross-device
    copy -- the same discipline `ParquetTransactionalSink.commit` uses.
    """
    token = uuid.uuid4().hex[:12]
    return output_dir.parent / f"{_STAGING_DIR_PREFIX}{token}"


def stage_tables(outputs: dict[str, pa.Table], staging_dir: Path) -> None:
    staging_dir.mkdir(parents=True, exist_ok=True)
    for table, data in outputs.items():
        pq.write_table(  # type: ignore[no-untyped-call, unused-ignore]
            data, staging_dir / f"{table}.parquet"
        )


def atomic_commit(
    envelope: _pub.ChildPreparationEnvelope,
    output_dir: Path,
    *,
    publish: PublishContext | None = None,
) -> _pub.CommitReceipt:
    """Re-measure the validated child preparation and bind it to `output_dir` (§12 ruling 3).

    Accepts ONLY a `ChildPreparationEnvelope` (already bound to the parent-assigned
    staging directory); the declared tables and row counts are enforced against the files.

    A different or legacy occupant raises a classified `CommitError` (never a raw
    `OSError`, with staging left for the caller to discard); an identical committed
    result is returned as `already_committed` and its redundant staging is removed.
    """
    if not isinstance(envelope, _pub.ChildPreparationEnvelope):
        raise TypeError("atomic_commit promotes only a validated ChildPreparationEnvelope")
    staging_dir = envelope.staging_dir
    publish = publish or PublishContext()
    request = publish.request_for(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    receipt = _pub.promote_child_envelope(envelope, request)
    if receipt.outcome == "already_committed":
        receipt = _pub.with_cleanup(receipt, _pub.cleanup_staging(staging_dir))
    return receipt


def resolve_staging_output_dir(work_root: Path, output_dir: Path | None) -> Path:
    """Where the child stages outputs before commit.

    When `output_dir` is given, staging must share its filesystem so the
    commit step's `os.replace` is a single atomic rename (never a copy) --
    the same same-parent-directory discipline `ParquetTransactionalSink`
    uses for exactly this reason. Without an `output_dir` there is nothing
    to rename into, so staging just lives under this run's own scratch root.
    """
    if output_dir is not None:
        return sibling_staging_dir(output_dir)
    return work_root / "staging_output"


def read_staged_row_errors(staging_output_dir: Path) -> tuple[RowErrorRecord, ...]:
    """Read back `row_errors.json` staged by the worker (dennis review MED-4).

    Missing (the common case: no row errors this run) or unreadable/corrupt
    both degrade to an empty tuple rather than failing the whole commit --
    row_errors is additive telemetry, not load-bearing for `outputs`.
    """
    path = staging_output_dir / "row_errors.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    return tuple(RowErrorRecord(**item) for item in raw)


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
        envelope: dict[str, Any] = json.loads(text)
        return envelope
    except json.JSONDecodeError:
        return None


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
