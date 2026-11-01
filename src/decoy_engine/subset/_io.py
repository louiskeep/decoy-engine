"""Subset input access: every table file is opened through the installed policy."""

from __future__ import annotations

from decoy_engine import fileaccess
from decoy_engine.fileaccess import FileHandle, FileRequest, FileRole
from decoy_engine.subset._types import SubsetSource


def open_input(source: SubsetSource, table: str) -> FileHandle:
    """The authorized handle for one table's Parquet file. The config position is
    `subset.sources.<table>.path`, never the path itself."""
    return fileaccess.current().open_read(
        FileRequest(FileRole.SUBSET_INPUT, "read", source.path, f"subset.sources.{table}.path")
    )
