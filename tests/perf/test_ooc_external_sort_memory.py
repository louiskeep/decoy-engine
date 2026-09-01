"""OOC-B milestone 1, task 3: the measured (real-RSS) proof.

Paper byte-accounting cannot prove a sorter's resident memory stays capped;
only a real process's OS-reported peak RSS can. This runs
`BoundedExternalSorter` in a FRESH subprocess (allocator env pinned so the
measurement reflects real allocation, not reserved-but-untouched address
space -- see `_CAPPED_ENV` below, matching `scripts/fk_memory_probe.py`'s
own established pattern) streaming a shuffled, variable-width dataset that
is far larger than a small process memory ceiling, and asserts the
subprocess's VmHWM (the OS high-water mark, not `ru_maxrss`, which survives
`execve` and would over-report under pytest's own parent process -- same
reasoning as `tests/perf/test_out_of_core_memory_sentinel.py`) stays within
a documented envelope of that ceiling.

This test is also how a real Task 2 bug was caught and fixed, not just
verified: an earlier `BoundedExternalSorter` read run files during merge via
`pyarrow.memory_map`, which left each mapped file's touched pages resident
(counted in VmHWM) even though `pyarrow.default_memory_pool().max_memory()`
never rose to match -- a subprocess run at this scale measured peak RSS
hundreds of MB above the ceiling before that fix landed (`pa.OSFile`
instead, a buffered read that copies through the tracked pool). Paper
accounting over the pool's own counters would have looked fine; only the
real subprocess measurement caught it.

Disk-safe by design: this devbox has only a few GiB of free disk, shared
with everything else running on it, so the dataset size here is tuned to
comfortably exceed the process ceiling and force real multi-run spill
without risking the box's free space (see the sizing note below).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.perf

# Allocator pinning, BEFORE the worker process starts (glibc reads
# MALLOC_ARENA_MAX at startup, pyarrow reads ARROW_DEFAULT_MEMORY_POOL at
# import): without this, arena/allocator reservations inflate RSS far beyond
# real usage, the same reasoning `scripts/fk_memory_probe.py` documents for
# its own capped workers.
_CAPPED_ENV = {
    "ARROW_DEFAULT_MEMORY_POOL": "system",
    "MALLOC_ARENA_MAX": "2",
}

# A small process ceiling: run_bytes_cap (F_SORT * M) is deliberately tiny
# relative to the dataset below, so the sorter MUST spill and merge
# multiple runs rather than buffering everything in one pass.
_CEILING_MIB = 512

# Sized to comfortably exceed the ceiling (~1.6x the total dataset bytes
# below vs. the 512 MiB ceiling) and force a real multi-run spill (double
# digits of on-disk runs), while staying disk-safe: this devbox measured
# only ~3.3 GiB of free disk shared with everything else on it, and a
# single-pass merge's transient peak (old runs plus the growing merged
# output, both resident on disk at once) roughly doubles the raw dataset
# size. 4,000,000 rows here measures ~830 MB of Arrow-encoded data, so peak
# on-disk usage during the merge stays under ~1.7 GB, leaving over a GiB of
# headroom. A guide-scale dataset (tens of millions of rows, several GiB)
# would risk filling this shared box's disk and was rejected for that
# reason, not for a lack of proof at this smaller scale: the ratio of
# dataset size to run_bytes_cap (~830 MB over an ~80 MB run cap, roughly
# 10x) already forces the same multi-run spill and merge behavior a larger
# dataset would, just with fewer total runs.
_ROWS = 4_000_000
_MIN_WIDTH = 8
_MAX_WIDTH = 400
_BATCH_ROWS = 30_000
_SEED = 20260722

# The tested PROCESS envelope, not a raw memory_limit promise (the
# milestone plan's own framing): three runs at this dataset size measured
# peak RSS at 1.14x-1.28x of the 512 MiB ceiling (baseline pyarrow/numpy/
# decoy_engine import alone costs ~165-195 MB on this box, a fixed cost this
# small ceiling has to absorb alongside the sorter's own bounded work).
# 1.35x leaves a deliberate margin above the highest of those three
# measurements rather than the exact observed max, so ordinary run-to-run
# variance does not turn this into a flaky gate.
_ENVELOPE_FACTOR = 1.35

_TIMEOUT_S = 180

_WORKER_SCRIPT = textwrap.dedent(
    '''
    """Fresh-subprocess worker: streams a shuffled, variable-width dataset
    through BoundedExternalSorter and reports peak RSS plus proof facts as one
    JSON line on stdout. Not a committed module -- written to a temp file by
    the perf test that drives it, so it is exercised only inside a pinned,
    disposable subprocess."""

    import argparse
    import json
    import random
    import time
    from pathlib import Path

    import pyarrow as pa

    from decoy_engine.execution.out_of_core._external_sort import BoundedExternalSorter
    from decoy_engine.execution.out_of_core._reorder_budget import resolve_reorder_budgets

    ROW_NR = "__decoy_row_nr"


    def _peak_rss_mb() -> float:
        # VmHWM, not ru_maxrss: see this test module's docstring.
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024.0
        raise RuntimeError("VmHWM not found in /proc/self/status")


    def main() -> None:
        parser = argparse.ArgumentParser()
        parser.add_argument("--ceiling-mib", type=int, required=True)
        parser.add_argument("--rows", type=int, required=True)
        parser.add_argument("--min-width", type=int, required=True)
        parser.add_argument("--max-width", type=int, required=True)
        parser.add_argument("--batch-rows", type=int, required=True)
        parser.add_argument("--spill-dir", type=str, required=True)
        parser.add_argument("--seed", type=int, required=True)
        args = parser.parse_args()

        ceiling_bytes = args.ceiling_mib * 1024 * 1024
        big_disk_bytes = 200 * 1024 * 1024 * 1024  # disk is not under test here
        budgets = resolve_reorder_budgets(ceiling_bytes, big_disk_bytes)

        sorter = BoundedExternalSorter(
            spill_dir=Path(args.spill_dir),
            run_bytes_cap=budgets.run_bytes_cap,
            merge_fan_in=budgets.merge_fan_in,
            row_nr_column=ROW_NR,
        )

        rng = random.Random(args.seed)
        rows = args.rows
        batch_rows = args.batch_rows

        # A permutation of range(rows), streamed out batch_rows at a time as
        # the row_nr values. Generated once, up front, as a bounded array:
        # that is the TEST HARNESS's own input-generation cost, not the
        # sorter's resident memory contract under test.
        permutation = list(range(rows))
        rng.shuffle(permutation)

        start = time.monotonic()
        total_bytes_generated = 0
        pos = 0
        while pos < rows:
            chunk = permutation[pos : pos + batch_rows]
            width = rng.randint(args.min_width, args.max_width)
            # One random payload repeated across the chunk: cheap to
            # generate (no per-row randomness needed, only per-row DATA,
            # which pa.array still copies into its own buffer per row), while
            # varying batch-to-batch width still exercises the sorter's
            # variable-width bisection and byte-cap logic realistically.
            payload_value = bytes(rng.getrandbits(8) for _ in range(width))
            payloads = [payload_value] * len(chunk)
            batch = pa.record_batch(
                {
                    ROW_NR: pa.array(chunk, type=pa.int64()),
                    "payload": pa.array(payloads, type=pa.binary()),
                }
            )
            total_bytes_generated += batch.nbytes
            sorter.write(batch)
            pos += batch_rows
        del permutation

        sorter.finish()
        # Read AFTER finish(): finish()'s own trailing flush() of any rows
        # still buffered at end-of-stream appends to _run_paths too, so
        # checking before finish() would undercount data that never crossed
        # run_bytes_cap during write() alone.
        initial_run_count = len(sorter._run_paths)  # noqa: SLF001

        prev = -1
        count = 0
        first_value = None
        last_value = None
        sorted_ok = True
        for out_batch in sorter.iter_ordered():
            values = out_batch.column(ROW_NR).to_pylist()
            for value in values:
                if first_value is None:
                    first_value = value
                if value <= prev:
                    sorted_ok = False
                prev = value
                count += 1
            if values:
                last_value = values[-1]
        wall_time_s = time.monotonic() - start

        sorted_ok = (
            sorted_ok and count == rows and first_value == 0 and last_value == rows - 1
        )
        sorter.close()

        print(
            json.dumps(
                {
                    "ceiling_mib": args.ceiling_mib,
                    "run_bytes_cap": budgets.run_bytes_cap,
                    "merge_fan_in": budgets.merge_fan_in,
                    "rows": rows,
                    "total_bytes_generated": total_bytes_generated,
                    "initial_run_count": initial_run_count,
                    "real_spill": initial_run_count > 1,
                    "sorted_ok": sorted_ok,
                    "count": count,
                    "first_value": first_value,
                    "last_value": last_value,
                    "wall_time_s": wall_time_s,
                    "peak_rss_mb": _peak_rss_mb(),
                }
            )
        )


    if __name__ == "__main__":
        main()
    '''
)


def _run_worker(tmp_path) -> dict:
    worker_path = tmp_path / "_ooc_sort_memory_worker.py"
    worker_path.write_text(_WORKER_SCRIPT)
    spill_dir = tmp_path / "spill"
    spill_dir.mkdir()
    env = {**os.environ, **_CAPPED_ENV}
    cmd = [
        sys.executable,
        str(worker_path),
        "--ceiling-mib",
        str(_CEILING_MIB),
        "--rows",
        str(_ROWS),
        "--min-width",
        str(_MIN_WIDTH),
        "--max-width",
        str(_MAX_WIDTH),
        "--batch-rows",
        str(_BATCH_ROWS),
        "--spill-dir",
        str(spill_dir),
        "--seed",
        str(_SEED),
    ]
    proc = subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, timeout=_TIMEOUT_S, env=env
    )
    assert proc.returncode == 0, (
        f"worker subprocess failed (code {proc.returncode}):\n{proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_external_sort_peak_rss_within_envelope_while_far_exceeding_ceiling(tmp_path):
    rec = _run_worker(tmp_path)

    assert rec["sorted_ok"] is True, rec
    assert rec["count"] == _ROWS
    assert rec["real_spill"] is True, "expected multiple on-disk runs, got a single buffered run"
    assert rec["initial_run_count"] > 1

    # The dataset must genuinely exceed run_bytes_cap for this to be a real
    # spill proof, not an accidental single-buffer pass.
    assert rec["total_bytes_generated"] > rec["run_bytes_cap"] * 5

    envelope_bytes = _CEILING_MIB * _ENVELOPE_FACTOR
    assert rec["peak_rss_mb"] <= envelope_bytes, (
        f"peak RSS {rec['peak_rss_mb']:.1f} MB exceeds the {envelope_bytes:.1f} MB "
        f"envelope ({_ENVELOPE_FACTOR}x the {_CEILING_MIB} MiB process ceiling)"
    )
