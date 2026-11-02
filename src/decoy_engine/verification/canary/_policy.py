"""The canary verdict policy: status by precedence, reasons by union (F34 invariant I6).

One table decides status for every caller, the same way a SARIF result's ``kind`` or a JUnit
suite outcome is decided once from its inputs rather than re-derived by each consumer:

1. validation failed: ``REJECTED``, with exactly ``(canary_input_rejected,)``;
2. ``binding_source = none`` or any observed reason: ``INCOMPLETE``;
3. otherwise ``COMPLETE``.

Reasons are the union of the observed set and the derived reasons, emitted in ``CanaryReason``
declaration order; ``canary_scan_incomplete`` is present if and only if the status is
``INCOMPLETE``. A short-excluded canary is a named limit and never changes status (Q3).
Derived reasons are never accepted as input: ``observed`` must be a ``frozenset`` of exact
``CanaryObservedReason`` members. The module holds no mutable state; ``POLICY_TABLE`` is an
immutable tuple built at import.
"""

from __future__ import annotations

import itertools
from typing import NamedTuple, cast

from decoy_engine.verification.canary._types import (
    CanaryBindingSource,
    CanaryCompletionClass,
    CanaryObservedReason,
    CanaryPolicyInputError,
    CanaryReason,
    CanarySpanReport,
    CanaryStatus,
)


class CanaryPolicyRow(NamedTuple):
    validation_failed: bool
    unbound: bool
    has_findings: bool
    has_short_excluded: bool
    has_observed: bool
    status: CanaryStatus


def _require(ok: bool) -> None:
    if not ok:
        raise CanaryPolicyInputError


def _check_inputs(
    validation_failed: object, binding_source: object, observed: object, *flags: object
) -> frozenset[CanaryObservedReason]:
    _require(all(type(v) is bool for v in (validation_failed, *flags)))
    _require(type(binding_source) is CanaryBindingSource)
    _require(type(observed) is frozenset)
    members = cast("frozenset[object]", observed)
    _require(all(type(o) is CanaryObservedReason for o in members))
    return cast("frozenset[CanaryObservedReason]", members)


def _status(
    validation_failed: bool, unbound: bool, observed: frozenset[CanaryObservedReason]
) -> CanaryStatus:
    if validation_failed:
        return CanaryStatus.REJECTED
    if unbound or observed:
        return CanaryStatus.INCOMPLETE
    return CanaryStatus.COMPLETE


def derive_status(
    *,
    validation_failed: bool,
    binding_source: CanaryBindingSource,
    observed: frozenset[CanaryObservedReason],
) -> CanaryStatus:
    observed = _check_inputs(validation_failed, binding_source, observed)
    return _status(validation_failed, binding_source is CanaryBindingSource.NONE, observed)


def compose_reasons(
    *,
    validation_failed: bool,
    binding_source: CanaryBindingSource,
    observed: frozenset[CanaryObservedReason],
    has_findings: bool,
    has_short_excluded: bool,
) -> tuple[CanaryReason, ...]:
    observed = _check_inputs(
        validation_failed, binding_source, observed, has_findings, has_short_excluded
    )
    if validation_failed:
        return (CanaryReason.INPUT_REJECTED,)
    unbound = binding_source is CanaryBindingSource.NONE
    chosen = {CanaryReason(o.value) for o in observed}
    if has_findings:
        chosen.add(CanaryReason.MATCH)
    if unbound:
        chosen.add(CanaryReason.BINDING_ABSENT)
    if has_short_excluded:
        chosen.add(CanaryReason.SPAN_SHORT_EXCLUDED)
    if _status(False, unbound, observed) is CanaryStatus.INCOMPLETE:
        chosen.add(CanaryReason.SCAN_INCOMPLETE)
    return tuple(r for r in CanaryReason if r in chosen)


def completion_class(report: CanarySpanReport) -> CanaryCompletionClass:
    """The one class a consumer maps to its own outcome (HTTP status, exit code)."""
    _require(type(report) is CanarySpanReport)
    if report.status is CanaryStatus.REJECTED:
        return CanaryCompletionClass.REJECTED
    if report.finding_count > 0:
        return CanaryCompletionClass.FINDINGS
    if report.status is CanaryStatus.COMPLETE:
        return CanaryCompletionClass.CLEAR
    return CanaryCompletionClass.INCOMPLETE


class CanaryVerdictPolicy:
    """Namespace for the single verdict policy; it has no instances and no state."""

    __slots__ = ()
    status = staticmethod(derive_status)
    reasons = staticmethod(compose_reasons)
    completion_class = staticmethod(completion_class)


def _build_table() -> tuple[CanaryPolicyRow, ...]:
    some = frozenset({CanaryObservedReason.TARGET_UNAVAILABLE})
    return tuple(
        CanaryPolicyRow(
            failed,
            unbound,
            findings,
            short,
            has_obs,
            _status(failed, unbound, some if has_obs else frozenset()),
        )
        for failed, unbound, findings, short, has_obs in itertools.product((False, True), repeat=5)
    )


POLICY_TABLE: tuple[CanaryPolicyRow, ...] = _build_table()
