"""Phase D / D1a performance gate (program section 8), predeclared in the D1a plan section 6.

Run explicitly (`pytest -m benchmark tests/perf/test_d1a_hash_native_perf.py -s`) on a box with
the abi-3 companion: it is a slice acceptance gate, not part of the default loop. Native and
Python each run in their own process so total peak RSS is per mode. Acceptance: native p50
strictly below Python p50, native TOTAL process peak RSS <= 1,024 MiB, and the two outputs
byte-identical on the whole 5M-row workload.
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
PEAK_RSS_CEILING_MIB = 1024


def _companion_loads() -> bool:
    try:
        load_compiled_crypto_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


def _run(mode: str) -> dict[str, Any]:
    env = {**os.environ, "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{REPO}"}
    done = subprocess.run(  # noqa: S603 - fixed argv: this interpreter running our own module
        [sys.executable, "-m", "tests.perf._d1a_hash_bench", "--mode", mode],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.benchmark
@pytest.mark.skipif(not _companion_loads(), reason="needs the abi-3 companion")
def test_native_ooc_hash_beats_python_within_the_memory_ceiling() -> None:
    native = _run("native")
    python = _run("python")
    report = json.dumps({"native": native, "python": python}, indent=2)
    out = os.environ.get("D1A_BENCH_OUT")
    if out:
        Path(out).write_text(report)
    print(report)
    assert native["output_digest"] == python["output_digest"], "native and Python outputs differ"
    assert native["digest_stable_across_rounds"] and python["digest_stable_across_rounds"]
    assert native["p50_seconds"] < python["p50_seconds"]
    assert native["peak_rss_mib"] <= PEAK_RSS_CEILING_MIB
