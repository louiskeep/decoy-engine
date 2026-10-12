"""Phase D / D2-cycle performance gate, predeclared in the D2-cycle plan.

Run explicitly (`pytest -m benchmark tests/perf/test_d2cycle_seq_native_perf.py -s`) on a box with
the abi-3 companion; it is a slice acceptance gate, not part of the default loop. Each mode runs in
its own process so peak RSS is per mode. Capability first: the cyclic job completes on the bounded
route with an output digest equal to the full-frame oracle's. Then speed on the key columns:
native total p50 strictly below Python's and native peak RSS at most 110% of Python's. The
full-frame peak RSS is reported, not gated: on three narrow tables the cached key maps outweigh
what eviction saves, and the bounded route pays off on wide tables.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)

REPO = Path(__file__).parents[2]
RSS_RATIO_CEILING = 1.10


def _companion_loads() -> bool:
    try:
        load_compiled_crypto_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


def _run(mode: str) -> dict[str, Any]:
    env = {**os.environ, "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{REPO}"}
    done = subprocess.run(  # noqa: S603 - fixed argv: this interpreter running our own module
        [sys.executable, "-m", "tests.perf._d2cycle_seq_bench", "--mode", mode],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.benchmark
@pytest.mark.skipif(not _companion_loads(), reason="needs the abi-3 companion")
def test_a_native_cycle_runs_bounded_matches_the_oracle_and_beats_python() -> None:
    native, python, full_frame = _run("native"), _run("python"), _run("full_frame")
    report = json.dumps({"native": native, "python": python, "full_frame": full_frame}, indent=2)
    out = os.environ.get("D2CYCLE_BENCH_OUT")
    if out:
        Path(out).write_text(report)
    print(report)
    assert native["backends"] == ["rust_companion"]
    assert python["backends"] == ["python_scalar"]
    assert native["output_digest"] == python["output_digest"] == full_frame["output_digest"]
    assert native["digest_stable_across_rounds"] and python["digest_stable_across_rounds"]
    assert native["p50_seconds"] < python["p50_seconds"]
    assert native["peak_rss_mib"] <= python["peak_rss_mib"] * RSS_RATIO_CEILING
