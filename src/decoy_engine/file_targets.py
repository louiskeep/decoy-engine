"""Which engine function writes each `FileTarget.format`.

A module of its own, beside the packages it names, so neither `decoy_engine.io` nor
`decoy_engine.profile` imports the other to build it. The file-format parity sentry checks that
every declared target format is here or in `CALLER_WRITTEN_TARGETS`, never both.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from decoy_engine import write_fixed_width
from decoy_engine.io import write_avro, write_orc

TARGET_WRITERS: dict[str, Callable[..., Any]] = {
    "avro": write_avro,
    "fixed_width": write_fixed_width,
    "orc": write_orc,
}
# Written by the caller, not by an engine writer.
CALLER_WRITTEN_TARGETS: dict[str, str] = {
    "csv": "written by the caller with pandas or pyarrow",
    "parquet": "written by the caller with pyarrow or ParquetTransactionalSink",
}

__all__ = ["CALLER_WRITTEN_TARGETS", "TARGET_WRITERS"]
