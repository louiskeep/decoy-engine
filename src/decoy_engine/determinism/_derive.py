"""Deterministic mapping primitives: the envelope + the helpers.

`derive(seed, namespace, source) -> bytes` is the single guarantee this
module makes: same `(seed, namespace, source)` produces byte-identical
output across processes, days, and engine versions while
`SEED_PROTOCOL_VERSION` is unchanged. Every deterministic-mode column
in V2 routes through this function.

The envelope (per spec §2):

    HMAC_key   = HKDF-SHA256(IKM=seed, salt=b"decoy-engine/determinism/v1",
                             info=namespace.encode("utf-8"), length=32)
    HMAC_input = (
        bytes([SEED_PROTOCOL_VERSION])               # 1 byte; 0x01 today
        + len(namespace).to_bytes(4, "big") + namespace.encode("utf-8")
        + len(source).to_bytes(4, "big") + source
    )
    output     = HMAC-SHA256(HMAC_key, HMAC_input)   # 32 bytes

Length-prefixing on namespace + source makes the concatenation injective
(without it, `"abc" + "def"` and `"abcd" + "ef"` would collide).

The `seed_protocol_version` byte is mixed into the HMAC input (not the
HKDF salt) because the salt binds "what is this primitive doing" (S3
determinism vs some other future primitive) while the version byte binds
"which exact envelope shape." Bumping v1 -> v2 in the future produces
different output for the same `(seed, namespace, source)`, which is the
contract the operating model's `seed_protocol_version` field encodes.

References:
- RFC 5869 (HKDF-SHA256): https://datatracker.ietf.org/doc/html/rfc5869
- RFC 2104 (HMAC-SHA256): https://datatracker.ietf.org/doc/html/rfc2104

Source pattern: HKDF binds the per-context key; HMAC binds the per-source
value. The split is the standard pattern for "derive a stable per-context
key from a master, then mix per-input data." Both primitives are implemented
on stdlib `hmac` + `hashlib` only (see `_hkdf.py` for the rationale; the
engine has an explicit anti-PyCA design choice in `transforms/fpe.py`).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Protocol

from decoy_engine.determinism._hkdf import hkdf_sha256

# The version byte mixed into every HMAC input. Bumping it requires a
# release-notes line per done-definition.md; manifests with an older
# version cannot be reproduced by post-bump builds.
SEED_PROTOCOL_VERSION: int = 1

_SALT = b"decoy-engine/determinism/v1"
_SEED_LENGTH = 8  # exactly 8 bytes; raises on any other length
_POOL_SIZE_MAX = 1 << 56  # > 2**56 raises pool_size_overflow


class DeterminismError(Exception):
    """Runtime input-validation failure inside the determinism layer.

    Not a subclass of PlanCompileError: the determinism layer is a runtime
    primitive, not a compile-time check. Callers decide whether to re-raise
    as `PlanCompileError` (the planner is the originating bug) or
    `RuntimeError` (a runtime caller passed something they shouldn't have).

    Mirrors the S2 `NamespaceConfigError` constructor shape (kwargs-only)
    so callers can `except DeterminismError as e: e.code` consistently.

    Codes:
        seed_wrong_length:   seed argument is not exactly 8 bytes
        namespace_empty:     namespace argument is empty string
        pool_size_overflow:  pool_size > 2**56 in derive_index
    """

    def __init__(self, *, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)


def derive(seed: bytes, namespace: str, source: bytes) -> bytes:
    """Return the 32-byte HMAC-SHA256 output for `(seed, namespace, source)`.

    Pure function. Same inputs produce byte-identical output across processes,
    across days, across engine versions while `SEED_PROTOCOL_VERSION` is
    unchanged.

    Args:
        seed: the job seed (exactly 8 bytes; raises on any other length).
            Comes from `plan.seed_envelope.job_seed` which is `bytes`-typed
            post-S3, or arbitrary 8-byte entropy for tests.
        namespace: the namespace string (non-empty; raises on empty). Comes
            from `NamespaceRegistry.for_column(...)` (S2) for the column
            being masked.
        source: the source bytes (any length including zero). An empty source
            is accepted and produces deterministic output; null-handling
            policy lives in the strategy layer (S9), not here. Caller
            encodes: `str.encode("utf-8")` for text, `int.to_bytes(...)` for
            ints, etc.

    Returns:
        32 bytes of stable derived material under `SEED_PROTOCOL_VERSION=1`.

    Raises:
        DeterminismError on invalid inputs (seed wrong length, empty namespace).
    """
    if len(seed) != _SEED_LENGTH:
        raise DeterminismError(
            code="seed_wrong_length",
            message=f"seed must be exactly {_SEED_LENGTH} bytes; got {len(seed)}",
        )
    if not namespace:
        raise DeterminismError(code="namespace_empty", message="namespace must be non-empty")
    namespace_bytes = namespace.encode("utf-8")
    hmac_key = hkdf_sha256(ikm=seed, salt=_SALT, info=namespace_bytes, length=32)
    hmac_input = (
        bytes([SEED_PROTOCOL_VERSION])
        + len(namespace_bytes).to_bytes(4, "big")
        + namespace_bytes
        + len(source).to_bytes(4, "big")
        + source
    )
    return hmac.new(hmac_key, hmac_input, hashlib.sha256).digest()


def derive_index(seed: bytes, namespace: str, source: bytes, *, pool_size: int) -> int:
    """Return a stable index in `[0, pool_size)` for `(seed, namespace, source)`.

    Convenience helper for pool sampling (S5's Pool Manager consumes this).

    Implementation: takes the first 8 bytes of `derive(seed, namespace, source)`,
    interprets as big-endian uint64, returns `value % pool_size`.

    Modulo bias bound: for `pool_size <= 2**56`, the most-favored output is at
    most `pool_size / 2**64 <= 2**-8` (~0.4 percent) more likely than the
    least-favored. For typical pool sizes (1k-100k), the bias is below `2**-44`
    (statistically undetectable). The `2**56` ceiling is a defensive guard.

    Raises:
        DeterminismError(code='pool_size_overflow') if pool_size > 2**56.
        Plus whatever `derive` raises on bad inputs.
    """
    if pool_size > _POOL_SIZE_MAX:
        raise DeterminismError(
            code="pool_size_overflow",
            message=f"pool_size {pool_size} exceeds maximum {_POOL_SIZE_MAX}",
        )
    if pool_size < 1:
        raise DeterminismError(
            code="pool_size_overflow",
            message=f"pool_size must be >= 1; got {pool_size}",
        )
    raw = derive(seed, namespace, source)
    return int.from_bytes(raw[:8], "big") % pool_size


class Domain(Protocol):
    """Stable bytes-to-value mapping.

    Implementations of `from_bytes` MUST satisfy:

    1. No I/O (no network, no file, no DB, no env vars).
    2. No use of process state (no `random.*`, no `time.*`, no PID,
       no thread-local state).
    3. Same `b` in -> same value out, regardless of process, machine,
       or call order (across days, across engine versions).

    These properties are NOT enforced at the Protocol type level
    (Python protocols cannot enforce purity); S6's reviewer enforces at
    PR time. The contract test pattern S6 must include for every
    concrete Domain:

        results = [d.from_bytes(b'\\x00' * 32) for _ in range(100)]
        assert all(r == results[0] for r in results)

    Plus a subprocess-stability variant per the done-definition.md gate.
    """

    def from_bytes(self, b: bytes) -> Any: ...


@dataclass(frozen=True)
class IdentityDomain:
    """Test-fixture `Domain` that returns its input bytes unchanged.

    Exported from `decoy_engine.determinism` so S4 (Provider Registry) and
    S5 (Pool Manager) integration tests have a concrete `Domain` to wire
    deterministic-mode tests against without each sprint inventing its own
    mock. NOT a customer-facing API: this Domain produces raw bytes, which
    are useless to a customer; concrete customer-facing Domains (SsnDomain,
    EinDomain, NpiDomain, etc.) are S6's surface.

    The carve-out is narrow (5 lines, no semantic meaning, single purpose)
    and documented as test-only per S3 spec §M4 resolution.
    """

    def from_bytes(self, b: bytes) -> bytes:
        return b


def derive_value(seed: bytes, namespace: str, source: bytes, *, domain: Domain) -> Any:
    """Return a stable typed value from `domain` for `(seed, namespace, source)`.

    Convenience helper for direct-generate callers (S6's custom-identifier
    generators). Calls `domain.from_bytes(b)` exactly once with the 32-byte
    output of `derive(...)`.

    Args:
        seed: 8 bytes (see `derive`).
        namespace: non-empty string (see `derive`).
        source: source bytes (see `derive`).
        domain: a `Domain` instance whose `from_bytes(b)` is pure.

    Returns:
        Whatever `domain.from_bytes(b)` returns. Type is `Any` because every
        Domain returns its own domain-specific type.
    """
    raw = derive(seed, namespace, source)
    return domain.from_bytes(raw)
