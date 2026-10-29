"""Per-sink commit-protocol declarations and admission.

A sink qualifies for F22 only by an explicit, typed `CommitProtocolDeclaration` that
passes `violations`; method shape (`write/commit/abort`) proves nothing. The capability
table in the docs is generated from these declarations, so there is one source of truth
and no second set of contradictory flags. Transfer primitives (`FileSink`, the callable
adapter) are declared honestly as not publishing a dataset.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from decoy_engine.publication._types import (
    UNIT_IMMUTABLE_DIRECTORY,
    UNIT_MANAGED_DATASET,
    CommitReceipt,
    ProtectedBackup,
    PublicationRefusedError,
    PublicationRequest,
    Replace,
)

AtomicScope = Literal["none", "object", "directory", "dataset", "transaction"]
Fencing = Literal["none", "lease_check", "process_lock", "storage_enforced"]
Durability = Literal["none", "file", "directory_sync", "provider"]
RetryHorizon = Literal["none", "process", "destination_lifetime"]
ReaderProtocol = Literal["none", "directory_listing", "resolve_current", "provider"]
_MULTI_OBJECT = ("dataset", "transaction")


@dataclass(frozen=True)
class CommitProtocolDeclaration:
    name: str
    atomic_scope: AtomicScope
    staging_isolation: bool
    descriptor_lookup: bool
    fencing: Fencing
    durability: Durability
    retry_horizon: RetryHorizon
    supports_backup: bool
    reader_protocol: ReaderProtocol
    publishes: bool = True  # False: cannot bind a result (preparation, transfer, legacy)
    unit: str | None = None
    note: str = ""


def violations(
    decl: CommitProtocolDeclaration, request: PublicationRequest | None = None
) -> list[str]:
    """Stable reason codes for every way `decl` cannot honor F22 for `request`."""
    out: list[str] = []
    if not decl.publishes:
        return ["not_a_publishing_protocol"]
    if decl.atomic_scope == "none":
        out.append("no_atomic_scope")
    if not decl.staging_isolation:
        out.append("no_staging_isolation")
    if not decl.descriptor_lookup:
        out.append("no_descriptor_lookup")
    if decl.durability == "none":
        out.append("no_durability")
    if decl.atomic_scope in _MULTI_OBJECT and decl.fencing in ("none", "lease_check"):
        out.append(
            "lease_check_without_storage_fencing"
            if decl.fencing == "lease_check"
            else "multi_object_release_without_cas"
        )
    if request is not None:
        if decl.unit is not None and request.unit != decl.unit:
            out.append("unit_mismatch")
        wants_backup = isinstance(request.overwrite, Replace) and isinstance(
            request.overwrite.backup, ProtectedBackup
        )
        if wants_backup and not decl.supports_backup:
            out.append("backup_unsupported")
        if isinstance(request.overwrite, Replace) and decl.unit == UNIT_IMMUTABLE_DIRECTORY:
            out.append("replace_unsupported_for_unit")
    return out


IMMUTABLE_DIRECTORY = CommitProtocolDeclaration(
    name="parquet_immutable_directory",
    atomic_scope="directory",
    staging_isolation=True,
    descriptor_lookup=True,
    fencing="process_lock",
    durability="directory_sync",
    retry_horizon="destination_lifetime",
    supports_backup=False,
    reader_protocol="directory_listing",
    unit=UNIT_IMMUTABLE_DIRECTORY,
    note="POSIX local filesystem with flock; one rename binds the descriptor.",
)
MANAGED_DATASET = CommitProtocolDeclaration(
    name="parquet_managed_dataset",
    atomic_scope="dataset",
    staging_isolation=True,
    descriptor_lookup=True,
    fencing="process_lock",
    durability="directory_sync",
    retry_horizon="destination_lifetime",
    supports_backup=True,
    reader_protocol="resolve_current",
    unit=UNIT_MANAGED_DATASET,
    note="Immutable generations; one atomically replaced CURRENT pointer.",
)
PREPARATION_ONLY = CommitProtocolDeclaration(
    name="parquet_preparation_only",
    atomic_scope="none",
    staging_isolation=True,
    descriptor_lookup=False,
    fencing="none",
    durability="none",
    retry_horizon="none",
    supports_backup=False,
    reader_protocol="none",
    publishes=False,
    note="Child-side seal; only the parent backend may publish.",
)
NON_TRANSACTIONAL = CommitProtocolDeclaration(
    name="callable_sink",
    atomic_scope="none",
    staging_isolation=False,
    descriptor_lookup=False,
    fencing="none",
    durability="none",
    retry_horizon="none",
    supports_backup=False,
    reader_protocol="none",
    publishes=False,
    note="Immediate writes; commit/abort are no-ops.",
)
TRANSFER_ONLY = CommitProtocolDeclaration(
    name="file_sink_transfer",
    atomic_scope="object",
    staging_isolation=False,
    descriptor_lookup=False,
    fencing="none",
    durability="provider",
    retry_horizon="none",
    supports_backup=False,
    reader_protocol="provider",
    publishes=False,
    note="Per-object byte transfer; no dataset commit or conditional descriptor write.",
)
UNDECLARED = CommitProtocolDeclaration(
    name="undeclared",
    atomic_scope="none",
    staging_isolation=False,
    descriptor_lookup=False,
    fencing="none",
    durability="none",
    retry_horizon="none",
    supports_backup=False,
    reader_protocol="none",
    publishes=False,
    note="No declaration: method shape alone never qualifies a sink.",
)

REGISTERED = (
    IMMUTABLE_DIRECTORY,
    MANAGED_DATASET,
    PREPARATION_ONLY,
    NON_TRANSACTIONAL,
    TRANSFER_ONLY,
)


def declaration_for(sink: Any) -> CommitProtocolDeclaration:
    declared = getattr(type(sink), "protocol_declaration", None)
    if isinstance(declared, CommitProtocolDeclaration):
        return declared
    from decoy_engine.sdk import FileSink

    if isinstance(sink, FileSink):
        return TRANSFER_ONLY
    return UNDECLARED


def admit(sink: Any, request: PublicationRequest | None = None) -> CommitProtocolDeclaration:
    """Refuse, before any write, a sink that cannot honor F22 for this request."""
    decl = declaration_for(sink)
    problems = violations(decl, request)
    if problems:
        destination = str(request.destination) if request else ""
        raise PublicationRefusedError(
            f"sink {decl.name!r} is not admitted for F22 publication: {', '.join(problems)}",
            CommitReceipt(
                "unsupported",
                destination,
                request.operation_id if request else "",
                problems[0],
            ),
        )
    return decl


def _unit_declaration(unit: str) -> CommitProtocolDeclaration | None:
    return {
        UNIT_IMMUTABLE_DIRECTORY: IMMUTABLE_DIRECTORY,
        UNIT_MANAGED_DATASET: MANAGED_DATASET,
    }.get(unit)


def admit_request(request: PublicationRequest, sink: Any = None) -> None:
    """The ONE admission authority for a publication request.

    Every public request-taking operation (module level or through a sink) calls this on
    its first line. A sink, when given, must pass its own declaration; the request's unit
    declaration is ALWAYS validated, so a caller with no sink cannot skip admission.
    """
    decls: list[CommitProtocolDeclaration] = []
    if sink is not None:
        decls.append(declaration_for(sink))
    unit_decl = _unit_declaration(request.unit)
    if unit_decl is None:
        problems = ["unknown_publication_unit"]
        decl_name = request.unit
    else:
        decls.append(unit_decl)
        problems = []
        decl_name = unit_decl.name
    for decl in decls:
        problems.extend(p for p in violations(decl, request) if p not in problems)
    if problems:
        raise PublicationRefusedError(
            f"publication request is not admitted for {decl_name!r}: {', '.join(problems)}",
            CommitReceipt(
                "unsupported",
                str(request.destination),
                request.operation_id,
                problems[0],
            ),
        )


def capability_rows() -> list[tuple[str, CommitProtocolDeclaration]]:
    return [(d.name, d) for d in REGISTERED]
