"""A2 and A3: the canary verdict policy is total, closed and refuses derived reasons as input."""

from __future__ import annotations

import itertools

import pytest

from decoy_engine.verification.canary import (
    POLICY_TABLE,
    CanaryBindingSource,
    CanaryCompletionClass,
    CanaryObservedReason,
    CanaryPolicyInputError,
    CanaryReason,
    CanaryStatus,
    CanaryVerdictPolicy,
    compose_reasons,
)
from tests.unit.verification.canary.test_report_fields import _report

R = CanaryReason
OBS = CanaryObservedReason
PLAN_ORDER = (
    "canary_match",
    "canary_scan_incomplete",
    "canary_input_rejected",
    "canary_binding_absent",
    "canary_publication_inactive",
    "canary_target_unavailable",
    "canary_publication_mismatch",
    "canary_artifact_unreadable",
    "canary_artifact_unsupported",
    "canary_resource_limit",
    "canary_cell_uncompared",
    "canary_span_short_excluded",
)
DERIVED = frozenset(
    {R.MATCH, R.SCAN_INCOMPLETE, R.INPUT_REJECTED, R.BINDING_ABSENT, R.SPAN_SHORT_EXCLUDED}
)


def _subsets(members: tuple) -> list[frozenset]:
    return [
        frozenset(c) for n in range(len(members) + 1) for c in itertools.combinations(members, n)
    ]


def _cells():
    for failed, unbound, findings, short in itertools.product((False, True), repeat=4):
        for observed in _subsets(tuple(OBS)):
            binding = (
                CanaryBindingSource.NONE if unbound else CanaryBindingSource.PUBLICATION_RECORD
            )
            yield failed, binding, findings, short, observed


def _call(failed, binding, findings, short, observed):
    kw = {"validation_failed": failed, "binding_source": binding, "observed": observed}
    status = CanaryVerdictPolicy.status(**kw)
    reasons = compose_reasons(**kw, has_findings=findings, has_short_excluded=short)
    return status, reasons


def _oracle(failed, binding, findings, short, observed):
    """The plan's precedence table and union rule, written independently of the module."""
    if failed:
        return CanaryStatus.REJECTED, {R.INPUT_REJECTED}
    unbound = binding is CanaryBindingSource.NONE
    status = CanaryStatus.INCOMPLETE if (unbound or observed) else CanaryStatus.COMPLETE
    chosen = {R(o.value) for o in observed}
    chosen |= {R.MATCH} if findings else set()
    chosen |= {R.BINDING_ABSENT} if unbound else set()
    chosen |= {R.SPAN_SHORT_EXCLUDED} if short else set()
    chosen |= {R.SCAN_INCOMPLETE} if status is CanaryStatus.INCOMPLETE else set()
    return status, chosen


def test_policy_power_set_is_total_and_closed() -> None:
    cells = list(_cells())
    assert len(cells) == 2 * 2 * 2 * 2 * 2**7 == 2048
    for cell in cells:
        status, reasons = _call(*cell)
        expected_status, expected_reasons = _oracle(*cell)
        assert type(status) is CanaryStatus and status is expected_status, cell
        assert type(reasons) is tuple and all(type(r) is CanaryReason for r in reasons), cell
        assert set(reasons) == expected_reasons, cell
    assert len(POLICY_TABLE) == 32
    keys = {row[:5] for row in POLICY_TABLE}
    assert keys == set(itertools.product((False, True), repeat=5))
    for failed, unbound, findings, short, has_observed, status in POLICY_TABLE:
        observed = frozenset({OBS.ARTIFACT_UNREADABLE}) if has_observed else frozenset()
        binding = CanaryBindingSource.NONE if unbound else CanaryBindingSource.PUBLICATION_RECORD
        assert _call(failed, binding, findings, short, observed)[0] is status


def test_reasons_canonical_order_and_unique() -> None:
    assert tuple(r.value for r in CanaryReason) == PLAN_ORDER
    assert tuple(o.value for o in OBS) == tuple(v for v in PLAN_ORDER if R(v) not in DERIVED)
    for cell in _cells():
        _, reasons = _call(*cell)
        assert len(set(reasons)) == len(reasons), cell
        assert list(reasons) == sorted(reasons, key=list(CanaryReason).index), cell
    # Seeded: two observed reasons and a finding give all four, in declaration order.
    observed = frozenset({OBS.ARTIFACT_UNREADABLE, OBS.RESOURCE_LIMIT})
    _, reasons = _call(False, CanaryBindingSource.PUBLICATION_RECORD, True, False, observed)
    assert reasons == (R.MATCH, R.SCAN_INCOMPLETE, R.ARTIFACT_UNREADABLE, R.RESOURCE_LIMIT)


def test_scan_incomplete_iff_incomplete() -> None:
    for cell in _cells():
        status, reasons = _call(*cell)
        assert (R.SCAN_INCOMPLETE in reasons) == (status is CanaryStatus.INCOMPLETE), cell
        if status is CanaryStatus.REJECTED:
            assert reasons == (R.INPUT_REJECTED,), cell


def test_short_excluded_does_not_change_status() -> None:
    for failed, binding, findings, _short, observed in _cells():
        without, _ = _call(failed, binding, findings, False, observed)
        with_short, reasons = _call(failed, binding, findings, True, observed)
        assert without is with_short
        assert (R.SPAN_SHORT_EXCLUDED in reasons) == (not failed)
    clean = _call(False, CanaryBindingSource.PUBLICATION_RECORD, False, True, frozenset())
    assert clean == (CanaryStatus.COMPLETE, (R.SPAN_SHORT_EXCLUDED,))


def test_derived_reasons_cannot_be_injected() -> None:
    subsets = _subsets(tuple(CanaryReason))
    assert len(subsets) == 4096
    bound = CanaryBindingSource.PUBLICATION_RECORD
    for subset in subsets:
        if subset:
            # Any CanaryReason as observed input is refused, derived or not.
            with pytest.raises(CanaryPolicyInputError) as info:
                _call(False, bound, False, False, subset)
            assert info.value.args == ()
        if subset & DERIVED:
            continue
        # The observable part is accepted only as CanaryObservedReason, and derived reasons
        # appear only from their own inputs.
        observed = frozenset(OBS(r.value) for r in subset)
        status, reasons = _call(False, bound, False, False, observed)
        assert not (set(reasons) & (DERIVED - {R.SCAN_INCOMPLETE}))
        assert set(reasons) - {R.SCAN_INCOMPLETE} == subset
    # Seeded: canary_match passed as observed, alone or with an observable reason.
    for seeded in ({R.MATCH}, {R.MATCH, OBS.ARTIFACT_UNREADABLE}, {"canary_match"}):
        with pytest.raises(CanaryPolicyInputError):
            _call(False, bound, False, False, frozenset(seeded))
    for bad in ({OBS.ARTIFACT_UNREADABLE}, [OBS.ARTIFACT_UNREADABLE], None):
        with pytest.raises(CanaryPolicyInputError):
            _call(False, bound, False, False, bad)
    for bad_flag in (1, None, "yes"):
        with pytest.raises(CanaryPolicyInputError):
            _call(bad_flag, bound, False, False, frozenset())
        with pytest.raises(CanaryPolicyInputError):
            _call(False, bound, bad_flag, False, frozenset())
    with pytest.raises(CanaryPolicyInputError):
        _call(False, "publication_record", False, False, frozenset())


@pytest.mark.parametrize(
    ("status", "finding_count", "expected"),
    [
        (CanaryStatus.REJECTED, 0, CanaryCompletionClass.REJECTED),
        (CanaryStatus.COMPLETE, 0, CanaryCompletionClass.CLEAR),
        (CanaryStatus.COMPLETE, 2, CanaryCompletionClass.FINDINGS),
        (CanaryStatus.INCOMPLETE, 2, CanaryCompletionClass.FINDINGS),
        (CanaryStatus.INCOMPLETE, 0, CanaryCompletionClass.INCOMPLETE),
    ],
)
def test_completion_class_is_total(status, finding_count, expected) -> None:
    reasons = {
        CanaryStatus.REJECTED: (R.INPUT_REJECTED,),
        CanaryStatus.COMPLETE: (R.MATCH,) if finding_count else (),
        CanaryStatus.INCOMPLETE: ((R.MATCH,) if finding_count else ()) + (R.SCAN_INCOMPLETE,),
    }[status]
    report = _report(
        status=status,
        complete=status is CanaryStatus.COMPLETE,
        reasons=reasons,
        finding_count=finding_count,
        count_capped=finding_count > 1,
        findings=() if finding_count == 0 else _report().findings,
    )
    assert CanaryVerdictPolicy.completion_class(report) is expected
    assert set(CanaryCompletionClass) == {
        CanaryCompletionClass.CLEAR,
        CanaryCompletionClass.FINDINGS,
        CanaryCompletionClass.INCOMPLETE,
        CanaryCompletionClass.REJECTED,
    }
