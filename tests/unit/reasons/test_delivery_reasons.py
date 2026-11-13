"""The four delivery reason codes: kind, outcome, retry class and release stamp."""

from __future__ import annotations

import pytest

from decoy_engine.reasons import Kind, Outcome, RetryClass, get

_PUB = Outcome.PUBLICATION

CASES = [
    ("delivery_withheld", Kind.DECISION, _PUB, RetryClass.NEVER),
    ("delivery_dependent_dropped", Kind.DECISION, _PUB, RetryClass.NEVER),
    ("delivery_pointer_moved", Kind.FAILURE, _PUB, RetryClass.RETRY_OPERATION),
    ("delivery_rolled_back", Kind.WARNING, _PUB, RetryClass.NEVER),
]


@pytest.mark.parametrize(("code", "kind", "outcome", "retry"), CASES)
def test_delivery_code_is_pinned(
    code: str, kind: Kind, outcome: Outcome, retry: RetryClass
) -> None:
    spec = get("decoy", code)
    assert spec.kind is kind
    assert {r.outcome for r in spec.rules} == {outcome}
    assert {r.retry for r in spec.rules} == {retry}
    assert spec.since == "1.25.0"
