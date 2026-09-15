"""Task 4.5 D9 remediation (Codex final-gate HIGH): the deferred perf-bench
harness under `scripts/bench-unified-slice/` + `scripts/native-baseline/
bench_driver.py` was broken -- `bench_worker_unified.py` hard-coded
`hash_ms=0.0`, which made `bench_driver.py`'s `hash_tput_median_rows_s`
compute `None`, which then crashed the summary line's `:.0f}` format;
`bench_compare.py`'s docstring claimed an alternating old/new sweep and a
`--baseline-old` option neither existed in code.

This module is the "harness is proven runnable" proof the remediation
requires: ONE real, tiny, end-to-end subprocess run of the worker and the
driver together (proving the fixed real timing survives the fixed None-safe
formatting with no crash), plus fast in-process unit coverage of `bench_
compare.py`'s alternating scheduling and `--baseline-old` check (a live
double-driver subprocess run for that piece would cost real wall-clock
minutes even at a tiny tier -- exactly the cost `bench_compare.py`'s own
docstring says this deferred script exists to avoid paying on every run).
The heavy multi-tier statistical sweep itself stays a deferred manual run,
per that same docstring.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = ENGINE_ROOT / "scripts" / "bench-unified-slice"
BASELINE_DIR = ENGINE_ROOT / "scripts" / "native-baseline"
VENV_PY = Path(sys.executable)
_WORKER_ENV = {**__import__("os").environ, "PYTHONPATH": str(ENGINE_ROOT / "src")}

_SMOKE_N_ROWS = 1_000


def _load_module(path: Path, name: str) -> ModuleType:
    """Loads a standalone script (no package `__init__.py` in `scripts/`)
    as an importable module by file path, independent of `sys.path` state --
    `bench_worker_unified.py` itself mutates `sys.path` to reach its sibling
    `bench_worker.py`, so importing by spec avoids the two interfering."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# bench_worker_unified.py: real, non-fabricated per-strategy timing.
# ---------------------------------------------------------------------------


def test_worker_emits_real_positive_per_strategy_timing() -> None:
    """A real subprocess run at a tiny row count: proves the worker script
    itself is runnable end-to-end and that `hash_ms` (and every other
    per-strategy metric) is a real measured number, not the old hard-coded
    `0.0`."""
    proc = subprocess.run(  # noqa: S603 fixed local benchmark command, no untrusted input
        [str(VENV_PY), str(BENCH_DIR / "bench_worker_unified.py"), str(_SMOKE_N_ROWS)],
        cwd=str(ENGINE_ROOT),
        env=_WORKER_ENV,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("BENCH_JSON "))
    rec = json.loads(line[len("BENCH_JSON ") :])

    assert rec["n_rows"] == _SMOKE_N_ROWS
    assert rec["out_rows"] == _SMOKE_N_ROWS
    assert rec["unified_slice_activated"] is True
    assert rec["hash_cols"] == 3
    for key in ("hash_ms", "redact_ms", "truncate_ms", "passthrough_ms"):
        assert rec[key] > 0.0, f"{key} was not a real positive measurement: {rec}"


# ---------------------------------------------------------------------------
# bench_driver.py: None-safe summary formatting.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bench_driver() -> ModuleType:
    return _load_module(BASELINE_DIR / "bench_driver.py", "bench_driver_under_test")


def test_driver_end_to_end_real_run_produces_a_finite_hash_tput(bench_driver: ModuleType) -> None:
    """The real end-to-end proof: the driver spawns the (now-fixed) unified
    worker for one tiny tier and one rep, and its own (now-fixed) summary
    formatting does not crash -- the exact crash the D9 finding reported."""
    out_path = BASELINE_DIR / f"_smoke_test_{_SMOKE_N_ROWS}.json"
    try:
        reps = [
            bench_driver.run_rep(
                _SMOKE_N_ROWS,
                BENCH_DIR / "bench_worker_unified.py",
                [],
            )
        ]
        summ = bench_driver.summarize(_SMOKE_N_ROWS, reps)
        # Must not raise: this is the exact call the driver's main loop makes
        # right after `summarize()`, where the pre-fix code crashed on a
        # `None` hash_tput formatted with `:.0f}`.
        line = bench_driver._format_tier_summary(_SMOKE_N_ROWS, summ)
        assert "n/a" not in line, "the real fixed worker should report a finite hash_tput"
        assert summ["hash_tput_median_rows_s"] is not None
    finally:
        out_path.unlink(missing_ok=True)


def test_format_tier_summary_is_none_safe(bench_driver: ModuleType) -> None:
    """Direct proof the None-safety fix holds even for a worker that
    legitimately reports no hash timing (e.g. an all-passthrough table) --
    the shape that crashed before the fix, exercised without needing a real
    subprocess."""
    summ = {
        "wall_median_s": 0.5,
        "wall_iqr_s": 0.1,
        "wall_p95of_s": 0.6,
        "peak_rss_max_mb": 12.3,
        "hash_tput_median_rows_s": None,
    }
    line = bench_driver._format_tier_summary(1000, summ)
    assert "n/a" in line


def test_summarize_leaves_hash_tput_none_when_every_rep_reports_zero_hash_ms(
    bench_driver: ModuleType,
) -> None:
    """The pre-fix worker's hard-coded `hash_ms=0.0` made every rep's
    `hash_ms` falsy, so `summarize()` never populated `hash_tputs` -- this
    pins that `summarize()` itself already handled the empty-list case
    correctly (`None`, not a crash); the crash was purely in formatting it,
    which the two tests above cover."""
    reps = [
        {"wall_s": 0.1, "peak_rss_kb": 1000, "hash_ms": 0.0, "hash_cols": 3},
        {"wall_s": 0.1, "peak_rss_kb": 1000, "hash_ms": 0.0, "hash_cols": 3},
    ]
    summ = bench_driver.summarize(1000, reps)
    assert summ["hash_tput_median_rows_s"] is None
    bench_driver._format_tier_summary(1000, summ)  # must not raise


# ---------------------------------------------------------------------------
# bench_compare.py: alternating tiers + --baseline-old.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bench_compare() -> ModuleType:
    return _load_module(BENCH_DIR / "bench_compare.py", "bench_compare_under_test")


def test_main_alternates_old_and_new_per_tier_instead_of_sweeping_each_arm_fully(
    bench_compare: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex final-gate HIGH: the docstring claims tier-by-tier alternation;
    the old code ran every tier for "old" and only then every tier for
    "new". Monkeypatches `_run_driver` to record call order instead of
    actually running benchmarks (a live double-driver sweep is exactly the
    real wall-clock cost this deferred script exists to avoid paying on
    every test run), and asserts the calls interleave old/new per tier."""
    calls: list[tuple[str, str, str]] = []

    def _fake_run_driver(
        tiers: str, reps: int, warmup: int, worker: str, out_path: Path, flag: str = "on"
    ) -> dict:
        calls.append((tiers, worker, flag))
        rec = {"wall_s": 1.0, "peak_rss_kb": 1000, "out_rows": int(tiers)}
        return {tiers: {"wall_median_s": 1.0, "wall_p95of_s": 1.0, "raw_reps": [rec]}}

    monkeypatch.setattr(bench_compare, "_run_driver", _fake_run_driver)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bench_compare.py",
            "--tiers",
            "1000,2000",
            "--reps",
            "1",
            "--warmup",
            "0",
            "--out-dir",
            str(tmp_path),
        ],
    )
    bench_compare.main()

    tier_order = [tiers for tiers, _worker, _flag in calls]
    assert tier_order == ["1000", "1000", "2000", "2000"], (
        "expected old(1000), new(1000), old(2000), new(2000) -- got "
        f"{tier_order}, not a per-tier alternation"
    )
    # Both arms run the SAME nine-column unified worker; only the env flag
    # (legacy "off" vs unified "on") differs, so the comparison is one workload.
    unified_worker = "../bench-unified-slice/bench_worker_unified.py"
    assert all(worker == unified_worker for _t, worker, _f in calls)
    flag_order = [flag for _t, _w, flag in calls]
    assert flag_order == ["off", "on", "off", "on"], (
        f"expected per-tier legacy(off) then unified(on); got {flag_order}"
    )


def test_check_old_vs_baseline_flags_a_regression_past_one_percent(
    bench_compare: ModuleType,
) -> None:
    old_results = {"10000": {"wall_median_s": 1.02}}
    baseline = {"10000": {"wall_median_s": 1.0}}
    failures = bench_compare._check_old_vs_baseline(old_results, baseline)
    assert failures and "10000" in failures[0]


def test_check_old_vs_baseline_passes_within_one_percent(bench_compare: ModuleType) -> None:
    old_results = {"10000": {"wall_median_s": 1.005}}
    baseline = {"10000": {"wall_median_s": 1.0}}
    assert bench_compare._check_old_vs_baseline(old_results, baseline) == []


def test_check_old_vs_baseline_skips_a_tier_the_baseline_never_measured(
    bench_compare: ModuleType,
) -> None:
    old_results = {"10000": {"wall_median_s": 5.0}}
    baseline = {"999999": {"wall_median_s": 1.0}}
    assert bench_compare._check_old_vs_baseline(old_results, baseline) == []


def test_baseline_old_flag_is_wired_into_main(
    bench_compare: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The docstring names `--baseline-old` as a real option; proves argparse
    accepts it and `main()` actually consults it (a regressed baseline
    fails the gate) rather than merely parsing and ignoring the flag."""
    baseline_path = tmp_path / "baseline_old.json"
    baseline_path.write_text(json.dumps({"1000": {"wall_median_s": 1.0}}))

    def _fake_run_driver(
        tiers: str, reps: int, warmup: int, worker: str, out_path: Path, flag: str = "on"
    ) -> dict:
        rec = {"wall_s": 2.0, "peak_rss_kb": 1000, "out_rows": int(tiers)}
        # A blown-out 2x regression vs the 1.0s baseline recorded above.
        return {tiers: {"wall_median_s": 2.0, "wall_p95of_s": 2.0, "raw_reps": [rec]}}

    monkeypatch.setattr(bench_compare, "_run_driver", _fake_run_driver)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bench_compare.py",
            "--tiers",
            "1000",
            "--reps",
            "1",
            "--warmup",
            "0",
            "--out-dir",
            str(tmp_path),
            "--baseline-old",
            str(baseline_path),
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        bench_compare.main()
    assert excinfo.value.code == 1
