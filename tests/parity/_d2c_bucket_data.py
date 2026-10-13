"""Phase D / D2c-iii: shared fixtures and stand-in kernels for the SEQ bucket_perturb contract.

Two stand-ins play the compiled INDEX kernel (`derive_index_batch`), and they answer different
questions. `BucketMarkingKernel` always returns the LAST offset of the bucket, so a test can tell
the kernel's output from the Python handler's keyed pick (consumption). `ReferenceBucketKernel`
reproduces the real keyed draw in Python, so its output must equal the Python handler's
(parity). Neither needs the companion.

Shapes reuse the categorical payload builder: bucket_perturb columns are payload, the FK key is
hashed.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.determinism import derive_index
from decoy_engine.execution import _sequential_native
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.plan._types import ColumnSeed
from tests.parity._d1b1_bucket_data import (
    DMY,
    ISO,
    NS,
    STAMP,
    bucket_seed,
    dmy_column,
    iso_column,
    stamp_column,
)
from tests.parity._d2b_shapes import Shape
from tests.parity._d2c_cat_data import payload_shape

BUCKET_NS = NS
OTHER_NS = "ns_other"

__all__ = [
    "BUCKET_NS",
    "DMY",
    "ISO",
    "STAMP",
    "BucketMarkingKernel",
    "IndexCall",
    "ReferenceBucketKernel",
    "bucket_payload_columns",
    "bucket_payload_shape",
    "bucket_seed",
    "expect_last_offset",
    "hide_bucket",
    "install_bucket",
]


def bucket_payload_columns() -> dict[str, tuple[pa.Array, ColumnSeed]]:
    """name -> (source column, seed): one bucket_perturb configuration per column."""
    return {
        "b_week": (iso_column(), bucket_seed("week")),
        "b_month": (iso_column(), bucket_seed("month")),
        "b_quarter": (iso_column(), bucket_seed("quarter")),
        "b_other_ns": (iso_column(), bucket_seed("month", namespace=OTHER_NS)),
        "b_dmy": (dmy_column(), bucket_seed("month", DMY)),
        "b_stamp": (stamp_column(), bucket_seed("month", STAMP)),
    }


ADMITTED = list(bucket_payload_columns())


def bucket_payload_shape(
    columns: dict[str, tuple[pa.Array, ColumnSeed]] | None = None, **kwargs: Any
) -> Shape:
    """P(payload..., hk) -> C(ck) with the bucket_perturb columns as payload."""
    return payload_shape(columns if columns is not None else bucket_payload_columns(), **kwargs)


@dataclass
class IndexCall:
    rows: int
    arrow_type: pa.DataType
    namespace: str
    pool_size: int
    mask_key: bytes


@dataclass
class _RecordingKernel:
    fail_on_call: int | None = None
    calls: list[IndexCall] = field(default_factory=list)

    def _record(self, values: Any, mask_key: bytes, namespace: str, pool_size: int) -> None:
        self.calls.append(IndexCall(len(values), values.type, namespace, pool_size, mask_key))
        if self.fail_on_call == len(self.calls):
            raise RuntimeError("injected kernel fault")


@dataclass
class BucketMarkingKernel(_RecordingKernel):
    """A `derive_index_batch` stand-in that picks the LAST day of the bucket for every row."""

    def derive_index_batch(
        self,
        values: Any,
        *,
        mask_key: bytes,
        namespace: str,
        pool_size: int,
        native_threads: int | None = None,
    ) -> pa.Array:
        self._record(values, mask_key, namespace, pool_size)
        valid = values.is_valid().to_numpy(zero_copy_only=False)
        marker = np.full(len(values), pool_size - 1, dtype=np.uint64)
        return pa.array(marker, mask=~valid, type=pa.uint64())


@dataclass
class ReferenceBucketKernel(_RecordingKernel):
    """A `derive_index_batch` stand-in computed with the Python keyed draw (what the real
    compiled kernel is KAT-pinned to), so its result must match the Python handler's."""

    def derive_index_batch(
        self,
        values: Any,
        *,
        mask_key: bytes,
        namespace: str,
        pool_size: int,
        native_threads: int | None = None,
    ) -> pa.Array:
        self._record(values, mask_key, namespace, pool_size)
        drawn = [
            None
            if v is None
            else derive_index(mask_key, namespace, _canonicalize_source(v), pool_size=pool_size)
            for v in values.to_pylist()
        ]
        return pa.array(drawn, pa.uint64())


def expect_last_offset(
    cells: list[str | None],
    bucket: str,
    date_format: str,
    *,
    skip: frozenset[str] = frozenset(),
) -> list[str | None]:
    """What `BucketMarkingKernel` makes of `cells`: the last day of each parseable cell's bucket.

    Null, unparseable and `skip`ped cells come back unchanged. Computed from the calendar, not
    from any output under test."""
    out: list[str | None] = []
    for cell in cells:
        parsed = (
            pd.NaT
            if cell is None or cell in skip
            else pd.to_datetime(cell, format=date_format, errors="coerce")
        )
        if pd.isna(parsed):
            out.append(cell)
            continue
        day = parsed.date()
        if bucket == "week":
            last = day - dt.timedelta(days=day.weekday()) + dt.timedelta(days=6)
        elif bucket == "month":
            last = day.replace(day=calendar.monthrange(day.year, day.month)[1])
        else:
            end_month = (day.month - 1) // 3 * 3 + 3
            last = dt.date(day.year, end_month, calendar.monthrange(day.year, end_month)[1])
        out.append(last.strftime(date_format))
    return out


def install_bucket(monkeypatch: pytest.MonkeyPatch, kernel: Any) -> Any:
    """Make the SEQ route's one-time index-kernel load return `kernel`."""
    monkeypatch.setattr(_sequential_native, "load_compiled_index_kernel", lambda: kernel)
    return kernel


def hide_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make only the index-kernel load fail, leaving the hash and fpe loads alone."""

    def unavailable() -> None:
        raise CryptoExtensionUnavailableError("index companion hidden for a Python-only control")

    monkeypatch.setattr(_sequential_native, "load_compiled_index_kernel", unavailable)
