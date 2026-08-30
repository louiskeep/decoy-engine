"""Builds the frozen W2 source shape to an on-disk Parquet file, in BATCHES.

Run ONCE per row-count tier, as a SEPARATE, non-timed, non-RSS-sampled step
before that tier's warmup + reps (`bench_driver.py --prebuild`). Generation
itself never holds more than one batch in memory (each batch is written as its
own row group and dropped), and -- critically -- it runs in a DIFFERENT process
than the one `bench_worker_native.py` is measured in, so the native route's
peak-RSS measurement reflects the streaming dispatch alone, never a source-
generation transient. The same file is reused for every rep/warmup of its tier
(mirrors the oracle worker's convention of excluding source construction from
its own timed/sampled window, just externalized one step further here since
Parquet's on-disk shape makes reuse across reps free).

Usage: python build_w2_parquet.py <n_rows> <out_path> [batch_rows]
"""

from __future__ import annotations

import sys

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

DEFAULT_BATCH_ROWS = 50_000


def build_batch(start: int, end: int) -> pa.Table:
    """One [start, end) row-range slice of the W2 shape (same per-row formula
    as `bench_worker.build_sources`), scoped to O(end - start) memory."""
    ids = np.arange(start, end, dtype=np.int64)
    ids_str = ids.astype("U")

    h_email = pa.array(np.char.add(np.char.add("user", ids_str), "@example.com"))
    h_token = pa.array(np.char.add("tok_", ids_str))
    h_uid = pa.array((100_000_000 + ids), type=pa.int64())

    pt_amount = pa.array((ids * 13) % 1_000_000, type=pa.int64())
    pt_flag = pa.array((ids % 2 == 0), type=pa.bool_())
    pt_ts = pa.array(
        (1_600_000_000_000_000 + ids * 1_000_000).astype(np.int64),
        type=pa.timestamp("us", tz="UTC"),
    )

    rd_ssn = pa.array(np.char.add(np.char.add("5", np.mod(ids, 900).astype("U")), "-11-2222"))
    rd_notes = pa.array(np.char.add("note-", ids_str))

    tr_phone = pa.array(np.char.add("512", np.mod(ids, 9000).astype("U")))
    tr_card = pa.array(np.char.add("4000", np.mod(ids, 9999).astype("U")))

    return pa.table(
        {
            "h_email": h_email,
            "h_token": h_token,
            "h_uid": h_uid,
            "pt_amount": pt_amount,
            "pt_flag": pt_flag,
            "pt_ts": pt_ts,
            "rd_ssn": rd_ssn,
            "rd_notes": rd_notes,
            "tr_phone": tr_phone,
            "tr_card": tr_card,
        }
    )


def build(n_rows: int, out_path: str, batch_rows: int = DEFAULT_BATCH_ROWS) -> None:
    writer: pq.ParquetWriter | None = None
    for start in range(0, n_rows, batch_rows):
        batch = build_batch(start, min(start + batch_rows, n_rows))
        if writer is None:
            writer = pq.ParquetWriter(out_path, batch.schema)
        writer.write_table(batch)
    if writer is not None:
        writer.close()
    else:
        # n_rows == 0: still emit a real (zero-row) file with the right schema
        # so downstream lazy readers see a consistent shape.
        pq.write_table(build_batch(0, 0), out_path)


def main() -> None:
    n_rows = int(sys.argv[1])
    out_path = sys.argv[2]
    batch_rows = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_BATCH_ROWS
    build(n_rows, out_path, batch_rows)


if __name__ == "__main__":
    main()
