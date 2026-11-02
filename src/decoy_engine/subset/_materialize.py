"""SS5: materialize the closure's survivor sets to Parquet.

Filters by row index, not by key re-join. The survivor sets ARE the subset;
re-deriving membership by key semi-join at write time would re-open
duplicate-key and null-key edge cases and could drift from the dry-run
estimate. Row-index filtering makes acceptance test 5 (dry-run ==
materialized) true BY CONSTRUCTION and preserves original row order
(verified: `filter(is_in)` is order-preserving on polars 1.42.1).

Full-frame note (GATE-1 #4): this loop holds one table at a time (tables are
processed sequentially and dropped after their write); peak memory is the
largest single table plus the key frames. Eviction plug-in point: when
`feat/fk-ri-memory-scaling`'s `_sequential.py` (Option 2 per-table
load/mask/evict) merges, this loop already composes as-is (it is already
per-table-sequential), and `collect().write_parquet(...)` can become
`sink_parquet` (streaming) behind the same function signature. Not built now.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import polars as pl

from decoy_engine.subset._errors import SubsetConfigError, SubsetInternalError
from decoy_engine.subset._keys import RI
from decoy_engine.subset._types import SubsetSource


def materialize_subset(
    *,
    sources: Mapping[str, SubsetSource],
    survivors: Mapping[str, frozenset[int]],
    output_dir: Path,
) -> tuple[tuple[str, str], ...]:
    """Write one filtered Parquet file per table under `output_dir`.

    Precondition (enforced by `_api.py`'s call ordering; asserted here
    defensively): the budget check and `verify_closure` have already passed.
    `output_dir` is created here, and only here -- refusing to reuse an
    existing non-empty directory is what makes "no partial Parquet was
    written" testable when an earlier stage raises.
    """
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SubsetConfigError(
            code="subset_output_dir_exists",
            message=f"output_dir {str(output_dir)!r} already exists and is not empty; "
            "subsetting refuses to write into a non-empty directory",
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    written: list[tuple[str, str]] = []
    for table in sorted(survivors):
        out_path = output_dir / f"{table}.parquet"
        idx = sorted(survivors[table])
        frame = (
            pl.scan_parquet(sources[table].path)
            .with_row_index(RI)
            .filter(pl.col(RI).is_in(idx))
            .drop(RI)
            .collect()
        )
        if frame.height != len(idx):
            raise SubsetInternalError(
                code="subset_materialize_count_mismatch",
                message=f"table {table!r}: wrote {frame.height} rows but expected "
                f"{len(idx)} survivors",
            )
        frame.write_parquet(out_path)
        written.append((table, str(out_path)))
    return tuple(written)
