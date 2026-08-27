"""Keyed-crypto + FPE native-extension contract with pure-Python references.

Phase 0 of the engine-efficiency program lands the CONTRACT and the oracle
only: the compiled Rust kernel is specified here (`CRYPTO_EXT_ABI`) but built
in Phase 2. Two pure-Python reference kernels reproduce the SHIPPED engine
crypto byte-for-byte so the compiled kernel can be graded against a known-good
oracle, and known-answer vectors pin the current output.

Crypto model (reproduced from the shipped engine, rolled nothing new):

- Keyed derivation (the `hash` strategy, `kernel/_scalar.hash_array`):
  ``token = derive(mask_key, namespace, canonicalize_derive_source(value)).hex()``,
  optionally truncated. `derive` is HKDF-SHA256 (per-context key) then
  HMAC-SHA256 (per-source value); see `determinism/_derive.py`. Nulls (None or
  NaN) pass through as None.

- FPE (the `fpe` strategy, `execution/_strategies/_fpe.py`, SEED_PROTOCOL v6):
  ONE Feistel key per `(mask_key, namespace)` = ``derive(mask_key, namespace,
  FPE_KEY_LABEL)`` with ``FPE_KEY_LABEL = b"fpe-key/v1"``. The per-column tweak
  is the column name UTF-8 (or the `fpe_join_group` name when set, so grouped
  columns share ciphertext). The primitive is the home-rolled 8-round
  HMAC-SHA256 type-II Feistel in `transforms/fpe.py` (NOT NIST SP 800-38G FF1:
  no AES, 8 rounds, no minimum-domain floor). Luhn mode permutes the body and
  appends the check digit (invertible); checksum mode takes priority over
  `validate_luhn` when both are set. Config resolution (charset dedup,
  Luhn digit-charset gate, checksum priority, join-group tweak) mirrors the
  shipped strategy's `run` exactly.

Fail-closed contract (hard, tested):

- A missing / None `mask_key` raises `MaskKeyRequiredError` BEFORE any output.
- A missing or ABI-incompatible compiled extension raises
  `CryptoExtensionUnavailableError` BEFORE any staging or output.
- A per-row value that cannot be format-preserving-encrypted is recorded as a
  structured `FpeRowError` (redacted, never carrying the cell value) and its
  output row is None. The execution boundary maps a non-empty error set to the
  shipped `StrategyError` fail-closed kill (see `CRYPTO_EXT_ABI`).

References:
- RFC 5869 (HKDF-SHA256), RFC 2104 (HMAC-SHA256).
- Feistel (1973), type-II Feistel construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

import pyarrow as pa

from decoy_engine.determinism import derive
from decoy_engine.errors import (
    DecoyError,
    FpeChecksumError,
    FpeUnencryptableError,
    MaskKeyRequiredError,
)
from decoy_engine.execution._strategies._fpe import FPE_KEY_LABEL, FpeStrategyHandler
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.kernel._canonicalize import canonicalize_derive_source
from decoy_engine.kernel._scalar import _array_to_pylist, _is_missing
from decoy_engine.transforms.fpe import _CHARSETS, fpe_decrypt_value, fpe_encrypt_value

KernelInput: TypeAlias = pa.Array | pa.ChunkedArray | list[Any]


# ---------------------------------------------------------------------------
# Compiled-extension ABI (specified now, built in Phase 2).
# ---------------------------------------------------------------------------

CRYPTO_EXT_ABI = """\
Compiled crypto extension ABI (Phase 2 target; specified in Phase 0).

Ownership
  The extension owns no engine state. Each call is pure: inputs are borrowed
  for the duration of the call and outputs are freshly allocated Arrow arrays
  the caller owns. Key material (mask_key, derived Feistel key) is passed in
  per call, never cached across calls, and never logged.

Thread model
  Kernels are stateless and reentrant. A batch may be split into row chunks
  processed on worker threads and concatenated; every row's derivation and
  encryption is independent and deterministic under the shared key, so chunked
  and serial output are byte-identical by construction (the parity gate). The
  extension holds no locks and no thread-local mutable state.

Packaging
  Shipped as a compiled dynamic library loaded through a thin Python shim. The
  shim verifies an ABI version tag on load; a tag mismatch is treated as an
  incompatible extension (see fail-before-output).

Arrow C Data Interface
  Typed columns cross the boundary as Arrow arrays over the C Data Interface
  (zero copy where the layout allows). The compiled derive_batch accepts ONLY
  the pa.Array form. A mixed-object column (str and int in one column) has no
  single Arrow type; the compiled kernel REJECTS the Python list form with a
  coded `mixed_object_not_native` error and the caller routes that column to
  the pure-Python reference kernel. The reference kernel here accepts BOTH the
  pa.Array and the list form.

Fail-before-output
  A missing shared library, a failed load, or an ABI version mismatch raises
  CryptoExtensionUnavailableError BEFORE any staging or output is produced, so
  a job never emits partially-native or silently-degraded output. A missing or
  None mask_key raises MaskKeyRequiredError before any row is processed. A
  per-row value that cannot be format-preserving-encrypted is returned as a
  structured, redacted FpeRowError (never the cell value); the execution
  boundary maps a non-empty error set to the shipped StrategyError kill.
"""


class CryptoExtensionUnavailableError(DecoyError):
    """The compiled crypto extension is missing or ABI-incompatible.

    Raised BEFORE any staging or output (the fail-before-output contract) so a
    job never runs half-native. In Phase 0 the extension is not built, so
    `load_compiled_crypto_kernel` always raises this."""

    code: str = "crypto_ext.unavailable"


class FpeConfigError(DecoyError):
    """An FPE configuration cannot mask a column (config-level fail-closed).

    Currently one case: a resolved charset with fewer than 2 distinct
    characters has nothing to permute over and would leave the column in the
    clear. Mirrors the shipped strategy's `fpe_charset_degenerate` raise."""

    code: str = "fpe.config_invalid"


# ---------------------------------------------------------------------------
# FPE configuration + batch result.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FpeConfig:
    """FPE options for one column, resolved exactly as the shipped strategy does.

    `charset` is a named preset (`digits`, `alpha`, `ALPHA`, `alphanum`,
    `ALPHANUM`) or an explicit charset string. `_resolve` reproduces
    `execution/_strategies/_fpe.py::run`: dedup the charset preserving order,
    gate `validate_luhn` to an all-digit charset with no checksum, and let
    `checksum` take priority over `validate_luhn`."""

    charset: str = "digits"
    preserve_separators: bool = True
    validate_luhn: bool = False
    checksum: str | None = None
    join_group: str | None = None

    @classmethod
    def from_mapping(cls, cfg: Mapping[str, Any]) -> FpeConfig:
        """Build from a provider-config mapping using the shipped defaults."""
        return cls(
            charset=cfg.get("charset", "digits"),
            preserve_separators=bool(cfg.get("preserve_separators", True)),
            validate_luhn=bool(cfg.get("validate_luhn", False)),
            checksum=cfg.get("checksum") or None,
            join_group=cfg.get("fpe_join_group") or None,
        )

    def _resolve(self) -> tuple[str, bool, bool, str | None]:
        """Return (charset, preserve_separators, validate_luhn, checksum).

        Byte-for-byte the resolution the shipped strategy performs before it
        derives the key or encrypts a value."""
        charset = "".join(dict.fromkeys(_CHARSETS.get(self.charset, self.charset)))
        if len(charset) < 2:
            raise FpeConfigError(
                f"fpe charset {self.charset!r} resolved to {charset!r} with fewer than "
                "2 distinct characters; a degenerate charset has nothing to permute "
                "over and would leave the column unmasked."
            )
        checksum = self.checksum or None
        validate_luhn = (
            checksum is None
            and bool(self.validate_luhn)
            and all(c in "0123456789" for c in charset)
        )
        return charset, bool(self.preserve_separators), validate_luhn, checksum


@dataclass(frozen=True)
class FpeRowError:
    """A structured, redacted per-row FPE failure.

    `message` NEVER embeds the cell value (the row-error framework's PII
    discipline; the source exception's own text does embed it, so it is not
    copied here)."""

    row_index: int
    code: str
    message: str


@dataclass(frozen=True)
class FpeBatchResult:
    """FPE batch output plus structured per-row errors and warnings."""

    values: pa.Array
    errors: tuple[FpeRowError, ...] = ()
    warnings: tuple[QualityWarning, ...] = ()

    def to_pylist(self) -> list[Any]:
        """Convenience passthrough to the output array's `to_pylist`."""
        return self.values.to_pylist()


# ---------------------------------------------------------------------------
# Known-answer vectors (generated from the shipped primitives, pinned here).
# ---------------------------------------------------------------------------

_KAT_MASK_KEY = bytes(range(32))
_KAT_NS = "people.ssn"


@dataclass(frozen=True)
class HashKatVector:
    mask_key: bytes
    namespace: str
    value: Any
    truncate: int | None
    expected: str


@dataclass(frozen=True)
class FpeKatVector:
    mask_key: bytes
    namespace: str
    tweak_column: str
    config: FpeConfig
    plaintext: str
    ciphertext: str


HASH_KAT: tuple[HashKatVector, ...] = (
    HashKatVector(
        _KAT_MASK_KEY,
        _KAT_NS,
        "alice",
        None,
        "398a93520101bdc8e91ad659396a2bdf262bb59224ba39bfc807e075c33ab64c",
    ),
    HashKatVector(
        _KAT_MASK_KEY,
        _KAT_NS,
        "bob",
        None,
        "c7f070cc2e08f825a284b4aa24fec05546ea4e65feb806f2221610e7a13195c1",
    ),
    HashKatVector(
        _KAT_MASK_KEY,
        _KAT_NS,
        12345,
        None,
        "afc185c92f6ff544f3fb3b4cb21435f67ada4a946a150e205aa59ef490168773",
    ),
    HashKatVector(_KAT_MASK_KEY, _KAT_NS, "alice", 16, "398a93520101bdc8"),
)

FPE_KAT: tuple[FpeKatVector, ...] = (
    FpeKatVector(
        _KAT_MASK_KEY, _KAT_NS, "ssn", FpeConfig(charset="digits"), "123456789", "528311328"
    ),
    FpeKatVector(
        _KAT_MASK_KEY,
        _KAT_NS,
        "ssn",
        FpeConfig(charset="digits"),
        "123-45-6789",
        "528-31-1328",
    ),
    FpeKatVector(
        _KAT_MASK_KEY,
        _KAT_NS,
        "ssn",
        FpeConfig(charset="digits", validate_luhn=True),
        "4111111111111111",
        "8913116545234802",
    ),
)


# ---------------------------------------------------------------------------
# Kernel protocols.
# ---------------------------------------------------------------------------


class KeyedDerivationKernel(Protocol):
    """The `hash` strategy as a batch kernel over one column."""

    def derive_batch(
        self,
        values: KernelInput,
        *,
        mask_key: bytes | None,
        namespace: str,
        truncate: int | None,
    ) -> pa.Array: ...


class FpeKernel(Protocol):
    """The `fpe` strategy as a batch kernel over one column."""

    def encrypt_batch(
        self,
        values: KernelInput,
        *,
        mask_key: bytes | None,
        namespace: str,
        tweak_column: str,
        config: FpeConfig,
    ) -> FpeBatchResult: ...

    def decrypt_batch(
        self,
        values: KernelInput,
        *,
        mask_key: bytes | None,
        namespace: str,
        tweak_column: str,
        config: FpeConfig,
    ) -> FpeBatchResult: ...


# ---------------------------------------------------------------------------
# Pure-Python reference implementations (the Phase 2 oracle).
# ---------------------------------------------------------------------------


def _require_mask_key(mask_key: bytes | None, kernel: str) -> bytes:
    """Fail closed before any output when the mask key is missing."""
    if not mask_key:
        raise MaskKeyRequiredError(
            f"{kernel} requires a mask_key; got {mask_key!r}. Refusing to emit "
            "unkeyed or default-keyed output.",
            kernel=kernel,
        )
    return mask_key


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
    ) -> pa.Array:
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


def load_compiled_crypto_kernel() -> Any:
    """Load the compiled crypto extension, or fail before any output.

    Phase 0 does not ship the compiled extension, so this always raises
    `CryptoExtensionUnavailableError`. Phase 2 replaces the body with the real
    load + ABI-version check; the fail-before-output contract is unchanged."""
    raise CryptoExtensionUnavailableError(
        "the compiled crypto extension is not built in Phase 0; it is specified by "
        "CRYPTO_EXT_ABI and built in Phase 2. Use the pure-Python reference kernels "
        "(reference_keyed_derivation / reference_fpe) until then."
    )


__all__ = [
    "CRYPTO_EXT_ABI",
    "FPE_KAT",
    "HASH_KAT",
    "CryptoExtensionUnavailableError",
    "FpeBatchResult",
    "FpeConfig",
    "FpeConfigError",
    "FpeKatVector",
    "FpeKernel",
    "FpeRowError",
    "HashKatVector",
    "KeyedDerivationKernel",
    "load_compiled_crypto_kernel",
    "reference_fpe",
    "reference_keyed_derivation",
]
