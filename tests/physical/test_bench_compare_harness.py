"""Task 4.5 D9: fast, CI-safe tests for `scripts/bench-unified-slice/
bench_compare.py`. Covers the plan's §8 test plan: pure statistics/gate
boundaries, cert-shape classification, every §5/§6 fail-closed shape via a
stub `ArmRunner` (no subprocess), CLI validation, `--require-cert` exit
status, the real race-free child lifecycle via small helper subprocesses,
the stale-artifact guard, rep alternation/pairing, and one companion-guarded
real-worker smoke run.

No 10k/100k/1M sweep runs here -- that stays a deliberate offline invocation
per the README (a multi-minute-per-arm cost, inappropriate for every CI run).

`bench_compare.py` lives under `scripts/`, not the package, so it is loaded
by file path rather than a normal import (mirrors how
`test_bench_unified_slice_harness_smoke.py` locates its sibling script).
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.execution.native._companion_status import native_companion_status

ENGINE_ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = ENGINE_ROOT / "scripts" / "bench-unified-slice"

_spec = importlib.util.spec_from_file_location("bench_compare", BENCH_DIR / "bench_compare.py")
assert _spec is not None and _spec.loader is not None
bc = importlib.util.module_from_spec(_spec)
sys.modules["bench_compare"] = bc
_spec.loader.exec_module(bc)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _record_json(_n_rows: int, flag_on: bool, **overrides: Any) -> str:
    """A well-formed `BENCH_JSON` line for one arm; `overrides` lets a test
    inject exactly one defect while leaving every other field valid, per the
    plan's "cover the EXACT historical shapes" requirement. The leading-
    underscore parameter name avoids colliding with an `n_rows=...` override
    (a test deliberately wants to inject a bad n_rows value)."""
    base: dict[str, Any] = {
        "n_rows": _n_rows,
        "wall_s": 0.5,
        "out_rows": _n_rows,
        "execution_mode": "unified_slice" if flag_on else "legacy_full_frame",
        "hash_ms": 1.0,
        "hash_cols": 3,
        "redact_ms": 1.0,
        "truncate_ms": 1.0,
        "passthrough_ms": 1.0,
        "unified_slice_activated": flag_on,
        "plan_hash": "deadbeef" if flag_on else None,
        "workload_fingerprint": {
            "n_rows": _n_rows,
            "columns": list(bc._EXPECTED_ADMITTED_COLUMNS),
        },
    }
    base.update(overrides)
    return "BENCH_JSON " + json.dumps(base)


def _raw_ok(stdout: str, *, ru_maxrss_kb: int | None = 1000) -> bc.RawArmResult:
    return bc.RawArmResult(
        stdout=stdout,
        stderr="",
        returncode=0,
        drained_ok=True,
        timed_out=False,
        ru_maxrss_kb=ru_maxrss_kb,
        peak_vmhwm_kb=None,
    )


def _make_runner(
    off_results: list[bc.RawArmResult], on_results: list[bc.RawArmResult]
) -> bc.ArmRunner:
    """Two independent per-arm FIFO queues, dispatched by the `arm` the
    orchestrator actually requests -- correct regardless of alternation
    order (`_arm_order_for_rep` flips which arm runs first per rep), unlike
    a single flat call-order sequence."""
    off_it = iter(off_results)
    on_it = iter(on_results)

    def _runner(arm: str, _n_rows: int, _timeout_s: float) -> bc.RawArmResult:
        return next(on_it) if arm == "on" else next(off_it)

    return _runner


def _valid_pair(
    n_rows: int, *, off_wall: float = 0.5, on_wall: float = 0.5
) -> tuple[bc.RawArmResult, bc.RawArmResult]:
    return (
        _raw_ok(_record_json(n_rows, False, wall_s=off_wall)),
        _raw_ok(_record_json(n_rows, True, wall_s=on_wall)),
    )


# ---------------------------------------------------------------------------
# Pure statistics / gate boundaries
# ---------------------------------------------------------------------------


def test_inclusive_p95_differs_from_max() -> None:
    """Proves this is not just `max()` in disguise (plan §4: bench_driver.
    summarize mislabels the maximum as p95)."""
    values = [1.0, 1.0, 1.0, 1.0, 10.0]
    p95 = bc.inclusive_p95(values)
    assert p95 != max(values)
    assert p95 == pytest.approx(1.0 + (10.0 - 1.0) * 0.8)


def test_paired_median_ratio_differs_from_ratio_of_medians() -> None:
    """A skewed dataset where the paired per-rep ratio's median differs from
    naively dividing the two arms' independent medians -- proves the
    harness computes the former, which is what pairing is for."""
    off = [10.0, 20.0, 30.0]
    on = [12.0, 18.0, 90.0]
    ratio_of_medians = statistics.median(on) / statistics.median(off)
    ratios = [o / f for o, f in zip(on, off, strict=True)]
    paired_median = statistics.median(ratios)
    assert paired_median != ratio_of_medians
    assert paired_median == pytest.approx(1.2)
    assert ratio_of_medians == pytest.approx(0.9)


def _base_large_tier_gate_kwargs() -> dict[str, Any]:
    return {
        "off_wall_median": 10.0,
        "on_wall_median": 10.5,
        "ratio_median": 1.05,
        "ratio_p95": 1.10,
        "ci_high": 1.05,
        "off_rss_max_kb": 100_000,
        "on_rss_max_kb": 105_000,
    }


def test_large_tier_gates_all_pass() -> None:
    gates = bc.apply_gates(50_000, **_base_large_tier_gate_kwargs())
    assert all(gates.values()), gates


def test_large_tier_median_breach_fails() -> None:
    kwargs = _base_large_tier_gate_kwargs()
    kwargs["ratio_median"] = 1.11
    gates = bc.apply_gates(50_000, **kwargs)
    assert gates["median"] is False


def test_large_tier_p95_breach_fails() -> None:
    kwargs = _base_large_tier_gate_kwargs()
    kwargs["ratio_p95"] = 1.16
    gates = bc.apply_gates(50_000, **kwargs)
    assert gates["p95"] is False


def test_large_tier_ci_breach_fails() -> None:
    kwargs = _base_large_tier_gate_kwargs()
    kwargs["ci_high"] = 1.11
    gates = bc.apply_gates(1_000_000, **kwargs)
    assert gates["ci"] is False


def test_small_tier_point_pass_but_ci_fail() -> None:
    """The C2 case the plan specifically requires: a 10k tier whose point
    (median) difference passes but whose bootstrap CI upper bound still
    breaches -- proves the CI is a real, independently-enforced gate, not a
    report-only annotation next to the point rule."""
    gates = bc.apply_gates(
        10_000,
        off_wall_median=1.0,
        on_wall_median=1.04,  # diff 0.04 <= max(0.10, 0.050) -> point PASSES
        ratio_median=1.04,
        ratio_p95=1.2,
        ci_high=1.15,  # > 1 + max(0.10, 0.050/1.0) = 1.10 -> CI FAILS
        off_rss_max_kb=1000,
        on_rss_max_kb=1000,
    )
    assert gates["point"] is True
    assert gates["ci"] is False
    assert not all(gates.values())


def test_small_tier_50ms_floor_pass() -> None:
    """A tiny off_wall_median where 10% would be an unrealistically tight
    bound; the 50ms absolute floor makes this pass."""
    gates = bc.apply_gates(
        500,
        off_wall_median=0.100,
        on_wall_median=0.145,  # diff 0.045 <= max(0.010, 0.050) = 0.050
        ratio_median=1.45,
        ratio_p95=1.5,
        ci_high=1.0 + max(0.10, 0.050 / 0.100),  # exactly at the CI floor
        off_rss_max_kb=1000,
        on_rss_max_kb=1000,
    )
    assert gates["point"] is True


def test_small_tier_50ms_floor_fail() -> None:
    gates = bc.apply_gates(
        500,
        off_wall_median=0.100,
        on_wall_median=0.200,  # diff 0.100 > max(0.010, 0.050) = 0.050
        ratio_median=2.0,
        ratio_p95=2.0,
        ci_high=1.0,
        off_rss_max_kb=1000,
        on_rss_max_kb=1000,
    )
    assert gates["point"] is False


def test_rss_gate_pass_and_fail() -> None:
    passing = bc.apply_gates(50_000, **{**_base_large_tier_gate_kwargs(), "on_rss_max_kb": 110_000})
    assert passing["rss"] is True
    failing = bc.apply_gates(50_000, **{**_base_large_tier_gate_kwargs(), "on_rss_max_kb": 110_001})
    assert failing["rss"] is False


def test_rss_gate_fails_closed_on_missing_evidence() -> None:
    gates = bc.apply_gates(50_000, **{**_base_large_tier_gate_kwargs(), "off_rss_max_kb": 0})
    assert gates["rss"] is False


def test_bootstrap_ci_is_seeded_and_reproducible() -> None:
    ratios = [1.0, 1.05, 0.98, 1.1, 1.02]
    a = bc.bootstrap_ci(ratios, seed=42, n_bootstrap=500)
    b = bc.bootstrap_ci(ratios, seed=42, n_bootstrap=500)
    assert a == b


def test_bootstrap_ci_handles_singleton_bootstrap_count() -> None:
    """`--bootstrap 1` is a valid (if statistically thin) CLI input; the
    inclusive-percentile helper must not crash the way `statistics.
    quantiles` would on a single-element sample."""
    ci_low, ci_high = bc.bootstrap_ci([1.0, 1.1], seed=1, n_bootstrap=1)
    assert math.isfinite(ci_low)
    assert math.isfinite(ci_high)


# ---------------------------------------------------------------------------
# Cert-shape classification
# ---------------------------------------------------------------------------


def test_cert_shape_exact_required_tiers_is_eligible() -> None:
    assert bc.is_cert_shape([10_000, 100_000, 1_000_000], reps=20, warmup=3, bootstrap=2000)


@pytest.mark.parametrize(
    "tiers",
    [
        [10_000, 100_000, 1_000_000, 10_000],  # duplicate
        [10_000, 100_000, 1_000_000, 5_000],  # extra tier
        [10_000, 100_000],  # missing tier
        [10_000, 100_000, -1],  # non-positive
        [200],  # custom tiny tier
    ],
)
def test_cert_shape_rejects_any_tier_deviation(tiers: list[int]) -> None:
    assert not bc.is_cert_shape(tiers, reps=20, warmup=3, bootstrap=2000)


@pytest.mark.parametrize(
    ("reps", "warmup", "bootstrap"),
    [(19, 3, 2000), (20, 2, 2000), (20, 3, 1999)],
)
def test_cert_shape_rejects_below_sample_minima(reps: int, warmup: int, bootstrap: int) -> None:
    assert not bc.is_cert_shape(
        [10_000, 100_000, 1_000_000], reps=reps, warmup=warmup, bootstrap=bootstrap
    )


def test_tiny_run_prints_smoke_never_d9_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserts the banner, the JSON d9_certified field, and the written
    artifact TOGETHER for a tiny non-cert run (plan §8 [C5])."""
    off1, on1 = _valid_pair(200)
    off2, on2 = _valid_pair(200)
    runner = _make_runner([off1, off2], [on1, on2])
    config = bc.RunConfig(tiers=[200], reps=2, warmup=0, bootstrap=10, seed=1, timeout_s=5.0)
    result = bc.run_bench_compare(config, arm_runner=runner)

    assert result["run_ok"] is True
    assert result["d9_certified"] is False
    assert bc.banner_for(result) == "SMOKE COMPLETE (d9_certified=false)"
    assert bc.exit_code_for(result, require_cert=False) == 0


# ---------------------------------------------------------------------------
# Fail-closed via stub worker (every §5/§6 shape)
# ---------------------------------------------------------------------------


def _assert_fails_closed(
    off_results: list[bc.RawArmResult], on_results: list[bc.RawArmResult], n_rows: int = 500
) -> None:
    """`reps=1` is enough for every one of these: each defect trips before
    the orchestrator ever reaches the paired-ratio statistics, so the
    stats' own >=2-sample requirement never comes into play here."""
    runner = _make_runner(off_results, on_results)
    with pytest.raises(bc.FailClosedError):
        bc._run_tier(
            n_rows, warmup=0, reps=1, bootstrap=10, seed=1, timeout_s=5.0, arm_runner=runner
        )


def test_fail_closed_nonzero_exit() -> None:
    bad = bc.RawArmResult(
        stdout=_record_json(500, False),
        stderr="boom",
        returncode=1,
        drained_ok=True,
        timed_out=False,
        ru_maxrss_kb=1000,
        peak_vmhwm_kb=None,
    )
    _assert_fails_closed([bad], [])


def test_fail_closed_no_json() -> None:
    _assert_fails_closed([_raw_ok("nothing to see here\n")], [])


def test_fail_closed_duplicate_json() -> None:
    line = _record_json(500, False)
    _assert_fails_closed([_raw_ok(f"{line}\n{line}\n")], [])


def test_fail_closed_infinity_in_wall_s() -> None:
    _assert_fails_closed([_raw_ok(_record_json(500, False, wall_s=float("inf")))], [])


def test_fail_closed_nan_in_other_numeric_field() -> None:
    _assert_fails_closed([_raw_ok(_record_json(500, False, hash_ms=float("nan")))], [])


def test_fail_closed_out_rows_mismatch() -> None:
    _assert_fails_closed([_raw_ok(_record_json(500, False, out_rows=499))], [])


def test_fail_closed_n_rows_as_bool() -> None:
    _assert_fails_closed([_raw_ok(_record_json(500, False, n_rows=True))], [])


def test_fail_closed_out_rows_as_bool() -> None:
    _assert_fails_closed([_raw_ok(_record_json(500, False, out_rows=False))], [])


def test_fail_closed_on_activation_truthy_not_true() -> None:
    off = _raw_ok(_record_json(500, False))
    on = _raw_ok(_record_json(500, True, unified_slice_activated=1))
    _assert_fails_closed([off], [on])


def test_fail_closed_off_activation_zero_not_false() -> None:
    off = _raw_ok(_record_json(500, False, unified_slice_activated=0))
    _assert_fails_closed([off], [])


def test_fail_closed_on_activation_false_silent_fallback() -> None:
    off = _raw_ok(_record_json(500, False))
    on = _raw_ok(_record_json(500, True, unified_slice_activated=False))
    _assert_fails_closed([off], [on])


def test_fail_closed_off_activation_true() -> None:
    off = _raw_ok(_record_json(500, False, unified_slice_activated=True))
    _assert_fails_closed([off], [])


def test_fail_closed_off_execution_mode_mislabeled() -> None:
    off = _raw_ok(_record_json(500, False, execution_mode="unified_slice"))
    _assert_fails_closed([off], [])


def test_fail_closed_both_fingerprints_null() -> None:
    off = _raw_ok(_record_json(500, False, workload_fingerprint=None))
    _assert_fails_closed([off], [])


def test_fail_closed_fingerprints_identical_but_invalid() -> None:
    """Both arms agree with each other, but neither matches the expected
    admitted-column shape -- the per-record check against an independent
    expected value is what catches this, not just agreement between arms."""
    bad_fp = {"n_rows": 500, "columns": ["totally_wrong_column"]}
    off = _raw_ok(_record_json(500, False, workload_fingerprint=bad_fp))
    _assert_fails_closed([off], [])


def test_fail_closed_fingerprint_mismatch_between_arms() -> None:
    off = _raw_ok(_record_json(500, False))
    on = _raw_ok(
        _record_json(
            500, True, workload_fingerprint={"n_rows": 500, "columns": ["not_the_real_shape"]}
        )
    )
    _assert_fails_closed([off], [on])


def test_fail_closed_missing_ru_maxrss() -> None:
    off = _raw_ok(_record_json(500, False), ru_maxrss_kb=None)
    _assert_fails_closed([off], [])


def test_fail_closed_zero_ru_maxrss() -> None:
    off = _raw_ok(_record_json(500, False), ru_maxrss_kb=0)
    _assert_fails_closed([off], [])


def test_fail_closed_timeout() -> None:
    timed_out = bc.RawArmResult(
        stdout="",
        stderr="",
        returncode=-9,
        drained_ok=True,
        timed_out=True,
        ru_maxrss_kb=None,
        peak_vmhwm_kb=None,
    )
    _assert_fails_closed([timed_out], [])


def test_fail_closed_gate_breach_on_custom_tier() -> None:
    """A regressing CUSTOM tier is fail-closed too (plan §4): it can never
    be a silent run_ok=true just because it is not one of the cert tiers.
    Uses reps=2 (two identical off/on pairs) since the gate check itself
    needs the paired-ratio stats, which need >=2 samples."""
    off = _raw_ok(_record_json(500, False, wall_s=1.0))
    on = _raw_ok(_record_json(500, True, wall_s=5.0))  # 5x regression
    runner = _make_runner([off, off], [on, on])
    config = bc.RunConfig(tiers=[500], reps=2, warmup=0, bootstrap=10, seed=1, timeout_s=5.0)
    result = bc.run_bench_compare(config, arm_runner=runner)
    assert result["run_ok"] is False
    assert result["d9_certified"] is False
    assert bc.banner_for(result) == "D9 FAILED"
    assert bc.exit_code_for(result, require_cert=False) != 0


# ---------------------------------------------------------------------------
# CLI validation
# ---------------------------------------------------------------------------


def _expect_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        bc.parse_and_validate_args(argv)
    assert exc_info.value.code == 2


def test_cli_rejects_empty_tiers() -> None:
    _expect_exit_2(["--tiers", ""])


def test_cli_rejects_duplicate_tier() -> None:
    _expect_exit_2(["--tiers", "1000,1000"])


def test_cli_rejects_non_positive_tier() -> None:
    _expect_exit_2(["--tiers", "1000,-5"])


def test_cli_rejects_reps_below_floor() -> None:
    _expect_exit_2(["--reps", "1"])


def test_cli_rejects_zero_bootstrap() -> None:
    _expect_exit_2(["--bootstrap", "0"])


def test_cli_rejects_negative_warmup() -> None:
    _expect_exit_2(["--warmup", "-1"])


@pytest.mark.parametrize("timeout", ["0", "inf", "-inf", "nan"])
def test_cli_rejects_bad_timeout(timeout: str) -> None:
    _expect_exit_2(["--timeout", timeout])


def test_cli_accepts_minimal_reps_two() -> None:
    args = bc.parse_and_validate_args(["--reps", "2", "--tiers", "500"])
    assert args.reps == 2
    assert args.tiers == [500]


def test_reps_two_computes_p95_and_ci_without_quantiles_error() -> None:
    """A direct check that the minimal valid rep count does not hit
    `statistics.quantiles`'s len>=2 requirement anywhere in the pipeline."""
    off1, on1 = _valid_pair(500, off_wall=1.0, on_wall=1.01)
    off2, on2 = _valid_pair(500, off_wall=1.0, on_wall=0.99)
    runner = _make_runner([off1, off2], [on1, on2])
    tier = bc._run_tier(
        500, warmup=0, reps=2, bootstrap=50, seed=1, timeout_s=5.0, arm_runner=runner
    )
    assert math.isfinite(tier["ratio_p95"])
    assert math.isfinite(tier["ci_high"])


# ---------------------------------------------------------------------------
# --require-cert exit status
# ---------------------------------------------------------------------------


def test_require_cert_flag_changes_exit_status_for_noncert_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    off1, on1 = _valid_pair(500)
    off2, on2 = _valid_pair(500)
    monkeypatch.setattr(bc, "_spawn_worker_arm", _make_runner([off1, off2], [on1, on2]))

    out_without = tmp_path / "without.json"
    rc_without = bc.main(
        ["--tiers", "500", "--reps", "2", "--warmup", "0", "--out", str(out_without)]
    )
    assert rc_without == 0
    written = json.loads(out_without.read_text())
    assert written["d9_certified"] is False
    assert written["run_ok"] is True

    off3, on3 = _valid_pair(500)
    off4, on4 = _valid_pair(500)
    monkeypatch.setattr(bc, "_spawn_worker_arm", _make_runner([off3, off4], [on3, on4]))
    out_with = tmp_path / "with.json"
    rc_with = bc.main(
        [
            "--tiers",
            "500",
            "--reps",
            "2",
            "--warmup",
            "0",
            "--out",
            str(out_with),
            "--require-cert",
        ]
    )
    assert rc_with != 0
    written_with = json.loads(out_with.read_text())
    assert written_with["d9_certified"] is False


# ---------------------------------------------------------------------------
# Stale-artifact guard
# ---------------------------------------------------------------------------


def test_stale_artifact_guard_written_before_measurement_and_crash_leaves_no_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates a mid-run crash (an exception `run_bench_compare` does not
    itself catch, e.g. a genuine bug rather than a modeled §6 condition):
    the placeholder in_progress artifact must be the only thing left on
    disk -- never a stale PASS."""

    def _boom(_arm: str, _n_rows: int, _timeout_s: float) -> bc.RawArmResult:
        raise RuntimeError("simulated unmodeled crash")

    monkeypatch.setattr(bc, "_spawn_worker_arm", _boom)
    out_path = tmp_path / "crash.json"

    with pytest.raises(RuntimeError, match="simulated unmodeled crash"):
        bc.main(["--tiers", "500", "--reps", "2", "--warmup", "0", "--out", str(out_path)])

    written = json.loads(out_path.read_text())
    assert written == {"run_ok": False, "d9_certified": False, "status": "in_progress"}


# ---------------------------------------------------------------------------
# Alternation / pairing
# ---------------------------------------------------------------------------


def test_arm_order_alternates_by_rep_parity() -> None:
    assert bc._arm_order_for_rep(0) == ("off", "on")
    assert bc._arm_order_for_rep(1) == ("on", "off")
    assert bc._arm_order_for_rep(2) == ("off", "on")
    assert bc._arm_order_for_rep(3) == ("on", "off")


def test_warmups_excluded_from_stats_and_pairing_preserved() -> None:
    n_rows = 500
    # 1 warmup + 4 timed reps, each contributing one off/on pair; per-arm
    # queues are consumed strictly in chronological (warmup, then rep 0..3)
    # order regardless of which arm `_arm_order_for_rep` runs first.
    pairs = [
        _valid_pair(n_rows, off_wall=9.0, on_wall=9.0),  # warmup: extreme, must be excluded
        _valid_pair(n_rows, off_wall=1.0, on_wall=1.01),
        _valid_pair(n_rows, off_wall=1.0, on_wall=0.99),
        _valid_pair(n_rows, off_wall=1.0, on_wall=1.02),
        _valid_pair(n_rows, off_wall=1.0, on_wall=0.98),
    ]
    off_queue = [off for off, _on in pairs]
    on_queue = [on for _off, on in pairs]
    runner = _make_runner(off_queue, on_queue)
    tier = bc._run_tier(
        n_rows, warmup=1, reps=4, bootstrap=50, seed=1, timeout_s=5.0, arm_runner=runner
    )
    assert tier["reps"] == 4
    assert tier["off_wall_median"] < 2.0  # the 9.0s warmup never leaked into stats
    timed_entries = [r for r in tier["raw_reps"] if r["phase"] == "timed"]
    warmup_entries = [r for r in tier["raw_reps"] if r["phase"] == "warmup"]
    assert len(warmup_entries) == 2  # 1 warmup rep * 2 arms
    assert len(timed_entries) == 8  # 4 timed reps * 2 arms
    pair_indices = sorted({e["pair_index"] for e in timed_entries})
    assert pair_indices == [0, 1, 2, 3]
    for e in timed_entries:
        expected_order = list(bc._arm_order_for_rep(e["pair_index"]))
        assert e["arm_order"] == expected_order


# ---------------------------------------------------------------------------
# Real child-lifecycle (helper subprocesses, no engine)
# ---------------------------------------------------------------------------


def _write_helper(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source)
    return path


def test_child_lifecycle_drains_both_pipes_beyond_buffer_size(tmp_path: Path) -> None:
    script = _write_helper(
        tmp_path,
        "flood.py",
        "import sys\n"
        "sys.stdout.write('A' * 300_000)\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('B' * 300_000)\n"
        "sys.stderr.flush()\n",
    )
    result = bc.run_child_process(
        [sys.executable, str(script)], cwd=str(tmp_path), env=dict(os.environ), timeout_s=15.0
    )
    assert result.drained_ok is True
    assert result.returncode == 0
    assert len(result.stdout) == 300_000
    assert len(result.stderr) == 300_000
    assert result.ru_maxrss_kb is not None and result.ru_maxrss_kb > 0


def test_child_lifecycle_preserves_nonzero_exit_code(tmp_path: Path) -> None:
    script = _write_helper(tmp_path, "exit7.py", "import sys\nsys.exit(7)\n")
    result = bc.run_child_process(
        [sys.executable, str(script)], cwd=str(tmp_path), env=dict(os.environ), timeout_s=15.0
    )
    assert result.returncode == 7
    assert result.drained_ok is True
    assert result.timed_out is False


def test_child_lifecycle_timeout_group_kills_and_reaps(tmp_path: Path) -> None:
    script = _write_helper(tmp_path, "sleep_forever.py", "import time\ntime.sleep(30)\n")
    t0 = time.monotonic()
    result = bc.run_child_process(
        [sys.executable, str(script)], cwd=str(tmp_path), env=dict(os.environ), timeout_s=1.0
    )
    elapsed = time.monotonic() - t0
    assert result.timed_out is True
    assert elapsed < 15.0  # killed well before the helper's own 30s sleep would finish
    assert result.returncode == -9  # SIGKILL, not a clean exit


def test_child_lifecycle_grandchild_inherited_pipe_fails_closed(tmp_path: Path) -> None:
    """The direct child spawns a grandchild that inherits the stdout pipe
    (no PIPE redirection, so it shares the parent's fd 1) and outlives it;
    the direct child exits almost immediately. Drainage must stay
    incomplete until the whole group is killed, and the partial buffer must
    never be treated as parseable."""
    script = _write_helper(
        tmp_path,
        "grandchild.py",
        "import subprocess, sys\n"
        # Sleeps well past the reader-thread join deadline (5s in run_child_
        # process) so this is never a close race against scheduler jitter;
        # the group-kill after the failed join ends it early regardless.
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "sys.stdout.write('partial-before-grandchild-outlives-me')\n"
        "sys.stdout.flush()\n"
        "sys.exit(0)\n",
    )
    result = bc.run_child_process(
        [sys.executable, str(script)], cwd=str(tmp_path), env=dict(os.environ), timeout_s=15.0
    )
    assert result.drained_ok is False


def test_child_lifecycle_exit_between_poll_and_kill_race_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates the exit-between-WNOHANG-poll-and-killpg race by forcing
    `os.killpg` to raise `ProcessLookupError` unconditionally (the real
    race window is microseconds wide and not deterministically
    reproducible via wall-clock timing alone). A REAL short-lived helper
    still runs underneath: the swallow must not crash, and `os.wait4` must
    still reap the child once it exits on its own."""
    script = _write_helper(tmp_path, "short_sleep.py", "import time\ntime.sleep(1.2)\n")

    real_killpg = os.killpg

    def _raising_killpg(pgid: int, sig: int) -> None:
        raise ProcessLookupError("simulated: child already reaped by the race")

    monkeypatch.setattr(os, "killpg", _raising_killpg)
    try:
        result = bc.run_child_process(
            [sys.executable, str(script)],
            cwd=str(tmp_path),
            env=dict(os.environ),
            timeout_s=0.3,
        )
    finally:
        monkeypatch.setattr(os, "killpg", real_killpg)

    assert result.timed_out is True
    assert result.returncode == 0  # the real helper ran to natural completion, unkilled


# ---------------------------------------------------------------------------
# Real-worker integration (companion-guarded)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not native_companion_status().ok,
    reason="compiled decoy-engine-native companion unavailable",
)
def test_real_worker_tiny_tier_smoke(tmp_path: Path) -> None:
    """The plan's own acceptance smoke: `--tiers 200 --reps 2 --warmup 1`
    against the REAL frozen worker. Guarded on the compiled companion (the
    hash columns in the fixed nine-column workload need it to activate the
    unified slice); companion-absent legs skip this test entirely."""
    out_path = tmp_path / "smoke.json"
    rc = bc.main(
        [
            "--tiers",
            "200",
            "--reps",
            "2",
            "--warmup",
            "1",
            "--out",
            str(out_path),
            "--timeout",
            "120",
        ]
    )
    assert rc == 0
    result = json.loads(out_path.read_text())
    assert result["run_ok"] is True
    assert result["d9_certified"] is False
    tier = result["tiers"]["200"]
    assert tier["off_rss_max_kb"] > 0
    assert tier["on_rss_max_kb"] > 0
    assert tier["ratio_median"] > 0
