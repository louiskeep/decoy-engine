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
