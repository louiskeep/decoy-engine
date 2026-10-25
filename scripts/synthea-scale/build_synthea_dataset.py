"""Write the Synthea-shaped FK dataset to disk-backed Parquet, chunk by chunk.

Row counts are TOTAL across the three tables; the per-table split follows a
realistic healthcare fan-out of patients : encounters : observations = 1 : 5 : 14
(each patient ~5 encounters, each encounter ~2.8 observations). The 1M/10M/100M
tiers are named presets; `--patients/--encounters/--observations` override them,
so the orchestrator's paid 100M run is this exact code path with a larger input.
No table is ever resident: peak memory is one `--chunk-rows` chunk.

    # named tier
    python scripts/synthea-scale/build_synthea_dataset.py --tier 10m --out-dir /data/synthea10m
    # explicit per-table counts (what the 100M run uses)
    python scripts/synthea-scale/build_synthea_dataset.py --out-dir /data/synthea100m \\
        --patients 5000000 --encounters 25000000 --observations 70000000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _synthea_layout import (
    DEFAULT_CHUNK_ROWS,
    TABLE_NAMES,
    column_count,
    total_columns,
    write_table_chunked,
)

DEFAULT_SEED = 20261025

# patients : encounters : observations = 1 : 5 : 14 (sums to 20 parts).
TIERS: dict[str, dict[str, int]] = {
    "1m": {"patients": 50_000, "encounters": 250_000, "observations": 700_000},
    "10m": {"patients": 500_000, "encounters": 2_500_000, "observations": 7_000_000},
    "100m": {"patients": 5_000_000, "encounters": 25_000_000, "observations": 70_000_000},
}


def build(
    out_dir: Path,
    counts: dict[str, int],
    *,
    chunk_rows: int = DEFAULT_CHUNK_ROWS,
    seed: int = DEFAULT_SEED,
) -> dict[str, Path]:
    if any(counts[t] < 1 for t in TABLE_NAMES):
        raise ValueError("every table needs at least one row")
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {t: out_dir / f"{t}.parquet" for t in TABLE_NAMES}
    for table in TABLE_NAMES:
        write_table_chunked(table, paths[table], counts, chunk_rows=chunk_rows, seed=seed)
    return paths


def _resolve_counts(args: argparse.Namespace) -> dict[str, int]:
    explicit = {
        "patients": args.patients,
        "encounters": args.encounters,
        "observations": args.observations,
    }
    if any(v is not None for v in explicit.values()):
        if any(v is None for v in explicit.values()):
            raise SystemExit("pass all of --patients/--encounters/--observations, or none")
        return {k: int(v) for k, v in explicit.items()}
    if args.tier is None:
        raise SystemExit("pass --tier {1m,10m,100m} or the three explicit counts")
    return dict(TIERS[args.tier])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--tier", choices=sorted(TIERS), help="named TOTAL-row preset")
    ap.add_argument("--patients", type=int, default=None)
    ap.add_argument("--encounters", type=int, default=None)
    ap.add_argument("--observations", type=int, default=None)
    ap.add_argument("--chunk-rows", type=int, default=DEFAULT_CHUNK_ROWS)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = ap.parse_args(argv)

    counts = _resolve_counts(args)
    t0 = time.perf_counter()
    paths = build(args.out_dir, counts, chunk_rows=args.chunk_rows, seed=args.seed)
    print(
        "BUILD_JSON "
        + json.dumps(
            {
                "tier": args.tier,
                "rows": counts,
                "total_rows": sum(counts.values()),
                "columns_per_table": {t: column_count(t) for t in TABLE_NAMES},
                "total_columns": total_columns(),
                "seed": args.seed,
                "chunk_rows": args.chunk_rows,
                "build_s": round(time.perf_counter() - t0, 2),
                "paths": {t: str(p) for t, p in paths.items()},
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
