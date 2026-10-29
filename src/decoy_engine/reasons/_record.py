"""Reason records: the safe, versioned occurrence of a registry reason.

Pattern source: RFC 9457 separates a stable problem identity (``type``) from occurrence detail and
from the transport status, and warns against echoing raw error text. This record is that split
for the engine: identity ``(namespace, code)`` and the semantics (summary, severity, outcome,
retry class) come from the registry; the occurrence carries only a closed context. There is no
exception object, no free text and no extension dictionary, so an untrusted value cannot ride a
reason out of the process. It is not an HTTP Problem Details response (the engine has no HTTP
surface).

The context vocabulary is deliberately small and closed:
  * ``object_ref`` / ``storage_ref``: an approved identifier (a table or column name, or a fixed
    storage alias), never a path or a connection string. ``approve_ref`` is the single seam where
    a shared safe-output policy (F31) plugs in; until it exists the seam enforces a strict shape.
  * ``operation`` / ``publication_state`` / ``cleanup_state``: fixed enums.
  * ``counts``: a bounded set of non-negative integers under a closed key vocabulary.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from decoy_engine.reasons import _registry
from decoy_engine.reasons._types import Kind, Outcome, Phase, ReasonCode, RetryClass, Severity

MAX_COUNTS = 16
MAX_REF_LEN = 64
MAX_IDENTITY_LEN = 96
COUNT_KEYS = frozenset(
    {"rows", "bytes", "tables", "columns", "batches", "attempt", "retries", "findings", "errors"}
)

_SAFE_REF = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9_.\-]{{0,{MAX_REF_LEN - 1}}}$")
_SAFE_IDENTITY = re.compile(rf"^[a-z][a-z0-9_.:\-]{{0,{MAX_IDENTITY_LEN - 1}}}$")
_VERSION = re.compile(r"^(\d{1,4})\.(\d{1,5})\.(\d{1,6})$")


class Operation(str, Enum):
    """Fixed operation identifiers: where in the pipeline a reason arose."""

    READ = "read"
    RESOLVE_SECRET = "resolve_secret"  # noqa: S105 -- a fixed operation id, not a credential
    STAGE = "stage"
    WRITE = "write"
    CLOSE = "close"
    COMMIT = "commit"
    ABORT = "abort"
    SIDECAR = "sidecar"
    VERIFY = "verify"


class PublicationState(str, Enum):
    NOT_PUBLISHED = "not_published"
    PUBLISHED = "published"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class CleanupState(str, Enum):
    NOT_NEEDED = "not_needed"
    ATTEMPTED = "attempted"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


def approve_ref(value: object) -> str | None:
    """``value`` when it is an approved identifier, else ``None``. The F31 integration seam."""
    if isinstance(value, str) and _SAFE_REF.match(value):
        return value
    return None


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _enum_or_none(enum: type[Enum], value: object) -> Any:
    if isinstance(value, enum):
        return value
    if isinstance(value, str):
        try:
            return enum(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class ReasonContext:
    """The closed, bounded occurrence detail of a reason."""

    object_ref: str | None = None
    operation: Operation | None = None
    storage_ref: str | None = None
    publication_state: PublicationState | None = None
    cleanup_state: CleanupState | None = None
    counts: tuple[tuple[str, int], ...] = ()
    truncated: bool = False
    # An identity this registry does not know (a newer registry). Audit trail only; never read.
    original_identity: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        for name in ("object_ref", "storage_ref"):
            value = getattr(self, name)
            if value is not None and approve_ref(value) is None:
                raise ValueError(f"{name} is not an approved identifier")
        if len(self.counts) > MAX_COUNTS:
            raise ValueError("too many context counts")
        for key, value in self.counts:
            if key not in COUNT_KEYS or not _is_count(value):
                raise ValueError("context counts take closed keys and non-negative integers")
        if self.original_identity is not None and not all(
            _SAFE_IDENTITY.match(part) for part in self.original_identity
        ):
            raise ValueError("original_identity is not a safe identity")

    @classmethod
    def build(cls, *, counts: Mapping[str, int] | None = None, **fields: Any) -> ReasonContext:
        """Strict construction for engine code: bad input is a programming error."""
        pairs = tuple(sorted((counts or {}).items()))
        return cls(counts=pairs, **fields)

    @classmethod
    def from_payload(cls, payload: object) -> ReasonContext:
        """Lenient parse of untrusted input: invalid fields are dropped, size is bounded.

        The size check precedes any iteration, so a mapping with millions of entries costs one
        ``len`` and at most ``MAX_COUNTS`` item reads.
        """
        if not isinstance(payload, Mapping):
            return cls(truncated=True)
        truncated = bool(payload.get("truncated") is True)
        raw_counts = payload.get("counts")
        pairs: list[tuple[str, int]] = []
        if isinstance(raw_counts, Mapping):
            if len(raw_counts) > MAX_COUNTS:
                truncated = True
            for key, value in itertools.islice(raw_counts.items(), MAX_COUNTS):
                if isinstance(key, str) and key in COUNT_KEYS and _is_count(value):
                    pairs.append((key, int(value)))
                else:
                    truncated = True
        elif raw_counts is not None:
            truncated = True
        original = payload.get("original_identity")
        identity: tuple[str, str] | None = None
        if (
            isinstance(original, (list, tuple))
            and len(original) == 2
            and all(isinstance(p, str) and _SAFE_IDENTITY.match(p) for p in original)
        ):
            identity = (original[0], original[1])
        return cls(
            object_ref=approve_ref(payload.get("object_ref")),
            operation=_enum_or_none(Operation, payload.get("operation")),
            storage_ref=approve_ref(payload.get("storage_ref")),
            publication_state=_enum_or_none(PublicationState, payload.get("publication_state")),
            cleanup_state=_enum_or_none(CleanupState, payload.get("cleanup_state")),
            counts=tuple(sorted(pairs)),
            truncated=truncated,
            original_identity=identity,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "object_ref": self.object_ref,
            "operation": self.operation.value if self.operation else None,
            "storage_ref": self.storage_ref,
            "publication_state": self.publication_state.value if self.publication_state else None,
            "cleanup_state": self.cleanup_state.value if self.cleanup_state else None,
            "counts": dict(self.counts),
            "truncated": self.truncated,
            "original_identity": list(self.original_identity) if self.original_identity else None,
        }

    def merged(self, other: ReasonContext) -> ReasonContext:
        """``self`` with every field ``other`` sets, counts combined (``other`` wins on a key)."""
        counts = dict(self.counts)
        counts.update(dict(other.counts))
        return ReasonContext(
            object_ref=other.object_ref or self.object_ref,
            operation=other.operation or self.operation,
            storage_ref=other.storage_ref or self.storage_ref,
            publication_state=other.publication_state or self.publication_state,
            cleanup_state=other.cleanup_state or self.cleanup_state,
            counts=tuple(sorted(counts.items()))[:MAX_COUNTS],
            truncated=self.truncated or other.truncated,
            original_identity=other.original_identity or self.original_identity,
        )


@dataclass(frozen=True)
class ReasonRecord:
    """A registry reason as it occurred: identity, resolved semantics, closed context."""

    schema_version: int
    registry_version: str
    namespace: str
    code: str
    kind: Kind
    phase: Phase
    severity: Severity
    retry_class: RetryClass
    outcome: Outcome
    context: ReasonContext = ReasonContext()

    @property
    def summary(self) -> str:
        """The registry's fixed safe summary; callers cannot override it."""
        return _registry.get(self.namespace, self.code).summary

    @classmethod
    def for_reason(
        cls,
        reason: tuple[str, str] | ReasonCode,
        *,
        phase: Phase = Phase.UNKNOWN,
        context: ReasonContext | None = None,
    ) -> ReasonRecord:
        """The record of a registered reason at ``phase``; semantics come from the registry.

        A phase the registry does not allow for this reason is reported as ``unknown`` with the
        reason's default rule, never as the caller's claim.
        """
        if isinstance(reason, tuple):
            namespace, code = reason
        else:
            namespace, code = reason.namespace, str(reason)
        spec = _registry.get(namespace, code)
        rule = spec.rule_for(phase)
        if rule is None:
            rule, phase = spec.default_rule, Phase.UNKNOWN
        return cls(
            schema_version=_registry.REASON_SCHEMA_VERSION,
            registry_version=_registry.REGISTRY_VERSION,
            namespace=namespace,
            code=code,
            kind=spec.kind,
            phase=phase,
            severity=rule.severity,
            retry_class=rule.retry,
            outcome=rule.outcome,
            context=context or ReasonContext(),
        )

    def with_context(self, context: ReasonContext) -> ReasonRecord:
        return ReasonRecord(
            self.schema_version,
            self.registry_version,
            self.namespace,
            self.code,
            self.kind,
            self.phase,
            self.severity,
            self.retry_class,
            self.outcome,
            self.context.merged(context),
        )

    def to_payload(self) -> dict[str, Any]:
        """The exact wire shape: explicit fields only, no extension keys."""
        return {
            "schema_version": self.schema_version,
            "registry_version": self.registry_version,
            "namespace": self.namespace,
            "code": self.code,
            "kind": self.kind.value,
            "phase": self.phase.value,
            "severity": self.severity.value,
            "retry_class": self.retry_class.value,
            "outcome": self.outcome.value,
            "context": self.context.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: object) -> ReasonRecord:
        """Validate an untrusted record (a worker envelope, a stored event). Never raises.

        Only identity, phase and context are read from ``payload``. Severity, outcome and retry
        class are recomputed from the registry, so a forged ``retry_class`` has no effect. An
        unknown identity under a supported registry major degrades to
        ``decoy.unknown_reason_from_future``, keeping the original identity as an uninterpreted
        context field; an unsupported major or a malformed record becomes a fixed diagnostic.
        """
        from decoy_engine.reasons import decoy

        if not isinstance(payload, Mapping):
            return cls.for_reason(decoy.REASON_RECORD_INVALID)
        schema = payload.get("schema_version")
        registry = payload.get("registry_version")
        if schema != _registry.REASON_SCHEMA_VERSION or not isinstance(registry, str):
            return cls.for_reason(decoy.UNSUPPORTED_SCHEMA_VERSION)
        match = _VERSION.match(registry)
        ours = _VERSION.match(_registry.REGISTRY_VERSION)
        if match is None or ours is None or match.group(1) != ours.group(1):
            return cls.for_reason(decoy.UNSUPPORTED_SCHEMA_VERSION)
        namespace, code = payload.get("namespace"), payload.get("code")
        phase = _enum_or_none(Phase, payload.get("phase")) or Phase.UNKNOWN
        context = ReasonContext.from_payload(payload.get("context"))
        spec = _registry.find(namespace, code) if isinstance(namespace, str) else None
        if spec is None:
            identity = (
                (namespace, code)
                if isinstance(namespace, str)
                and isinstance(code, str)
                and _SAFE_IDENTITY.match(namespace)
                and _SAFE_IDENTITY.match(code)
                else None
            )
            fallback = ReasonContext.build(original_identity=identity) if identity else None
            return cls.for_reason(
                decoy.UNKNOWN_REASON_FROM_FUTURE,
                phase=phase,
                context=context.merged(fallback) if fallback else context,
            )
        return cls.for_reason((spec.namespace, spec.code), phase=phase, context=context)
