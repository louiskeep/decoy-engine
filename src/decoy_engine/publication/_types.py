"""Publication value types: request, receipt, outcomes, and the typed refusal errors.

Identity pattern: Delta Lake records a transaction identifier with the data change
and re-reads committed progress before applying a repeated write; Git names objects
by content. `result_id` here is a SHA-256 over the artifact inventory (physical
identity), the operation id ties a retry to the original commit.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from decoy_engine.errors import DecoyError

Outcome = Literal[
    "published",
    "already_committed",
    "conflict",
    "unsupported",
    "rejected",
    "cancelled_before_commit",
    "commit_uncertain",
]
SideStatus = Literal["not_requested", "succeeded", "failed", "retained"]
CheckDisposition = Literal["pass", "fail", "inconclusive", "not_required"]

UNIT_IMMUTABLE_DIRECTORY = "immutable_directory/v1"
UNIT_MANAGED_DATASET = "managed_dataset/v1"
SUCCESS_OUTCOMES = frozenset({"published", "already_committed"})


@dataclass(frozen=True)
class NoBackup:
    """Replacement without a protected copy of the prior generation."""


@dataclass(frozen=True)
class ProtectedBackup:
    """Replacement that first holds a verified private copy of the prior generation."""

    policy_ref: str


@dataclass(frozen=True)
class ErrorIfDifferent:
    """Default: a verified identical result is reported, anything else is refused."""


@dataclass(frozen=True)
class Replace:
    """Authorize a different result over the predecessor with this descriptor digest."""

    expected_descriptor_digest: str
    backup: NoBackup | ProtectedBackup = field(default_factory=NoBackup)


OverwritePolicy = ErrorIfDifferent | Replace


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    disposition: CheckDisposition
    required: bool = True


@dataclass(frozen=True)
class RetentionPolicy:
    """Opt-in retention of a rejected attempt's staging in authorized private storage.

    Default (no policy) is best-effort discard. Retained data is never a published result:
    its descriptor is neutralized and it carries separate retention metadata.
    """

    store: Path
    owner: str
    expiry_s: float
    byte_budget: int


@dataclass(frozen=True)
class PublicationRequest:
    """Fixed before the first output write. Retry callers keep `operation_id` stable."""

    destination: Path
    operation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    job_id: str | None = None
    unit: str = UNIT_IMMUTABLE_DIRECTORY
    expected_tables: tuple[str, ...] | None = None
    expected_result_id: str | None = None
    validation_policy_fingerprint: str | None = None
    checks: tuple[CheckResult, ...] = ()
    overwrite: OverwritePolicy = field(default_factory=ErrorIfDifferent)
    required_free_bytes: int | None = None
    cancelled: Callable[[], bool] | None = None
    retention: RetentionPolicy | None = None
    # F22 complete-unit mode: required sidecars (quarantine) are staged inside the unit and
    # bound by the descriptor; a sidecar that cannot participate is refused before masking.
    sidecars_in_unit: bool = False


@dataclass(frozen=True)
class ArtifactRef:
    role: str
    path: str
    size: int
    sha256: str
    rows: int | None = None
    schema_digest: str | None = None

    def summary(self) -> ArtifactSummary:
        return ArtifactSummary(self.role, self.size, self.sha256, self.rows, self.schema_digest)


@dataclass(frozen=True)
class ArtifactSummary:
    """An artifact as a receipt reports it: identity and size, never a name or a path.

    A receipt is persisted and shown outside the process; artifact file names derive from
    table names and the destination path is the caller's own, so neither is carried. The
    descriptor inside the committed unit still binds the real relative paths."""

    role: str
    size: int
    sha256: str
    rows: int | None = None
    schema_digest: str | None = None


@dataclass(frozen=True)
class CommitReceipt:
    """What a publication attempt observably did. Visibility and durability are separate.

    Value-free by construction: stable tokens, digests and counts only. The destination the
    caller named is not echoed (the caller already holds it)."""

    outcome: Outcome
    operation_id: str
    reason_code: str | None = None
    result_id: str | None = None
    descriptor_digest: str | None = None
    evidence_digest: str | None = None
    artifacts: tuple[ArtifactSummary, ...] = ()
    is_current: bool = False
    visible: bool = False
    durable: bool = False
    backup_status: SideStatus = "not_requested"
    cleanup_status: SideStatus = "not_requested"
    generation: str | None = None  # managed unit: path relative to the dataset root
    backup_ref: str | None = None  # managed unit: path of the held protected backup

    @property
    def committed(self) -> bool:
        return self.outcome in SUCCESS_OUTCOMES


class CommitError(DecoyError):
    """The commit did not publish a new result; `receipt` says exactly why.

    Subclassed by `PublicationRefusedError` and `CommitUncertainError`; kept under the old name so
    the isolated-run classification (`except CommitError`) keeps working unchanged.
    """

    def __init__(self, message: str, receipt: CommitReceipt | None = None) -> None:
        super().__init__(message)
        self.receipt = receipt


class PublicationRefusedError(CommitError):
    """conflict / unsupported / rejected / cancelled_before_commit: nothing new bound."""


class CommitUncertainError(CommitError):
    """The binding may be visible but the acknowledgment, sync or reread failed.

    Never delete the possible committed generation and never blindly rewrite; call
    `reconcile` to learn the authoritative state.
    """


@dataclass(frozen=True)
class PreparedPublication:
    """A sealed, validated claim. Issued only by the backend's prepare step; a staged
    descriptor is still just a claim until the destination binding resolves to it."""

    request: PublicationRequest
    staging: Path
    artifacts: tuple[ArtifactRef, ...]
    result_id: str
    descriptor_digest: str
    evidence_digest: str


@dataclass(frozen=True)
class ChildPreparationEnvelope:
    """What an isolated child returns: a prepared directory, never a commit.

    Deliberately not a `CommitReceipt` and not accepted by anything that resolves
    committed output; only the parent's backend re-measures and promotes the directory.
    """

    staging_dir: Path
    tables: tuple[str, ...]
    row_counts: tuple[tuple[str, int], ...] = ()
