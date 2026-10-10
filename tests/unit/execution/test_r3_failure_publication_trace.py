"""R3 acceptance test 5 (baseline half): failure and publication behavior of `run_pipeline`.

Plan: docs/plans/2026-10-10-r3-run-context-route-executors.md section 6, test 5 and the
"also captured before extraction" paragraph. `r3_failure_trace_baseline.json` was captured on
origin/main @ ac6a0e8e before any production edit. It pins, per scenario: the exception type,
module, code and message; whether anything was read or published before the failure; and the
exact ordered sink event trace (write / write_batches / commit / abort), including a failure
injected AFTER streamed output was written (the post-validation seam).

The explicit assertions below restate the load-bearing invariants in words so a reader does
not have to decode the JSON; the JSON comparison is the exhaustive check. Regenerate only with
reviewer approval: `R3_WRITE_BASELINES=1 pytest <this file>`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.unit.execution import _r3_scenarios as sc

pytestmark = pytest.mark.filterwarnings("ignore")

_BASELINE = Path(__file__).parent / "r3_failure_trace_baseline.json"


def _load() -> dict[str, Any]:
    return json.loads(_BASELINE.read_text()) if _BASELINE.exists() else {}


@pytest.mark.skipif(not os.environ.get("R3_WRITE_BASELINES"), reason="baseline writer")
def test_write_baseline(tmp_path: Path) -> None:
    out: dict[str, Any] = {}
    for name in sorted(sc.FAILURE_SCENARIOS):
        scratch = tmp_path / name
        scratch.mkdir()
        out[name] = sc.failure_snapshot(name, scratch)
    _BASELINE.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")


@pytest.mark.parametrize("name", sorted(sc.FAILURE_SCENARIOS))
def test_failure_and_publication_trace_is_unchanged(name: str, tmp_path: Path) -> None:
    baseline = _load()
    assert name in baseline, "baseline missing; it must come from the pre-extraction tree"
    assert sc.failure_snapshot(name, tmp_path) == baseline[name]


# ---------------------------------------------------------------------------
# The invariants, stated directly (independent of the recorded JSON).
# ---------------------------------------------------------------------------


def _run(name: str, tmp_path: Path) -> dict[str, Any]:
    out: dict[str, Any] = sc.failure_snapshot(name, tmp_path)
    return out


def test_reject_before_read_reads_and_publishes_nothing(tmp_path: Path) -> None:
    got = _run("f_reject_before_read", tmp_path)
    assert got["exception"]["code"] == "fk_full_frame_oom_risk_rejected"
    assert got["source_loader_calls"] == []
    assert got["sink_calls"] == [] and got["sink_dir_exists"] is False


def test_lazy_sources_are_not_materialized_by_a_rejection(tmp_path: Path) -> None:
    got = _run("f_lazy_not_materialized_on_reject", tmp_path)
    assert got["exception"]["code"] == "fk_full_frame_oom_risk_rejected"
    assert got["sink_calls"] == [] and got["sink_dir_exists"] is False


def test_forced_out_of_core_on_an_ineligible_job_is_a_config_error_before_any_write(
    tmp_path: Path,
) -> None:
    got = _run("f_forced_ooc_incompatible", tmp_path)
    assert got["exception"]["type"] == "ConfigError"
    assert got["sink_calls"] == []


def test_a_bad_substrate_fails_before_profiling(tmp_path: Path) -> None:
    got = _run("f_invalid_substrate_before_profile", tmp_path)
    assert got["exception"]["code"] == "invalid_substrate"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("f_seq_sink_success", ["write:parent", "write:child", "commit"]),
        ("f_seq_sink_write_failure", ["write:parent", "write:child", "abort"]),
        ("f_seq_orphan_fail", ["write:parent", "abort"]),
        ("f_ooc_sink_success", ["write_batches:parent", "write_batches:child", "commit"]),
        ("f_ooc_sink_write_failure", ["write_batches:parent", "write_batches:child", "abort"]),
        ("f_ooc_orphan_fail", ["write_batches:parent", "abort"]),
        ("f_ff_streamed_success", ["write_batches:t", "commit"]),
        ("f_ff_streamed_write_failure", ["write_batches:t", "abort"]),
        ("f_ff_streamed_post_validation_failure", ["write_batches:t", "abort"]),
        ("f_ff_post_validation_failure_resident", []),
        ("f_ff_resident_sink_untouched", []),
        ("f_row_error_fails_closed", []),
        ("f_row_error_fails_closed_streamed", ["abort"]),
    ],
)
def test_sink_event_order(name: str, expected: list[str], tmp_path: Path) -> None:
    assert _run(name, tmp_path)["sink_calls"] == expected


@pytest.mark.parametrize(
    "name",
    [
        "f_seq_sink_write_failure",
        "f_seq_orphan_fail",
        "f_ooc_sink_write_failure",
        "f_ooc_orphan_fail",
        "f_ff_streamed_write_failure",
        "f_ff_streamed_post_validation_failure",
        "f_row_error_fails_closed",
        "f_row_error_fails_closed_streamed",
    ],
)
def test_a_failed_run_publishes_nothing(name: str, tmp_path: Path) -> None:
    got = _run(name, tmp_path)
    assert got["exception"] is not None
    assert got["published_files"] == []


def test_unified_lane_distinguishes_miss_invariant_and_provider_failure(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    miss = _run("f_unified_fallback_miss", tmp_path / "a")
    assert miss["exception"] is None and miss["completed"] is True
    assert miss["unified_slice_activation_stamped"] is False
    (tmp_path / "b").mkdir()
    invariant = _run("f_unified_invariant_failure", tmp_path / "b")
    assert invariant["exception"]["type"] == "UnifiedSliceInvariantError"
    assert invariant["exception"]["code"] == "unified_slice_invariant_violation"
    (tmp_path / "c").mkdir()
    provider = _run("f_unified_provider_failure", tmp_path / "c")
    assert provider["exception"]["type"] == "ValueError" and provider["is_original"] is True
