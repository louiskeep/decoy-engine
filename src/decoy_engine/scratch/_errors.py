"""Typed failures for the protected-scratch subsystem.

Every class here is a ``DecoyError`` so ``except DecoyError`` catches it (the
exception-taxonomy sentry enforces that). Each carries a stable ``code`` for
machine routing and never puts source values, keys, DSNs or user-derived
filenames in its message (plan 1.5 ResourceLedger record rule, 1.7).

The shared quota-failure code is ``scratch_quota_exceeded``; existing public
wrappers keep their documented code (e.g. ``out_of_core_temp_disk_exceeded``)
and attach the shared report when they migrate in Slices 2-3.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from decoy_engine.errors import DecoyError

if TYPE_CHECKING:
    from decoy_engine.scratch._reports import CleanupReport, QuotaReport


class ScratchError(DecoyError):
    """Base class for every protected-scratch failure."""

    def __init__(self, *, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}" if message else f"[{code}]")


class ScratchBackendUnavailable(ScratchError):  # noqa: N818 -- design-named F25 failure type (plan 1.5)
    """No qualified storage backend, so a temp-storage operation is refused
    before any raw byte is written (plan 1.5, A13). This is an intentional
    fail-closed compatibility posture, never a silent fallback to /tmp."""

    def __init__(self, *, message: str = "", reasons: tuple[str, ...] = ()) -> None:
        self.reasons = reasons
        super().__init__(code="scratch_backend_unavailable", message=message)


class ScratchQuotaExceeded(ScratchError):  # noqa: N818 -- design-named F25 failure type (plan 1.5)
    """A write, close, footer flush, fsync or SQL step hit the kernel hard
    quota (byte or inode). The attempt stops and publication is prohibited
    (plan 1.5 item 5, A02). Carries the typed report; the primary failure is
    preserved even if cleanup also fails."""

    def __init__(self, *, report: QuotaReport, message: str = "") -> None:
        self.report = report
        super().__init__(code="scratch_quota_exceeded", message=message)


class ScratchPathUnsafe(ScratchError):  # noqa: N818 -- design-named F25 failure type (plan 1.5)
    """An arena path failed a safety check: symlink, traversal, unexpected
    owner/mode, hard-link alias, mount substitution or escape beneath the
    approved root (plan 1.5, A01)."""

    def __init__(self, *, message: str = "") -> None:
        super().__init__(code="scratch_path_unsafe", message=message)


class ScratchOwnershipError(ScratchError):
    """A durable ownership transaction failed its affected-row / generation
    predicate: a CAS loser, a moved fence, or a row the authority no longer
    owns (plan 1.5, ledger subordination contract 2.4)."""

    def __init__(self, *, code: str = "scratch_ownership_conflict", message: str = "") -> None:
        super().__init__(code=code, message=message)


class ScratchControlUnavailable(ScratchError):  # noqa: N818 -- design-named F25 failure type (plan 1.5)
    """The durable control authority (SQLite) could not be reached or reserved:
    a bounded busy-wait timed out, the control arena is full, or its sidecars do
    not resolve to one device (plan 1.5 control-arena bootstrap, A13). Treated as
    unavailable authority: resources are retained, never deleted."""

    def __init__(self, *, code: str = "scratch_control_unavailable", message: str = "") -> None:
        super().__init__(code=code, message=message)


class ScratchCleanupError(ScratchError):
    """Cleanup could not confirm reclamation. The charge and inventory are
    retained; the report distinguishes deleted/retained/pending/unknown and
    never claims a securely-erased zero just because a pathname disappeared
    (plan 1.5 item 6, 1.7)."""

    def __init__(self, *, report: CleanupReport, message: str = "") -> None:
        self.report = report
        super().__init__(code="scratch_cleanup_incomplete", message=message)


class ScratchRecoveryKeyUnavailable(ScratchError):  # noqa: N818 -- design-named F25 failure type (plan 1.7)
    """A checkpoint's wrapping key is missing, revoked, wrong-version or the key
    service is down. The checkpoint is retained; key failure is never evidence
    that it is disposable (plan 1.7). API surface only in Slice 1; the envelope
    is wired in Slice 4."""

    def __init__(self, *, message: str = "") -> None:
        super().__init__(code="scratch_recovery_key_unavailable", message=message)


class ScratchRecoveryInvalid(ScratchError):  # noqa: N818 -- design-named F25 failure type (plan 1.7)
    """A checkpoint recovery envelope failed authentication (wrong key, tamper,
    arena substitution). Retained with a safe diagnostic (plan 1.7). API surface
    only in Slice 1."""

    def __init__(self, *, message: str = "") -> None:
        super().__init__(code="scratch_recovery_invalid", message=message)
