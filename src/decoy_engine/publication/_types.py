"""Publication request and receipt types, and the shape checks they share with the descriptor.

A receipt reaches user-visible surfaces (job logs, the UI, notifications), so every field is a
closed token, a hex digest, a count, a bool or a shape-checked caller id, checked when the receipt
is built; artifact names and the destination never appear in one. A failed check raises a
``ValueError`` naming the field, never its value.

A caller that wants an exception instead of an outcome passes the receipt to
``raise_for_outcome``: ``PublicationRefused`` (refused, ``operation_conflict``),
``PublicationFailed`` (failed) or the file-access ``CommitUncertain``. Each carries a registered
``decoy`` code and fixed text (RFC 9457: a stable identity apart from the occurrence; never the
raw error text).

Identity: Delta Lake records a caller-chosen transaction identifier with each write and treats a
repeat as a retry; here ``operation_id`` names one intended result for one destination, and a
matching ``operation_id`` is never taken as evidence of an equal result (``result_id`` decides).
"""

from __future__ import annotations

import errno
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, get_args

from decoy_engine import reasons
from decoy_engine._error_base import DecoyError
from decoy_engine.fileaccess._types import CommitUncertain

if TYPE_CHECKING:
    from decoy_engine.fileaccess import OutputDir
    from decoy_engine.vault import VaultWriter

__all__ = [
    "DEFAULT_LOCK_TIMEOUT_S",
    "MAX_COUNT",
    "OPERATION_ID_PATTERN",
    "ROLES",
    "UNITS",
    "UNIT_IMMUTABLE_DIRECTORY",
    "ArtifactSummary",
    "CommitReceipt",
    "Outcome",
    "PublicationFailed",
    "PublicationRefused",
    "PublicationRequest",
    "Role",
    "Unit",
    "VaultBinding",
    "is_count",
    "is_hex64",
    "is_operation_id",
    "is_token",
    "raise_for_outcome",
]

Outcome = Literal[
    "published",
    "already_committed",
    "operation_conflict",
    "refused",
    "failed",
    "commit_uncertain",
]
Unit = Literal["immutable_directory/v1"]
Role = Literal["table"]

UNIT_IMMUTABLE_DIRECTORY: Final[Unit] = "immutable_directory/v1"
UNITS: Final[tuple[str, ...]] = get_args(Unit)
ROLES: Final[tuple[str, ...]] = get_args(Role)
_OUTCOMES: Final[tuple[str, ...]] = get_args(Outcome)

OPERATION_ID_PATTERN: Final = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"
_OPERATION_ID = re.compile(OPERATION_ID_PATTERN)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_ERRNO_NAMES: Final = frozenset(errno.errorcode.values())
# Counts and sizes are stored as JSON integers that other readers parse as signed 64-bit.
MAX_COUNT: Final = 2**63 - 1
DEFAULT_LOCK_TIMEOUT_S: Final = 30.0


def is_operation_id(value: object) -> bool:
    # fullmatch, not a `$` anchor: `$` also matches before a trailing newline.
    return type(value) is str and _OPERATION_ID.fullmatch(value) is not None


def is_hex64(value: object) -> bool:
    return type(value) is str and _HEX64.fullmatch(value) is not None


def is_count(value: object) -> bool:
    # `type(...) is int`: `bool` is an `int` subclass and must never pass as a count.
    return type(value) is int and 0 <= value <= MAX_COUNT


def is_token(value: object, allowed: tuple[str, ...]) -> bool:
    return type(value) is str and value in allowed


def _is_decoy_code(value: object) -> bool:
    # Registry membership, not character shape: a well-formed string is not a known reason.
    return isinstance(value, str) and reasons.find("decoy", value) is not None


def _require(ok: bool, field: str) -> None:
    if not ok:
        raise ValueError(f"invalid {field}")


@dataclass(frozen=True)
class ArtifactSummary:
    """One bound artifact as a receipt reports it: no name and no path."""

    role: Role
    size: int
    rows: int
    sha256: str

    def __post_init__(self) -> None:
        _require(is_token(self.role, ROLES), "role")
        _require(is_count(self.size), "size")
        _require(is_count(self.rows), "rows")
        _require(is_hex64(self.sha256), "sha256")


@dataclass(frozen=True)
class CommitReceipt:
    """What one publication attempt observably did; visibility and durability are separate."""

    outcome: Outcome
    operation_id: str
    reason_code: str | None
    result_id: str | None
    descriptor_digest: str | None
    vault_digest: str | None
    artifacts: tuple[ArtifactSummary, ...]
    visible: bool
    durable: bool
    os_error: str | None

    def __post_init__(self) -> None:
        _require(is_token(self.outcome, _OUTCOMES), "outcome")
        _require(is_operation_id(self.operation_id), "operation_id")
        code = self.reason_code
        _require(code is None or (type(code) is str and _is_decoy_code(code)), "reason_code")
        for name in ("result_id", "descriptor_digest", "vault_digest"):
            value = getattr(self, name)
            _require(value is None or is_hex64(value), name)
        _require(
            type(self.artifacts) is tuple
            and all(type(a) is ArtifactSummary for a in self.artifacts),
            "artifacts",
        )
        _require(type(self.visible) is bool, "visible")
        _require(type(self.durable) is bool, "durable")
        _require(
            self.os_error is None or (type(self.os_error) is str and self.os_error in _ERRNO_NAMES),
            "os_error",
        )


@dataclass(frozen=True)
class VaultBinding:
    """The vault a unit is bound with: its writer and its own output capability (b1, b2)."""

    writer: VaultWriter
    out: OutputDir


@dataclass(frozen=True)
class PublicationRequest:
    """Fixed before the first output write; a retry keeps the same ``operation_id``."""

    operation_id: str
    unit: Unit = UNIT_IMMUTABLE_DIRECTORY
    overwrite: Literal["refuse_different"] = "refuse_different"
    vault: VaultBinding | None = None
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S

    def __post_init__(self) -> None:
        _require(is_operation_id(self.operation_id), "operation_id")
        _require(is_token(self.unit, UNITS), "unit")
        _require(is_token(self.overwrite, ("refuse_different",)), "overwrite")
        _require(self.vault is None or type(self.vault) is VaultBinding, "vault")
        timeout = self.lock_timeout_s
        _require(
            type(timeout) in (int, float) and math.isfinite(timeout) and timeout >= 0,
            "lock_timeout_s",
        )


class _CodedOutcome:
    """Mixin for the publication errors: a registered ``decoy`` code, the receipt, and fixed
    text. Not an exception itself, so each error below is its own ``DecoyError`` family."""

    summary = "publication did not complete"

    def __init__(self, code: str, receipt: CommitReceipt) -> None:
        if not _is_decoy_code(code):
            raise ValueError("invalid reason code")
        self.code = str(code)
        self.receipt = receipt
        super().__init__(f"{self.summary}: {code}")  # type: ignore[call-arg]

    def __reduce__(self) -> tuple[object, tuple[str, CommitReceipt]]:
        return (type(self), (self.code, self.receipt))


class PublicationRefused(_CodedOutcome, DecoyError):  # noqa: N818 - the plan's name
    """The destination refused this result (``refused`` or ``operation_conflict``); nothing of
    this attempt is bound. ``str()`` is a fixed summary and the code, never a path or value."""

    summary = "publication refused"


class PublicationFailed(_CodedOutcome, DecoyError):  # noqa: N818 - the plan's name
    """Staging, measuring or binding failed before the unit was bound; the stage was aborted."""

    summary = "publication failed"


def raise_for_outcome(receipt: CommitReceipt) -> CommitReceipt:
    """Return ``receipt`` when it is ``published`` or ``already_committed``; otherwise raise the
    typed error for its outcome (raised here, outside any handler, so no OS text rides along)."""
    outcome, code = receipt.outcome, receipt.reason_code
    if outcome in ("published", "already_committed"):
        return receipt
    if outcome == "commit_uncertain":
        raise CommitUncertain(getattr(errno, receipt.os_error or "EIO", errno.EIO))
    if code is None:
        raise ValueError("invalid reason_code")
    if outcome == "failed":
        raise PublicationFailed(code, receipt)
    raise PublicationRefused(code, receipt)
