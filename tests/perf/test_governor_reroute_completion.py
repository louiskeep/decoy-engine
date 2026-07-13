"""TB-2: the reroute-to-COMPLETION proof the 50M benchmark's governor phase
(B6) never produced.

`docs/plans/2026-07-12-track-b-completion-program.md` TB-2. B6 proved
*containment* (a route over budget gets a clean `SIGKILL` + honest
diagnostic, never a wedge) but not *completion* (nothing ever rerouted to a
route that actually finished). Root-cause (measured on this box, not just
reasoned):

  1. **Foundational (TB-1, `#56`, already fixed on `main`).** Pre-TB-1, the
     production out-of-core path (`run_pipeline` -> governor ->
     `_isolated_worker`) materialized every input table via `pq.read_table`
     and returned a fully resident output (no sink) -- so its real peak RSS
     was NOT bounded by the DuckDB `memory_limit` at all, it was dominated by
     full input+output residency (`~2.1x` direct at 50M). No budget the
     governor could pick would let it complete under a tight cap: the
     "bounded" route was not actually bounded.
  2. **B6's budget window was never calibrated against the post-fix peaks.**
     Even with #56 fixed, `run_job_with_governor` only reroutes to
     completion if SOME budget value exists that sits above out-of-core's
     real peak and below full_frame's real peak for the job at hand. B6's
     fixed-budget benchmark run predates TB-1 and was never re-measured
     against it, so it used a budget below EVERY route's real need -- pure
     miscalibration, not a broken reroute mechanism.

**What this test found (empirical, not assumed).** The reroute LADDER
mechanism in `_governor.py` was already correct pre-TB-2 (`test_governor.py`
proves it exhaustively with mocks, and its own
`TestRealSubprocessIntegration` proves a real kill+reroute end to end --
just onto an ineligible-for-that-fixture ladder, so it lands on "exhausted",
never "completed"). With TB-1 landed, calibrating a real budget window for a
genuinely FK-eligible job (parent -> child, pure-mask, hash/redact/truncate
strategies -- the out-of-core-supported shape `_compat.py` requires) makes
the SAME mechanism reroute all the way to a completed run, no `_governor.py`
code change required. Measured on this box (2026-07-13), `_ROWS=200_000`,
2 tables, ~100-byte rows: full_frame peaks ~450-460 MB, out-of-core (TB-1
lazy sources + sink streaming) peaks ~330-380 MB regardless of the exact
`out_of_core_budget_bytes` forwarded (DuckDB's own `memory_limit` bounds only
its buffer manager, not the fixed interpreter/Arrow-batch overhead riding on
top of it -- the true floor is overhead-dominated at this tier, the same
"fixed per-edge overhead dominates at small scale" shape
`test_out_of_core_memory_sentinel.py` documents for the direct route). A
`_BUDGET_MB=380` window (hard-kill line `0.93 * 380 ≈ 353 MB`) sits
comfortably above that floor and comfortably below full_frame's peak --
verified stable across repeated real-subprocess runs, not a one-off.

**Why this is the vacuity guard, not a happy-path echo.** This test can only
pass if the run genuinely (a) TRIPS full_frame (`trips` non-empty, a real
`SIGKILL`), (b) REROUTES off it (`final_route != "full_frame"`), (c)
COMPLETES (`outcome == "completed"`, not `"exhausted"`), and (d) preserves
referential integrity end to end on the completed output. A governor that
only contains (the pre-TB-2 shape: kill -> exhausted ladder, exactly what
`test_governor.py`'s `TestRealSubprocessIntegration` demonstrates today for
an out-of-core-ineligible fixture) fails (c). A miscalibrated budget where
out-of-core ALSO exceeds the hard-kill line fails (c) the same way. A
governor that never trips full_frame in the first place (budget too loose)
fails (a)/(b). No mocking of `run_pipeline_isolated` or the VmRSS reader
anywhere in this file -- a real child's real RSS growth trips a real
`SIGKILL`, and the real ladder reroutes to a real completed out-of-core run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._governor import GovernorResult, run_job_with_governor

pytestmark = pytest.mark.perf

_ENGINE_VERSION = "tb2-reroute-completion"
_MB = 1024 * 1024

# Calibrated 2026-07-13 on this box (see module docstring for the measured
# peaks this window is chosen against). Large enough that full_frame's
# resident working set (input + masked copy + output) clears the hard-kill
# line with real margin; small enough the real-subprocess run stays a
# few-second test, not a benchmark-tier one.
_ROWS = 200_000
_BUDGET_MB = 380
_POLL_INTERVAL_S = 0.05


def _fk_mask_config(tmp_path: Path, n_rows: int) -> dict[str, Any]:
    """Pure-mask parent -> child FK job, out-of-core-eligible by construction
    (hash/redact/truncate are all out-of-core-supported strategies --
    `out_of_core/_compat.py`). `parent.id` and `child.pid` share the hash
    namespace `pns`, so masking preserves the FK edge deterministically --
    the property `_assert_fk_internal_consistency` checks below.

    `child.pid[i]` is built as `f"p{i % n_rows}"` == `parent.id[i]` for every
    row (an exact bijection, not merely "some subset") so the post-mask
    integrity check can assert an exact per-row match rather than a weaker
    subset check.
    """
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(n_rows)], type=pa.string()),
            "note": pa.array(
                [f"secret-note-{i}-" + ("x" * 40) for i in range(n_rows)], type=pa.string()
            ),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(n_rows)], type=pa.string()),
            "pid": pa.array([f"p{i % n_rows}" for i in range(n_rows)], type=pa.string()),
            "code": pa.array(
                [f"CODE{i:06d}-" + ("y" * 40) for i in range(n_rows)], type=pa.string()
            ),
        }
    )
    tables = {"parent": parent, "child": child}
    for name, tbl in tables.items():
        pq.write_table(tbl, tmp_path / f"{name}.parquet")
    return {
        "version": 1,
        "global_settings": {"job_name": "tb2-reroute-completion", "seed": 11},
        "sources": {
            name: {"type": "file", "path": str(tmp_path / f"{name}.parquet"), "format": "parquet"}
            for name in tables
        },
        "targets": {
            name: {
                "type": "file",
                "path": str(tmp_path / f"{name}.out.parquet"),
                "format": "parquet",
            }
            for name in tables
        },
        "tables": [
            {
                "name": "parent",
                "columns": [
                    {"name": "id", "strategy": "hash", "namespace": "pns"},
                    {"name": "note", "strategy": "redact"},
                ],
            },
            {
                "name": "child",
                "columns": [
                    {"name": "cid", "strategy": "hash", "namespace": "cns"},
                    {"name": "pid", "strategy": "hash", "namespace": "pns"},
                    {"name": "code", "strategy": "truncate", "provider_config": {"length": 4}},
                ],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["pid"]}],
                "orphan_policy": "preserve",
                "namespace": "pns",
            },
        ],
    }


def _sources(config: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}


def _assert_fk_internal_consistency(outputs: dict[str, pa.Table]) -> None:
    """`fk_internal_consistency=ok`: every masked `child.pid` equals the
    masked `parent.id` at the SAME row index the pre-mask fixture built it
    from (an exact per-row match, stronger than a subset check) -- the
    hash strategy's shared `pns` namespace must preserve the FK edge, not
    merely produce values that happen to overlap."""
    parent_ids = outputs["parent"].column("id").to_pylist()
    child_pids = outputs["child"].column("pid").to_pylist()
    assert len(parent_ids) == _ROWS
    assert len(child_pids) == _ROWS
    assert child_pids == parent_ids, (
        "fk_internal_consistency violated: masked child.pid diverged from "
        "masked parent.id at at least one row after governor reroute"
    )


def test_auto_run_that_trips_full_frame_reroutes_to_a_completed_out_of_core_run(
    tmp_path: Path,
) -> None:
    """The TB-2 acceptance test: `tripped=true, route!=full_frame,
    completed=true, fk_internal_consistency=ok`.

    `use_runtime_governor=True` is passed ONLY to this call -- no default
    flag is flipped anywhere by this test (Track B machinery stays
    flag-gated default-OFF through TB-4 per the program doc).
    """
    config = _fk_mask_config(tmp_path, _ROWS)
    sources = _sources(config)

    result = run_job_with_governor(
        config,
        sources,
        budget_bytes=_BUDGET_MB * _MB,
        use_runtime_governor=True,
        poll_interval_s=_POLL_INTERVAL_S,
        engine_version=_ENGINE_VERSION,
    )

    assert isinstance(result, GovernorResult)

    # tripped=true: full_frame was really killed by the governor, not merely
    # routed around up front -- a real SIGKILL happened.
    assert len(result.trips) >= 1, "expected the governor to trip at least once"
    first_trip = result.trips[0]
    assert first_trip.route == "full_frame"
    assert first_trip.trip_kind == "governor_kill", (
        f"expected a real supervisor SIGKILL on full_frame, got {first_trip.trip_kind} "
        f"(error={first_trip.error!r}) -- the budget window is miscalibrated if full_frame "
        "did not genuinely trip"
    )
    assert first_trip.observed_peak_mb is not None
    assert first_trip.observed_peak_mb * _MB >= _BUDGET_MB * _MB * 0.93 * 0.99  # hard-threshold-ish

    # route != full_frame: it actually moved off the tripped route.
    assert result.final_route is not None
    assert result.final_route != "full_frame"
    assert first_trip.reroute_to == result.final_route

    # completed=true: THIS is the property B6 never proved. A governor that
    # only contains (kills -> ladder exhausted, e.g. because out-of-core is
    # not really memory-bounded, or the budget window is too tight for
    # EVERY route) fails here with outcome="exhausted" instead.
    assert result.outcome == "completed", (
        f"governor did not reroute to completion (outcome={result.outcome}, "
        f"diagnostic={result.diagnostic!r}) -- this is the exact vacuity B6 failed on: "
        "containment without completion"
    )
    assert result.result is not None
    assert result.result.outcome == "completed"

    # fk_internal_consistency=ok: the completed run's output is not just
    # present, it is REFERENTIALLY CORRECT after streaming through the
    # rerouted-to route.
    assert result.result.outputs is not None
    _assert_fk_internal_consistency(result.result.outputs)
