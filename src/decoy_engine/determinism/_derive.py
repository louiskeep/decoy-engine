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
        bytes([SEED_PROTOCOL_VERSION])               # 1 byte; 0x04 today
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
#
# v1 -> v2 (F-series corrections, pre-GA): a coordinated bump covering the
# three deterministic-output-shifting fixes that landed together: Faker pool
# builds are now seeded (S5 F2), and the canonicalize integer encoding moved
# to a length-prefixed arbitrary-magnitude form covering numpy scalars (S5
# NF1/NF2). No manifests exist in the wild (pre-GA), so v1 marks the
# pre-correction era and v2 is the corrected baseline.
#
# QA walks/generators F3 (2026-06-01, HIGH correctness + perf, PO
# Q-F3=b): bump to v3. The null-injection path in
# generators/columns.py::generate_column swapped from
# Python random.Random(column_seed + i) per-row to
# numpy.random.default_rng(column_seed).random(num_rows). The null
# FRACTION still converges to null_probability, but the null PATTERN
# (which specific rows are nulled) differs because numpy and Python
# random produce different floats for the same integer seed. This
# changes byte-output for any pipeline with null_probability > 0.
# Pre-GA, no manifests to break (PO Q-F3=b 2026-06-01 confirmed).
#
# Formula-hash migration (2026-06-01, PO confirmed): bump to v4. The
# formula sandbox `hash()` function swapped from the legacy
# deterministic_hash (SHA256(value + str(seed))) to HMAC-SHA256
# keyed by the per-row local_seed (_formula_hash_keyed in
# generators/columns.py). The per-row output bytes change for any
# pipeline that uses `hash(col)` inside a formula column. The
# legacy primitive is still callable via the public surface but
# emits a DeprecationWarning (QA-internal F12, 2026-06-01).
#
# WS1 detokenization (2026-06-12): bump to v5. Two coordinated
# FPE output-shifting changes: (a) the Feistel key moved from
# per-value `derive(seed, ns, canonicalize(value))` to one key per
# (seed, namespace) `derive(seed, ns, b"fpe-key/v1")` -- the NIST
# SP 800-38G FF1 key model -- making ciphertext decryptable via
# decoy_engine.unmask; (b) validate_luhn permutes the body and
# appends the check digit instead of overwriting the last encrypted
# digit (the old shape discarded a character and was irreversible).
# Output bytes change for every fpe column. Pre-GA, hard cutover.
SEED_PROTOCOL_VERSION: int = 5

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


@dataclass(frozen=True)
class DeriveContext:
    """Pre-computed per-(seed, namespace) state. Amortises HKDF cost.

    QA-10 F4 (2026-06-01): the scalar `derive(seed, namespace, source)`
    function recomputes the HKDF key on every call. `hmac_key` depends
    only on `(seed, namespace)` -- constant for every row in a column.
    At 1M rows per column the wasted HKDF work is two HMAC-SHA256
    invocations per row (~0.5s/column on a modern core; ~25s for a
    50-column masked table).

    DeriveContext pre-computes the key once via
    `DeriveContext.for_column(seed, namespace)` and exposes
    `ctx.derive_source(namespace, source)` for the per-row HMAC.
    Strategy adapters that process a column instantiate the context
    once + call derive_source per row.

    The scalar `derive(...)` function stays unchanged for back-compat.

    Output is byte-identical to `derive(seed, namespace, source)` for
    the same inputs.
    """

    _hmac_key: bytes

    @classmethod
    def for_column(cls, seed: bytes, namespace: str) -> DeriveContext:
        """Build a context for one (seed, namespace) pair.

        Validates seed length + namespace emptiness the same way the
        scalar `derive(...)` does. Construction cost is the same as
        one HKDF-SHA256 call (2x HMAC-SHA256 invocations).
        """
        if len(seed) != _SEED_LENGTH:
            raise DeterminismError(
                code="seed_wrong_length",
                message=f"seed must be exactly {_SEED_LENGTH} bytes; got {len(seed)}",
            )
        if not namespace:
            raise DeterminismError(code="namespace_empty", message="namespace must be non-empty")
        key = hkdf_sha256(ikm=seed, salt=_SALT, info=namespace.encode("utf-8"), length=32)
        return cls(_hmac_key=key)

    def derive_source(self, namespace: str, source: bytes) -> bytes:
        """Per-row HMAC. Same output as `derive(seed, namespace, source)`
        for the same (seed, namespace) that built this context.

        The `namespace` arg is required (not derived from the context)
        because the HMAC input includes it in length-prefixed form;
        callers MUST pass the same namespace used in `for_column` or
        the output diverges from `derive(...)`.
        """
        namespace_bytes = namespace.encode("utf-8")
        hmac_input = (
            bytes([SEED_PROTOCOL_VERSION])
            + len(namespace_bytes).to_bytes(4, "big")
            + namespace_bytes
            + len(source).to_bytes(4, "big")
            + source
        )
        return hmac.new(self._hmac_key, hmac_input, hashlib.sha256).digest()


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
        32 bytes of stable derived material under `SEED_PROTOCOL_VERSION=4`.

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
    # QA-7 F11 (2026-06-01): distinct code for the underflow case. A
    # zero or negative pool size is not an overflow -- callers
    # catching DeterminismError and inspecting `e.code` would
    # misclassify. The message stays similar; only the code shifts.
    if pool_size < 1:
        raise DeterminismError(
            code="pool_size_invalid",
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
