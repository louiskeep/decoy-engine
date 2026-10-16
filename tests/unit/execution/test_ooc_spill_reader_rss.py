"""Out-of-core spill readers must not memory-map their spill files.

`pa.ipc.open_stream(str(path))` memory-maps, so every page the reader touches
stays resident as file-backed RSS until the reader closes: unbounded by the
engine's memory budget (measured ~29 GB at 100M rows). The readers now go
through `pa.OSFile`, which copies each batch through the tracked pool.

The fixture is written in THIS process; each reader API is then consumed in a
fresh subprocess that checksums every payload buffer without retaining a batch
and samples its own resident cost WHILE the reader is still open. Two
signals: the summed `Rss` of the mappings backed by the spill file
(`/proc/self/smaps`), and peak-RSS growth (`ru_maxrss`) over the pre-read
baseline. Both stay near zero for a read()-based reader and approach the file
size for a mapped one, so the test fails against the pre-fix readers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.execution.out_of_core._payload_store import (
    RawParentKeySpill,
    SpillChildKeys,
    SpillPayloadStore,
)

pytestmark = [
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/self/smaps"),
    pytest.mark.regression,
]

_SRC = Path(
    RawParentKeySpill.__module__ and sys.modules[RawParentKeySpill.__module__].__file__
).parents[3]
_ROW_BYTES = 1024
_BATCH_ROWS = 1000
_N_BATCHES = 200  # ~205 MB on disk per spill file
_TIMEOUT_S = 180
# A mapped reader leaves ~100% of the file resident; a read()-based one leaves
# a handful of batches. 25% leaves wide margin on both sides.
_MAX_RESIDENT_FRACTION = 0.25

_READER = textwrap.dedent(
    """
    import json, resource, sys
    from pathlib import Path
    import pyarrow as pa
    from decoy_engine.execution.out_of_core._payload_store import (
        RawParentKeySpill, SpillChildKeys, SpillPayloadStore,
    )

    api, path = sys.argv[1], Path(sys.argv[2])

    def mapped_rss_kb():
        total, current = 0, False
        with open("/proc/self/smaps") as fh:
            for line in fh:
                head = line.split(None, 1)[0]
                if "-" in head and ":" not in head:
                    current = line.rstrip().endswith(str(path))
                elif current and line.startswith("Rss:"):
                    total += int(line.split()[1])
        return total

    def peak_kb():
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    def checksum(batch):
        n = 0
        for col in batch.columns:
            for buf in col.buffers():
                if buf is not None:
                    n += sum(memoryview(buf)[::4096])  # touch every page
        return n

    store_schema = pa.schema([("payload", pa.binary())])
    base_peak = peak_kb()
    seen_mapped = 0
    rows = 0

    def sample():
        global seen_mapped
        seen_mapped = max(seen_mapped, mapped_rss_kb())

    if api == "iter_batches":
        store = SpillPayloadStore(path)
        store._schema = store_schema  # reading a store written by another process
        for i, (_, b) in enumerate(store.iter_batches()):
            rows += b.num_rows
            checksum(b)
            if i % 20 == 0:
                sample()
        sample()
    elif api == "raw_parent":
        spill = RawParentKeySpill.__new__(RawParentKeySpill)
        spill._path = path
        spill._writer = None
        for i, b in enumerate(spill):
            rows += b.num_rows
            checksum(b)
            if i % 20 == 0:
                sample()
    elif api == "open_reader":
        keys = SpillChildKeys.__new__(SpillChildKeys)
        keys._path = path
        keys._writer = None
        with keys.open_reader() as reader:
            for i, b in enumerate(reader):
                rows += b.num_rows
                checksum(b)
                if i % 20 == 0:
                    sample()
            sample()  # still inside the with: reader open, fully consumed
    else:
        raise SystemExit("unknown api")

    print(json.dumps({
        "rows": rows,
        "mapped_kb": seen_mapped,
        "peak_growth_kb": peak_kb() - base_peak,
    }))
    """
)


def _batches() -> list[pa.RecordBatch]:
    blob = os.urandom(_ROW_BYTES)
    batch = pa.record_batch([pa.array([blob] * _BATCH_ROWS, type=pa.binary())], names=["payload"])
    return [batch] * _N_BATCHES


@pytest.fixture(scope="module")
def spill_files(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("spill-rss")
    schema = pa.schema([("payload", pa.binary())])
    batches = _batches()

    store = SpillPayloadStore(root / "payload.arrow")
    raw = RawParentKeySpill(root / "raw.arrow", schema)
    keys = SpillChildKeys(root / "child_keys.arrow", schema)
    for b in batches:
        store.append(b)
        raw.append(b)
        keys.append(b)
    for s in (raw, keys):
        s.finalize()
    store.close()
    return {
        "iter_batches": root / "payload.arrow",
        "raw_parent": root / "raw.arrow",
        "open_reader": root / "child_keys.arrow",
    }


def _measure(api: str, path: Path) -> dict[str, int]:
    proc = subprocess.run(  # noqa: S603 -- fixed child script, test-controlled args
        [sys.executable, "-c", _READER, api, str(path)],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=False,
        # Import the engine under test, not whichever checkout an editable
        # install points at.
        env={**os.environ, "PYTHONPATH": str(_SRC)},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("api", ["iter_batches", "raw_parent", "open_reader"])
def test_spill_reader_does_not_hold_file_resident(api: str, spill_files: dict[str, Path]) -> None:
    path = spill_files[api]
    file_kb = path.stat().st_size // 1024
    assert file_kb > 150_000, "fixture too small to discriminate a mapped reader"

    out = _measure(api, path)

    assert out["rows"] == _BATCH_ROWS * _N_BATCHES
    limit = file_kb * _MAX_RESIDENT_FRACTION
    assert out["mapped_kb"] < limit, f"spill file mapped resident: {out}"
    assert out["peak_growth_kb"] < limit, f"peak RSS grew with the payload: {out}"
