"""GP1 generation-throughput probe: fast, CI-safe tests.

Covers the plan's acceptance-test list: the worker's real end-to-end
smoke (both arms), the threshold-override mechanism taking effect in the
CHILD process, the off arm's unconditional per-row behavior, the
production `pool_eligible` predicate staying untouched by the override,
the vacuity guard genuinely catching a non-activation, the compare
driver's pure statistics (paired ratio, bootstrap CI, crossover
confirmation, the RSS gate), every fail-closed shape via a stub
`ArmRunner` (no subprocess), and the smoke-vs-full-sweep label.

No real multi-tier sweep runs here -- that stays a deliberate offline
invocation per the README (a multi-minute cost across ten types and
eight tiers, inappropriate for every CI run).

Both scripts live under `scripts/`, not the package, so they are loaded
either by subprocess (the worker; matches how a real sweep invokes it)
or by file-path import (the driver; mirrors how the D9 harness's own
test suite loads its sibling script).
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.generation import _faker_pool

ENGINE_ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = ENGINE_ROOT / "scripts" / "bench-generation-pool"
WORKER_PATH = BENCH_DIR / "bench_worker_gen.py"
VENV_PY = Path(sys.executable)
_WORKER_ENV_BASE = {**os.environ, "PYTHONPATH": str(ENGINE_ROOT / "src")}

_spec = importlib.util.spec_from_file_location(
    "bench_compare_gen", BENCH_DIR / "bench_compare_gen.py"
)
assert _spec is not None and _spec.loader is not None
bc = importlib.util.module_from_spec(_spec)
sys.modules["bench_compare_gen"] = bc
_spec.loader.exec_module(bc)

_SMOKE_N_ROWS = 500  # well below production N_THRESHOLD (50_000)


def _run_worker(
    n_rows: int, faker_type: str, *, flag: str, threshold: int | None
) -> subprocess.CompletedProcess[str]:
    env = {**_WORKER_ENV_BASE, "GEN_POOL_BENCH_FLAG": flag}
    if threshold is not None:
        env["GEN_POOL_BENCH_THRESHOLD"] = str(threshold)
    return subprocess.run(  # noqa: S603 fixed local benchmark command, no untrusted input
        [str(VENV_PY), str(WORKER_PATH), str(n_rows), faker_type],
        cwd=str(ENGINE_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _bench_json_record(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("BENCH_JSON "))
    return json.loads(line[len("BENCH_JSON ") :])


# ---------------------------------------------------------------------------
# Worker smoke: real subprocess, both arms.
# ---------------------------------------------------------------------------


def test_worker_smoke_on_arm_emits_valid_bench_json() -> None:
    proc = _run_worker(_SMOKE_N_ROWS, "city", flag="on", threshold=1)
    rec = _bench_json_record(proc)
    assert rec["n_rows"] == _SMOKE_N_ROWS
    assert rec["out_rows"] == _SMOKE_N_ROWS
    assert rec["faker_type"] == "city"
    assert rec["arm"] == "pooled"
    assert rec["rows_per_s"] > 0.0
    assert rec["pooled_activated"] is True
    assert rec["bench_threshold_override"] == 1
    assert rec["workload_fingerprint"] == {
        "n_rows": _SMOKE_N_ROWS,
        "faker_type": "city",
        "seed": 20260917,
        "bench_threshold_override": 1,
    }


def test_worker_smoke_off_arm_emits_valid_bench_json() -> None:
    proc = _run_worker(_SMOKE_N_ROWS, "city", flag="off", threshold=1)
    rec = _bench_json_record(proc)
    assert rec["n_rows"] == _SMOKE_N_ROWS
    assert rec["out_rows"] == _SMOKE_N_ROWS
    assert rec["arm"] == "per_row"
    assert rec["rows_per_s"] > 0.0
    assert rec["pooled_activated"] is False


# ---------------------------------------------------------------------------
# Override takes effect in the CHILD process (not a parent-process
# monkeypatch): the whole point of the threshold-override mechanism.
# ---------------------------------------------------------------------------


def test_threshold_override_activates_pooling_below_production_threshold() -> None:
    assert _SMOKE_N_ROWS < _faker_pool.N_THRESHOLD
    proc = _run_worker(_SMOKE_N_ROWS, "city", flag="on", threshold=1)
    rec = _bench_json_record(proc)
    assert rec["pooled_activated"] is True


def test_off_arm_never_pools_even_with_override_set() -> None:
    proc = _run_worker(_SMOKE_N_ROWS, "city", flag="off", threshold=1)
    rec = _bench_json_record(proc)
    assert rec["pooled_activated"] is False


# ---------------------------------------------------------------------------
# The real production predicate is untouched by the harness's own override
# (a same-process assignment in a subprocess that already exited).
# ---------------------------------------------------------------------------


def test_production_pool_eligible_predicate_unchanged() -> None:
    assert (
        _faker_pool.pool_eligible("city", None, _faker_pool.N_THRESHOLD - 1, opted_out=False)
        is False
    )
    assert _faker_pool.pool_eligible("city", None, _faker_pool.N_THRESHOLD, opted_out=False) is True


# ---------------------------------------------------------------------------
# Vacuity guard genuinely catches a non-activation.
# ---------------------------------------------------------------------------

_VACUITY_RUNNER_TEMPLATE = """
import sys
sys.argv = ["bench_worker_gen.py", "{n_rows}", "city"]
sys.path.insert(0, {bench_dir!r})

from decoy_engine.generation import _faker_pool


def _stub_build_and_sample(**kwargs):
    # Simulates a resolver decline that still hands back SOMETHING
    # without ever reaching PoolSampler.sample -- the exact mismatch the
    # worker's vacuity guard (both wrappers must fire on the on arm)
    # exists to catch.
    return ["stub"] * kwargs["n"]


_faker_pool.build_and_sample = _stub_build_and_sample

import bench_worker_gen

bench_worker_gen.main()
"""


def test_vacuity_guard_catches_build_and_sample_that_never_reaches_pool_sampler(
    tmp_path: Path,
) -> None:
    runner_script = tmp_path / "vacuity_runner.py"
    runner_script.write_text(
        _VACUITY_RUNNER_TEMPLATE.format(n_rows=_SMOKE_N_ROWS, bench_dir=str(BENCH_DIR))
    )
    proc = subprocess.run(  # noqa: S603 fixed local test-only helper script, no untrusted input
        [str(VENV_PY), str(runner_script)],
        cwd=str(ENGINE_ROOT),
        env={**_WORKER_ENV_BASE, "GEN_POOL_BENCH_FLAG": "on", "GEN_POOL_BENCH_THRESHOLD": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode != 0
    assert "vacuity guard" in proc.stderr
    assert "did not activate" in proc.stderr


# ---------------------------------------------------------------------------
# Driver: pure statistics (no subprocess).
# ---------------------------------------------------------------------------


def test_inclusive_p95_differs_from_max() -> None:
    values = [1.0, 1.0, 1.0, 1.0, 10.0]
    p95 = bc.inclusive_p95(values)
    assert p95 != max(values)
    assert p95 == pytest.approx(1.0 + (10.0 - 1.0) * 0.8)


def test_bootstrap_ci_is_seeded_and_reproducible() -> None:
    ratios = [1.0, 1.05, 0.98, 1.1, 1.02]
    a = bc.bootstrap_ci(ratios, seed=42, n_bootstrap=500)
    b = bc.bootstrap_ci(ratios, seed=42, n_bootstrap=500)
    assert a == b


def test_bootstrap_ci_handles_singleton_bootstrap_count() -> None:
    ci_low, ci_high = bc.bootstrap_ci([1.0, 1.1], seed=1, n_bootstrap=1)
    assert math.isfinite(ci_low)
    assert math.isfinite(ci_high)


# ---------------------------------------------------------------------------
# Crossover monotonic-confirmation logic on synthetic injected data.
# ---------------------------------------------------------------------------


def test_crossover_picks_smallest_confirmed_tier() -> None:
    tiers = [1_000, 5_000, 10_000, 25_000]
    ci_highs = [1.2, 0.95, 0.90, 0.85]  # wins from 5_000 onward, all confirmed
    result = bc.find_crossover_tier(tiers, ci_highs)
    assert result.tier == 5_000
    assert result.boundary_fallback is False


def test_crossover_rejects_unconfirmed_single_tier_win() -> None:
    """A lone win at 5_000 that regresses again at 10_000 is a noisy blip,
    not a crossover; the next confirmed win (25_000, held through 50_000)
    is what gets picked."""
    tiers = [1_000, 5_000, 10_000, 25_000, 50_000]
    ci_highs = [1.2, 0.90, 1.10, 0.85, 0.80]
    result = bc.find_crossover_tier(tiers, ci_highs)
    assert result.tier == 25_000
    assert result.boundary_fallback is False


def test_crossover_boundary_fallback_on_last_tier_only() -> None:
    tiers = [1_000, 5_000, 10_000]
    ci_highs = [1.2, 1.1, 0.9]  # only the LAST tier wins, nothing to confirm it
    result = bc.find_crossover_tier(tiers, ci_highs)
    assert result.tier is None
    assert result.boundary_fallback is True


def test_crossover_none_when_pooling_never_wins() -> None:
    tiers = [1_000, 5_000, 10_000]
    ci_highs = [1.2, 1.1, 1.05]
    result = bc.find_crossover_tier(tiers, ci_highs)
    assert result.tier is None
    assert result.boundary_fallback is False


# ---------------------------------------------------------------------------
# RSS gate: an absolute peak-RSS ceiling (2026-09-30: the ratio+delta gate is
# retired -- docs/plans/2026-09-30-retire-d9-rss-ratio.md). `rss_ratio` and
# `rss_delta_kb` stay on the cell as information; the gate itself now checks
# `on_rss_max_kb` against a declared `max_peak_rss_mb` ceiling. No ceiling
# declared means the recommendation is always withheld, with a reason.
# ---------------------------------------------------------------------------


def _cell(
    faker_type: str,
    n_rows: int,
    *,
    on_rss_max_kb: int,
    rss_ratio: float = 1.0,
    rss_delta_kb: int = 0,
) -> dict[str, Any]:
    return {
        "faker_type": faker_type,
        "n_rows": n_rows,
        "on_rss_max_kb": on_rss_max_kb,
        "rss_ratio": rss_ratio,
        "rss_delta_kb": rss_delta_kb,
    }


def test_rss_gate_passes_within_ceiling() -> None:
    cells = [_cell("city", 25_000, on_rss_max_kb=100_000)]
    result = bc.apply_rss_gate(cells, recommended_tier=25_000, max_peak_rss_mb=200.0)
    assert result.ok is True


def test_rss_gate_fails_when_ceiling_exceeded() -> None:
    cells = [_cell("city", 25_000, on_rss_max_kb=300_000)]
    result = bc.apply_rss_gate(cells, recommended_tier=25_000, max_peak_rss_mb=200.0)
    assert result.ok is False
    assert "on_rss_max_kb" in result.reasons[0]


def test_rss_gate_boundary_equal_to_ceiling_passes() -> None:
    ceiling_mib = 100.0
    cells = [_cell("city", 25_000, on_rss_max_kb=int(ceiling_mib * 1024))]
    result = bc.apply_rss_gate(cells, recommended_tier=25_000, max_peak_rss_mb=ceiling_mib)
    assert result.ok is True


def test_rss_gate_boundary_one_kib_over_ceiling_fails() -> None:
    ceiling_mib = 100.0
    cells = [_cell("city", 25_000, on_rss_max_kb=int(ceiling_mib * 1024) + 1)]
    result = bc.apply_rss_gate(cells, recommended_tier=25_000, max_peak_rss_mb=ceiling_mib)
    assert result.ok is False


def test_rss_gate_withheld_when_ceiling_not_declared() -> None:
    """No ceiling means a recommendation can never be emitted, whatever the
    cells look like -- always withheld, always with a stated reason."""
    cells = [_cell("city", 25_000, on_rss_max_kb=1_000)]
    result = bc.apply_rss_gate(cells, recommended_tier=25_000, max_peak_rss_mb=None)
    assert result.ok is False
    assert result.reasons


def test_rss_gate_ignores_cells_below_the_recommended_tier() -> None:
    """A blown ceiling below the recommended tier must not withhold it --
    only cells AT OR ABOVE the recommendation are in scope."""
    cells = [_cell("city", 1_000, on_rss_max_kb=999_999_999)]
    result = bc.apply_rss_gate(cells, recommended_tier=25_000, max_peak_rss_mb=200.0)
    assert result.ok is True


def test_build_recommendation_withheld_when_rss_gate_fails() -> None:
    crossovers = {"city": bc.CrossoverResult(tier=25_000, boundary_fallback=False)}
    cells = [_cell("city", 25_000, on_rss_max_kb=300_000)]
    rec = bc.build_recommendation(crossovers, cells, max_peak_rss_mb=200.0)
    assert rec["recommended_threshold_raw"] == 25_000
    assert rec["recommended_threshold_rounded"] is None
    assert "RSS gate failed" in rec["withheld_reason"]


def test_build_recommendation_withheld_when_ceiling_not_declared() -> None:
    crossovers = {"city": bc.CrossoverResult(tier=25_000, boundary_fallback=False)}
    cells = [_cell("city", 25_000, on_rss_max_kb=1_000)]
    rec = bc.build_recommendation(crossovers, cells, max_peak_rss_mb=None)
    assert rec["recommended_threshold_raw"] == 25_000
    assert rec["recommended_threshold_rounded"] is None
    assert "RSS gate failed" in rec["withheld_reason"]


def test_build_recommendation_withheld_when_a_type_never_confirms() -> None:
    crossovers = {
        "city": bc.CrossoverResult(tier=25_000, boundary_fallback=False),
        "job": bc.CrossoverResult(tier=None, boundary_fallback=True),
    }
    cells = [_cell("city", 25_000, on_rss_max_kb=1_000)]
    rec = bc.build_recommendation(crossovers, cells, max_peak_rss_mb=200.0)
    assert rec["recommended_threshold_raw"] is None
    assert "job" in rec["withheld_reason"]


def test_build_recommendation_emits_raw_and_rounded_when_clean() -> None:
    crossovers = {
        "city": bc.CrossoverResult(tier=10_000, boundary_fallback=False),
        "job": bc.CrossoverResult(tier=25_000, boundary_fallback=False),
    }
    cells = [
        _cell("city", 25_000, on_rss_max_kb=1_000),
        _cell("job", 25_000, on_rss_max_kb=1_000),
    ]
    rec = bc.build_recommendation(crossovers, cells, max_peak_rss_mb=200.0)
    assert rec["recommended_threshold_raw"] == 25_000  # MAX across types
    assert rec["recommended_threshold_rounded"] is not None
    assert rec["recommended_threshold_rounded"] >= 25_000
    assert rec["withheld_reason"] is None


# ---------------------------------------------------------------------------
# Fail-closed shapes via a stub ArmRunner (no subprocess).
# ---------------------------------------------------------------------------


def _record_json(n_rows: int, faker_type: str, flag_on: bool, **overrides: Any) -> str:
    base: dict[str, Any] = {
        "n_rows": n_rows,
        "faker_type": faker_type,
        "arm": "pooled" if flag_on else "per_row",
        "wall_s": 0.5,
        "rows_per_s": n_rows / 0.5,
        "out_rows": n_rows,
        "pooled_activated": flag_on,
        "bench_threshold_override": 1,
        "workload_fingerprint": {
            "n_rows": n_rows,
            "faker_type": faker_type,
            "seed": 20260917,
            "bench_threshold_override": 1,
        },
    }
    base.update(overrides)
    return "BENCH_JSON " + json.dumps(base)


def _raw_ok(stdout: str, *, ru_maxrss_kb: int | None = 1000) -> Any:
    return bc.RawArmResult(
        stdout=stdout,
        stderr="",
        returncode=0,
        drained_ok=True,
        timed_out=False,
        ru_maxrss_kb=ru_maxrss_kb,
    )


def _make_runner(off_results: list[Any], on_results: list[Any]) -> Any:
    off_it = iter(off_results)
    on_it = iter(on_results)

    def _runner(arm: str, _n_rows: int, _faker_type: str, _timeout_s: float) -> Any:
        return next(on_it) if arm == "on" else next(off_it)

    return _runner


def _valid_pair(n_rows: int, faker_type: str = "city") -> tuple[Any, Any]:
    return (
        _raw_ok(_record_json(n_rows, faker_type, False)),
        _raw_ok(_record_json(n_rows, faker_type, True)),
    )


def _assert_fails_closed(off_results: list[Any], on_results: list[Any], n_rows: int = 500) -> None:
    runner = _make_runner(off_results, on_results)
    with pytest.raises(bc.FailClosedError):
        bc._run_cell(
            "city", n_rows, warmup=0, reps=1, bootstrap=10, seed=1, timeout_s=5.0, arm_runner=runner
        )


def test_fail_closed_missing_bench_json() -> None:
    _assert_fails_closed([_raw_ok("nothing to see here\n")], [])


def test_fail_closed_out_rows_mismatch() -> None:
    _assert_fails_closed([_raw_ok(_record_json(500, "city", False, out_rows=499))], [])


def test_fail_closed_rows_per_s_inconsistent() -> None:
    # rows_per_s is derived (n_rows/wall_s); a plausible-but-wrong value must fail
    # closed so a reported throughput can never disagree with its own timing.
    # Correct here is 500/0.5 = 1000.0; 12345.0 is finite/positive but wrong.
    _assert_fails_closed([_raw_ok(_record_json(500, "city", False, rows_per_s=12345.0))], [])


def test_fail_closed_missing_ru_maxrss() -> None:
    off = _raw_ok(_record_json(500, "city", False), ru_maxrss_kb=None)
    _assert_fails_closed([off], [])


def test_fail_closed_zero_ru_maxrss() -> None:
    off = _raw_ok(_record_json(500, "city", False), ru_maxrss_kb=0)
    _assert_fails_closed([off], [])


def test_fail_closed_activation_mismatch_on_arm() -> None:
    off = _raw_ok(_record_json(500, "city", False))
    on = _raw_ok(_record_json(500, "city", True, pooled_activated=False))
    _assert_fails_closed([off], [on])


def test_fail_closed_activation_mismatch_off_arm() -> None:
    off = _raw_ok(_record_json(500, "city", False, pooled_activated=True))
    _assert_fails_closed([off], [])


def test_fail_closed_nonzero_exit() -> None:
    bad = bc.RawArmResult(
        stdout=_record_json(500, "city", False),
        stderr="boom",
        returncode=1,
        drained_ok=True,
        timed_out=False,
        ru_maxrss_kb=1000,
    )
    _assert_fails_closed([bad], [])


def test_fail_closed_fingerprint_mismatch_between_arms() -> None:
    off = _raw_ok(_record_json(500, "city", False))
    on = _raw_ok(
        _record_json(
            500,
            "city",
            True,
            workload_fingerprint={
                "n_rows": 500,
                "faker_type": "city",
                "seed": 1,
                "bench_threshold_override": 1,
            },
        )
    )
    _assert_fails_closed([off], [on])


def test_reps_two_computes_p95_and_ci_without_error() -> None:
    off1, on1 = _valid_pair(500)
    off2, on2 = _valid_pair(500)
    runner = _make_runner([off1, off2], [on1, on2])
    cell = bc._run_cell(
        "city", 500, warmup=0, reps=2, bootstrap=50, seed=1, timeout_s=5.0, arm_runner=runner
    )
    assert math.isfinite(cell["ratio_p95"])
    assert math.isfinite(cell["ci_high"])


# ---------------------------------------------------------------------------
# Smoke label: a smoke invocation prints SMOKE COMPLETE and never emits a
# crossover verdict, even when the underlying (fake) results would cross.
# ---------------------------------------------------------------------------


def test_smoke_invocation_never_emits_a_recommendation() -> None:
    off1, on1 = _valid_pair(1_000, "city")
    off2, on2 = _valid_pair(1_000, "city")
    runner = _make_runner([off1, off2], [on1, on2])
    config = bc.RunConfig(
        tiers=[1_000],
        types=["city"],
        reps=2,
        warmup=0,
        bootstrap=10,
        seed=1,
        timeout_s=5.0,
        max_peak_rss_mb=None,
    )
    result = bc.run_bench_compare(config, arm_runner=runner)
    assert result["run_ok"] is True
    assert result["full_sweep"] is False
    assert result["recommendation"] is None
    assert bc.banner_for(result) == "SMOKE COMPLETE"
    assert "d9_certified" not in result  # GP1 gains no cert concept


def test_gp1_result_schema_v2_keys_and_removed_keys() -> None:
    off1, on1 = _valid_pair(500)
    off2, on2 = _valid_pair(500)
    runner = _make_runner([off1, off2], [on1, on2])
    config = bc.RunConfig(
        tiers=[500],
        types=["city"],
        reps=2,
        warmup=0,
        bootstrap=10,
        seed=1,
        timeout_s=5.0,
        max_peak_rss_mb=200.0,
    )
    result = bc.run_bench_compare(config, arm_runner=runner)

    assert result["harness_version"] == "2.0.0"
    assert result["memory_gate_declared"] is True
    assert result["max_peak_rss_mb"] == 200.0
    assert "max_rss_ratio" not in result
    assert "max_rss_delta_kb" not in result
    assert "d9_certified" not in result

    cell = result["cells"]["city@500"]
    assert "rss_ratio" in cell  # information, kept
    assert "rss_delta_kb" in cell  # information, kept
    assert "on_rss_max_kb" in cell


def test_is_full_sweep_shape_requires_the_exact_default_shape() -> None:
    assert bc.is_full_sweep_shape(
        bc._DEFAULT_TIERS, bc._ALL_TYPES, reps=20, warmup=3, bootstrap=10_000
    )
    assert not bc.is_full_sweep_shape(
        bc._DEFAULT_TIERS, bc._ALL_TYPES, reps=19, warmup=3, bootstrap=10_000
    )
    assert not bc.is_full_sweep_shape([1_000], bc._ALL_TYPES, reps=20, warmup=3, bootstrap=10_000)
    assert not bc.is_full_sweep_shape(
        bc._DEFAULT_TIERS, ["city"], reps=20, warmup=3, bootstrap=10_000
    )


# ---------------------------------------------------------------------------
# GP1 memory-gate transition-table row: "not declared or exceeded" withholds
# the recommendation but leaves run_ok/full_sweep untouched, and GP1 gains no
# cert concept either way.
# ---------------------------------------------------------------------------


def _full_sweep_config(*, max_peak_rss_mb: float | None) -> bc.RunConfig:
    return bc.RunConfig(
        tiers=[500, 1_000],
        types=["city"],
        reps=20,
        warmup=3,
        bootstrap=10_000,
        seed=1,
        timeout_s=5.0,
        max_peak_rss_mb=max_peak_rss_mb,
    )


def _pair_with_wall(
    n_rows: int, faker_type: str, *, off_wall: float, on_wall: float
) -> tuple[Any, Any]:
    """Unlike `_valid_pair` (fixed 0.5s for both arms), lets the on arm win
    consistently -- `rows_per_s` is overridden alongside `wall_s` since
    `validate_worker_record` fails closed on a derived-field mismatch."""
    off = _raw_ok(
        _record_json(n_rows, faker_type, False, wall_s=off_wall, rows_per_s=n_rows / off_wall)
    )
    on = _raw_ok(
        _record_json(n_rows, faker_type, True, wall_s=on_wall, rows_per_s=n_rows / on_wall)
    )
    return off, on


def _confirmed_crossover_runner(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The on arm consistently wins at both tiers, so `find_crossover_tier`
    confirms a crossover at the smaller tier and `build_recommendation`
    reaches the RSS gate (rather than withholding for an unconfirmed
    crossover first) -- monkeypatched down to a 2-tier, 1-type full-sweep
    shape so the test stays fast."""
    monkeypatch.setattr(bc, "_DEFAULT_TIERS", (500, 1_000))
    monkeypatch.setattr(bc, "_ALL_TYPES", ("city",))
    off_queue: list[Any] = []
    on_queue: list[Any] = []
    for n_rows in (500, 1_000):
        for _ in range(3 + 20):
            off, on = _pair_with_wall(n_rows, "city", off_wall=1.0, on_wall=0.5)
            off_queue.append(off)
            on_queue.append(on)
    return _make_runner(off_queue, on_queue)


def test_full_sweep_ceiling_not_declared_withholds_but_keeps_run_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _confirmed_crossover_runner(monkeypatch)
    result = bc.run_bench_compare(_full_sweep_config(max_peak_rss_mb=None), arm_runner=runner)
    assert result["run_ok"] is True
    assert result["full_sweep"] is True
    assert result["recommendation"]["withheld_reason"] is not None
    assert "d9_certified" not in result


def test_full_sweep_ceiling_exceeded_withholds_but_keeps_run_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _confirmed_crossover_runner(monkeypatch)
    # `_valid_pair`'s default ru_maxrss_kb is 1000; a near-zero ceiling
    # guarantees the gate trips without needing to fabricate huge RSS values.
    result = bc.run_bench_compare(_full_sweep_config(max_peak_rss_mb=0.001), arm_runner=runner)
    assert result["run_ok"] is True
    assert result["full_sweep"] is True
    assert result["recommendation"]["withheld_reason"] is not None
    assert "RSS gate failed" in result["recommendation"]["withheld_reason"]


# ---------------------------------------------------------------------------
# CLI validation
# ---------------------------------------------------------------------------


def _expect_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        bc.parse_and_validate_args(argv)
    assert exc_info.value.code == 2


def test_cli_rejects_non_allowlisted_type() -> None:
    _expect_exit_2(["--types", "email"])


def test_cli_rejects_duplicate_type() -> None:
    _expect_exit_2(["--types", "city,city"])


def test_cli_rejects_empty_tiers() -> None:
    _expect_exit_2(["--tiers", ""])


def test_cli_rejects_duplicate_tier() -> None:
    _expect_exit_2(["--tiers", "1000,1000"])


def test_cli_rejects_reps_below_floor() -> None:
    _expect_exit_2(["--reps", "1"])


def test_cli_accepts_minimal_valid_shape() -> None:
    args = bc.parse_and_validate_args(["--tiers", "1000", "--types", "city", "--reps", "2"])
    assert args.tiers == [1_000]
    assert args.types == ["city"]


# ---------------------------------------------------------------------------
# --max-peak-rss-mb CLI validation (2026-09-30: replaces the retired
# --max-rss-ratio / --max-rss-delta-kb flags)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_cli_rejects_bad_max_peak_rss_mb(value: str) -> None:
    _expect_exit_2(["--tiers", "1000", "--types", "city", "--max-peak-rss-mb", value])


def test_cli_accepts_valid_max_peak_rss_mb() -> None:
    args = bc.parse_and_validate_args(
        ["--tiers", "1000", "--types", "city", "--max-peak-rss-mb", "500"]
    )
    assert args.max_peak_rss_mb == 500.0


def test_cli_max_peak_rss_mb_defaults_to_none() -> None:
    args = bc.parse_and_validate_args(["--tiers", "1000", "--types", "city"])
    assert args.max_peak_rss_mb is None


def test_cli_no_longer_has_max_rss_ratio_or_delta_flags() -> None:
    with pytest.raises(SystemExit) as exc_info:
        bc.parse_and_validate_args(["--max-rss-ratio", "1.25"])
    assert exc_info.value.code == 2
    with pytest.raises(SystemExit) as exc_info:
        bc.parse_and_validate_args(["--max-rss-delta-kb", "51200"])
    assert exc_info.value.code == 2


def test_cli_rejects_non_linux_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """RSS evidence (`os.wait4`'s `ru_maxrss`) is Linux-only; a non-Linux run
    must be refused rather than silently mis-scaled."""
    monkeypatch.setattr(sys, "platform", "darwin")
    _expect_exit_2(["--tiers", "1000", "--types", "city"])


def test_harness_version_is_2_0_0() -> None:
    assert bc.HARNESS_VERSION == "2.0.0"
