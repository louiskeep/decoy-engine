"""
Pluggable runtime context for engine execution.

The engine accepts an ExecutionContext from its caller (CLI or platform)
to receive a logger and telemetry client. This lets the CLI surface logs
through Rich and the platform surface them through structured logging to
a database -- without the engine knowing which.

Status: the Protocol and ExecutionContext class are published and stable.
Both Masker and DataGenerator accept an optional ``ctx`` parameter and
extract ``ctx.logger`` and key-resolver callbacks
(``derive_key``, ``pipeline_derive_key``) from it. They do not propagate
the full context to every internal subsystem; the graph runner uses
ExecutionContext end-to-end while the legacy Masker and DataGenerator
paths use it partially.
"""

import hashlib
import hmac
from typing import Any, Callable, Literal, Protocol, runtime_checkable


@runtime_checkable
class Logger(Protocol):
    """Logger surface the engine expects from its caller.

    A stdlib logging.Logger satisfies this protocol directly. The CLI
    provides a Rich-backed implementation; the platform provides a
    structured DB-backed one.

    Structured events (step boundaries, lineage, fidelity, quarantines,
    throughput samples) are an *optional* surface -- see ``StructuredEvents``
    below. The engine reaches them via the ``emit_step`` / ``emit_lineage``
    / etc. helpers in this module, which no-op gracefully when the active
    logger doesn't implement them. Keeping that surface off of the
    runtime_checkable ``Logger`` Protocol means a bare stdlib logger
    (without the structured methods) still satisfies ``isinstance(.,
    Logger)`` -- engine fallback paths don't need to be wrapped.
    """

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None: ...
    def info(self, msg: str, *args: Any, **kwargs: Any) -> None: ...
    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None: ...
    def error(self, msg: str, *args: Any, **kwargs: Any) -> None: ...


class StructuredEvents(Protocol):
    """Optional structured-event surface on top of ``Logger``.

    Implementations that want the platform's reporting UI to render a
    step timeline, throughput chart, lineage view, quarantine summary,
    or fidelity rollup expose these methods. The platform's ``JobLogger``
    implements all of them and persists into the companion job_* tables
    (see LOGGING_GUIDE.md §4f). Implementations that only need
    narrative output (stdlib, RichLogger in --quiet mode, tests) can
    skip them entirely -- the ``emit_*`` helpers below are no-ops in
    that case.

    Not ``@runtime_checkable``: an ``isinstance(..., StructuredEvents)``
    test would conflict with stdlib loggers (which don't have any of
    these methods) and force engine code to wrap every fallback logger.
    Use the module-level ``emit_*`` helpers for safe dispatch instead.
    """

    def step(
        self,
        name: str,
        *,
        status: str = "running",
        rows_in: int | None = None,
        rows_out: int | None = None,
        error_class: str | None = None,
        error_msg: str | None = None,
        node_id: str | None = None,
    ) -> None: ...
    def lineage(
        self,
        kind: Literal["source", "transform", "output"],
        label: str,
        type_: str,
    ) -> None: ...
    def fidelity(self, metric: str, value: float) -> None: ...
    def quarantine(self, step: str, reason: str, count: int) -> None: ...
    def throughput_sample(self, rows_per_sec: float) -> None: ...


# -- safe emit helpers ---------------------------------------------------------
# Each helper looks up the named method on the logger and calls it if
# present. The engine uses these instead of direct method calls so a
# bare stdlib logger (the common ctx-omitted fallback) doesn't raise
# AttributeError when the engine emits step / lineage / etc. A logger
# implementing ``StructuredEvents`` receives the call; everything else
# silently no-ops. Exceptions inside the structured method itself are
# swallowed -- a JobLogger DB hiccup mid-run mustn't take the engine
# down. Narrative logging (info/warning/error) is the source of truth.

def emit_step(
    logger: Logger | None,
    name: str,
    *,
    status: str = "running",
    rows_in: int | None = None,
    rows_out: int | None = None,
    error_class: str | None = None,
    error_msg: str | None = None,
    node_id: str | None = None,
) -> None:
    """Mark a step boundary: ``start`` / ``finish`` / ``error``.

    ``rows_in`` and ``rows_out`` are populated at ``finish`` when known.

    On ``status='error'`` callers can pass ``error_class`` (the Python
    exception type name) and ``error_msg`` (the str(exc)) so the
    JobLogger can format the spec ERROR line tail per LOGGING_GUIDE
    §4c (``request_id=`` + ``error_class=`` + ``node_id=``). The
    JobLogger also persists ``error_class`` to the JobStep companion
    row so the reporting UI can group errors by exception type without
    re-parsing the log tail.

    ``node_id`` defaults to ``name`` when unset -- most engine ops emit
    one step per node so the step name *is* the node id, but callers
    that emit a coarser step (e.g. a "mask" step covering multiple
    nodes) can pass a finer ``node_id`` for the canvas deep-link.
    """
    if logger is None:
        return
    fn = getattr(logger, "step", None)
    if fn is None:
        return
    try:
        fn(
            name, status=status,
            rows_in=rows_in, rows_out=rows_out,
            error_class=error_class, error_msg=error_msg,
            node_id=node_id,
        )
    except TypeError:
        # Older JobLogger implementations don't accept the new error
        # / node_id kwargs. Fall back to the original signature so a
        # forward-compatible call site still works against older
        # platform versions.
        try:
            fn(name, status=status, rows_in=rows_in, rows_out=rows_out)
        except Exception:
            pass
    except Exception:
        pass


def emit_lineage(
    logger: Logger | None,
    kind: Literal["source", "transform", "output"],
    label: str,
    type_: str,
) -> None:
    """Record a node in the source -> transform -> output graph."""
    if logger is None:
        return
    fn = getattr(logger, "lineage", None)
    if fn is None:
        return
    try:
        fn(kind, label, type_)
    except Exception:
        pass


def emit_fidelity(logger: Logger | None, metric: str, value: float) -> None:
    """Record a data-quality measurement (ks_test, cardinality, etc.)."""
    if logger is None:
        return
    fn = getattr(logger, "fidelity", None)
    if fn is None:
        return
    try:
        fn(metric, value)
    except Exception:
        pass


def emit_quarantine(
    logger: Logger | None,
    step: str,
    reason: str,
    count: int,
) -> None:
    """Record rows that failed validation and were diverted from output."""
    if logger is None:
        return
    fn = getattr(logger, "quarantine", None)
    if fn is None:
        return
    try:
        fn(step, reason, count)
    except Exception:
        pass


def emit_throughput_sample(logger: Logger | None, rows_per_sec: float) -> None:
    """Tick a rows/sec sample for the throughput chart (~1 Hz)."""
    if logger is None:
        return
    fn = getattr(logger, "throughput_sample", None)
    if fn is None:
        return
    try:
        fn(rows_per_sec)
    except Exception:
        pass


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
        pipeline_derive_key: Callable[[str], bytes] | None = None,
        captured_outputs: list[dict[str, Any]] | None = None,
        pool_resolver: Callable[[str, str], list[Any]] | None = None,
        column_relationships: list[dict[str, Any]] | None = None,
        instance_default_locale: str | None = None,
    ) -> None:
        self.logger = logger
        self.telemetry = telemetry
        # Used by graph ops `source.db` / `target.db` to turn a platform
        # connector_id into a DSN string. Platform passes a closure over its
        # connector store; CLI leaves it None and users supply inline `dsn:`.
        self.resolve_connector = resolve_connector
        # Side channel for ops that produce out-of-band artifacts the runner
        # can't reach via the dataframe stream (e.g. `run_storm` produces a
        # StormProfile, not row data). Each entry is a dict shaped like
        # {"kind": "<artifact-kind>", ...}; the platform reads it after the
        # graph completes and persists each entry per its kind. List rather
        # than dict because a single graph may run multiple instances of the
        # same op, and node_id is not visible inside `op.apply`.
        self.captured_outputs = captured_outputs if captured_outputs is not None else []
        # Per-node exports the runner reads after each op completes and folds
        # into the node's NodeRunRecord. Keyed by node id; values are flat
        # dicts of JSON-serializable scalars. Runner sets `_current_node_id`
        # before each `op.apply()` so ops can call `ctx.export(key, value)`
        # without needing to know their own node id. The keys are documented
        # per op kind in PIPELINE_GRAPH_GUIDE.md "Node exports".
        self._exports: dict[str, dict[str, Any]] = {}
        self._current_node_id: str | None = None
        # -- two key resolvers, by design --
        #
        # `derive_key(info)` is the **mask** resolver. Caller pre-binds the
        # tenant master instance key (and only the master). Same input row +
        # same column always maps to the same masked bytes across pipelines,
        # which is what gives mask its cross-pipeline FK-stability property.
        # Mask is always deterministic at the instance level; pipeline keys
        # do *not* affect mask output.
        #
        # `pipeline_derive_key(info)` is the **generate** resolver. Caller
        # pre-binds master + pipeline-specific label. When None, generate
        # falls back to seed-based RNG (random across runs) -- the policy
        # surface in front of this resolver decides whether a label exists
        # for the current pipeline (admin modes A/B/C in the platform).
        #
        # Engine ops pick the right resolver for their semantics: mask uses
        # `derive_key`, generate uses `pipeline_derive_key`.
        self.derive_key = derive_key
        self.pipeline_derive_key = pipeline_derive_key

        # -- FK preservation (Sprint 4, item 4) --
        #
        # Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG;
        # materialize parent pool; child samples with replacement.
        #
        # `pool_resolver(parent_node_id, column)` returns the distinct
        # non-null parent values for a column produced by `parent_node_id`.
        # The runner builds this closure over the live GraphCache before
        # the topological execution loop; ops that need to enforce FK
        # stability (notably the `generate` op consuming a parent's PK
        # pool for an FK column) call it on demand. None when the YAML
        # carries no `column_relationships:` block, which keeps the
        # generate op's existing fast path intact.
        #
        # `column_relationships` is the parsed block from the pipeline
        # YAML (engine-native shape: each entry is
        # {"kind": "fk", "parent": {"node": ..., "column": ...},
        #  "child":  {"node": ..., "column": ...}}). Ops read this to
        # discover which of their columns is an FK; the runner reads it
        # to extend the cache keep-set so parent nodes survive past
        # their normal consumer count.
        self.pool_resolver = pool_resolver
        self.column_relationships = column_relationships

        # Instance-wide default Faker locale. Platform reads
        # AppSettings.default_faker_locale + passes it in here so the
        # engine can fall back to the operator's chosen locale when a
        # per-column locale isn't set. None means "engine library
        # default" (en_US in Faker), preserving the legacy behavior
        # for callers that don't supply this.
        self.instance_default_locale = instance_default_locale

    def export(self, key: str, value: Any) -> None:
        """Emit a node-level export the runner folds into NodeRunRecord.

        Op authors call this from inside `apply()`; the runner sets
        `_current_node_id` before each call so the op doesn't need to know
        its own id. Downstream nodes consume the value via
        `${nodes.<id>.<key>}` substitution which the runner resolves
        live before each op runs.

        Silently no-ops when called outside a node-execution scope so the
        preview path (which doesn't record exports) and external callers
        don't trip on op code that always calls export()."""
        if self._current_node_id is None:
            return
        self._exports.setdefault(self._current_node_id, {})[key] = value


# -- helpers callers (CLI, platform) can use to build a derive_key resolver --

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
    same pipeline_label always yields the same column subkeys, anywhere --
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
