"""Compiled FPE (FF1) kernel loader + the drop-in `FpeKernel` over the Rust companion.

Split from `_crypto_ext` (module-size ratchet): the keyed-derivation loader and this FPE
loader share the contract types + the ABI tag defined in `_crypto_ext`, but the compiled FPE
entry point (`decoy_engine_native._kernel.fpe_transform_batch`, C6a) has its own load-time
known-answer self-test and its own wrapper. `load_compiled_fpe_kernel` parallels
`load_compiled_crypto_kernel`: import the companion, check the ABI tag, run one `FPE_KAT`
vector through the FF1 entry point at load, and return a thin wrapper, else raise
`CryptoExtensionUnavailableError` before any output (the fail-before-output contract).

The compiled kernel reproduces only the non-checksum FF1 path: checksum modes decline to the
pandas oracle (C6a plan §3g), and config-level fail-closed cases (duplicate / degenerate
charset, missing namespace, tweak construction) are handled before dispatch so the oracle's
shipped handler raises them (plan §3d grading split). This wrapper therefore sees only
valid, non-checksum configs; it computes NO warnings (the Rust kernel computes none, and the
route adapter computes them from the original input at the oracle invocation scope, plan §3e).
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.determinism import DeterminismError
from decoy_engine.transforms.fpe import (
    FF1_TWEAK_SCOPE_COLUMN,
    FF1_TWEAK_SCOPE_JOIN_GROUP,
    build_ff1_tweak,
)

from ._crypto_ext import (
    _EXPECTED_ABI_VERSION,
    FPE_KAT,
    CryptoExtensionUnavailableError,
    FpeBatchResult,
    FpeConfig,
    FpeKernel,
    _require_mask_key,
)
from ._crypto_reference import _row_error

__all__ = [
    "load_compiled_fpe_kernel",
    "native_fpe",
]


def _translate_compiled_fpe_error(exc: ValueError) -> Exception:
    """Map one of the compiled FPE kernel's coded `ValueError`s onto the exception type the
    reference/strategy raises for the same input, so a caller catching the reference's types
    behaves identically against the compiled kernel. `mask_key_required` never reaches here:
    `_run` checks it up front via `_require_mask_key`, matching the reference's pre-loop guard."""
    code, _, detail = str(exc).partition(": ")
    if code in ("seed_wrong_length", "namespace_empty"):
        return DeterminismError(code=code, message=detail)
    # An unrecognized code means the compiled kernel raised something this wrapper was not built
    # to translate (a wiring error: a non-string array, a bad thread budget); surface it as-is.
    return exc


class _CompiledFpeKernel:
    """Thin wrapper around the compiled `decoy_engine_native._kernel.fpe_transform_batch`,
    satisfying the `FpeKernel` Protocol over `pa.Array` input and behaving as a drop-in for the
    values + per-row errors of `_ReferenceFpe` (it carries no warnings; those are computed in
    Python at the route's oracle invocation scope, plan §3e)."""

    def __init__(self, fpe_fn: Any) -> None:
        self._fpe_fn = fpe_fn

    def encrypt_batch(
        self,
        values: Any,
        *,
        mask_key: bytes | None,
        namespace: str,
        tweak_column: str,
        config: FpeConfig,
    ) -> FpeBatchResult:
        return self.run(
            values,
            mask_key=mask_key,
            namespace=namespace,
            tweak_column=tweak_column,
            config=config,
            forward=True,
        )

    def decrypt_batch(
        self,
        values: Any,
        *,
        mask_key: bytes | None,
        namespace: str,
        tweak_column: str,
        config: FpeConfig,
    ) -> FpeBatchResult:
        return self.run(
            values,
            mask_key=mask_key,
            namespace=namespace,
            tweak_column=tweak_column,
            config=config,
            forward=False,
        )

    def run(
        self,
        values: Any,
        *,
        mask_key: bytes | None,
        namespace: str,
        tweak_column: str,
        config: FpeConfig,
        forward: bool,
        native_threads: int | None = None,
    ) -> FpeBatchResult:
        # Fail before the compiled kernel is called, matching `_ReferenceFpe._run`'s pre-loop
        # `_require_mask_key` exactly (same message, same exception type).
        key = _require_mask_key(mask_key, "fpe")
        charset, preserve_sep, validate_luhn, checksum = config._resolve()
        if checksum is not None:  # pragma: no cover - checksum declines to the oracle before here
            raise AssertionError(
                "fpe checksum mode must decline to the pandas oracle before the native kernel "
                f"(column {tweak_column!r}); the native FF1 kernel has no checksum path."
            )
        tweak = build_ff1_tweak(
            FF1_TWEAK_SCOPE_JOIN_GROUP if config.join_group else FF1_TWEAK_SCOPE_COLUMN,
            config.join_group or tweak_column,
        )
        array = values.combine_chunks() if isinstance(values, pa.ChunkedArray) else values
        try:
            out, raw_errors = self._fpe_fn(
                array,
                mask_key=key,
                namespace=namespace,
                tweak=tweak,
                charset=charset,
                preserve_separators=preserve_sep,
                validate_luhn=validate_luhn,
                forward=forward,
                native_threads=native_threads,
            )
        except ValueError as exc:
            raise _translate_compiled_fpe_error(exc) from exc
        errors = tuple(_row_error(int(row_index), code) for row_index, code in raw_errors)
        return FpeBatchResult(values=out, errors=errors, warnings=())


def load_compiled_fpe_kernel() -> FpeKernel:
    """Load the compiled FF1 FPE kernel, or fail before any output.

    Imports the canonical compiled module (`decoy_engine_native._kernel`), checks its
    `abi_version()` against `_EXPECTED_ABI_VERSION` (abi-3 adds the `fpe_transform_batch`
    entry point), runs one `FPE_KAT` vector through it at load, and returns a thin wrapper.
    Any failure (the companion is absent, its import raises, its ABI tag does not match, its
    entry point is missing/not callable/raises, or it does not reproduce the reference FPE
    output) raises `CryptoExtensionUnavailableError` BEFORE returning, so a caller never holds
    a half-initialized or wrong-behaving kernel."""
    try:
        from decoy_engine_native import _kernel
    except Exception as exc:
        raise CryptoExtensionUnavailableError(
            "the decoy-engine-native companion is not installed or failed to load; "
            "install it directly, or use the pure-Python reference kernel (reference_fpe) instead."
        ) from exc

    try:
        reported_abi = _kernel.abi_version()
    except Exception as exc:
        raise CryptoExtensionUnavailableError(
            "the decoy-engine-native companion's abi_version() call failed; "
            "treating it as incompatible rather than risking a stale binary."
        ) from exc

    if reported_abi != _EXPECTED_ABI_VERSION:
        raise CryptoExtensionUnavailableError(
            f"the decoy-engine-native companion reports ABI {reported_abi!r}, "
            f"expected {_EXPECTED_ABI_VERSION!r}; refusing to run against a stale "
            "or incompatible binary (abi-3 adds the fpe_transform_batch entry point)."
        )

    # A matching ABI tag is necessary but not sufficient (same reasoning as
    # `load_compiled_crypto_kernel`): run one known-answer FPE vector through the entry point
    # HERE, at load, inside a guard, so a missing / non-callable / raising / mis-built FF1 kernel
    # becomes a fail-closed load error rather than a mid-encrypt failure.
    try:
        fpe_fn = _kernel.fpe_transform_batch
        probe = FPE_KAT[0]
        charset, preserve_sep, validate_luhn, checksum = probe.config._resolve()
        tweak = build_ff1_tweak(FF1_TWEAK_SCOPE_COLUMN, probe.tweak_column)
        out, errors = fpe_fn(
            pa.array([probe.plaintext], type=pa.string()),
            mask_key=probe.mask_key,
            namespace=probe.namespace,
            tweak=tweak,
            charset=charset,
            preserve_separators=preserve_sep,
            validate_luhn=validate_luhn,
            forward=True,
            native_threads=1,
        )
        probe_reproduces_reference = (
            checksum is None
            and isinstance(out, pa.Array)
            and out.type == pa.string()
            and out.to_pylist() == [probe.ciphertext]
            and len(errors) == 0
        )
    except Exception as exc:
        raise CryptoExtensionUnavailableError(
            "the decoy-engine-native companion's 'fpe_transform_batch' entry point is missing, "
            "not callable, or raised during the load-time self-test; treating it as incompatible "
            "rather than returning a half-initialized kernel that would fail mid-encrypt."
        ) from exc

    if not probe_reproduces_reference:
        raise CryptoExtensionUnavailableError(
            "the decoy-engine-native companion failed its load-time known-answer FPE self-test; "
            "refusing to run a binary that does not reproduce the reference FF1 encryption."
        )

    return _CompiledFpeKernel(fpe_fn)


def native_fpe(
    array: pa.Array | pa.ChunkedArray,
    *,
    mask_key: bytes | None,
    namespace: str,
    tweak_column: str,
    config: FpeConfig,
    forward: bool = True,
    native_threads: int | None = None,
) -> FpeBatchResult:
    """Run the compiled FF1 kernel over one column: load it (fail-closed), then encrypt/decrypt.

    Mirrors `native_keyed_hash`'s shape (load the compiled kernel, forward the call); this module
    never falls back to the reference kernel. `tweak_column` is the FF1 tweak identity (the target
    column name, or the join group when set). The result carries the output array and the ordered
    per-row errors; the fail-closed `StrategyError` kill is applied by the route adapter, not here."""
    kernel = load_compiled_fpe_kernel()
    return kernel.run(  # type: ignore[attr-defined]
        array,
        mask_key=mask_key,
        namespace=namespace,
        tweak_column=tweak_column,
        config=config,
        forward=forward,
        native_threads=native_threads,
    )
