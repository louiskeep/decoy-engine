"""Write the D4 three-table FK chain to disk-backed Parquet, chunk by chunk.

Plan: docs/plans/2026-10-15-d4-scale-proof.md section 3.2. Row counts are PER TABLE
and the plan's tier totals are the SUM across the chain (10M tier = 2M/3.5M/4.5M,
100M = 20M/35M/45M, 200M = 40M/70M/90M); parent+child growth means the three
counts differ. No table is ever resident: peak memory is one `--chunk-rows` chunk.

    python scripts/fk-scale/build_fk_chain.py --out-dir /data/fk100 \\
        --parent-rows 20000000 --child-rows 35000000 --grandchild-rows 45000000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _chain_layout import (
    DEFAULT_CHUNK_ROWS,
    TABLE_NAMES,
    payload_names,
    write_table_chunked,
)

# ~15 columns per table: key (+ FK) + 13 payload gives 14/15/14.
DEFAULT_WIDTH = 13
DEFAULT_SEED = 20261015


def build(
    out_dir: Path,
    counts: dict[str, int],
    *,
    width: int = DEFAULT_WIDTH,
    orphan_frac: float = 0.0,
    chunk_rows: int = DEFAULT_CHUNK_ROWS,
    seed: int = DEFAULT_SEED,
) -> dict[str, Path]:
    """Write the chain under `out_dir`; returns `{table: parquet path}`."""
    if not 0.0 <= orphan_frac < 1.0:
        raise ValueError("orphan_frac must be in [0, 1)")
    if any(counts[t] < 1 for t in TABLE_NAMES):
        raise ValueError("every table needs at least one row")
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {t: out_dir / f"{t}.parquet" for t in TABLE_NAMES}
    for table in TABLE_NAMES:
        write_table_chunked(
            table,
            paths[table],
            counts,
            width=width,
            orphan_frac=orphan_frac,
            chunk_rows=chunk_rows,
            seed=seed,
        )
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--parent-rows", type=int, required=True)
    ap.add_argument("--child-rows", type=int, required=True)
    ap.add_argument("--grandchild-rows", type=int, required=True)
    ap.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="payload columns per table")
    ap.add_argument("--orphan-frac", type=float, default=0.0)
    ap.add_argument("--chunk-rows", type=int, default=DEFAULT_CHUNK_ROWS)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = ap.parse_args(argv)
    counts = {
        "parent": args.parent_rows,
        "child": args.child_rows,
        "grandchild": args.grandchild_rows,
    }
    t0 = time.perf_counter()
    paths = build(
        args.out_dir,
        counts,
        width=args.width,
        orphan_frac=args.orphan_frac,
        chunk_rows=args.chunk_rows,
        seed=args.seed,
    )
    print(
        "BUILD_JSON "
        + json.dumps(
            {
                "rows": counts,
                "total_rows": sum(counts.values()),
                "width": args.width,
                "payload_columns": payload_names(args.width),
                "orphan_frac": args.orphan_frac,
                "seed": args.seed,
                "build_s": round(time.perf_counter() - t0, 2),
                "paths": {t: str(p) for t, p in paths.items()},
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
