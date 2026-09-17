"""FOLLOWUP-BENCH-DRIVER-HARDEN: fast, CI-safe tests for `scripts/native-
baseline/bench_driver.py`'s two hardenings -- None-safe log-line formatting
and fail-closed per-rep RSS aggregation. Covers:
strict-by-default failure before a tier summary is written, an all-present
success, the `--allow-missing-rss` tolerant shape, None-safe rendering on
the warmup/per-rep/summary log lines, a non-hash workload's `None` hash
throughput not raising, and the pre-existing native-reroute fail-closed
still firing.

`bench_driver.py` lives under `scripts/`, not the package, so it is loaded
by file path (mirrors `test_bench_compare_harness.py` for its sibling
script). Rep dicts are built by hand; nothing here spawns a real benchmark
worker, except the one test that exercises `run_rep`'s own subprocess
lifecycle against a tiny stub script.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = ENGINE_ROOT / "scripts" / "native-baseline"

_spec = importlib.util.spec_from_file_location("bench_driver", BENCH_DIR / "bench_driver.py")
assert _spec is not None and _spec.loader is not None
bd = importlib.util.module_from_spec(_spec)
sys.modules["bench_driver"] = bd
_spec.loader.exec_module(bd)


def _rep(peak_rss_kb: int | None = 1000, wall_s: float = 1.0, **overrides: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "wall_s": wall_s,
        "peak_rss_kb": peak_rss_kb,
        "execution_mode": "test_mode",
    }
    rec.update(overrides)
    return rec


def _run_main_with_fake_reps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_run_rep: Any,
    *,
    extra_argv: list[str] | None = None,
) -> Path:
    monkeypatch.setattr(bd, "run_rep", fake_run_rep)
    out_path = tmp_path / "out.json"
    argv = [
        "bench_driver.py",
        "--tiers",
        "1000",
        "--reps",
        "2",
        "--warmup",
        "1",
        "--out",
        str(out_path),
        *(extra_argv or []),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    bd.main()
    return out_path


# ---------------------------------------------------------------------------
# fail-closed per-rep RSS aggregation
# ---------------------------------------------------------------------------


def test_strict_default_raises_on_missing_rss() -> None:
    reps = [_rep(peak_rss_kb=1000), _rep(peak_rss_kb=None), _rep(peak_rss_kb=1200)]
    with pytest.raises(bd.MissingRssError, match=r"1/3 timed reps"):
        bd.summarize(1000, reps)


def test_strict_default_raise_leaves_no_summary_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Drives the actual tier path through `main`, not just the `summarize`
    helper: a missing RSS in one of two timed reps must raise before that
    tier's result is ever written to the --out file."""
    call_count = {"n": 0}

    def _fake_run_rep(n_rows: int, worker: Path, worker_args: list[str]) -> dict[str, Any]:
        call_count["n"] += 1
        # call 1 = warmup (RSS ok), call 2 = timed rep 1 (RSS missing), call 3 = timed rep 2 (RSS ok)
        return _rep(peak_rss_kb=None if call_count["n"] == 2 else 1000)

    out_path = tmp_path / "out.json"
    monkeypatch.setattr(bd, "run_rep", _fake_run_rep)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bench_driver.py",
            "--tiers",
            "1000",
            "--reps",
            "2",
            "--warmup",
            "1",
            "--out",
            str(out_path),
        ],
    )
    with pytest.raises(bd.MissingRssError):
        bd.main()
    assert not out_path.exists()


def test_all_present_succeeds_with_real_max_and_complete_true() -> None:
    reps = [_rep(peak_rss_kb=1000), _rep(peak_rss_kb=3000), _rep(peak_rss_kb=2000)]
    summ = bd.summarize(2000, reps)
    assert summ["peak_rss_max_kb"] == 3000
    assert summ["peak_rss_max_mb"] == round(3000 / 1024, 1)
    assert summ["peak_rss_observed_max_kb"] == 3000
    assert summ["rss_reps"] == 3
    assert summ["rss_missing_reps"] == 0
    assert summ["rss_complete"] is True


def test_allow_missing_rss_all_present_populates_certified_max() -> None:
    # Tolerant flag over a fully-present list must still populate the certified
    # peak_rss_max_* and mark rss_complete True (not withhold them).
    reps = [_rep(peak_rss_kb=1000), _rep(peak_rss_kb=3000), _rep(peak_rss_kb=2000)]
    summ = bd.summarize(2000, reps, allow_missing_rss=True)
    assert summ["peak_rss_max_kb"] == 3000
    assert summ["peak_rss_observed_max_kb"] == 3000
    assert summ["rss_missing_reps"] == 0
    assert summ["rss_complete"] is True


def test_allow_missing_rss_tolerant_shape() -> None:
    reps = [
        _rep(peak_rss_kb=1000),
        _rep(peak_rss_kb=None),
        _rep(peak_rss_kb=4000),
        _rep(peak_rss_kb=None),
    ]
    summ = bd.summarize(2000, reps, allow_missing_rss=True)
    assert summ["peak_rss_max_kb"] is None
    assert summ["peak_rss_max_mb"] is None
    assert summ["peak_rss_observed_max_kb"] == 4000
    assert summ["peak_rss_observed_max_mb"] == round(4000 / 1024, 1)
    assert summ["rss_reps"] == 2
    assert summ["rss_missing_reps"] == 2
    assert summ["rss_complete"] is False


def test_allow_missing_rss_all_missing_observed_max_is_none() -> None:
    """When every rep misses RSS under the tolerant flag, there is no observed
    max to report either."""
    reps = [_rep(peak_rss_kb=None), _rep(peak_rss_kb=None)]
    summ = bd.summarize(500, reps, allow_missing_rss=True)
    assert summ["peak_rss_max_kb"] is None
    assert summ["peak_rss_observed_max_kb"] is None
    assert summ["rss_missing_reps"] == 2
    assert summ["rss_complete"] is False


def test_allow_missing_rss_flag_wired_through_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The CLI flag must actually reach `summarize`'s policy, not just exist
    in argparse."""
    reps_queue = iter([_rep(peak_rss_kb=1000), _rep(peak_rss_kb=None), _rep(peak_rss_kb=500)])

    def _fake_run_rep(n_rows: int, worker: Path, worker_args: list[str]) -> dict[str, Any]:
        return next(reps_queue)

    out_path = _run_main_with_fake_reps(
        monkeypatch, tmp_path, _fake_run_rep, extra_argv=["--allow-missing-rss"]
    )
    tier = json.loads(out_path.read_text())["1000"]
    assert tier["peak_rss_max_kb"] is None
    assert tier["rss_complete"] is False
    assert tier["rss_missing_reps"] == 1
    assert tier["rss_reps"] == 1


def test_non_hash_workload_hash_tput_none_does_not_raise() -> None:
    reps = [_rep(peak_rss_kb=1000), _rep(peak_rss_kb=1200)]  # no hash_ms/hash_cols
    summ = bd.summarize(1000, reps)
    assert summ["hash_tput_median_rows_s"] is None


# ---------------------------------------------------------------------------
# None-safe formatting
# ---------------------------------------------------------------------------


def test_fmt_none_renders_na_with_unit() -> None:
    assert bd._fmt(None, "kb") == "n/akb"
    assert bd._fmt(None, " MB") == "n/a MB"


def test_fmt_value_renders_with_format_spec_and_unit() -> None:
    assert bd._fmt(12345.678, "rows/s", ".0f") == "12346rows/s"
    assert bd._fmt(12.34, " MB") == "12.34 MB"


def test_warmup_rep_and_summary_lines_render_na_not_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing RSS and a non-hash workload's missing throughput must
    render as 'n/a' on the warmup line, every timed-rep line, and the
    summary line -- never a crash, never a bare 'None'."""

    def _fake_run_rep(n_rows: int, worker: Path, worker_args: list[str]) -> dict[str, Any]:
        return _rep(peak_rss_kb=None)  # no hash_ms/hash_cols -> non-hash workload too

    _run_main_with_fake_reps(
        monkeypatch, tmp_path, _fake_run_rep, extra_argv=["--allow-missing-rss"]
    )
    err = capsys.readouterr().err
    lines = err.splitlines()
    warmup_line = next(line for line in lines if line.strip().startswith("warmup wall="))
    rep_lines = [line for line in lines if line.strip().startswith("rep ")]
    summary_line = next(line for line in lines if "SUMMARY" in line)

    assert rep_lines  # sanity: both timed reps logged
    assert "n/a" in warmup_line
    assert all("n/a" in line for line in rep_lines)
    assert "n/a" in summary_line
    for line in (warmup_line, *rep_lines, summary_line):
        assert "None" not in line


# ---------------------------------------------------------------------------
# Pre-existing native-reroute fail-closed (unchanged; run_rep ~104-110)
# ---------------------------------------------------------------------------


def test_native_reroute_still_fails_closed(tmp_path: Path) -> None:
    """A native worker that silently rerouted to the oracle must still be
    refused, unchanged by this hardening. Uses a tiny stub worker script
    rather than a real benchmark run to exercise `run_rep`'s own subprocess
    + JSON-parsing path."""
    fake_worker = tmp_path / "fake_native_worker.py"
    fake_worker.write_text(
        "import json\n"
        "import sys\n"
        "record = {\n"
        "    'wall_s': 0.01,\n"
        "    'n_rows': int(sys.argv[1]),\n"
        "    'native_admitted': True,\n"
        "    'compiled_kernel_executed': False,\n"
        "    'reroute_reason': 'test_reroute',\n"
        "}\n"
        "print('BENCH_JSON ' + json.dumps(record))\n"
    )
    with pytest.raises(RuntimeError, match="did not execute the compiled kernel"):
        bd.run_rep(500, fake_worker, [])
