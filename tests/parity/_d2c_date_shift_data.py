"""Phase D / D2c-iv: shared fixtures and stand-in kernels for the SEQ date_shift contract.

The two stand-ins play the compiled INDEX kernel (`derive_index_batch`) that `date_shift`
shares with categorical and bucket_perturb. `DateShiftMarkingKernel` always returns the LAST
offset of the range, so every parseable cell moves by exactly `max_days` and a test can tell the
kernel's output from the Python handler's keyed pick (consumption). `ReferenceDateShiftKernel`
reproduces the real keyed draw in Python, so its output must equal the Python handler's
(parity). Neither needs the companion.

Error-bearing runs go through `run_seq_quarantine`, which forwards a quarantine config so the
SEQ pipeline completes instead of raising; native-SEQ is compared with Python-SEQ (the same
run with the index companion hidden), never with a different enforcement boundary.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import PandasExecutionAdapter, _sequential_native
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.plan._types import ColumnSeed
from tests.parity._d1a_hash_data import column_seed
from tests.parity._d1b1_bucket_data import DMY, ISO, STAMP
from tests.parity._d2b_shapes import NAMESPACES, REGISTRY, Shape
from tests.parity._d2c_bucket_data import (
    BucketMarkingKernel,
    IndexCall,
    ReferenceBucketKernel,
)
from tests.parity._d2c_cat_data import payload_shape

DS_NS = "ns_ds"
OTHER_NS = "ns_other"
FORMAT_ERROR_REASON_LITERAL = "value is not a parseable date under date_shift"

__all__ = [
    "ADMITTED",
    "DMY",
    "DS_NS",
    "FORMAT_ERROR_REASON_LITERAL",
    "ISO",
    "STAMP",
    "DateShiftMarkingKernel",
    "IndexCall",
    "ReferenceDateShiftKernel",
    "date_payload_columns",
    "date_payload_shape",
    "date_shift_seed",
    "expect_shift",
    "hide_date_shift",
    "install_date_shift",
    "run_seq_quarantine",
]

PARSEABLE = [
    "2024-03-05",
    None,
    "2023-07-19",
    "2021-11-14",
    "2020-02-29",
    "2022-02-02",
    "2019-12-31",
]
# Parseable, unparseable (non-null) and null rows: positions 1, 4 and 6 are format errors.
MIXED = ["2024-03-05", "junk", None, "2023-07-19", "", "2021-11-14", "2024-13-45"]
MIXED_ERROR_ROWS = [1, 4, 6]


def date_shift_seed(
    min_days: int = -30,
    max_days: int = 30,
    date_format: str | None = ISO,
    *,
    namespace: str | None = DS_NS,
    extra: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    config: tuple[tuple[str, Any], ...] = (("min_days", min_days), ("max_days", max_days))
    if date_format is not None:
        config = (*config, ("date_format", date_format))
    return column_seed("date_shift", namespace=namespace, provider_config=(*config, *extra))


def _reformat(cells: Sequence[str | None], fmt: str) -> list[str | None]:
    out: list[str | None] = []
    for cell in cells:
        if cell is None:
            out.append(None)
        else:
            day = dt.date.fromisoformat(cell)
            out.append(day.strftime(fmt) if fmt != STAMP else f"{cell} 13:45:09")
    return out


def date_payload_columns() -> dict[str, tuple[pa.Array, ColumnSeed]]:
    """name -> (source column, seed): one date_shift configuration per column."""
    return {
        "s_wide": (pa.array(PARSEABLE, pa.string()), date_shift_seed(-30, 30)),
        "s_back": (pa.array(PARSEABLE, pa.string()), date_shift_seed(-400, -100)),
        "s_inverted": (pa.array(PARSEABLE, pa.string()), date_shift_seed(20, -20)),
        "s_other_ns": (pa.array(PARSEABLE, pa.string()), date_shift_seed(namespace=OTHER_NS)),
        "s_dmy": (pa.array(_reformat(PARSEABLE, DMY), pa.string()), date_shift_seed(-9, 9, DMY)),
        "s_stamp": (
            pa.array(_reformat(PARSEABLE, STAMP), pa.string()),
            date_shift_seed(-5, 5, STAMP),
        ),
    }


ADMITTED = list(date_payload_columns())

# column -> (max_days after normalizing an inverted range, date_format)
_MARKED = {
    "s_wide": (30, ISO),
    "s_back": (-100, ISO),
    "s_inverted": (20, ISO),
    "s_other_ns": (30, ISO),
    "s_dmy": (9, DMY),
    "s_stamp": (5, STAMP),
}


def marked_days(column: str) -> tuple[int, str]:
    """The shift the marking kernel produces for `column`, and its format."""
    return _MARKED[column]


def date_payload_shape(
    columns: dict[str, tuple[pa.Array, ColumnSeed]] | None = None, **kwargs: Any
) -> Shape:
    """P(payload..., hk) -> C(ck) with the date_shift columns as payload."""
    return payload_shape(columns if columns is not None else date_payload_columns(), **kwargs)


class DateShiftMarkingKernel(BucketMarkingKernel):
    """Picks the LAST offset of the range for every row, so each parseable date moves by
    exactly `max_days`."""


class ReferenceDateShiftKernel(ReferenceBucketKernel):
    """The Python keyed draw (what the real compiled kernel is KAT-pinned to): its result must
    match the Python handler's."""


def expect_shift(cells: Sequence[str | None], days: int, date_format: str) -> list[str | None]:
    """What `DateShiftMarkingKernel` makes of `cells`: every parseable cell moved by `days`.

    Null and unparseable cells come back unchanged. Computed from the calendar, not from any
    output under test."""
    out: list[str | None] = []
    for cell in cells:
        parsed = (
            pd.NaT if cell is None else pd.to_datetime(cell, format=date_format, errors="coerce")
        )
        if pd.isna(parsed):
            out.append(cell)
        else:
            out.append((parsed + pd.Timedelta(days=days)).strftime(date_format))
    return out


def install_date_shift(monkeypatch: pytest.MonkeyPatch, kernel: Any) -> Any:
    """Make the SEQ route's one-time index-kernel load return `kernel`."""
    monkeypatch.setattr(_sequential_native, "load_compiled_index_kernel", lambda: kernel)
    return kernel


def hide_date_shift(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make only the index-kernel load fail, leaving the hash and fpe loads alone."""

    def unavailable() -> None:
        raise CryptoExtensionUnavailableError("index companion hidden for a Python-only control")

    monkeypatch.setattr(_sequential_native, "load_compiled_index_kernel", unavailable)


def run_seq_quarantine(
    shape: Shape,
    out_path: Path,
    *,
    triggers: Sequence[str] = ("format_error",),
    enabled: bool = True,
    sink: Any = None,
) -> Any:
    """`run_sequential` with a quarantine config, writing the sidecar to its own `out_path`."""
    srcs = shape.sources
    return PandasExecutionAdapter().run_sequential(
        shape.plan,
        lambda t: srcs[t],
        registry=REGISTRY,
        relationship_graph=shape.graph,
        namespace_registry=NAMESPACES,
        sink=sink,
        quarantine_config={
            "enabled": enabled,
            "output_path": str(out_path),
            "triggers": list(triggers),
        },
    )
