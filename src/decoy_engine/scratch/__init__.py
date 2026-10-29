"""F25 protected scratch: owned, quota-enforced temporary storage.

Slice 1 builds the standalone subsystem: typed capabilities, the storage-provider
seam, a local SQLite ResourceLedger under an ownership-authority seam, restrictive
allocation with durable-registration-before-write, hard-quota verification and
typed quota/cleanup reports. It is intentionally disconnected from the T01-T17
production producers; migrating them is Slices 2-3, encryption is Slice 4, and
lease-checked orphan recovery is Slice 5 (plan
docs/plans/2026-10-29-f25-protected-scratch-quotas-orphan.md).

Nothing here is wired into any live execution route. The allocation sentry
(tests/sentry/test_scratch_allocation_sentry.py) seeds the T01-T17 inventory but
enforces migration on none of them yet.
"""

from __future__ import annotations

from decoy_engine.scratch._arena import Arena
from decoy_engine.scratch._authority import (
    AuthorityUnavailable,
    LeaseAuthorityDisabledError,
    OwnershipAuthority,
    PlatformLeaseAuthority,
    PlatformLeaseClient,
    ReapVerdict,
    TargetLockAuthority,
    TargetLockClient,
)
from decoy_engine.scratch._capabilities import (
    ArenaCapability,
    CheckpointRef,
    NativeDirectory,
    PublicationStage,
    RegisteredFile,
    unwrap_dir,
    unwrap_path,
)
from decoy_engine.scratch._errors import (
    ScratchBackendUnavailable,
    ScratchCleanupError,
    ScratchControlUnavailable,
    ScratchError,
    ScratchOwnershipError,
    ScratchPathUnsafe,
    ScratchQuotaExceeded,
    ScratchRecoveryInvalid,
    ScratchRecoveryKeyUnavailable,
)
from decoy_engine.scratch._ledger import ResourceLedger
from decoy_engine.scratch._local_provider import KernelProjectQuotaProvider
from decoy_engine.scratch._manager import ScratchManager
from decoy_engine.scratch._policy import (
    AuthorityMode,
    LeaseIdentity,
    ResourceKind,
    ResourceState,
    Retention,
    ScratchPolicy,
    StorageProfile,
)
from decoy_engine.scratch._provider import StorageProvider
from decoy_engine.scratch._reports import (
    CleanupReport,
    Disposition,
    QualificationResult,
    QuotaObservation,
    QuotaReport,
    SweepReport,
    SweptResource,
)
from decoy_engine.scratch._seam import (
    ArenaLike,
    LocalArena,
    adopt_file,
    as_native_dir,
    ensure_native_dir,
    ensure_publication_file,
    ensure_publication_stage,
    ensure_scratch,
    ensure_scratch_file,
    native_dir_path,
    provider_io_report,
    release_resource,
    route_write_failure,
)

__all__ = [
    "Arena",
    "ArenaCapability",
    "ArenaLike",
    "AuthorityMode",
    "AuthorityUnavailable",
    "CheckpointRef",
    "CleanupReport",
    "Disposition",
    "KernelProjectQuotaProvider",
    "LeaseAuthorityDisabledError",
    "LeaseIdentity",
    "LocalArena",
    "NativeDirectory",
    "OwnershipAuthority",
    "PlatformLeaseAuthority",
    "PlatformLeaseClient",
    "PublicationStage",
    "QualificationResult",
    "QuotaObservation",
    "QuotaReport",
    "ReapVerdict",
    "RegisteredFile",
    "ResourceKind",
    "ResourceLedger",
    "ResourceState",
    "Retention",
    "ScratchBackendUnavailable",
    "ScratchCleanupError",
    "ScratchControlUnavailable",
    "ScratchError",
    "ScratchManager",
    "ScratchOwnershipError",
    "ScratchPathUnsafe",
    "ScratchPolicy",
    "ScratchQuotaExceeded",
    "ScratchRecoveryInvalid",
    "ScratchRecoveryKeyUnavailable",
    "StorageProfile",
    "StorageProvider",
    "SweepReport",
    "SweptResource",
    "TargetLockAuthority",
    "TargetLockClient",
    "adopt_file",
    "as_native_dir",
    "ensure_native_dir",
    "ensure_publication_file",
    "ensure_publication_stage",
    "ensure_scratch",
    "ensure_scratch_file",
    "native_dir_path",
    "provider_io_report",
    "release_resource",
    "route_write_failure",
    "unwrap_dir",
    "unwrap_path",
]
