"""PERF.BASE.2: load-time sanity envelope for the fixture suite.

The sprint spec asks for "<30s to load on the dev-machine tier". The
small + medium fixtures are well below that; we assert generously so
the test stays green on slower CI runners too. The point is to catch
catastrophic regressions (a 30x load slowdown after an engine pyarrow
bump) rather than to gate exact timings.
"""

from __future__ import annotations

import time

import pytest

from .loaders import available_tiers, load_tier
from .schema import get_tier

pytestmark = pytest.mark.perf


# Per-tier soft envelopes in seconds. Generous; we want a regression
# alarm, not a calibration test. The dev box loads small in <100ms and
# medium in <2s, so 5s / 30s leave plenty of headroom.
_LOAD_ENVELOPE_S: dict[str, float] = {
    "small": 5.0,
    "medium": 30.0,
    "large": 300.0,
}


@pytest.mark.parametrize("tier_name", available_tiers() or ["small"])
def test_tier_loads_within_envelope(tier_name: str) -> None:
    t0 = time.perf_counter()
    df = load_tier(tier_name)
    elapsed = time.perf_counter() - t0

    envelope = _LOAD_ENVELOPE_S[tier_name]
    assert elapsed < envelope, (
        f"tier {tier_name!r} load took {elapsed:.2f}s, envelope is {envelope}s"
    )
    # Defensive: confirm the load actually returned data; an empty df
    # would also pass the timing assert.
    assert len(df) == get_tier(tier_name).rows
