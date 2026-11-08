"""pandas adapter: CSV and Parquet reads over a policy-issued file handle."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pandas as pd

from decoy_engine.fileaccess._errors import guarded, guarded_iter, guarded_with_parser_errors
from decoy_engine.fileaccess._types import FileHandle

# The pandas failures of a malformed file. Their text can name a cell, so only a caller that
# replaces it with its own value-free refusal may receive them (the `*_parser_errors` reads).
PARSER_ERRORS: tuple[type[Exception], ...] = (pd.errors.EmptyDataError, pd.errors.ParserError)


def _strict(handle: FileHandle) -> bool:
    return bool(getattr(handle, "strict", True))


def read_csv(handle: FileHandle, **kwargs: Any) -> pd.DataFrame:
    return guarded(handle.display, _strict(handle), pd.read_csv, handle.library_path(), **kwargs)


def read_parquet(handle: FileHandle, **kwargs: Any) -> pd.DataFrame:
    return guarded(
        handle.display, _strict(handle), pd.read_parquet, handle.library_path(), **kwargs
    )


def read_csv_chunks(handle: FileHandle, **kwargs: Any) -> Iterator[pd.DataFrame]:
    """`read_csv(chunksize=...)`, the call and each step guarded under a strict handle.

    The one sanctioned chunked pandas read on a policy handle (as `iter_batches` is for Arrow).
    """
    reader = guarded(handle.display, _strict(handle), pd.read_csv, handle.library_path(), **kwargs)
    return guarded_iter(handle.display, _strict(handle), reader)


def read_csv_parser_errors(handle: FileHandle, **kwargs: Any) -> pd.DataFrame:
    """`read_csv` that lets `PARSER_ERRORS` through a strict policy. The caller must sanitize."""
    return guarded_with_parser_errors(
        handle.display, _strict(handle), PARSER_ERRORS, pd.read_csv, handle.library_path(), **kwargs
    )


def read_csv_chunks_parser_errors(handle: FileHandle, **kwargs: Any) -> Iterator[pd.DataFrame]:
    """`read_csv_chunks` that lets `PARSER_ERRORS` through at the call and on each step."""
    reader = read_csv_parser_errors(handle, **kwargs)
    return guarded_iter(handle.display, _strict(handle), reader, PARSER_ERRORS)
