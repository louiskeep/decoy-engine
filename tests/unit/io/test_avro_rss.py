"""AT-B3: a chunked Avro read holds a bounded amount of memory (peak RSS, in a subprocess)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.io import write_avro

pytestmark = [
    pytest.mark.perf,
    pytest.mark.serial,
    pytest.mark.skipif(not Path("/proc/self/clear_refs").exists(), reason="Linux peak-RSS reset"),
]

_PROBE = textwrap.dedent(
    """
    import sys
    from decoy_engine.io import ChunkBudget, iter_avro_chunks, read_avro

    def hwm_kib():
        with open("/proc/self/status") as fh:
            return next(int(l.split()[1]) for l in fh if l.startswith("VmHWM:"))

    def rss_kib():
        with open("/proc/self/status") as fh:
            return next(int(l.split()[1]) for l in fh if l.startswith("VmRSS:"))

    path, mode = sys.argv[1], sys.argv[2]
    # Reset the peak to the current RSS, so import-time peaks do not hide the read's.
    with open("/proc/self/clear_refs", "w") as fh:
        fh.write("5")
    base = rss_kib()
    if mode == "chunked":
        budget = ChunkBudget(chunk_budget_bytes=4 << 20, max_chunk_rows=1 << 20, max_row_bytes=1 << 16)
        rows = 0
        for chunk in iter_avro_chunks(path, budget, table="t"):
            rows += chunk.num_rows
            del chunk
    else:
        table = read_avro(path, table="t")
        rows = table.num_rows
    peak = hwm_kib()
    print(rows, (peak - base) // 1024)
    """
)


def _peak_mib(path: Path, mode: str) -> tuple[int, int]:
    out = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", _PROBE, str(path), mode], capture_output=True, text=True, check=True
    )
    rows, mib = out.stdout.split()
    return int(rows), int(mib)


def test_chunked_read_peak_rss_is_bounded(tmp_path: Path) -> None:
    n = 1_000_000
    table = pa.table({"id": pa.array(range(n), pa.int64()), "s": pa.array(["v" * 60] * n)})
    assert table.nbytes > 64 << 20
    path = tmp_path / "big.avro"
    write_avro(table, path, table="t")
    del table
    rows, chunked = _peak_mib(path, "chunked")
    rows_whole, whole = _peak_mib(path, "whole")
    assert rows == rows_whole == n
    assert whole > 64, (whole, chunked)
    assert chunked < 24, chunked
