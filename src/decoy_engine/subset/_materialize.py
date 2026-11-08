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

from decoy_engine import fileaccess, reasons
from decoy_engine.fileaccess import FileRequest, FileRole, OutputTxn
from decoy_engine.fileaccess.adapters import polars as polars_adapter
from decoy_engine.subset._errors import SubsetConfigError, SubsetInternalError
from decoy_engine.subset._io import open_input
from decoy_engine.subset._keys import RI
from decoy_engine.subset._types import SubsetSource


def materialize_subset(
    *,
    sources: Mapping[str, SubsetSource],
    survivors: Mapping[str, frozenset[int]],
    output_dir: Path,
) -> tuple[OutputTxn, tuple[tuple[str, str], ...]]:
    """Stage one filtered Parquet file per table for `output_dir`; the caller publishes.

    Precondition (enforced by `_api.py`'s call ordering; asserted here
    defensively): the budget check and `verify_closure` have already passed.
    The output directory is created here, and only here -- refusing to reuse an
    existing non-empty directory is what makes "no partial Parquet was
    written" testable when an earlier stage raises. Files land in a private
    stage and become visible with one rename when the caller publishes, so a
    failure partway leaves no partial directory either.
    """
    out = fileaccess.current().output_dir(
        FileRequest(FileRole.TARGET_OUTPUT, "write", str(output_dir), "subset.output_dir")
    )
    if not out.is_empty_or_absent():
        raise SubsetConfigError(
            code=reasons.config.SUBSET_OUTPUT_DIR_EXISTS,
            message=f"output_dir {out.display!r} already exists and is not empty; "
            "subsetting refuses to write into a non-empty directory",
        )
    txn = out.begin("dir", "exclusive", label="subset")
    try:
        written: list[tuple[str, str]] = []
        for table in sorted(survivors):
            idx = sorted(survivors[table])
            with open_input(sources[table], table) as handle:
                frame = polars_adapter.collect(
                    handle,
                    polars_adapter.scan_parquet(handle)
                    .with_row_index(RI)
                    .filter(pl.col(RI).is_in(idx))
                    .drop(RI),
                )
            if frame.height != len(idx):
                raise SubsetInternalError(
                    code=reasons.runtime.SUBSET_MATERIALIZE_COUNT_MISMATCH,
                    message=f"table {table!r}: wrote {frame.height} rows but expected "
                    f"{len(idx)} survivors",
                )
            polars_adapter.write_parquet(frame, txn, f"{table}.parquet")
            written.append((table, str(Path(output_dir) / f"{table}.parquet")))
    except BaseException:
        txn.abort()
        raise
    return txn, tuple(written)
