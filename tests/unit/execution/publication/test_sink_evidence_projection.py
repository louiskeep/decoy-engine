"""The sink's state-table state projected onto its F29 evidence (plan 5.6; slice c1b).

SEALED is `published`, UNCERTAIN `unknown` (bound, not confirmed durable), REFUSED, FAILED and
ABORTED `not_published`; a bound unit has no stage left to clean. `observed_commit` keeps
working for a foreign sink and returns the reference sink's receipt.

Red before c1b (start commit `acb2c4aa`): no projection table and no receipt; the sink's
commit returns `None` and a refused commit is a raw `OSError` that leaves the evidence at the
pre-F22 "rename failed" values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from decoy_engine.execution._publication_observer import (
    SINK_STATE_EVIDENCE,
    observed_commit,
    project_sink_state,
)
from decoy_engine.publication import CommitReceipt, PublicationRefused, PublicationRequest
from decoy_engine.publication._sink_state import SinkState
from decoy_engine.reasons import CleanupState, PublicationState
from tests.unit.execution.publication._sink_support import POLICIES, sink_factory, table

P, C = PublicationState, CleanupState


def test_every_sink_state_has_one_evidence_row() -> None:
    assert set(SINK_STATE_EVIDENCE) == set(SinkState)
    assert SINK_STATE_EVIDENCE[SinkState.SEALED] == (P.PUBLISHED, C.NOT_NEEDED)
    assert SINK_STATE_EVIDENCE[SinkState.UNCERTAIN] == (P.UNKNOWN, C.NOT_NEEDED)
    for state in (SinkState.REFUSED, SinkState.FAILED, SinkState.ABORTED):
        assert SINK_STATE_EVIDENCE[state][0] is P.NOT_PUBLISHED, state


def test_projection_keeps_observed_cleanup_where_the_row_has_none() -> None:
    from decoy_engine.execution._publication_observer import PublicationEvidence

    evidence = PublicationEvidence(state=P.PUBLISHED, cleanup=C.CONFIRMED)
    project_sink_state(evidence, SinkState.REFUSED)
    assert (evidence.state, evidence.cleanup) == (P.NOT_PUBLISHED, C.CONFIRMED)
    project_sink_state(evidence, SinkState.SEALED)
    assert (evidence.state, evidence.cleanup) == (P.PUBLISHED, C.NOT_NEEDED)


@pytest.mark.parametrize("policy", POLICIES)
def test_observed_commit_returns_the_receipt_and_projects_sealed(
    policy: str, tmp_path: Path
) -> None:
    with sink_factory(policy, tmp_path) as make:
        sink = make(PublicationRequest("op-1"))
        sink.write("customers", table("A"))
        receipt = observed_commit(sink)
    assert isinstance(receipt, CommitReceipt)
    assert receipt.outcome == "published" and receipt is sink.receipt
    evidence = sink.publication_evidence
    assert (evidence.state, evidence.cleanup) == (P.PUBLISHED, C.NOT_NEEDED)


@pytest.mark.parametrize("policy", POLICIES)
def test_a_refused_commit_is_not_published_with_its_stage_cleaned(
    policy: str, tmp_path: Path
) -> None:
    with sink_factory(policy, tmp_path) as make:
        first = make()
        first.write("customers", table("A"))
        first.commit()
        sink = make()
        sink.write("customers", table("B"))
        with pytest.raises(PublicationRefused) as info:
            observed_commit(sink)
    assert info.value.code == "publication_target_occupied"
    assert sink.state is SinkState.REFUSED
    evidence = sink.publication_evidence
    expected = C.CONFIRMED if policy == "ambient" else C.ATTEMPTED
    assert (evidence.state, evidence.cleanup) == (P.NOT_PUBLISHED, expected)


@pytest.mark.parametrize("policy", POLICIES)
def test_an_aborted_sink_is_not_published(policy: str, tmp_path: Path) -> None:
    with sink_factory(policy, tmp_path) as make:
        sink = make()
        sink.write("customers", table("A"))
        sink.abort()
    assert sink.state is SinkState.ABORTED
    evidence = sink.publication_evidence
    expected = C.CONFIRMED if policy == "ambient" else C.ATTEMPTED
    assert (evidence.state, evidence.cleanup) == (P.NOT_PUBLISHED, expected)


def test_observed_commit_of_a_foreign_sink_returns_no_receipt() -> None:
    class Foreign:
        committed = 0

        def commit(self) -> Any:
            self.committed += 1
            return "not a receipt"

    sink = Foreign()
    assert observed_commit(sink) is None
    assert sink.committed == 1


def test_a_sink_request_carrying_a_vault_is_refused(tmp_path: Path) -> None:
    """B-6 rule B0 stays (plan Q3): an engine sink never binds a vault."""
    from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
    from decoy_engine.publication import VaultBinding

    request = PublicationRequest("op-1", vault=VaultBinding(writer=None, out=None))  # type: ignore[arg-type]
    assert request.vault is not None  # non-vacuity: the request really carries a vault
    with pytest.raises(ValueError, match="without a vault"):
        ParquetTransactionalSink(tmp_path / "out", request=request)
    assert list(tmp_path.iterdir()) == []
