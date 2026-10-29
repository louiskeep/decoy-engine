"""Unit dispatch: one public commit/lookup/reconcile surface over both local units."""

from __future__ import annotations

from dataclasses import replace

from decoy_engine.publication import _local_directory as _ld
from decoy_engine.publication import _managed
from decoy_engine.publication._types import (
    UNIT_IMMUTABLE_DIRECTORY,
    UNIT_MANAGED_DATASET,
    CommitReceipt,
    PreparedPublication,
    ProtectedBackup,
    PublicationRequest,
    Replace,
)


def _check_unit(request: PublicationRequest) -> None:
    if request.unit not in (UNIT_IMMUTABLE_DIRECTORY, UNIT_MANAGED_DATASET):
        raise _ld._refuse(request, "unsupported", "unknown_publication_unit")


def commit_prepared(prepared: PreparedPublication) -> CommitReceipt:
    """Bind a prepared unit to its destination. Raises for every non-success outcome."""
    _check_unit(prepared.request)
    if prepared.request.unit == UNIT_MANAGED_DATASET:
        return _managed.commit_managed(prepared)
    return _ld.commit_directory(prepared)


def lookup_committed(request: PublicationRequest) -> CommitReceipt | None:
    _check_unit(request)
    if request.unit == UNIT_MANAGED_DATASET:
        return _managed.lookup_committed_managed(request)
    return _ld.lookup_committed(request)


def reconcile(request: PublicationRequest) -> CommitReceipt | None:
    _check_unit(request)
    if request.unit == UNIT_MANAGED_DATASET:
        return _managed.reconcile_managed(request)
    return _ld.reconcile(request)


def validate_request(request: PublicationRequest) -> None:
    """Admission before the first output write; a protected backup adds the prior size."""
    _check_unit(request)
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
