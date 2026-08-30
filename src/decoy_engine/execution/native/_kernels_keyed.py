"""Native keyed-`hash` node: the joinability-preserving token, wired to the
compiled Rust `KeyedDerivationKernel` instead of `kernel/_scalar.hash_array`.

`native_keyed_hash` reproduces `HashStrategyHandler.run`'s config resolution
(namespace required, `truncate` resolved from a raw config value) so a
config that reached the handler would drive the compiled kernel to the
same output, but it does so over a bare Arrow array with no DataFrame and
no `StrategyContext` -- this is a pure array-to-array transform, matching
`_kernels_scalar.py`'s shape for the non-keyed strategies. The actual
derivation (HKDF-SHA256 then HMAC-SHA256, see `determinism/_derive.py`)
lives in the compiled kernel, loaded via `load_compiled_crypto_kernel`;
this module only resolves config and forwards the call. The pure-Python
`reference_keyed_derivation` stays the test oracle (crypto-testing-reference
Section 3): this module never falls back to it.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution.native._crypto_ext import load_compiled_crypto_kernel


def native_keyed_hash(
    array: pa.Array | pa.ChunkedArray,
    *,
    mask_key: bytes | None,
    namespace: str | None,
    truncate: Any = None,
) -> pa.Array:
    """Derive one token per value: `derive(mask_key, namespace, canonical(value))`,
    optionally truncated, matching `HashStrategyHandler` byte for byte.

    A `None` namespace is a wiring error (a hash column reached this node
    without one), checked here before the compiled kernel is even loaded so
    the failure never depends on whether the companion is installed.
    `truncate` accepts the raw provider-config value and resolves it exactly
    the way the handler does: an `int > 0` is used as-is, anything else
    (`None`, zero, negative, wrong type) becomes "no truncation" -- this
    resolution happens BEFORE the value reaches the compiled kernel, whose
    own `truncate` is a raw signed-slice width and would otherwise apply a
    negative truncate as a from-the-end slice instead of treating it as
    absent.

    A missing, `None`, or empty `mask_key` fails closed via the loaded
    kernel's own contract (`MaskKeyRequiredError`, raised before any row is
    processed); this function does not duplicate that guard.
    """
    if namespace is None:
        raise StrategyError(
            code="hash_requires_namespace",
            strategy="hash",
            message="the hash strategy requires a namespace; got None.",
        )
    resolved_truncate = truncate if isinstance(truncate, int) and truncate > 0 else None
    kernel = load_compiled_crypto_kernel()
    return kernel.derive_batch(
        array, mask_key=mask_key, namespace=namespace, truncate=resolved_truncate
    )


__all__ = ["native_keyed_hash"]
