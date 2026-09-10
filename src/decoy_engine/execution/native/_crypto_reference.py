"""Pure-Python reference kernels for the native crypto contract.

Split out of `_crypto_ext` (module-size ratchet, native-throughput program): the
compiled-loader half and these oracle kernels have no runtime dependency on each
other (the loader self-tests against the embedded `HASH_KAT` vectors, not these
classes), so they live in sibling modules sharing the contract types + KATs
defined in `_crypto_ext`. These kernels reproduce the SHIPPED engine crypto
byte-for-byte so the compiled kernel can be graded against a known-good oracle;
see `_crypto_ext.CRYPTO_EXT_ABI` for the full contract.
"""

from __future__ import annotations

import pyarrow as pa

from decoy_engine.determinism import derive
from decoy_engine.errors import FpeChecksumError, FpeUnencryptableError
from decoy_engine.execution._strategies._fpe import FPE_KEY_LABEL, FpeStrategyHandler
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.kernel._canonicalize import canonicalize_derive_source
from decoy_engine.kernel._scalar import _array_to_pylist, _is_missing
from decoy_engine.transforms.fpe import fpe_decrypt_value, fpe_encrypt_value

from ._crypto_ext import (
    FpeBatchResult,
    FpeConfig,
    FpeKernel,
    FpeRowError,
    KernelInput,
    KeyedDerivationKernel,
    _require_mask_key,
)


class _ReferenceKeyedDerivation:
    """Pure-Python reference for `kernel/_scalar.hash_array`.

    Reuses the shipped normalization (`_array_to_pylist`), null policy
    (`_is_missing`), canonicalizer, and `derive`, so output is byte-identical
    by construction while carrying the native contract's mask_key naming and
    fail-closed guard. Accepts the pa.Array form and the mixed-object list
    form; the compiled kernel accepts only pa.Array (see CRYPTO_EXT_ABI)."""

    def derive_batch(
        self,
        values: KernelInput,
        *,
        mask_key: bytes | None,
        namespace: str,
        truncate: int | None,
        native_threads: int | None = None,
    ) -> pa.Array:
        # `native_threads` is accepted for Protocol conformance but IGNORED: this
        # single-threaded Python reference is the parity oracle, and the compiled
        # kernel's output is thread-invariant (proven at every thread count), so the
        # thread count never changes bytes on either side.
        del native_threads
        key = _require_mask_key(mask_key, "keyed_derivation")
        out: list[str | None] = []
        for value in _array_to_pylist(values):
            if _is_missing(value):
                out.append(None)
                continue
            token = derive(key, namespace, canonicalize_derive_source(value)).hex()
            out.append(token[:truncate] if truncate is not None else token)
        return pa.array(out, type=pa.string())


class _ReferenceFpe:
    """Pure-Python reference for `execution/_strategies/_fpe.py`.

    Reuses the shipped value primitive (`fpe_encrypt_value` / `fpe_decrypt_value`)
    and the shipped residual-risk warning method, and reproduces the strategy's
    config resolution, key derivation, tweak, null policy, and fail-closed
    error mapping, so output is byte-identical to the strategy."""

    _warner = FpeStrategyHandler()

    def encrypt_batch(
        self,
        values: KernelInput,
        *,
        mask_key: bytes | None,
        namespace: str,
        tweak_column: str,
        config: FpeConfig,
    ) -> FpeBatchResult:
        return self._run(
            values,
            mask_key=mask_key,
            namespace=namespace,
            tweak_column=tweak_column,
            config=config,
            forward=True,
        )

    def decrypt_batch(
        self,
        values: KernelInput,
        *,
        mask_key: bytes | None,
        namespace: str,
        tweak_column: str,
        config: FpeConfig,
    ) -> FpeBatchResult:
        return self._run(
            values,
            mask_key=mask_key,
            namespace=namespace,
            tweak_column=tweak_column,
            config=config,
            forward=False,
        )

    def _run(
        self,
        values: KernelInput,
        *,
        mask_key: bytes | None,
        namespace: str,
        tweak_column: str,
        config: FpeConfig,
        forward: bool,
    ) -> FpeBatchResult:
        key_material = _require_mask_key(mask_key, "fpe")
        charset, preserve_sep, validate_luhn, checksum = config._resolve()
        tweak = (config.join_group or tweak_column).encode("utf-8", errors="replace")
        key = derive(key_material, namespace, FPE_KEY_LABEL)
        transform = fpe_encrypt_value if forward else fpe_decrypt_value

        out: list[str | None] = []
        errors: list[FpeRowError] = []
        non_na_values: list[str] = []
        for row_index, value in enumerate(_array_to_pylist(values)):
            if _is_missing(value):
                out.append(None)
                continue
            text = str(value)
            non_na_values.append(text)
            try:
                out.append(
                    transform(text, key, charset, tweak, preserve_sep, validate_luhn, checksum)
                )
            except FpeUnencryptableError:
                out.append(None)
                errors.append(_row_error(row_index, "fpe_unencryptable_value"))
            except FpeChecksumError:
                out.append(None)
                errors.append(_row_error(row_index, "fpe_checksum_unsupported"))

        warnings = self._warnings(
            non_na_values,
            charset=charset,
            preserve_sep=preserve_sep,
            column=tweak_column,
            join_group=config.join_group,
        )
        return FpeBatchResult(
            values=pa.array(out, type=pa.string()),
            errors=tuple(errors),
            warnings=tuple(warnings),
        )

    def _warnings(
        self,
        non_na_values: list[str],
        *,
        charset: str,
        preserve_sep: bool,
        column: str,
        join_group: str | None,
    ) -> list[QualityWarning]:
        warnings = list(
            self._warner._residual_risk_warnings(
                non_na_values,
                charset_set=set(charset),
                radix=len(charset),
                preserve_sep=preserve_sep,
                column=column,
            )
        )
        if join_group:
            warnings.append(
                QualityWarning(
                    code="fpe_join_group_active",
                    provider="fpe",
                    column=column,
                    detail={
                        "join_group": join_group,
                        "security_note": "cross-column domain separation intentionally waived",
                    },
                )
            )
        return warnings


_ROW_ERROR_MESSAGES = {
    "fpe_unencryptable_value": (
        "value cannot be format-preserving-encrypted without leaking cleartext or "
        "producing non-invertible output; the engine fails closed."
    ),
    "fpe_checksum_unsupported": (
        "value has an invalid length for the configured checksum scheme; the engine fails closed."
    ),
}


def _row_error(row_index: int, code: str) -> FpeRowError:
    """Build a redacted per-row error (never carries the cell value)."""
    return FpeRowError(row_index=row_index, code=code, message=_ROW_ERROR_MESSAGES[code])


def reference_keyed_derivation() -> KeyedDerivationKernel:
    """Return the pure-Python keyed-derivation reference kernel."""
    return _ReferenceKeyedDerivation()


def reference_fpe() -> FpeKernel:
    """Return the pure-Python FPE reference kernel."""
    return _ReferenceFpe()
