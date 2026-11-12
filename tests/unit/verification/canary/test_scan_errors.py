"""A10: a reader's failure is caught by exact type only; plus the binding and consumer facts."""

from __future__ import annotations

import pytest

from decoy_engine.verification.canary import (
    CanaryArtifactInput,
    CanaryArtifactReadFailure,
    CanaryBindingSource,
    CanaryConsumerReason,
    CanaryStatus,
)
from decoy_engine.verification.canary import (
    CanaryArtifactOutcome as O,
)
from decoy_engine.verification.canary import (
    CanaryReason as R,
)
from tests.unit.verification.canary.test_scan_end_to_end import Spy, art, scan, strings

CANARY = ("canary-value-1",)


class Sub(CanaryArtifactReadFailure):
    def __init__(self) -> None:
        super().__init__(O.UNREADABLE)


@pytest.mark.parametrize("outcome", [O.UNREADABLE, O.UNSUPPORTED, O.RESOURCE_LIMITED])
@pytest.mark.parametrize("when", ["at_open", "mid_iteration"])
def test_read_failure_exact_type_only(outcome: O, when: str) -> None:
    failure = CanaryArtifactReadFailure(outcome)
    spy = Spy([strings("canary-value-1")], failure if when == "mid_iteration" else None)
    if when == "at_open":

        def opener():  # type: ignore[no-untyped-def]
            raise failure

        artifact = CanaryArtifactInput(0, None, opener)
    else:
        artifact = CanaryArtifactInput(0, None, spy.opener())
    report = scan(CANARY, [artifact, art(1, strings("x"))])
    assert getattr(report, f"{outcome.value}_artifact_count") == 1
    assert report.scanned_artifact_count == 1 and report.status is CanaryStatus.INCOMPLETE
    if when == "mid_iteration":  # the batch read before the failure still counts
        assert report.finding_count == 1 and spy.closed == 1


@pytest.mark.parametrize("when", ["at_open", "mid_iteration"])
def test_subclass_of_read_failure_propagates(when: str) -> None:
    spy = Spy([], Sub())
    if when == "at_open":

        def opener():  # type: ignore[no-untyped-def]
            raise Sub()

        artifact = CanaryArtifactInput(0, None, opener)
    else:
        artifact = CanaryArtifactInput(0, None, spy.opener())
    with pytest.raises(Sub):
        scan(CANARY, [artifact])
    assert spy.closed == spy.opened


def test_binding_none_is_always_incomplete() -> None:
    report = scan(CANARY, [art(0, strings("x"))], binding=CanaryBindingSource.NONE)
    assert report.status is CanaryStatus.INCOMPLETE and not report.complete
    assert report.reasons == (R.SCAN_INCOMPLETE, R.BINDING_ABSENT)


def test_consumer_publication_inactive_is_incomplete() -> None:
    report = scan(CANARY, [], consumer=frozenset({CanaryConsumerReason.PUBLICATION_INACTIVE}))
    assert report.reasons == (R.SCAN_INCOMPLETE, R.PUBLICATION_INACTIVE, R.TARGET_UNAVAILABLE)
