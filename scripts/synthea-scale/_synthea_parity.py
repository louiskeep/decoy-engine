"""Order-independent, bounded FK-parity check of a committed Synthea run.

The D4 chain parity (`scripts/fk-scale/_parity.py`) matches a child FK to its
parent key POSITIONALLY, which assumes the route preserves row order on every
table. That holds once the reorder lane fires (>=2M parent keys), but the batch /
DuckDB joiner the smaller tiers take may reorder an intermediate table, so a
positional check reports a false MISMATCH there. This harness runs at the 1M tier
too, so it verifies the property that actually matters -- the FK join SURVIVES
masking -- without any order assumption:

  1. row counts per table are preserved (rows_in == rows_out);
  2. referential integrity: a head/middle/tail sample of each masked child FK
     resolves to some masked parent key, checked by a single row-group-bounded
     scan of the parent key column (memory bounded by the sample, not the table);
  3. masking actually happened: the sampled parent keys no longer carry the
     source-id format and keep their cardinality (no collapse to a constant).

(2)+(3) together are the honest statement of "FK parity on the edge": every child
reference still points at a parent, and both ends were genuinely masked. This is
the ONLY FK check the harness runs: the engine's built-in `post_validation`
fk_validity scan is not used, because it needs the full frame resident and is
declined on the out-of-core route this harness measures (it would divert the job
to full-frame). This sample verifies the same referential-integrity property
directly on the committed out-of-core output.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from _synthea_layout import FK_EDGE, TABLE_NAMES, read_window

_DEFAULT_SAMPLE = 2000
_SOURCE_KEY_RE = re.compile(r"^[PE]\d+$")  # the fixture's raw key format (P{i} / E{i})


def table_rows(path: Path) -> int:
    return int(pq.ParquetFile(path).metadata.num_rows)


def _window_starts(rows: int, sample: int) -> list[int]:
    return sorted({0, max(rows // 2 - sample // 2, 0), max(rows - sample, 0)})


def _sample_child_fk(out_child: Path, fk_col: str, sample: int) -> set[str]:
    rows = table_rows(out_child)
    needles: set[str] = set()
    for start in _window_starts(rows, min(sample, rows)):
        needles.update(read_window(out_child, fk_col, start, min(sample, rows)))
    return needles


def _resolve_against_parent(out_parent: Path, key_col: str, needles: set[str]) -> set[str]:
    """Which `needles` appear in the parent key column; one bounded pass, stops early."""
    found: set[str] = set()
    pf = pq.ParquetFile(out_parent)
    for rg in range(pf.metadata.num_row_groups):
        for v in pf.read_row_group(rg, columns=[key_col]).column(0).to_pylist():
            if v in needles:
                found.add(v)
        if len(found) == len(needles):
            break
    return found


def _masking_witness(out_parent: Path, key_col: str, sample: int) -> dict[str, Any]:
    vals = read_window(out_parent, key_col, 0, sample)
    still_source = sum(1 for v in vals if _SOURCE_KEY_RE.match(str(v)))
    return {"sampled": len(vals), "distinct": len(set(vals)), "still_source_format": still_source}


def _check_edge(
    out: Mapping[str, Path], child: str, fk_col: str, parent: str, key_col: str, sample: int
) -> dict[str, Any]:
    needles = _sample_child_fk(out[child], fk_col, sample)
    if not needles:
        return {"status": "MISMATCH", "reason": "no_fk_sampled"}
    found = _resolve_against_parent(out[parent], key_col, needles)
    witness = _masking_witness(out[parent], key_col, sample)
    unresolved = len(needles) - len(found)
    if unresolved:
        return {
            "status": "MISMATCH",
            "reason": "unresolved_fk",
            "unresolved": unresolved,
            "sampled_fk": len(needles),
            "masking": witness,
        }
    if witness["still_source_format"]:
        return {"status": "MISMATCH", "reason": "parent_key_unmasked", "masking": witness}
    if witness["distinct"] <= 1:
        return {"status": "MISMATCH", "reason": "parent_key_collapsed", "masking": witness}
    return {"status": "ok", "resolved_fk": len(found), "masking": witness}


def verify_committed_chain(
    src: Mapping[str, Path], committed_dir: Path, sample: int = _DEFAULT_SAMPLE
) -> dict[str, Any]:
    """Row counts per table plus order-independent FK parity on every declared edge."""
    out = {t: committed_dir / f"{t}.parquet" for t in TABLE_NAMES}
    missing = [t for t, p in out.items() if not p.exists()]
    if missing:
        return {
            "rowcount_match": False,
            "rows_in": {},
            "rows_out": {},
            "parity": {
                "status": "MISMATCH",
                "reason": "missing_output",
                "missing": missing,
                "edges": {},
            },
        }
    rows_in = {t: table_rows(src[t]) for t in TABLE_NAMES}
    rows_out = {t: table_rows(out[t]) for t in TABLE_NAMES}
    edges = {
        f"{parent}->{child}": _check_edge(out, child, fk_col, parent, key_col, sample)
        for child, (fk_col, parent, key_col) in FK_EDGE.items()
    }
    ok = all(e["status"] == "ok" for e in edges.values())
    return {
        "rowcount_match": rows_in == rows_out,
        "rows_in": rows_in,
        "rows_out": rows_out,
        "parity": {"status": "ok" if ok else "MISMATCH", "edges": edges},
    }
