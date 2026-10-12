"""Phase D / D2c-ii performance gate, predeclared in the D2c-ii plan.

Run explicitly (`pytest -m benchmark tests/perf/test_d2c_ii_seq_cat_native_perf.py -s`) on a box
with the abi-3 companion: it is a slice acceptance gate, not part of the default loop. Native and
Python each run in their own process so peak RSS is per mode. Acceptance: native total p50
strictly below Python total p50, native categorical-handler p50 strictly below Python's, native peak RSS
at most 110% of Python's, and byte-identical output digests on the whole 400k-row FK workload.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._index_ext import load_compiled_index_kernel

REPO = Path(__file__).parents[2]
RSS_RATIO_CEILING = 1.10


def _companion_loads() -> bool:
    try:
        load_compiled_index_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


def _run(mode: str) -> dict[str, Any]:
    env = {**os.environ, "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{REPO}"}
    done = subprocess.run(  # noqa: S603 - fixed argv: this interpreter running our own module
        [sys.executable, "-m", "tests.perf._d2c_ii_seq_cat_bench", "--mode", mode],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.benchmark
@pytest.mark.skipif(not _companion_loads(), reason="needs the abi-3 companion")
def test_native_seq_categorical_beats_python_within_the_memory_envelope() -> None:
    native = _run("native")
    python = _run("python")
    report = json.dumps({"native": native, "python": python}, indent=2)
    out = os.environ.get("D2C_II_BENCH_OUT")
    if out:
        Path(out).write_text(report)
    print(report)
    assert native["scalar_backends"] == ["rust_companion"]
    assert python["scalar_backends"] == ["python_scalar"]
    assert native["output_digest"] == python["output_digest"], "native and Python outputs differ"
    assert native["digest_stable_across_rounds"] and python["digest_stable_across_rounds"]
    assert native["p50_seconds"] < python["p50_seconds"]
    assert native["cat_handler_p50_seconds"] < python["cat_handler_p50_seconds"]
    assert native["peak_rss_mib"] <= python["peak_rss_mib"] * RSS_RATIO_CEILING
