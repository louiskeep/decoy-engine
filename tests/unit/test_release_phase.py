"""RELEASE_PHASE is the engine-owned switch the pre-GA to GA gates read."""

from __future__ import annotations

import decoy_engine
from decoy_engine.release import RELEASE_PHASE, is_pre_ga


def test_release_phase_is_a_known_value() -> None:
    assert RELEASE_PHASE in ("pre-ga", "ga")


def test_is_pre_ga_tracks_the_constant() -> None:
    assert is_pre_ga() == (RELEASE_PHASE == "pre-ga")


def test_currently_pre_ga() -> None:
    # We are not live yet. This test is the tripwire for the GA flip: when
    # RELEASE_PHASE moves to "ga", update it deliberately alongside the gates
    # that branch on it (compat corpus, section 8.1 deletion policy).
    assert RELEASE_PHASE == "pre-ga"
    assert is_pre_ga() is True


def test_exported_from_public_api() -> None:
    # Platform reads this across the in-process boundary, so it must stay on the
    # public surface (ADR-0001).
    assert decoy_engine.RELEASE_PHASE == RELEASE_PHASE
    assert "RELEASE_PHASE" in decoy_engine.__all__
    assert "is_pre_ga" in decoy_engine.__all__
