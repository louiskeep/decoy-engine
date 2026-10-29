"""Exceptions -> reason records: the boundary between engine failures and consumers.

Pattern sources: RFC 9457 (stable identity separate from occurrence detail; never echo raw
error text) and the OpenTelemetry guidance that ``error.type`` should be a low-cardinality
identifier rather than the exception message. A consumer reads a :class:`ReasonRecord`, never an
exception's text, class name or ``.code`` attribute.

Trust model. An exception's ``.code`` is trusted only when the exception's class is one of the
engine families registered below *and* the code is registered in that family's namespace.
Character shape is not trust: an exception raised by a parser or a user callback can carry a
data-derived ``code`` that is still lowercase and underscores, and a third-party class can copy an
engine code string. Everything else becomes ``decoy.unexpected_error``; the original exception
is untouched and keeps propagating for direct callers.

This module is the adapter, so it may import the engine's error families; ``decoy_engine.reasons``
itself stays a leaf (it imports none of them).
"""

from __future__ import annotations

import errno
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from decoy_engine import reasons
from decoy_engine.reasons import Operation, Phase, ReasonCode, ReasonContext, ReasonRecord

_CAPACITY_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})
_PUBLICATION_OPERATIONS = frozenset(
    {Operation.STAGE, Operation.WRITE, Operation.CLOSE, Operation.COMMIT, Operation.SIDECAR}
)
_INTERRUPTS = (KeyboardInterrupt, SystemExit, GeneratorExit)

# (module, class, namespace of its ``.code``) for every engine family that carries a code.
_CODED: tuple[tuple[str, str, str], ...] = (
    ("decoy_engine.errors", "PipelineValidationError", "config"),
    ("decoy_engine.errors", "ValidationError", "config"),
    ("decoy_engine.relationships._namespace", "NamespaceConfigError", "config"),
    ("decoy_engine.subset._errors", "SubsetConfigError", "config"),
    ("decoy_engine.plan._errors", "PlanCompileError", "plan"),
    ("decoy_engine.plan.validate._consolidator", "PlanCheckError", "plan"),
    ("decoy_engine.generation.statistical._spec", "StatisticalSpecError", "plan"),
    ("decoy_engine.generation.composite._errors", "CompositeError", "plan"),
    ("decoy_engine._reference_date", "PinnedModeError", "plan"),
    ("decoy_engine.generation._window_admission", "GenerationAdmissionError", "plan"),
    ("decoy_engine.generation._window_admission", "GenerationWindowError", "runtime"),
    ("decoy_engine.generation._budget_ledger", "GenerationBudgetError", "runtime"),
    ("decoy_engine.execution._errors", "ExecutionError", "runtime"),
    ("decoy_engine.generation.pool._errors", "GenerationError", "runtime"),
    ("decoy_engine.generation.pool._errors", "PoolCapacityError", "runtime"),
    ("decoy_engine.execution._transforms", "TransformError", "runtime"),
    ("decoy_engine.determinism._derive", "DeterminismError", "runtime"),
    ("decoy_engine.errors", "FpeUnencryptableError", "runtime"),
    ("decoy_engine.errors", "FpeChecksumError", "runtime"),
    ("decoy_engine.errors", "MaskKeyDerivationError", "runtime"),
    ("decoy_engine.errors", "MaskKeyRequiredError", "runtime"),
    ("decoy_engine.errors", "PKDuplicatesError", "runtime"),
    ("decoy_engine.keyprovider", "MaskSecretError", "runtime"),
    ("decoy_engine.vault", "VaultError", "runtime"),
    ("decoy_engine.execution._mixed_admission", "MixedDeclineError", "runtime"),
    ("decoy_engine.execution._unified_slice", "UnifiedSliceInvariantError", "runtime"),
    ("decoy_engine.execution.native._chunk_schema", "NativeChunkSchemaDriftError", "runtime"),
    ("decoy_engine.execution.native._companion_status", "NativeCompanionCheckError", "runtime"),
    ("decoy_engine.execution.native._crypto_ext", "CryptoExtensionUnavailableError", "runtime"),
    ("decoy_engine.execution.native._crypto_ext", "FpeConfigError", "runtime"),
    ("decoy_engine.execution.native._draw_site_providers", "DrawSiteProtocolError", "runtime"),
    ("decoy_engine.execution.native._pool_quality", "PoolQualityError", "runtime"),
    ("decoy_engine.io.bounded_reader", "BoundedReadError", "runtime"),
    ("decoy_engine.subset._errors", "SubsetError", "subset"),
    ("decoy_engine.subset._errors", "SubsetInternalError", "runtime"),
    ("decoy_engine.providers_v2._errors", "ProviderError", "provider"),
    ("decoy_engine.providers_v2.identifiers._errors", "IdentifierError", "provider"),
    ("decoy_engine.detection.ner", "NerUnavailableError", "provider"),
    ("decoy_engine.quality.carriers", "CarrierError", "quality"),
    ("decoy_engine.quality.dp", "DpError", "quality"),
    ("decoy_engine.quality.dp_budget", "DpBudgetError", "quality"),
    ("decoy_engine.quality.dp_provenance", "ProvenanceError", "quality"),
    ("decoy_engine.quality.snapshot", "DistributionSnapshotError", "quality"),
    ("decoy_engine.connectors.postgres_snapshot", "PostgresSnapshotError", "connector"),
    ("decoy_engine.execution.physical._shadow_diff_codes", "ShadowDifference", "physical"),
)
# Engine exceptions that carry no code but have one fixed meaning: (module, class, reason).
_FIXED: tuple[tuple[str, str, ReasonCode], ...] = (
    ("decoy_engine.errors", "EmptyParentPoolError", reasons.config.FK_EMPTY_PARENT_POOL),
    ("decoy_engine.errors", "UnknownFKColumnError", reasons.config.FK_UNKNOWN_COLUMN),
    ("decoy_engine.errors", "ConfigError", reasons.config.UNTAGGED),
    ("decoy_engine.config._errors", "PipelineConfigError", reasons.config.UNTAGGED),
    ("decoy_engine.errors", "FlagPauseSignal", reasons.decoy.RUN_PAUSED),
    ("decoy_engine.errors", "RowErrorsFailedError", reasons.decoy.ROW_ERRORS_FAILED),
    ("decoy_engine.errors", "ValidatorFailedError", reasons.decoy.VERIFICATION_FAILED),
    ("decoy_engine.execution._isolated_commit", "CommitError", reasons.decoy.PUBLICATION_FAILED),
    ("decoy_engine.sdk", "TransientError", reasons.decoy.CONNECTOR_TRANSIENT_FAILURE),
    ("decoy_engine.sdk", "PermanentError", reasons.decoy.CONNECTOR_PERMANENT_FAILURE),
)


@lru_cache(maxsize=1)
def _families() -> tuple[dict[type, str], dict[type, ReasonCode]]:
    """Resolve the tables to class objects once; classes are the trust anchor, not strings."""
    import importlib

    coded: dict[type, str] = {}
    for module, name, namespace in _CODED:
        coded[getattr(importlib.import_module(module), name)] = namespace
    fixed: dict[type, ReasonCode] = {}
    for module, name, reason in _FIXED:
        fixed[getattr(importlib.import_module(module), name)] = reason
    return coded, fixed


def registered_families() -> frozenset[type]:
    """Every exception class the adapter trusts (coded or fixed), for completeness tests."""
    coded, fixed = _families()
    return frozenset(coded) | frozenset(fixed)


def _first_in_mro(exc: BaseException, table: dict[type, Any]) -> Any:
    for cls in type(exc).__mro__:
        if cls in table:
            return table[cls]
    return None


def _capacity_evidence(exc: BaseException) -> bool:
    """Typed ENOSPC/EDQUOT on the exception or its direct cause; a generic OSError is not."""
    for candidate in (exc, getattr(exc, "__cause__", None)):
        if isinstance(candidate, OSError) and candidate.errno in _CAPACITY_ERRNOS:
            return True
    return False


def _record(
    reason: tuple[str, str] | ReasonCode,
    phase: Phase,
    context: ReasonContext | None,
) -> ReasonRecord:
    return ReasonRecord.for_reason(reason, phase=phase, context=context)


def reason_from_exception(
    exc: BaseException,
    *,
    phase: Phase = Phase.UNKNOWN,
    context: ReasonContext | None = None,
) -> ReasonRecord:
    """The reason record for ``exc`` at the operation boundary ``phase``.

    Never raises, never reads the exception message, class name, ``__cause__`` text or a
    ``.code`` of an unregistered class, and leaves ``exc`` untouched. ``phase`` and ``context``
    come from the operation that caught the failure, never from the exception.
    """
    known = _lookup_captured(exc)
    if known is not None:
        # The engine already observed this exception at its boundary, with evidence a caller
        # holding only the exception cannot reconstruct (phase, publication and cleanup state).
        return known if context is None else known.with_context(context)
    try:
        return _classify(exc, phase, context)
    except Exception:
        return _record(reasons.decoy.UNEXPECTED_ERROR, phase, context)


def _classify(exc: BaseException, phase: Phase, context: ReasonContext | None) -> ReasonRecord:
    if isinstance(exc, _INTERRUPTS):
        return _record(reasons.decoy.RUN_CANCELLED, phase, context)
    publishing = phase is Phase.PUBLISH or (
        context is not None and context.operation in _PUBLICATION_OPERATIONS
    )
    if publishing and _capacity_evidence(exc):
        return _record(reasons.decoy.STORAGE_CAPACITY_EXHAUSTED, phase, context)
    coded, fixed = _families()
    namespace = _first_in_mro(exc, coded)
    if namespace is not None:
        code = _read_code(exc)
        if code is not None and reasons.find(namespace, code) is not None:
            return _record((namespace, code), phase, context)
        return _record(reasons.decoy.UNREGISTERED_REASON, phase, context)
    identity = _first_in_mro(exc, fixed)
    if identity is not None:
        return _record(identity, phase, context)
    if publishing and isinstance(exc, OSError):
        return _record(reasons.decoy.PUBLICATION_FAILED, phase, context)
    return _record(reasons.decoy.UNEXPECTED_ERROR, phase, context)


def _read_code(exc: BaseException) -> str | None:
    """The exception's ``.code`` if reading it is safe and it is a string; else ``None``."""
    try:
        code = exc.code  # type: ignore[attr-defined]
    except Exception:
        return None
    return code if isinstance(code, str) else None


# -- verification reports ------------------------------------------------------------------------


def _count_of(report: Any, name: str) -> int:
    value = report.get(name) if isinstance(report, Mapping) else getattr(report, name, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def reasons_from_report(report: Any, *, enforcing: bool = False) -> tuple[ReasonRecord, ...]:
    """The reasons a verification report supports, without serializing the report.

    Reads only the counters (``fail_count``, ``error_count``) and whether the whole pass failed
    (``pass_failed_with`` set; its text, an exception type name, is never copied). A verifier that
    could not complete (infrastructure failure or per-check errors) takes precedence over its
    findings and is listed first; findings are ``findings_reported`` when the check only reports,
    and ``verification_failed`` when ``enforcing`` made the negative verdict binding.
    A clean report yields no reasons. Accepts the `PostMaskReport` object or its dict form."""
    failed_with = (
        report.get("pass_failed_with")
        if isinstance(report, Mapping)
        else getattr(report, "pass_failed_with", None)
    )
    errors = _count_of(report, "error_count")
    findings = _count_of(report, "fail_count")
    out: list[ReasonRecord] = []
    if failed_with is not None or errors > 0:
        out.append(
            ReasonRecord.for_reason(
                reasons.decoy.VERIFICATION_UNAVAILABLE,
                phase=Phase.VERIFY,
                context=ReasonContext.build(operation=Operation.VERIFY, counts={"errors": errors}),
            )
        )
    if findings > 0:
        out.append(
            ReasonRecord.for_reason(
                reasons.decoy.VERIFICATION_FAILED if enforcing else reasons.decoy.FINDINGS_REPORTED,
                phase=Phase.VERIFY,
                context=ReasonContext.build(
                    operation=Operation.VERIFY, counts={"findings": findings}
                ),
            )
        )
    return tuple(out)


# -- first-failure capture ---------------------------------------------------------------------

_MAX_SECONDARY = 4
# The record captured for an exception rides on the exception itself. Builtin exceptions cannot be
# weakly referenced, and a strong cache would keep a failed run's traceback frames (and the row data
# in their locals) alive. An exception that refuses the attribute simply is not remembered.
_ATTR = "_decoy_reason_record"
_local = threading.local()


@dataclass
class FailureScope:
    """The failure state of one run: the first causal failure plus bounded secondary ones.

    Owned by the run, not by a progress sink, so it exists with no sink bound. A nested or a
    successive run gets its own scope and never sees this one's state.
    """

    first: ReasonRecord | None = None
    secondary: list[ReasonRecord] = field(default_factory=list)
    _first_exc: BaseException | None = field(default=None, repr=False)

    def capture(
        self,
        exc: BaseException,
        *,
        phase: Phase = Phase.UNKNOWN,
        context: ReasonContext | None = None,
        secondary: bool = False,
        reason: ReasonCode | None = None,
    ) -> ReasonRecord:
        """Record ``exc`` and return the record to report for the run.

        ``reason`` names the registry reason when the operation boundary, not the exception,
        decides it (a sidecar failing after the tables committed is ``publication_partial``).

        The first causal failure stays primary. A later failure (a cleanup that also failed, an
        outer scope seeing the same exception again) never replaces it: the same exception reuses
        its record, a different one is kept as bounded secondary state.
        """
        known = _lookup_captured(exc)
        if known is not None:
            record = known if context is None else known.with_context(context)
            if self.first is None:
                self.first, self._first_exc = record, exc
            return self.first
        record = (
            ReasonRecord.for_reason(reason, phase=phase, context=context)
            if reason is not None
            else reason_from_exception(exc, phase=phase, context=context)
        )
        _remember(exc, record)
        if self.first is None and not secondary:
            self.first, self._first_exc = record, exc
            return record
        if len(self.secondary) < _MAX_SECONDARY:
            self.secondary.append(record)
        return self.first or record


def _lookup_captured(exc: BaseException) -> ReasonRecord | None:
    try:
        record = exc.__dict__.get(_ATTR)
    except Exception:  # a hostile or slotted exception is simply not remembered
        return None
    return record if isinstance(record, ReasonRecord) else None


def _remember(exc: BaseException, record: ReasonRecord) -> None:
    try:
        exc.__dict__[_ATTR] = record
    except Exception:
        pass


def _stack() -> list[FailureScope]:
    stack: list[FailureScope] | None = getattr(_local, "stack", None)
    if stack is None:
        stack = _local.stack = []
    return stack


@contextmanager
def failure_scope() -> Iterator[FailureScope]:
    """Open this run's failure scope; the innermost open scope receives captures."""
    scope = FailureScope()
    stack = _stack()
    stack.append(scope)
    try:
        yield scope
    finally:
        stack.pop()


def current_failure_scope() -> FailureScope | None:
    stack = _stack()
    return stack[-1] if stack else None


def capture_failure(
    exc: BaseException,
    *,
    phase: Phase = Phase.UNKNOWN,
    context: ReasonContext | None = None,
    secondary: bool = False,
    reason: ReasonCode | None = None,
) -> ReasonRecord:
    """Record ``exc`` in the innermost open scope, or classify it standalone when none is open."""
    scope = current_failure_scope()
    if scope is not None:
        return scope.capture(exc, phase=phase, context=context, secondary=secondary, reason=reason)
    # No scope open: the record is still remembered per exception, so an outer observer reuses it.
    known = _lookup_captured(exc)
    if known is not None:
        return known if context is None else known.with_context(context)
    record = (
        ReasonRecord.for_reason(reason, phase=phase, context=context)
        if reason is not None
        else reason_from_exception(exc, phase=phase, context=context)
    )
    _remember(exc, record)
    return record


def amend_failure(exc: BaseException, context: ReasonContext) -> ReasonRecord | None:
    """Merge ``context`` into the record already captured for ``exc`` (the first failure keeps
    its identity; later evidence, such as how cleanup ended, is added). ``None`` if never captured."""
    known = _lookup_captured(exc)
    if known is None:
        return None
    merged = known.with_context(context)
    _remember(exc, merged)
    scope = current_failure_scope()
    if scope is not None and scope._first_exc is exc:
        scope.first = merged
    return merged


__all__ = [
    "FailureScope",
    "amend_failure",
    "capture_failure",
    "current_failure_scope",
    "failure_scope",
    "reason_from_exception",
    "reasons_from_report",
    "registered_families",
]
