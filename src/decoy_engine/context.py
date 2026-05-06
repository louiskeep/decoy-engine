"""
Pluggable runtime context for engine execution.

The engine accepts an ExecutionContext from its caller (CLI or platform)
to receive a logger and telemetry client. This lets the CLI surface logs
through Rich and the platform surface them through structured logging to
a database — without the engine knowing which.

Status: the Protocol and ExecutionContext class are published now so CLI
and platform code have a stable contract to depend on. The engine entry
points (Masker, DataGenerator) do not yet accept an ExecutionContext;
they construct their own logger from the YAML config. Wiring the engine
to consume ExecutionContext is a follow-up change.
"""

import hashlib
import hmac
from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class Logger(Protocol):
    """Logger surface the engine expects from its caller.

    A stdlib logging.Logger satisfies this protocol directly. The CLI
    provides a Rich-backed implementation; the platform provides a
    structured DB-backed one.
    """

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None: ...
    def info(self, msg: str, *args: Any, **kwargs: Any) -> None: ...
    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None: ...
    def error(self, msg: str, *args: Any, **kwargs: Any) -> None: ...


@runtime_checkable
class TelemetryClient(Protocol):
    """Optional telemetry sink. Published for forward compatibility; unused today."""

    def emit(self, event: str, properties: dict[str, Any] | None = None) -> None: ...


class ExecutionContext:
    """Caller-provided runtime context for engine execution.

    Construct one in the CLI or platform layer and pass it to engine
    entry points. The engine treats every field as optional and falls
    back to defaults when not supplied.
    """

    def __init__(
        self,
        logger: Logger | None = None,
        telemetry: TelemetryClient | None = None,
        resolve_connector: Callable[[int], str] | None = None,
        derive_key: Callable[[str], bytes] | None = None,
    ) -> None:
        self.logger = logger
        self.telemetry = telemetry
        # Used by graph ops `source.db` / `target.db` to turn a platform
        # connector_id into a DSN string. Platform passes a closure over its
        # connector store; CLI leaves it None and users supply inline `dsn:`.
        self.resolve_connector = resolve_connector
        # `derive_key(info)` returns 32 bytes of HKDF-derived key material
        # given a stable info label (e.g. "col:email"). Caller pre-binds the
        # tenant master key + pipeline key_label; the engine just asks for
        # column-scoped subkeys. When None, deterministic-by-input strategies
        # fall back to the legacy `seed`-coupled path.
        self.derive_key = derive_key


# ── helpers callers (CLI, platform) can use to build a derive_key resolver ──

def _hkdf_sha256(master: bytes, info: str, length: int = 32) -> bytes:
    """HKDF-SHA256(master, info) -> `length` bytes (max 32 in this impl).

    Stdlib-only implementation so the engine doesn't pull `cryptography`
    just for keyed determinism. Empty-salt HKDF: PRK = HMAC(zero, master);
    OKM = HMAC(PRK, info || 0x01)[:length]. One expansion round is enough
    while length <= hash output (32 for SHA-256).
    """
    if length > 32:
        raise ValueError("length must be <= 32 (single HKDF-Expand round)")
    salt = b"\x00" * 32
    prk = hmac.new(salt, master, hashlib.sha256).digest()
    okm = hmac.new(prk, info.encode("utf-8") + b"\x01", hashlib.sha256).digest()
    return okm[:length]


def make_key_resolver(
    master: bytes,
    pipeline_label: str,
) -> Callable[[str], bytes]:
    """Build the closure assigned to ``ExecutionContext.derive_key``.

    Pre-binds master + pipeline_label so the engine just asks for
    column-scoped subkeys via labels like ``"col:email"``. Same master +
    same pipeline_label always yields the same column subkeys, anywhere —
    that's the cross-instance recovery property.

    CLI passes a master key from ``--master-key``; platform's
    ``api/keys/make_resolver`` is structurally identical and produces the
    same bytes given the same inputs.
    """
    if not isinstance(master, (bytes, bytearray)) or len(master) != 32:
        raise ValueError("master key must be 32 bytes")
    pipeline_key = _hkdf_sha256(master, f"pipeline:{pipeline_label}")

    def resolver(info: str) -> bytes:
        return _hkdf_sha256(pipeline_key, info)

    return resolver
