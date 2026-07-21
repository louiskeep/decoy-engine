"""BENCH-CODE-1 regression: the `--strategy` payload-column knob in
scripts/fk_memory_probe.py (decoy-platform NEXT-RUNS-HANDOFF.md R4, option (a)).

Runs the real CLI end to end (subprocess, not an in-process call) at tiny
`--rows`/`--width` for each of the four strategies, the same invocation shape
BENCH-CODE-1's hard rule requires: the probe MUST emit valid JSON at small
`--rows` on the devbox. Kept small (rows=50, width=2) so all five subprocess
calls stay well under a second of masking work each; the real per-strategy
cost curves are the paid-GCP-session's job (R4), not this gate's.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "fk_memory_probe.py"

_STRATEGIES = ("hash", "fpe", "faker", "categorical")

# Keys present on a completed single-tier run before BENCH-CODE-1 (the
# default-run shape this change must not disturb), plus the new `strategy`
# field BENCH-CODE-1 adds.
_EXPECTED_KEYS = {
    "mode",
    "rows_per_table",
    "tables",
    "width",
    "orphan_frac",
    "orphan_policy",
    "mem_cap_mb",
    "strategy",
    "completed",
    "peak_rss_mb",
    "peak_vms_mb",
    "build_s",
    "mask_s",
    "tracemalloc_peak_mb",
    "fk_rows_checked",
}


def _run_probe(*extra_args: str) -> dict:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(_SCRIPT), "--rows", "50", "--width", "2", "--json", *extra_args],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=120,
    )
    assert proc.returncode == 0, f"probe failed (rc={proc.returncode}): {proc.stderr}"
    line = proc.stdout.strip().splitlines()[-1]
    return json.loads(line)  # raises on invalid JSON, the hard rule this test pins


@pytest.mark.parametrize("strategy", _STRATEGIES)
def test_strategy_run_emits_valid_json_with_strategy_field(strategy: str) -> None:
    rec = _run_probe("--strategy", strategy)
    assert rec["strategy"] == strategy
    assert rec["completed"] is True
    assert rec["fk_rows_checked"] == 50
    assert rec.keys() >= _EXPECTED_KEYS


def test_default_strategy_run_matches_pre_bench_code_1_shape() -> None:
    """No `--strategy` flag: the default-run JSON keeps every field the probe
    emitted before this knob existed, plus the new `strategy` field (hash,
    per DEFAULT_PAYLOAD_STRATEGY -- see tests/perf_fixtures/fk_relational.py).
    Shape, not exact values: masking payload columns by default is new
    behavior (they were unmasked filler before), documented in both that
    module's docstring and scripts/fk_memory_probe.py's module docstring.
    """
    rec = _run_probe()
    assert rec["strategy"] == "hash"
    assert rec["completed"] is True
    assert rec.keys() >= _EXPECTED_KEYS
    # No field lost relative to the pre-BENCH-CODE-1 shape (strategy is the
    # only addition).
    assert rec.keys() == _EXPECTED_KEYS


def test_source_dir_and_capability_reject_explicit_strategy(tmp_path: Path) -> None:
    """BENCH-CODE-1 option (a) scope: neither flag guarantees a payload_NN
    schema to mask, so an explicit --strategy with either is a clear typed
    error (argparse `ap.error`, exit code 2), not a silent no-op."""
    for combo in (
        ["--strategy", "hash", "--source-dir", str(tmp_path)],
        ["--strategy", "hash", "--capability", "--mem-cap-mb", "256"],
    ):
        proc = subprocess.run(  # noqa: S603
            [sys.executable, str(_SCRIPT), "--rows", "50", *combo],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
            timeout=30,
        )
        assert proc.returncode == 2, proc.stderr
        assert "not supported together with --source-dir or --capability" in proc.stderr
