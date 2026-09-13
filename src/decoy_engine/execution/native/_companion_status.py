"""Public, side-effect-free probe of the optional `decoy-engine-native` companion.

`load_compiled_crypto_kernel` / `load_compiled_index_kernel` (`_crypto_ext.py`,
`_index_ext.py`) each run the same staged check -- import, ABI-tag compare, a
load-time known-answer self-test -- and fold every failure into one
`CryptoExtensionUnavailableError` so a masking caller has a single thing to
catch. A platform-level fail-closed startup gate needs the opposite: which
stage failed, and something to chain a `RuntimeError` onto, without pulling in
the private loaders or duplicating any cryptographic primitive. This module
reuses the same staged primitives (`_EXPECTED_ABI_VERSION`, `HASH_KAT`,
`INDEX_KAT`) the loaders already pin, in a read-only probe that never raises
and never returns a usable kernel -- it only classifies.

Both kernels are checked because the index kernel is an ADDITIVE symbol on the
shared abi-2 companion (`_index_ext` module docstring): a companion built
before it existed reports the right ABI and passes the hash KAT while lacking
`derive_index_batch` entirely. Such a companion is real but incomplete, so
`ok` is False for it too -- a caller must not see `present-ok` for a partially
capable companion.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
from dataclasses import dataclass
from typing import Literal, TypeAlias

import pyarrow as pa

from decoy_engine.errors import DecoyError

from ._crypto_ext import _EXPECTED_ABI_VERSION, HASH_KAT
from ._index_ext import INDEX_KAT

_DISTRIBUTION_NAME = "decoy-engine-native"

Reason: TypeAlias = Literal["present-ok", "absent", "abi-mismatch", "kat-corrupt", "load-error"]


class NativeCompanionCheckError(DecoyError):
    """Synthesized `cause` for a companion-probe failure with no natural exception.

    `native_companion_status()` never raises; the contract is `cause is not
    None` whenever `not ok`. A stage that fails via a real exception (an
    import failure, a raising `abi_version()` call, an entry point that
    raises during its known-answer self-test) preserves that exception as
    `cause` unchanged. A stage that fails without one -- the module is
    simply absent, the ABI tag compares unequal, or a self-test ran to
    completion and returned the wrong value -- has no exception to preserve,
    so it gets one of these instead, carrying the same `reason` /
    `abi_expected` / `abi_actual` the status itself reports, so a caller
    chaining `raise RuntimeError(...) from status.cause` gets a `cause` that
    is informative on every path, not just the ones a loader happened to
    raise on."""

    code: str = "native_companion.check_failed"

    def __init__(
        self,
        message: str,
        *,
        reason: Reason,
        abi_expected: str,
        abi_actual: str | None,
    ) -> None:
        self.reason = reason
        self.abi_expected = abi_expected
        self.abi_actual = abi_actual
        super().__init__(message)


@dataclass(frozen=True)
class NativeCompanionStatus:
    """Result of probing the optional `decoy-engine-native` companion.

    `present` is True once the module resolves and imports at all (even if
    it then fails ABI or KAT); it is False only for `absent`. `ok` is True
    only for `present-ok`: both the crypto and index kernels imported,
    reported the expected ABI tag, and reproduced their known-answer
    vectors. `version` is the installed companion distribution's version,
    recorded for provenance only -- never gated on, since ABI compatibility
    is what the tag check already establishes. `cause` is populated on
    every failure (never None when `ok` is False); see
    `NativeCompanionCheckError` for the synthesized case."""

    present: bool
    ok: bool
    abi_expected: str
    abi_actual: str | None
    version: str | None
    reason: Reason
    cause: BaseException | None


def _companion_version() -> str | None:
    """Best-effort installed-distribution version; None if not registered.

    Purely informational (see `NativeCompanionStatus.version`): a companion
    can be present and even fully functional without a discoverable
    distribution metadata record (e.g. a dev build installed by copying the
    `.so` directly), so a lookup failure here is not itself a probe failure."""
    try:
        return importlib.metadata.version(_DISTRIBUTION_NAME)
    except Exception:
        # Purely informational, so any metadata failure (not just
        # PackageNotFoundError -- a malformed record can raise other
        # importlib.metadata errors) degrades to None rather than breaking the
        # never-raises contract a startup gate relies on.
        return None


def _probe_hash_kat(kernel: object, abi_actual: str) -> tuple[Reason, BaseException] | None:
    """Run `HASH_KAT[0]` through `kernel.derive_batch`, mirroring
    `load_compiled_crypto_kernel`'s load-time self-test exactly (same vector,
    same call shape, same Arrow-type-and-value check). Returns None on a
    reproduced result, `("load-error", exc)` if resolving or calling the
    entry point raises (missing symbol, non-callable, a raising descriptor,
    or the entry point itself erroring), or `("kat-corrupt", ...)` if it
    returns without raising but reproduces the wrong value."""
    probe = HASH_KAT[0]
    try:
        derive_batch_fn = kernel.derive_batch  # type: ignore[attr-defined]
        probe_out = derive_batch_fn(
            pa.array([probe.value]),
            mask_key=probe.mask_key,
            namespace=probe.namespace,
            truncate=probe.truncate,
            native_threads=1,
        )
    except Exception as exc:
        return "load-error", exc
    reproduces = (
        isinstance(probe_out, pa.Array)
        and probe_out.type == pa.string()
        and probe_out.to_pylist() == [probe.expected]
    )
    if reproduces:
        return None
    return "kat-corrupt", NativeCompanionCheckError(
        "the decoy-engine-native companion's derive_batch reproduced the wrong "
        "value for the pinned HASH_KAT known-answer vector",
        reason="kat-corrupt",
        abi_expected=_EXPECTED_ABI_VERSION,
        abi_actual=abi_actual,
    )


def _probe_index_kat(kernel: object, abi_actual: str) -> tuple[Reason, BaseException] | None:
    """Run `INDEX_KAT` through `kernel.derive_index_batch`, mirroring
    `load_compiled_index_kernel`'s load-time self-test exactly. A companion
    built before the index kernel existed (a legitimately still-valid abi-2
    build for the hash route alone) lacks `derive_index_batch` entirely,
    which is an `AttributeError` caught here and classified `load-error` --
    such a companion is incomplete, so the overall probe must not report
    `present-ok` for it (see the module docstring)."""
    try:
        derive_index_batch_fn = kernel.derive_index_batch  # type: ignore[attr-defined]
        probe_out = derive_index_batch_fn(
            pa.array(INDEX_KAT.values, type=pa.string()),
            mask_key=INDEX_KAT.mask_key,
            namespace=INDEX_KAT.namespace,
            pool_size=INDEX_KAT.pool_size,
            native_threads=1,
        )
    except Exception as exc:
        return "load-error", exc
    reproduces = (
        isinstance(probe_out, pa.Array)
        and probe_out.type == pa.uint64()
        and probe_out.to_pylist() == list(INDEX_KAT.expected)
    )
    if reproduces:
        return None
    return "kat-corrupt", NativeCompanionCheckError(
        "the decoy-engine-native companion's derive_index_batch reproduced the "
        "wrong value for the pinned INDEX_KAT known-answer vector",
        reason="kat-corrupt",
        abi_expected=_EXPECTED_ABI_VERSION,
        abi_actual=abi_actual,
    )


def _absent_status(exc: ModuleNotFoundError | None) -> NativeCompanionStatus:
    cause: BaseException = exc or NativeCompanionCheckError(
        "the decoy-engine-native companion is not installed (no 'native' extra "
        "exists yet; install it directly, see docs/native/supported-matrix.md)",
        reason="absent",
        abi_expected=_EXPECTED_ABI_VERSION,
        abi_actual=None,
    )
    return NativeCompanionStatus(
        present=False,
        ok=False,
        abi_expected=_EXPECTED_ABI_VERSION,
        abi_actual=None,
        version=None,
        reason="absent",
        cause=cause,
    )


def _load_error_status(exc: BaseException) -> NativeCompanionStatus:
    return NativeCompanionStatus(
        present=True,
        ok=False,
        abi_expected=_EXPECTED_ABI_VERSION,
        abi_actual=None,
        version=_companion_version(),
        reason="load-error",
        cause=exc,
    )


def native_companion_status() -> NativeCompanionStatus:
    """Probe the optional `decoy-engine-native` companion; never raises.

    Drives the same staged check the two private loaders perform -- import,
    ABI-tag compare, known-answer self-test -- for BOTH the crypto and index
    kernels, so a partially-capable companion (only one kernel actually
    works) is reported `ok=False`, never `present-ok`. Every failure stage
    populates `cause`: a real loader exception is preserved as caught; a
    stage with no natural exception (absent, ABI mismatch, a self-test that
    ran and returned the wrong value) gets a synthesized
    `NativeCompanionCheckError` carrying the same `reason` /
    `abi_expected` / `abi_actual`.

    This function loads no kernel for use -- `load_compiled_crypto_kernel`
    and `load_compiled_index_kernel` remain the only way to get a callable
    kernel; this is a read-only status check for a startup gate."""
    try:
        spec = importlib.util.find_spec("decoy_engine_native")
    except ModuleNotFoundError as exc:
        # Only a MISSING decoy_engine_native itself is genuine absence; a
        # different module surfacing here (a broken dependency of its import
        # machinery) is a load-error, not absence -- keeps the diagnostic honest.
        if exc.name in (None, "decoy_engine_native"):
            return _absent_status(exc)
        return _load_error_status(exc)
    except Exception as exc:
        # find_spec can raise beyond ModuleNotFoundError (e.g. ValueError when a
        # parent package's __spec__ is None): the import machinery is present but
        # broken, so this is a load-error, not absence. Keeps the never-raises
        # contract that a startup gate relies on.
        return _load_error_status(exc)
    if spec is None:
        return _absent_status(None)

    try:
        kernel = importlib.import_module("decoy_engine_native._kernel")
    except Exception as exc:
        return _load_error_status(exc)

    try:
        reported_abi = kernel.abi_version()
    except Exception as exc:
        return _load_error_status(exc)

    version = _companion_version()

    if reported_abi != _EXPECTED_ABI_VERSION:
        cause = NativeCompanionCheckError(
            f"the decoy-engine-native companion reports ABI {reported_abi!r}, "
            f"expected {_EXPECTED_ABI_VERSION!r}",
            reason="abi-mismatch",
            abi_expected=_EXPECTED_ABI_VERSION,
            abi_actual=reported_abi,
        )
        return NativeCompanionStatus(
            present=True,
            ok=False,
            abi_expected=_EXPECTED_ABI_VERSION,
            abi_actual=reported_abi,
            version=version,
            reason="abi-mismatch",
            cause=cause,
        )

    for outcome in (
        _probe_hash_kat(kernel, reported_abi),
        _probe_index_kat(kernel, reported_abi),
    ):
        if outcome is not None:
            kat_reason, kat_cause = outcome
            return NativeCompanionStatus(
                present=True,
                ok=False,
                abi_expected=_EXPECTED_ABI_VERSION,
                abi_actual=reported_abi,
                version=version,
                reason=kat_reason,
                cause=kat_cause,
            )

    return NativeCompanionStatus(
        present=True,
        ok=True,
        abi_expected=_EXPECTED_ABI_VERSION,
        abi_actual=reported_abi,
        version=version,
        reason="present-ok",
        cause=None,
    )


__all__ = [
    "NativeCompanionCheckError",
    "NativeCompanionStatus",
    "native_companion_status",
]
