"""Fixture layout, config and verifier for the F4 mixed (generate + mask) capacity bench.

Plan: docs/plans/2026-10-17-phase-f-f4-capacity.md section 3. The job is TWO tables:

  people    GENERATED parent (resident): id = sequence 0..P-1 (generated as text), tier = categorical
  accounts  MASKED child (disk-backed Parquet, chunked lane): person_id (text FK, same type as the parent key),
            email/payload_NN hashed, note redacted

The parent key is deterministic from the generate config, so the verifier can compute
every expected child FK value without the parent being resident. The generated parent is
never masked, so a `preserve` FK stays exactly what the source child held (orphans too):

  person_id[i] = i % P                    normally
               = P + i                    on every `orphan_every`-th row (not a parent key)

Why this is not `_parity.py`: that checker needs three source tables, both edges masked
and a rewritten parent key. Here the parent is generated and unchanged, and the check is
row-specific (expected value at each sampled row), not membership.

The child writer reuses `_chain_layout`'s chunked-write helpers (one bounded chunk
resident, one row group per chunk) and its windowed row-group reader.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from _chain_layout import DEFAULT_CHUNK_ROWS, FILLER_POOL, _string_pool, read_window

PARENT = "people"
CHILD = "accounts"
FK = "person_id"
TIERS = ["gold", "silver", "bronze"]
SEED = 20261017
HASH_NS = "ns_email"
FK_NS = "ns_people"
REDACTED = "REDACTED"
_WINDOW = 2000


def payload_names(width: int) -> list[str]:
    return [f"payload_{i:02d}" for i in range(width)]


def child_schema(width: int) -> pa.Schema:
    fields = [
        pa.field(FK, pa.string()),
        pa.field("email", pa.string()),
        pa.field("note", pa.string()),
    ]
    fields += [pa.field(c, pa.string()) for c in payload_names(width)]
    return pa.schema(fields)


def expected_fk(idx: np.ndarray, parents: int, orphan_every: int) -> np.ndarray:
    """The child FK value at each row index; orphans take a value no parent holds."""
    values = idx % parents
    if orphan_every:
        orphan = (idx % orphan_every) == 0
        values = np.where(orphan, parents + idx, values)
    return values.astype(np.int64)


def write_child(
    path: Path,
    *,
    rows: int,
    parents: int,
    orphan_every: int,
    width: int,
    chunk_rows: int = DEFAULT_CHUNK_ROWS,
    seed: int = SEED,
) -> None:
    """Write the child one chunk at a time; never more than one chunk resident."""
    rng = np.random.default_rng(seed + 1)
    pool = _string_pool(rng, FILLER_POOL)
    schema = child_schema(width)
    writer = pq.ParquetWriter(path, schema)
    try:
        for start in range(0, rows, chunk_rows):
            idx = np.arange(start, min(start + chunk_rows, rows))
            cols: dict[str, Any] = {
                FK: expected_fk(idx, parents, orphan_every).astype("U").astype(object)
            }
            cols["email"] = ("u" + idx.astype("U") + "@example.com").astype(object)
            cols["note"] = ("note-" + idx.astype("U")).astype(object)
            for name in payload_names(width):
                cols[name] = pool[rng.integers(0, len(pool), size=len(idx))]
            writer.write_table(pa.table(cols, schema=schema))
    finally:
        writer.close()


def parent_columns(rich: bool) -> list[dict[str, Any]]:
    """The generated parent's columns. `rich` (F4a) adds the other eligible generation families
    (categorical, pooled faker, windowed_date) so the generate half of the mixed job exercises the
    representative plan, not just a sequence and one categorical. It deliberately OMITS null_mask:
    a null-bearing generated parent makes the mixed route decline to the unbounded full-frame path
    (F2 scope), so the bounded mixed bench keeps the parent null-free."""
    base: list[dict[str, Any]] = [
        {"name": "id", "type": "sequence", "start": 0, "step": 1},
        {"name": "tier", "type": "categorical", "categories": TIERS},
    ]
    if not rich:
        return base
    from _f4a_gen import rich_parent_columns

    return [*base, *rich_parent_columns()]


def build_config(
    child_path: Path, out_dir: Path, *, parents: int, width: int, rich_parent: bool = False
) -> dict[str, Any]:
    """The production config: generate `people`, mask `accounts`, one preserve FK edge."""
    from decoy_engine.config import PipelineConfig

    columns: list[dict[str, Any]] = [{"name": FK, "strategy": "passthrough"}]
    columns.append({"name": "email", "strategy": "hash", "namespace": HASH_NS})
    columns.append({"name": "note", "strategy": "redact"})
    columns += [
        {"name": c, "strategy": "hash", "namespace": f"ns_{c}"} for c in payload_names(width)
    ]
    raw = {
        "version": 1,
        "global_settings": {"seed": SEED},
        "sources": {CHILD: {"type": "file", "format": "parquet", "path": str(child_path)}},
        "targets": {
            PARENT: {"type": "file", "format": "csv", "path": str(out_dir / "people.out.csv")},
            CHILD: {
                "type": "file",
                "format": "parquet",
                "path": str(out_dir / "accounts.out.parquet"),
            },
        },
        "tables": [
            {
                "name": PARENT,
                "row_count": parents,
                "generate_columns": parent_columns(rich_parent),
            },
            {"name": CHILD, "columns": columns},
        ],
        "relationships": [
            {
                "parent": {"table": PARENT, "columns": ["id"]},
                "children": [{"table": CHILD, "columns": [FK]}],
                "orphan_policy": "preserve",
                "namespace": FK_NS,
            }
        ],
    }
    return PipelineConfig.model_validate(raw).model_dump()


def _starts(rows: int, sample: int) -> dict[str, int]:
    """Head, middle and tail windows, so late-run corruption is not invisible."""
    sample = min(sample, rows)
    return {"head": 0, "middle": max(0, rows // 2 - sample // 2), "tail": max(0, rows - sample)}


def _rows(path: Path) -> int:
    return int(pq.ParquetFile(path).metadata.num_rows)


def _full_fk_scan(path: Path, parents: int, orphan_every: int) -> dict[str, Any]:
    """Every committed FK value against its expected value, one row group at a time."""
    pf = pq.ParquetFile(path)
    offset = bad = orphans = 0
    for rg in range(pf.metadata.num_row_groups):
        got = pf.read_row_group(rg, columns=[FK]).column(0).cast(pa.int64()).to_numpy()
        want = expected_fk(np.arange(offset, offset + len(got)), parents, orphan_every)
        bad += int(np.count_nonzero(got != want))
        orphans += int(np.count_nonzero(got >= parents))
        offset += len(got)
    return {"rows_scanned": offset, "mismatches": bad, "orphan_rows": orphans}


def verify_committed(
    committed: Path,
    child_src: Path,
    *,
    parents: int,
    child_rows: int,
    orphan_every: int,
    width: int,
    sample: int = _WINDOW,
) -> dict[str, Any]:
    """Row-count and row-specific FK/payload parity of one committed run.

    Coverage is SAMPLED (head/middle/tail windows of `sample` rows) for the payload and
    parent checks; the FK column itself is additionally scanned in full."""
    parent_out, child_out = committed / f"{PARENT}.parquet", committed / f"{CHILD}.parquet"
    rows_out = {PARENT: _rows(parent_out), CHILD: _rows(child_out)}
    rowcount_match = rows_out == {PARENT: parents, CHILD: child_rows}

    problems: list[str] = []
    for label, start in _starts(parents, sample).items():
        ids = read_window(parent_out, "id", start, min(sample, parents))
        # The oracle's pandas echo publishes the generated sequence key as text.
        if ids != [str(i) for i in range(start, start + len(ids))]:
            problems.append(f"parent_id_{label}")
        tiers = set(read_window(parent_out, "tier", start, min(sample, parents)))
        if not tiers <= set(TIERS):
            problems.append(f"parent_tier_{label}")

    payload = ["email", *payload_names(width)]
    sampled_orphans = 0
    for label, start in _starts(child_rows, sample).items():
        n = min(sample, child_rows)
        idx = np.arange(start, start + n)
        want_int = expected_fk(idx, parents, orphan_every)
        want = [str(v) for v in want_int.tolist()]
        sampled_orphans += int(np.count_nonzero(want_int >= parents))
        if read_window(child_out, FK, start, n) != want:
            problems.append(f"child_fk_{label}")
        if read_window(child_src, FK, start, n) != want:
            problems.append(f"child_src_fk_{label}")
        if set(read_window(child_out, "note", start, n)) != {REDACTED}:
            problems.append(f"child_note_{label}")
        for col in payload:
            out_vals = read_window(child_out, col, start, n)
            src_vals = read_window(child_src, col, start, n)
            # A hash must change every sampled value and never emit an empty one.
            if any(o is None or o == "" or o == s for o, s in zip(out_vals, src_vals, strict=True)):
                problems.append(f"child_{col}_{label}")

    full = _full_fk_scan(child_out, parents, orphan_every)
    expected_orphans = len(range(0, child_rows, orphan_every)) if orphan_every else 0
    if full["mismatches"] or full["orphan_rows"] != expected_orphans:
        problems.append("child_fk_full_scan")
    return {
        "status": "ok" if rowcount_match and not problems else "mismatch",
        "coverage": "sampled",
        "sample_rows_per_window": min(sample, child_rows),
        "windows": ["head", "middle", "tail"],
        "sampled_orphan_rows": sampled_orphans,
        "fk_full_scan": {**full, "expected_orphan_rows": expected_orphans},
        "problems": problems,
        "rows_out": rows_out,
        "rowcount_match": rowcount_match,
    }


def disposition(quality_metrics: Mapping[str, Any], child: str = CHILD) -> dict[str, Any]:
    """Per-column native disposition of the chunked mask lane (F2's own evidence)."""
    from decoy_engine.execution._operator_registry import RUST_COMPANION

    ev = quality_metrics["mixed_route"]["mask_tables"][child]["chunked_route"]
    cols = ev["columns"]
    return {
        "native_admitted": bool(ev.get("native_admitted")),
        "columns": [
            {
                "column": c["column"],
                "strategy": c["strategy"],
                "executed_backend": c["executed_backend"],
                "calls": c["calls"],
            }
            for c in cols
        ],
        "chunk_count": max(int(c["calls"]) for c in cols),
        "native_hash_columns": sum(
            1 for c in cols if c["strategy"] == "hash" and c["executed_backend"] == RUST_COMPANION
        ),
        "hash_columns": sum(1 for c in cols if c["strategy"] == "hash"),
    }
