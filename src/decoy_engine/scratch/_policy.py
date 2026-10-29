"""Provisional value types for the F25 protected-scratch subsystem.

These are the frozen vocabulary the ScratchManager, ResourceLedger and the
allocation sentry share: resource kinds, lifecycle states, retention classes,
the attempt policy, and the lease identity the engine receives from (never
decides for) the platform ownership authority.

The full retention/state schema is defined up front, in Slice 1, so the later
sweep and key slices never have to reinterpret an old ledger row unsafely (plan
docs/plans/2026-10-29-f25-protected-scratch-quotas-orphan.md Slice 1 scope).

Why not an enum-free string soup: a resource row in the durable ledger outlives
the process that wrote it, so a typo in a state string would silently corrupt a
recovery decision. Closed enums make an unknown value a load error, not a wrong
deletion.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class StorageProfile(str, enum.Enum):
    """Which qualified storage backend an attempt runs on.

    Only ``LINUX_EXT4_FSCRYPT_PRJQUOTA`` is a production-admissible profile, and
    it is admissible only once Slice 4 wires encryption. There is deliberately
    no "plaintext" or "unquota'd" profile: an unsupported host fails closed with
    ``scratch_backend_unavailable`` rather than silently using ordinary /tmp
    (plan 1.5). ``UNQUALIFIED`` is a sentinel the provider returns from a failed
    qualification; it never authorizes a write.
    """

    LINUX_EXT4_FSCRYPT_PRJQUOTA = "linux-ext4-fscrypt-prjquota"
    UNQUALIFIED = "unqualified"


class ResourceKind(str, enum.Enum):
    """What a registered resource is, which fixes how it is written and swept."""

    # A single Python-created file (Arrow IPC, Parquet, JSON, fixed-width tmp).
    FILE = "file"
    # An opaque directory handed to a native library (DuckDB spill, sort runs).
    # Its inner filenames are controlled by the library; one directory-level
    # record covers the whole inherited namespace (plan 1.5).
    NATIVE_DIR = "native_dir"
    # A destination-side staging area that becomes durable output on rename.
    PUBLICATION = "publication"
    # A resumable checkpoint payload plus its recovery envelope (Slice 4/6).
    CHECKPOINT = "checkpoint"
    # The control database's own sidecars (DB, journal, lock). The manager can
    # never create a recursive scratch exemption for its own metadata (plan
    # 1.3), so the control arena's files are themselves registered resources.
    CONTROL = "control"


class ResourceState(str, enum.Enum):
    """Durable lifecycle state. Transitions are CAS-guarded in the ledger.

    A crash during creation leaves a ``CREATING`` row (or a conservatively
    retained unknown object), never an unowned data directory (plan 1.5). An
    ``UNKNOWN`` row is always retained by the sweep.
    """

    CREATING = "creating"
    ACTIVE = "active"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    CHECKPOINT_WRITING = "checkpoint_writing"
    CHECKPOINT_VALID = "checkpoint_valid"
    REAPING = "reaping"
    REAPED = "reaped"
    FAILED = "failed"
    UNKNOWN = "unknown"


# States that make a resource ineligible for an orphan sweep regardless of lease
# expiry (plan 1.8 step 2). Lease expiry alone is only candidate selection.
SWEEP_INELIGIBLE_STATES: frozenset[ResourceState] = frozenset(
    {
        ResourceState.PUBLISHING,
        ResourceState.PUBLISHED,
        ResourceState.CHECKPOINT_VALID,
        ResourceState.UNKNOWN,
    }
)


class Retention(str, enum.Enum):
    """Retention class: it fixes custody, crash behaviour and resume rule."""

    EPHEMERAL = "ephemeral"
    RESUMABLE = "resumable"
    DURABLE_PUBLICATION = "durable_publication"


class AuthorityMode(str, enum.Enum):
    """Which ownership-authority implementation stamped a lease identity.

    The engine ledger carries the mode so a row written while the platform lease
    authority is flag-gated off (``TARGET_LOCK``) is never misread as one written
    under the enabled authority (``PLATFORM_LEASE``), and vice versa (ledger
    subordination contract 2.5).
    """

    PLATFORM_LEASE = "platform_lease"
    TARGET_LOCK = "target_lock"


@dataclass(frozen=True, slots=True)
class LeaseIdentity:
    """The read-only ownership tuple the engine receives from the authority.

    The engine never invents a ``generation`` and never advances the fence; it
    stamps its ledger rows with whatever the authority issued (ledger
    subordination contract 2.2). ``local_stage_root`` is the arena root the
    platform admitted on the dispatch-plan binding, not a path the engine chose.
    """

    job_id: str
    attempt_id: str
    lease_id: str
    generation: int
    mode: AuthorityMode
    local_stage_root: str


@dataclass(frozen=True, slots=True)
class ScratchPolicy:
    """The per-attempt resource policy frozen before any writable capability.

    ``quota_bytes`` / ``quota_inodes`` are the hard caps the deployment adapter
    turned into a kernel project quota on the arena root; the engine verifies
    them and never maintains a parallel Python byte counter (plan 1.5 item 4).
    ``quota_inodes`` is a first-class field so a runaway tiny-file producer
    cannot evade the byte cap (Gemini Tier-2 fix, plan 1.9 A02 inode case).
    """

    job_id: str
    attempt_id: str
    quota_bytes: int
    quota_inodes: int
    retention: Retention = Retention.EPHEMERAL
    storage_profile: StorageProfile = StorageProfile.LINUX_EXT4_FSCRYPT_PRJQUOTA
    # An explicit control-plane byte/inode margin reserved on the control arena,
    # separate from the data quota, so recording a terminal data-quota breach can
    # never itself fail on a control-quota limit (plan 1.5 control-arena sizing).
    control_margin_bytes: int = 1 << 20
    control_margin_inodes: int = 256
    # Optional provenance labels; never source values, DSNs or user filenames.
    labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.quota_bytes <= 0:
            raise ValueError("quota_bytes must be positive")
        if self.quota_inodes <= 0:
            raise ValueError("quota_inodes must be positive")
        if not self.job_id or not self.attempt_id:
            raise ValueError("job_id and attempt_id are required")
