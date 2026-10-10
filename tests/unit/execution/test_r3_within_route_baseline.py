"""R3 acceptance test 1: within-route output identity across the executor extraction.

Plan: docs/plans/2026-10-10-r3-run-context-route-executors.md section 6, test 1.
`r3_within_route_baseline.json` was captured on origin/main @ ac6a0e8e before any
production edit, through the public `run_pipeline`, for every layer-1 route and the
full_frame sub-lanes (unified admitted, unified fallback, flag off, auto-chunk, multi-table
split, generate+mask, fidelity, post-validation, quarantine, streamed sink). Each scenario's
projection (`_r3_project.project`) compares tables, schema, warnings, row errors, table kinds,
the five `execution` telemetry members, `execution_plan`, `fidelity_reports` and all other
quality metrics EXACTLY; the enumerated volatile fields (wall times, path-dependent hashes)
compare structure only.

Do not weaken a scenario, drop a field or regenerate the baseline to make a refactor pass:
a diff here is a behavior change in a route. Regenerate only with reviewer approval:
`R3_WRITE_BASELINES=1 pytest tests/unit/execution/test_r3_within_route_baseline.py`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.unit.execution import _r3_project as proj
from tests.unit.execution import _r3_scenarios as sc

pytestmark = pytest.mark.filterwarnings("ignore")

_BASELINE = Path(__file__).parent / "r3_within_route_baseline.json"

# Anti-vacuity: the layer-1 route each scenario must actually take. A baseline that silently
# records the wrong route would pass its own comparison after a routing regression.
EXPECTED_MODE: dict[str, str] = {
    "ff_legacy_single": "full_frame",
    "ff_unified_admitted": "full_frame",
    "ff_unified_fallback": "full_frame",
    "ff_unified_flag_off": "full_frame",
    "ff_multi_table": "full_frame",
    "ff_multi_table_split": "full_frame",
    "ff_quarantine_format_error": "full_frame",
    "ff_generate_mask": "full_frame",
    "ff_fidelity": "full_frame",
    "ff_post_validation": "full_frame",
    "ff_explain_plan": "full_frame",
    "ff_auto_chunk_oracle_lane": "full_frame",
    "ff_auto_chunk_dispatcher_lane": "full_frame",
    "ff_streamed_sink": "full_frame",
    "ff_streamed_sink_native_lane": "full_frame",
    "ff_resident_sink_untouched": "full_frame",
    "seq_fk": "sequential",
    "seq_fk_orphans_remap": "sequential",
    "seq_fk_orphans_warn": "sequential",
    "seq_fk_sink": "sequential",
    "seq_fk_loader": "sequential",
    "seq_fk_explain": "sequential",
    "seq_fk_transforms_declined": "sequential",
    "ff_fk_transforms_declined_byte_routing": "full_frame",
    "ooc_fk_forced_loader": "out_of_core",
    "ooc_fk_forced": "out_of_core",
    "ooc_fk_auto": "out_of_core",
    "ooc_fk_forced_sink": "out_of_core",
    "ooc_fk_orphans_remap": "out_of_core",
    "ooc_fk_orphans_warn_sink": "out_of_core",
    "ooc_fk_lazy_sources_sink": "out_of_core",
    "ooc_fk_budget_bytes": "out_of_core",
}

# The sub-lane each full_frame scenario must really exercise (read off quality_metrics).
EXPECTED_LANE_MARKER: dict[str, tuple[str, bool]] = {
    "ff_unified_admitted": ("unified_slice_activation", True),
    "ff_unified_fallback": ("unified_slice_activation", False),
    "ff_unified_flag_off": ("unified_slice_activation", False),
    "ff_auto_chunk_oracle_lane": ("auto_chunk", True),
    "ff_auto_chunk_dispatcher_lane": ("auto_chunk", True),
    "ff_multi_table_split": ("chunked_route_by_table", True),
    "ff_quarantine_format_error": ("quarantine", True),
    "ff_fidelity": ("fidelity_reports", True),
    "ff_post_validation": ("quality_summary", True),
    "ff_explain_plan": ("execution_plan", True),
    "ff_legacy_single": ("auto_chunk", False),
    "ooc_fk_forced_loader": ("residency", True),
}


def _load() -> dict[str, Any]:
    return json.loads(_BASELINE.read_text()) if _BASELINE.exists() else {}


def test_every_scenario_has_a_declared_route() -> None:
    assert set(EXPECTED_MODE) == set(sc.SUCCESS_SCENARIOS)


@pytest.mark.skipif(not os.environ.get("R3_WRITE_BASELINES"), reason="baseline writer")
def test_write_baseline(tmp_path: Path) -> None:
    out: dict[str, Any] = {}
    for name in sorted(sc.SUCCESS_SCENARIOS):
        scratch = tmp_path / name
        scratch.mkdir()
        out[name] = sc.success_snapshot(name, scratch)
    _BASELINE.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")


@pytest.mark.parametrize("name", sorted(sc.SUCCESS_SCENARIOS))
def test_route_output_is_unchanged(name: str, tmp_path: Path) -> None:
    baseline = _load()
    assert name in baseline, "baseline missing; it must come from the pre-extraction tree"
    got = sc.success_snapshot(name, tmp_path)
    assert got["execution"]["execution_mode"] == EXPECTED_MODE[name]
    if name in EXPECTED_LANE_MARKER:
        key, present = EXPECTED_LANE_MARKER[name]
        assert (key in got["quality_metrics"]) is present, f"{name}: lane marker {key!r}"
    assert got == baseline[name]


def test_volatile_fields_are_structure_only_and_enumerated(tmp_path: Path) -> None:
    """No volatile value is compared and none is dropped silently: each is replaced by a typed
    marker, and the timing members of `ExecutionResult` appear as structure."""
    got = sc.success_snapshot("ff_post_validation", tmp_path)
    phase = got["quality_metrics"]["quality_summary"]["timing_per_phase"]
    assert phase["post_validation_phase_ms"] == {"__volatile__": "float"}
    assert got["volatile_structure"]["timings"] == "tuple"
    (tmp_path / "second").mkdir()
    admitted = sc.success_snapshot("ff_unified_admitted", tmp_path / "second")
    activation = admitted["quality_metrics"]["unified_slice_activation"]
    assert activation["plan_hash"] == {"__volatile__": "str"}
    assert {"timings", "boundary_conversion_ms"} <= set(proj.VOLATILE_RESULT_FIELDS)
