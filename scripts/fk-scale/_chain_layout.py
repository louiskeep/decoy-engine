"""Shared on-disk layout of the D4 parent -> child -> grandchild FK chain.

Plan: docs/plans/2026-10-15-d4-scale-proof.md section 3.2. Used by
`build_fk_chain.py` (writer) and `bench_worker_fk.py` (parity check), so the key
formula lives in exactly one place.

Established pattern, not a new one: this is `tests/perf_fixtures/fk_relational.py`'s
`write_large_fk_chain` (row-index key formula, one bounded chunk resident at a
time) generalized to PER-TABLE row counts. The original takes one `rows` for all
three tables; D4 needs parent+child growth (the plan's tier shapes are 2M/3.5M/4.5M
etc.), so the FK target is each table's PARENT row count, not its own.

Layout, positional so no table needs its parent resident to build a valid FK:
  parent     id        = p{i}                       i in [0, P)
  child      id        = c{i}                       i in [0, C)
             parent_id = p{i % P}   (orphan_p{i} on every orphan_every-th row)
  grandchild child_id  = c{i % C}   (orphan_c{i} on every orphan_every-th row)
Referential integrity holds by construction, and the first `s` FK rows resolve
inside the first `s` parent rows, which is what keeps the parity check O(sample).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

TABLE_NAMES = ("parent", "child", "grandchild")
# (fk column, referenced table) per child table; the key column of a table is below.
FK_COLUMN = {"child": ("parent_id", "parent"), "grandchild": ("child_id", "child")}
KEY_COLUMN = {"parent": "id", "child": "id"}
KEY_PREFIX = {"parent": "p", "child": "c"}
ORPHAN_PREFIX = {"parent": "orphan_p", "child": "orphan_c"}

# Pool the filler columns draw from: enough distinct strings to be realistic, small
# enough to generate fast at tens of millions of rows (same figure as the probe).
FILLER_POOL = 4096
_SEED_OFFSET = {"parent": 0, "child": 1, "grandchild": 2}
DEFAULT_CHUNK_ROWS = 50_000


def payload_names(width: int) -> list[str]:
    return [f"payload_{i:02d}" for i in range(width)]


def table_schema(name: str, width: int) -> pa.Schema:
    fields: list[pa.Field] = []
    if name in KEY_COLUMN:
        fields.append(pa.field(KEY_COLUMN[name], pa.string()))
    if name in FK_COLUMN:
        fields.append(pa.field(FK_COLUMN[name][0], pa.string()))
    fields.extend(pa.field(c, pa.string()) for c in payload_names(width))
    return pa.schema(fields)


def keys_from_idx(prefix: str, idx: np.ndarray) -> np.ndarray:
    """Vectorized `{prefix}{i}` keys for an arbitrary row-index array."""
    return (prefix + idx.astype("U")).astype(object)


def _fk_values(
    name: str, idx: np.ndarray, counts: Mapping[str, int], orphan_every: int
) -> np.ndarray:
    _, ref_table = FK_COLUMN[name]
    keys = keys_from_idx(KEY_PREFIX[ref_table], idx % counts[ref_table])
    if orphan_every:
        is_orphan = (idx % orphan_every) == 0
        if is_orphan.any():
            keys = keys.copy()
            keys[is_orphan] = keys_from_idx(ORPHAN_PREFIX[ref_table], idx[is_orphan])
    return keys


def _string_pool(rng: np.random.Generator, n: int, width: int = 12) -> np.ndarray:
    alphabet = np.frombuffer(b"abcdefghijklmnopqrstuvwxyz0123456789", dtype="S1")
    picks = rng.integers(0, len(alphabet), size=(n, width))
    return np.array([b"".join(row).decode("ascii") for row in alphabet[picks]], dtype=object)


def _chunk(
    name: str,
    start: int,
    length: int,
    counts: Mapping[str, int],
    width: int,
    orphan_every: int,
    rng: np.random.Generator,
    pool: np.ndarray,
) -> pa.Table:
    idx = np.arange(start, start + length)
    cols: dict[str, object] = {}
    if name in KEY_COLUMN:
        cols[KEY_COLUMN[name]] = keys_from_idx(KEY_PREFIX[name], idx)
    if name in FK_COLUMN:
        cols[FK_COLUMN[name][0]] = _fk_values(name, idx, counts, orphan_every)
    for col in payload_names(width):
        cols[col] = pool[rng.integers(0, len(pool), size=length)]
    return pa.table(cols, schema=table_schema(name, width))


def write_table_chunked(
    name: str,
    path: Path,
    counts: Mapping[str, int],
    *,
    width: int,
    orphan_frac: float,
    chunk_rows: int = DEFAULT_CHUNK_ROWS,
    seed: int,
) -> None:
    """Write one table `chunk_rows` rows at a time; never holds more than one chunk.

    One Parquet row group per chunk, which the parity reader relies on to read a
    row window without scanning the file.
    """
    orphan_every = round(1 / orphan_frac) if orphan_frac > 0 else 0
    rng = np.random.default_rng(seed + _SEED_OFFSET[name])
    pool = _string_pool(rng, FILLER_POOL)
    writer = pq.ParquetWriter(path, table_schema(name, width))
    try:
        for start in range(0, counts[name], chunk_rows):
            length = min(chunk_rows, counts[name] - start)
            writer.write_table(_chunk(name, start, length, counts, width, orphan_every, rng, pool))
    finally:
        writer.close()


def read_window(path: Path, column: str, start: int, n: int) -> list[str]:
    """`n` values of one column from row `start` (wrapping past the end), reading only
    the row groups that overlap the window."""
    pf = pq.ParquetFile(path)
    total = pf.metadata.num_rows
    out: list[str] = []
    for lo, hi in _segments(start % total, min(n, total), total):
        out.extend(_read_segment(pf, column, lo, hi))
    return out


def _segments(start: int, n: int, total: int) -> list[tuple[int, int]]:
    end = start + n
    if end <= total:
        return [(start, end)]
    return [(start, total), (0, end - total)]


def _read_segment(pf: pq.ParquetFile, column: str, lo: int, hi: int) -> list[str]:
    values: list[str] = []
    offset = 0
    for rg in range(pf.metadata.num_row_groups):
        rg_rows = pf.metadata.row_group(rg).num_rows
        if offset + rg_rows > lo and offset < hi:
            col = pf.read_row_group(rg, columns=[column]).column(0).to_pylist()
            values.extend(col[max(lo - offset, 0) : hi - offset])
        offset += rg_rows
        if offset >= hi:
            break
    return values
