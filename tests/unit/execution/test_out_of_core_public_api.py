"""SC5 (2026-07-09): the cross-repo out-of-core eligibility query surface.

Pins that `check_out_of_core_compatibility`, its result dataclasses, the two
SC2 routing threshold constants, and the currently-admitted strategy set are
importable from the public `decoy_engine.execution` package -- the surface
decoy-platform's admission estimator (or any future caller) consults instead
of reaching into the `_`-prefixed `out_of_core` internals directly. A thin
re-export test: it does not re-verify the gate's own decision logic (that is
`test_out_of_core_routing.py`'s job), only that the surface exists, is the
SAME objects the live router uses (no accidental copy/drift), and behaves
identically whether imported from the top-level package or the `out_of_core`
subpackage.
"""

from __future__ import annotations

from decoy_engine.execution import (
    FULL_FRAME_REJECT_ROWS_DEFAULT,
    OUT_OF_CORE_SUPPORTED_STRATEGIES,
    OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT,
    OutOfCoreCompatibility,
    OutOfCoreRejection,
    check_out_of_core_compatibility,
)
from decoy_engine.execution._pipeline_routing import (
    FULL_FRAME_REJECT_ROWS_DEFAULT as _ROUTING_FULL_FRAME_REJECT_ROWS_DEFAULT,
)
from decoy_engine.execution._pipeline_routing import (
    OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT as _ROUTING_OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT,
)
from decoy_engine.execution.out_of_core import check_out_of_core_compatibility as _internal_check
from decoy_engine.execution.out_of_core._compat import (
    _INITIAL_SUPPORTED_STRATEGIES,
)
from decoy_engine.relationships import RelationshipGraph


def test_check_out_of_core_compatibility_is_the_same_function_the_router_uses():
    # Identity, not just equality: the public re-export must be the exact
    # object `_pipeline_routing.out_of_core_admission` calls, so the public
    # surface can never silently drift from the live routing decision.
    assert check_out_of_core_compatibility is _internal_check


def test_thresholds_match_the_live_routing_defaults():
    assert OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT == _ROUTING_OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT
    assert FULL_FRAME_REJECT_ROWS_DEFAULT == _ROUTING_FULL_FRAME_REJECT_ROWS_DEFAULT
    assert OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT == 5_000_000
    assert FULL_FRAME_REJECT_ROWS_DEFAULT == 7_500_000


def test_supported_strategies_is_the_same_set_the_gate_admits():
    assert OUT_OF_CORE_SUPPORTED_STRATEGIES == _INITIAL_SUPPORTED_STRATEGIES
    assert (
        frozenset({"hash", "redact", "truncate", "passthrough"}) == OUT_OF_CORE_SUPPORTED_STRATEGIES
    )


def test_no_relationships_is_rejected_through_the_public_entrypoint():
    # Smoke-tests the public name is callable with real engine types (not
    # just importable) and returns the same dataclass shape as the private one.
    result = check_out_of_core_compatibility(
        plan=None,  # type: ignore[arg-type]
        work=[],
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
    )
    assert isinstance(result, OutOfCoreCompatibility)
    assert result.accepted is False
    assert result.rejections
    assert isinstance(result.rejections[0], OutOfCoreRejection)
    assert result.primary_code == "out_of_core_no_relationships"
