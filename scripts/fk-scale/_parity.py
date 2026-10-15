"""Bounded FK-parity and row-count check of a committed D4 run.

Plan: docs/plans/2026-10-15-d4-scale-proof.md section 3.1. Same idea as the probe's
`_verify_fk_sample_committed` (`scripts/fk_memory_probe.py`), generalized in two ways
the scale run needs: per-table row counts that differ, and windows at the head, middle
and tail of each FK column so a corruption that only appears late in a long run is not
invisible to a head-only sample. Every read is one row-group-bounded window, so the
check stays O(sample) at any row count and cannot blow the cap the run just proved.

Relies on the chain layout (`_chain_layout`) and on the route preserving row order and
row count, which every admitted orphan policy does.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from _chain_layout import FK_COLUMN, KEY_COLUMN, TABLE_NAMES, read_window

_DEFAULT_SAMPLE = 2000


def table_rows(path: Path) -> int:
    return int(pq.ParquetFile(path).metadata.num_rows)


def _window_starts(rows: int, sample: int) -> list[int]:
    starts = {0, max(rows // 2 - sample // 2, 0), max(rows - sample, 0)}
    return sorted(starts)


def _check_edge(
    src: Mapping[str, Path], out: Mapping[str, Path], child: str, sample: int
) -> dict[str, Any]:
    fk_col, parent = FK_COLUMN[child]
    key_col = KEY_COLUMN[parent]
    child_rows, parent_rows = table_rows(src[child]), table_rows(src[parent])
    n = min(sample, child_rows, parent_rows)
    links = orphans = 0
    for start in _window_starts(child_rows, n):
        c_src = read_window(src[child], fk_col, start, n)
        c_msk = read_window(out[child], fk_col, start, n)
        # Child row i references parent row i % parent_rows, so the window's parents are
        # one contiguous (wrapping) window of the parent table.
        p_start = start % parent_rows
        p_src = read_window(src[parent], key_col, p_start, n)
        p_msk = read_window(out[parent], key_col, p_start, n)
        if len(c_src) != len(c_msk) or len(p_src) != len(p_msk):
            return {"status": "MISMATCH", "reason": "window_length", "links": 0}
        if p_src and p_src[0] == p_msk[0]:
            return {"status": "MISMATCH", "reason": "parent_key_unmasked", "links": 0}
        pmap = dict(zip(p_src, p_msk, strict=True))
        for s, m in zip(c_src, c_msk, strict=True):
            if s not in pmap:
                orphans += 1
            elif m != pmap[s]:
                return {"status": "MISMATCH", "reason": "broken_link", "links": 0}
            else:
                links += 1
    if links == 0:
        return {"status": "MISMATCH", "reason": "no_links_verified", "links": 0}
    return {"status": "ok", "links": links, "orphans_in_window": orphans}


def verify_committed_chain(
    src: Mapping[str, Path], committed_dir: Path, sample: int = _DEFAULT_SAMPLE
) -> dict[str, Any]:
    """Row counts per table plus FK parity on every edge of the committed output."""
    out = {t: committed_dir / f"{t}.parquet" for t in TABLE_NAMES}
    missing = [t for t, p in out.items() if not p.exists()]
    if missing:
        return {
            "rowcount_match": False,
            "rows_in": {},
            "rows_out": {},
            "parity": {"status": "MISMATCH", "reason": "missing_output", "edges": {}},
        }
    rows_in = {t: table_rows(src[t]) for t in TABLE_NAMES}
    rows_out = {t: table_rows(out[t]) for t in TABLE_NAMES}
    edges = {f"{FK_COLUMN[c][1]}->{c}": _check_edge(src, out, c, sample) for c in FK_COLUMN}
    ok = all(e["status"] == "ok" for e in edges.values())
    return {
        "rowcount_match": rows_in == rows_out,
        "rows_in": rows_in,
        "rows_out": rows_out,
        "parity": {"status": "ok" if ok else "MISMATCH", "edges": edges},
    }
