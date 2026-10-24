"""Bounded source readers shared by every consumer (parity E2a).

`csv_framer` frames CSV logical records by bytes before any parser sees them;
`bounded_reader` cuts Parquet and CSV sources into owned chunks within a
`ChunkBudget`. Credential handling, pricing and fingerprints stay with the
caller: these readers take a local path and a budget, nothing else.
"""

from decoy_engine.io import csv_framer
from decoy_engine.io.bounded_reader import (
    PANDAS_DEFAULT_NA_VALUES,
    BoundedReadError,
    ChunkBudget,
    CsvMalformed,
    CsvRecordExceedsMaxRowBytes,
    RowExceedsMaxRowBytes,
    csv_convert_options,
    cut_batch,
    iter_csv_chunks,
    iter_parquet_chunks,
    own_array,
)

__all__ = [
    "PANDAS_DEFAULT_NA_VALUES",
    "BoundedReadError",
    "ChunkBudget",
    "CsvMalformed",
    "CsvRecordExceedsMaxRowBytes",
    "RowExceedsMaxRowBytes",
    "csv_convert_options",
    "csv_framer",
    "cut_batch",
    "iter_csv_chunks",
    "iter_parquet_chunks",
    "own_array",
]
