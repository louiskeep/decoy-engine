"""Unit dispatch: one public commit/lookup/reconcile surface over both local units."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from decoy_engine.publication import _local_directory as _ld
from decoy_engine.publication import _managed
from decoy_engine.publication._child import verify_child_envelope
from decoy_engine.publication._types import (
    UNIT_MANAGED_DATASET,
    ChildPreparationEnvelope,
    CommitReceipt,
    PreparedPublication,
    ProtectedBackup,
    PublicationRefusedError,
    PublicationRequest,
    Replace,
)

_CHILD_DEFECTS = frozenset(
    {"unreadable_table", "row_count_mismatch", "unexpected_table", "missing_expected_table"}
)


def prepare_directory(
    stage: Path, request: PublicationRequest, *, producer_rows: Mapping[str, int] | None = None
) -> PreparedPublication:
    _ld.gate_request(request)
    return _ld.prepare_directory(stage, request, producer_rows=producer_rows)


def promote_child_envelope(
    envelope: ChildPreparationEnvelope, request: PublicationRequest
) -> CommitReceipt:
    """Verify a child's sealed directory BEFORE reading it, re-measure, then publish.

    Accepts only the typed envelope. Any defect in the child's claim (missing, extra,
    corrupt or row-mismatched tables) is `child_envelope_invalid`; nothing is touched.
    """
    if not isinstance(envelope, ChildPreparationEnvelope):
        raise TypeError("only a validated ChildPreparationEnvelope can be promoted")
    _ld.gate_request(request)
    verify_child_envelope(envelope, request)
    if request.expected_tables is None:
        request = replace(request, expected_tables=envelope.tables)
    try:
        prepared = _ld.prepare_directory(
            envelope.staging_dir, request, producer_rows=dict(envelope.row_counts)
        )
    except PublicationRefusedError as exc:
        if exc.receipt is not None and exc.receipt.reason_code in _CHILD_DEFECTS:
            raise _ld._refuse(request, "rejected", "child_envelope_invalid") from exc
        raise
    return commit_prepared(prepared)


def commit_prepared(prepared: PreparedPublication) -> CommitReceipt:
    """Bind a prepared unit to its destination. Raises for every non-success outcome."""
    _ld.gate_request(prepared.request)
    if prepared.request.unit == UNIT_MANAGED_DATASET:
        return _managed.commit_managed(prepared)
    return _ld.commit_directory(prepared)


def lookup_committed(request: PublicationRequest) -> CommitReceipt | None:
    _ld.gate_request(request)
    if request.unit == UNIT_MANAGED_DATASET:
        return _managed.lookup_committed_managed(request)
    return _ld.lookup_committed(request)


def reconcile(request: PublicationRequest) -> CommitReceipt | None:
    _ld.gate_request(request)
    if request.unit == UNIT_MANAGED_DATASET:
        return _managed.reconcile_managed(request)
    return _ld.reconcile(request)


def validate_request(request: PublicationRequest) -> None:
    """Admission before the first output write; a protected backup adds the prior size."""
    _ld.gate_request(request)
    base = request.required_free_bytes
    extra = 0
    if (
        request.unit == UNIT_MANAGED_DATASET
        and base is not None
        and isinstance(request.overwrite, Replace)
        and isinstance(request.overwrite.backup, ProtectedBackup)
    ):
        try:
            cur = _managed.resolve_current(request.destination)
        except _managed.ResolveError:
            cur = None
        extra = sum(a.size for a in cur.artifacts) if cur else 0
    if base is not None and extra:
        request = replace(request, required_free_bytes=base + extra)
    _ld.validate_request(request)
