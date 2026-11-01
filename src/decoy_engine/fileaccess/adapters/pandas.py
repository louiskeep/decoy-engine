"""pandas adapter: CSV and Parquet reads over a policy-issued file handle."""

from __future__ import annotations

from typing import Any

import pandas as pd

from decoy_engine.fileaccess._errors import guarded
from decoy_engine.fileaccess._types import FileHandle


def _strict(handle: FileHandle) -> bool:
    return bool(getattr(handle, "strict", True))


def read_csv(handle: FileHandle, **kwargs: Any) -> pd.DataFrame:
    return guarded(handle.display, _strict(handle), pd.read_csv, handle.library_path(), **kwargs)


def read_parquet(handle: FileHandle, **kwargs: Any) -> pd.DataFrame:
    return guarded(
        handle.display, _strict(handle), pd.read_parquet, handle.library_path(), **kwargs
    )
