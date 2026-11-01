"""Polars adapter: Parquet scans and writes over policy capabilities."""

from __future__ import annotations

import os
from typing import Any, cast

import polars as pl

from decoy_engine.fileaccess._errors import guarded
from decoy_engine.fileaccess._types import FileHandle, OutputTxn


def _strict(handle: FileHandle) -> bool:
    return bool(getattr(handle, "strict", True))


def scan_parquet(handle: FileHandle, **kwargs: Any) -> pl.LazyFrame:
    """A lazy scan of the handle's file. The handle must stay open until the frame is
    collected: under a strict policy the scan reads the descriptor."""
    return guarded(
        handle.display, _strict(handle), pl.scan_parquet, handle.library_path(), **kwargs
    )


def collect(handle: FileHandle, lazy: pl.LazyFrame) -> pl.DataFrame:
    return cast("pl.DataFrame", guarded(handle.display, _strict(handle), lazy.collect))


def collect_schema(handle: FileHandle, lazy: pl.LazyFrame) -> pl.Schema:
    return guarded(handle.display, _strict(handle), lazy.collect_schema)


def write_parquet(frame: pl.DataFrame, txn: OutputTxn, component: str) -> None:
    with os.fdopen(txn.create_file(component), "wb") as fh:
        guarded(txn.display, txn.strict, frame.write_parquet, fh)
