"""Prefer the bounded route over the full-frame oracle for a RAM-fitting FK job.

`decide_execution_route` used to send a bounded-eligible relationship job to
full-frame whenever the byte estimate or the probe said it fits, although the
full-frame oracle is the slowest route per row. These tests pin the rule that
replaced it:

  - a bounded-eligible job (`eligible and not cyclic`) takes its bounded route
    even when it fits, and the choice between out-of-core and sequential is the
    unchanged eligibility rule;
  - a job no bounded route can take (a cross-table cycle, or one disqualified
    from sequential) still runs full-frame on a confirmed fit and still rejects
    when it does not fit;
  - a fitting job carrying a key type out-of-core rejects but the oracle
    completes takes sequential instead, on both full-frame admission paths;
  - the explicit modes, the lazy-transform rule and the
    `use_byte_estimate_routing=False` rollback are untouched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pipeline_routing import (
    _has_cross_table_fk_cycle,
    _sequential_eligible,
    decide_execution_route,
)
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

_OOC_THRESHOLD = 5_000_000
_REJECT_ROWS = 7_500_000
_BOUNDED_OOC = ("out_of_core", "byte_estimate_bounded_out_of_core")
_BOUNDED_SEQ = ("sequential", "pure_mask_fk")
_FITS_FULL_FRAME = ("full_frame", "byte_estimate_full_frame_fits")
_PROBE_FULL_FRAME = ("full_frame", "probe_recovered_full_frame")


def _edge(parent: str, child: str, ns: str) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table=parent,
        parent_columns=("id",),
        child_table=child,
        child_columns=(f"ref_{parent}",),
        namespace=ns,
        orphan_policy=OrphanPolicy.PRESERVE,
    )


def _acyclic() -> RelationshipGraph:
    return RelationshipGraph(edges=(_edge("parent", "child", "n"),), ordering=())


def _cyclic() -> RelationshipGraph:
    return RelationshipGraph(edges=(_edge("a", "b", "na"), _edge("b", "a", "nb")), ordering=())


def _profile() -> Any:
    return SimpleNamespace(relationships=(object(),), tables=())


def _route(**overrides: Any) -> tuple[str, str]:
    kwargs: dict[str, Any] = dict(
        has_generate_table=False,
        has_mask_table=True,
        validators=[],
        fidelity_report=False,
        vault_writer=None,
        execution_mode="auto",
        graph=_acyclic(),
        out_of_core_compatible=True,
        largest_table_rows=10,
        out_of_core_threshold_rows=_OOC_THRESHOLD,
        full_frame_reject_rows=_REJECT_ROWS,
        use_byte_estimate_routing=True,
        full_frame_fits_estimate=True,
        use_probe_routing=True,
        probe_recovers_full_frame=None,
    )
    kwargs.update(overrides)
    return decide_execution_route(_profile(), **kwargs)


# A fit can be established by the static estimate or, when that is inconclusive, by the
# probe; every rule below must hold on both paths.
_ESTIMATE_FIT = {"full_frame_fits_estimate": True}
_PROBE_FIT = {"full_frame_fits_estimate": False, "probe_recovers_full_frame": True}
_FIT_PATHS = pytest.mark.parametrize("fit", [_ESTIMATE_FIT, _PROBE_FIT], ids=["estimate", "probe"])


class TestT1FittingBoundedEligibleJobTakesTheBoundedRoute:
    @_FIT_PATHS
    def test_compatible_job_takes_out_of_core_even_when_tiny(self, fit: dict[str, Any]) -> None:
        assert _route(**fit) == _BOUNDED_OOC

    @_FIT_PATHS
    def test_incompatible_job_takes_sequential(self, fit: dict[str, Any]) -> None:
        assert _route(out_of_core_compatible=False, **fit) == _BOUNDED_SEQ

    @_FIT_PATHS
    def test_size_does_not_matter_for_a_fitting_job(self, fit: dict[str, Any]) -> None:
        assert _route(largest_table_rows=50_000_000, **fit) == _BOUNDED_OOC

    def test_non_fitting_bounded_job_takes_the_same_route(self) -> None:
        # The fit signals above must not be what selects the bounded route.
        got = _route(full_frame_fits_estimate=False, probe_recovers_full_frame=None)
        assert got == _BOUNDED_OOC


class TestT2CyclicJobIsNotRegressed:
    def test_the_cycle_is_eligible_by_shape_yet_cyclic(self) -> None:
        # The premise of the `not cyclic` term: `_sequential_eligible` checks job shape
        # only, so it stays True for a cross-table cycle and the cycle is flagged apart.
        eligible, reason = _sequential_eligible(
            _profile(),
            has_generate_table=False,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
        )
        assert eligible is True
        assert reason == "pure_mask_fk"
        assert _has_cross_table_fk_cycle(_cyclic()) is True
        assert _has_cross_table_fk_cycle(_acyclic()) is False

    def test_small_cyclic_job_still_runs_full_frame_on_an_estimate_fit(self) -> None:
        assert _route(graph=_cyclic(), out_of_core_compatible=False) == _FITS_FULL_FRAME

    def test_small_cyclic_job_still_runs_full_frame_on_a_probe_fit(self) -> None:
        got = _route(graph=_cyclic(), out_of_core_compatible=False, **_PROBE_FIT)
        assert got == _PROBE_FULL_FRAME

    @_FIT_PATHS
    def test_cyclic_job_is_not_diverted_even_if_the_compat_gate_were_wrong(
        self, fit: dict[str, Any]
    ) -> None:
        # Defense in depth: the compat gate excludes cycles, but the router must not rely
        # on it to keep a cyclic job off out-of-core.
        assert _route(graph=_cyclic(), out_of_core_compatible=True, **fit)[0] == "full_frame"


class TestT3NonBoundedJobsStillRejectWhenTheyDoNotFit:
    @pytest.mark.parametrize("fits", [False, None])
    def test_large_cyclic_job_rejects(self, fits: bool | None) -> None:
        with pytest.raises(ExecutionError) as exc:
            _route(
                graph=_cyclic(),
                out_of_core_compatible=False,
                largest_table_rows=50_000_000,
                full_frame_fits_estimate=fits,
            )
        assert exc.value.code == "fk_full_frame_oom_risk_rejected"

    def test_cyclic_job_rejects_even_when_tiny_if_it_does_not_fit(self) -> None:
        # Rejection needs "does not fit and no probe recovery", not "large".
        with pytest.raises(ExecutionError) as exc:
            _route(graph=_cyclic(), out_of_core_compatible=False, full_frame_fits_estimate=False)
        assert exc.value.code == "fk_full_frame_oom_risk_rejected"

    def test_sequential_ineligible_job_rejects_when_it_does_not_fit(self) -> None:
        with pytest.raises(ExecutionError) as exc:
            _route(fidelity_report=True, full_frame_fits_estimate=False)
        assert exc.value.code == "fk_full_frame_oom_risk_rejected"

    def test_sequential_ineligible_job_stays_full_frame_on_a_fit(self) -> None:
        assert _route(fidelity_report=True) == _FITS_FULL_FRAME
        assert _route(validators=["v"]) == _FITS_FULL_FRAME

    def test_sequential_ineligible_job_stays_full_frame_on_a_probe_fit(self) -> None:
        got = _route(fidelity_report=True, **_PROBE_FIT)
        assert got == _PROBE_FULL_FRAME

    def test_a_cycle_carrying_the_dtype_hazard_still_runs_full_frame(self) -> None:
        # The dtype retention only redirects a bounded-eligible job; nothing bounded
        # exists for a cycle, so the oracle is still its route.
        got = _route(graph=_cyclic(), out_of_core_compatible=False, fk_ooc_dtype_hazard=True)
        assert got == _FITS_FULL_FRAME
        got = _route(
            graph=_cyclic(), out_of_core_compatible=False, fk_ooc_dtype_hazard=True, **_PROBE_FIT
        )
        assert got == _PROBE_FULL_FRAME


class TestT4RouteUnchangedClasses:
    def test_no_relationships_stays_full_frame(self) -> None:
        got = decide_execution_route(
            SimpleNamespace(relationships=(), tables=()),
            has_generate_table=False,
            has_mask_table=True,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
            execution_mode="auto",
            graph=RelationshipGraph(edges=(), ordering=()),
            full_frame_fits_estimate=True,
        )
        assert got == ("full_frame", "no_relationships")

    def test_generate_plus_mask_stays_full_frame(self) -> None:
        assert _route(has_generate_table=True) == ("full_frame", "generate_plus_mask")

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("full_frame", ("full_frame", "override_full_frame")),
            ("sequential", _BOUNDED_SEQ),
            ("out_of_core", ("out_of_core", "override_out_of_core")),
        ],
    )
    @_FIT_PATHS
    def test_explicit_modes_ignore_the_fit(
        self, mode: str, expected: tuple[str, str], fit: dict[str, Any]
    ) -> None:
        assert _route(execution_mode=mode, **fit) == expected

    @pytest.mark.parametrize("probe", [None, True])
    def test_rollback_flag_off_is_the_row_count_logic(self, probe: bool | None) -> None:
        # A fit signal that would matter under the default is ignored, and the estimate
        # never decides: small -> sequential, at/above the threshold -> out-of-core.
        off = {
            "use_byte_estimate_routing": False,
            "probe_recovers_full_frame": probe,
            "full_frame_fits_estimate": True,
        }
        assert _route(**off) == _BOUNDED_SEQ
        assert _route(largest_table_rows=_OOC_THRESHOLD, **off) == (
            "out_of_core",
            "out_of_core_large_fk",
        )

    def test_rollback_flag_off_ignores_the_dtype_hazard(self) -> None:
        off = {"use_byte_estimate_routing": False, "fk_ooc_dtype_hazard": True}
        assert _route(largest_table_rows=_OOC_THRESHOLD, **off) == (
            "out_of_core",
            "out_of_core_large_fk",
        )

    def test_probe_flag_off_ignores_a_probe_fit(self) -> None:
        # With the probe off, a probe fit is not a fit at all: the job is bounded because
        # it does not fit, exactly as before.
        got = _route(use_probe_routing=False, **_PROBE_FIT)
        assert got == _BOUNDED_OOC

    def test_lazy_transform_rule_is_untouched(self) -> None:
        assert _route(lazy_transform_bearing=True) == _BOUNDED_SEQ
        with pytest.raises(ExecutionError) as exc:
            _route(lazy_transform_bearing=True, graph=_cyclic(), out_of_core_compatible=False)
        assert exc.value.code == "fk_full_frame_oom_risk_rejected"


class TestT5cDtypeRetentionOnBothAdmissionPaths:
    """A fitting job whose key types out-of-core rejects (but the oracle completes) must
    not turn into an out-of-core reject. Sequential completes every such shape."""

    @_FIT_PATHS
    def test_hazard_job_takes_sequential_not_out_of_core(self, fit: dict[str, Any]) -> None:
        assert _route(fk_ooc_dtype_hazard=True, **fit) == _BOUNDED_SEQ

    @_FIT_PATHS
    def test_hazard_job_takes_sequential_at_any_size(self, fit: dict[str, Any]) -> None:
        got = _route(fk_ooc_dtype_hazard=True, largest_table_rows=50_000_000, **fit)
        assert got == _BOUNDED_SEQ

    @_FIT_PATHS
    def test_no_hazard_still_takes_out_of_core(self, fit: dict[str, Any]) -> None:
        assert _route(fk_ooc_dtype_hazard=False, **fit) == _BOUNDED_OOC

    def test_the_guard_applies_only_to_a_fit_admission(self) -> None:
        # Not fitting is the existing behavior: eligibility alone picks the bounded route.
        got = _route(
            fk_ooc_dtype_hazard=True, full_frame_fits_estimate=False, probe_recovers_full_frame=None
        )
        assert got == _BOUNDED_OOC

    def test_a_probe_fit_with_the_probe_off_is_not_a_fit(self) -> None:
        got = _route(fk_ooc_dtype_hazard=True, use_probe_routing=False, **_PROBE_FIT)
        assert got == _BOUNDED_OOC

    def test_unpriceable_estimate_is_not_a_fit(self) -> None:
        got = _route(fk_ooc_dtype_hazard=True, full_frame_fits_estimate=None)
        assert got == _BOUNDED_OOC

    def test_incompatible_hazard_job_is_sequential_either_way(self) -> None:
        got = _route(fk_ooc_dtype_hazard=True, out_of_core_compatible=False)
        assert got == _BOUNDED_SEQ


class TestT6ProbePathGatedIdentically:
    def test_bounded_eligible_probe_fit_diverts_to_the_bounded_route(self) -> None:
        assert _route(**_PROBE_FIT) == _BOUNDED_OOC
        assert _route(out_of_core_compatible=False, **_PROBE_FIT) == _BOUNDED_SEQ

    def test_cyclic_probe_fit_stays_full_frame(self) -> None:
        got = _route(graph=_cyclic(), out_of_core_compatible=False, **_PROBE_FIT)
        assert got == _PROBE_FULL_FRAME

    def test_inconclusive_probe_is_not_a_fit(self) -> None:
        with pytest.raises(ExecutionError):
            _route(
                graph=_cyclic(),
                out_of_core_compatible=False,
                full_frame_fits_estimate=False,
                probe_recovers_full_frame=None,
            )
