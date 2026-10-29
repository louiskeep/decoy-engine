"""Atomic and idempotent output publication (F22).

`PublicationDescriptor` bytes (`_decoy_publication.json`) are the single commit truth:
a result is committed only when the destination binding resolves to a descriptor whose
bound artifacts all verify. Receipts, statuses and file listings never overrule it.
"""

from decoy_engine.publication._dispatch import (
    commit_prepared,
    lookup_committed,
    reconcile,
    validate_request,
)
from decoy_engine.publication._local_directory import (
    cleanup_staging,
    inspect_destination,
    prepare_directory,
    with_cleanup,
    write_private_file,
)
from decoy_engine.publication._managed import (
    ResolvedGeneration,
    ResolveError,
    expire_historical_payload,
    pin_reader,
    resolve_current,
    restore_backup,
)
from decoy_engine.publication._protocol import (
    CommitProtocolDeclaration,
    admit,
    capability_rows,
    declaration_for,
    violations,
)
from decoy_engine.publication._retention import (
    AbortReport,
    CleanupResult,
    RetainedRecord,
    add_hold,
    cleanup_retained,
    inventory,
    remove_hold,
)
from decoy_engine.publication._types import (
    UNIT_IMMUTABLE_DIRECTORY,
    UNIT_MANAGED_DATASET,
    ArtifactRef,
    CheckResult,
    ChildPreparationEnvelope,
    CommitError,
    CommitReceipt,
    CommitUncertainError,
    ErrorIfDifferent,
    NoBackup,
    PreparedPublication,
    ProtectedBackup,
    PublicationRefusedError,
    PublicationRequest,
    Replace,
    RetentionPolicy,
)

__all__ = [
    "UNIT_IMMUTABLE_DIRECTORY",
    "UNIT_MANAGED_DATASET",
    "AbortReport",
    "ArtifactRef",
    "CheckResult",
    "ChildPreparationEnvelope",
    "CleanupResult",
    "CommitError",
    "CommitProtocolDeclaration",
    "CommitReceipt",
    "CommitUncertainError",
    "ErrorIfDifferent",
    "NoBackup",
    "PreparedPublication",
    "ProtectedBackup",
    "PublicationRefusedError",
    "PublicationRequest",
    "Replace",
    "ResolveError",
    "ResolvedGeneration",
    "RetainedRecord",
    "RetentionPolicy",
    "add_hold",
    "admit",
    "capability_rows",
    "cleanup_retained",
    "cleanup_staging",
    "commit_prepared",
    "declaration_for",
    "expire_historical_payload",
    "inspect_destination",
    "inventory",
    "lookup_committed",
    "pin_reader",
    "prepare_directory",
    "reconcile",
    "remove_hold",
    "resolve_current",
    "restore_backup",
    "validate_request",
    "violations",
    "with_cleanup",
    "write_private_file",
]
