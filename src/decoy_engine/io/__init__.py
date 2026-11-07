"""Bounded source readers and file-format writers shared by every consumer (parity E2a).

`csv_framer` frames CSV logical records by bytes before any parser sees them;
`bounded_reader` cuts Parquet and CSV sources into owned chunks within a
`ChunkBudget`. `avro_io` and `orc_io` read Avro and ORC files the same way (bounded chunks, or
the whole file) and write them atomically. Credential handling, pricing and fingerprints stay
with the caller: these readers take a local path and a budget, nothing else.
"""

from collections.abc import Callable
from typing import Any

from decoy_engine.io import csv_framer
from decoy_engine.io.avro_io import iter_avro_chunks, read_avro, write_avro
from decoy_engine.io.bounded_reader import (
    PANDAS_DEFAULT_NA_VALUES,
    BoundedReadError,
    ChunkBudget,
    CsvMalformed,
    CsvRecordExceedsMaxRowBytes,
    RowExceedsMaxRowBytes,
    UnsupportedColumnType,
    csv_convert_options,
    cut_batch,
    iter_csv_chunks,
    iter_parquet_chunks,
    own_array,
)
from decoy_engine.io.format_errors import (
    AvroBlockTooLarge,
    AvroMalformed,
    AvroSchemaUnsupported,
    OrcMalformed,
    OrcTypeUnsupported,
    TargetColumnTypeUnsupported,
    TargetSchemaMismatch,
)
from decoy_engine.io.orc_io import iter_orc_chunks, read_orc, write_orc
from decoy_engine.profile import write_fixed_width

# The engine's writer for each `FileTarget.format` it writes itself. CSV and Parquet targets
# are written by the caller (pandas / pyarrow `write_table`, or `ParquetTransactionalSink`).
TARGET_WRITERS: dict[str, Callable[..., Any]] = {
    "avro": write_avro,
    "fixed_width": write_fixed_width,
    "orc": write_orc,
}
CALLER_WRITTEN_TARGETS: dict[str, str] = {
    "csv": "written by the caller with pandas or pyarrow",
    "parquet": "written by the caller with pyarrow or ParquetTransactionalSink",
}

__all__ = [
    "CALLER_WRITTEN_TARGETS",
    "PANDAS_DEFAULT_NA_VALUES",
    "TARGET_WRITERS",
    "AvroBlockTooLarge",
    "AvroMalformed",
    "AvroSchemaUnsupported",
    "BoundedReadError",
    "ChunkBudget",
    "CsvMalformed",
    "CsvRecordExceedsMaxRowBytes",
    "OrcMalformed",
    "OrcTypeUnsupported",
    "RowExceedsMaxRowBytes",
    "TargetColumnTypeUnsupported",
    "TargetSchemaMismatch",
    "UnsupportedColumnType",
    "csv_convert_options",
    "csv_framer",
    "cut_batch",
    "iter_avro_chunks",
    "iter_csv_chunks",
    "iter_orc_chunks",
    "iter_parquet_chunks",
    "own_array",
    "read_avro",
    "read_orc",
    "write_avro",
    "write_orc",
]
