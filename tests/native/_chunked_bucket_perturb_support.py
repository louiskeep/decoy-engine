"""Shared builders for the C2 (bucket_perturb on the chunked route) tests.

The oracle leg is the same table run through `run_mask_chunked` with a still-vetoed
`date_shift` column beside the bucket_perturb one (`force_oracle`), so the schema rule
applies to both legs and the comparison is native-chunked vs oracle-chunked, byte for byte.
"""

from __future__ import annotations

import calendar
from typing import Any

import pyarrow as pa

from tests.native._b8_support import FORCE, Run, assert_same_as_oracle, run_one, run_pair, with_force
from tests.native._chunked_entry_support import (
    make_config,
    passthrough,
)

__all__ = [
    "FORCE",
    "Run",
    "assert_same_as_oracle",
    "bp_col",
    "date_value",
    "expected_derive_sizes",
    "make_config",
    "passthrough",
    "run_one",
    "run_pair",
    "source",
    "spy_index_kernel",
    "with_force",
]

NAMESPACE = "ns_d"


def bp_col(
    name: str = "d",
    *,
    bucket: str = "month",
    date_format: str | None = "%Y-%m-%d",
    namespace: str | None = NAMESPACE,
    **extra: Any,
) -> dict[str, Any]:
    """A bucket_perturb column. `date_format=None` omits the key (autodetect)."""
    cfg: dict[str, Any] = {"bucket": bucket}
    if date_format is not None:
        cfg["date_format"] = date_format
    col: dict[str, Any] = {"name": name, "strategy": "bucket_perturb", "provider_config": cfg}
    if namespace is not None:
        col["namespace"] = namespace
    col.update(extra)
    return col


def date_value(i: int) -> str:
    """A valid ISO date that varies in year, month and day (leap February included)."""
    year = (2019, 2020, 2021, 2024)[i % 4]
    month = 1 + (i * 5) % 12
    day = 1 + (i * 11) % calendar.monthrange(year, month)[1]
    return f"{year}-{month:02d}-{day:02d}"


def source(values: list[str | None], *, typ: pa.DataType | None = None) -> pa.Table:
    """A table with the bucket_perturb source `d` and an integer passthrough `p`."""
    return pa.table(
        {
            "d": pa.array(values, typ or pa.string()),
            "p": pa.array(list(range(len(values))), pa.int64()),
        }
    )


def expected_derive_sizes(values: list[str | None], bucket: str, fmt: str = "%Y-%m-%d") -> list[int]:
    """The distinct bucket sizes among the PARSEABLE values of one chunk, computed from the
    calendar alone (not through the code under test), sorted."""
    import datetime as dt

    sizes: set[int] = set()
    for value in values:
        if value is None:
            continue
        try:
            day = dt.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
        if bucket == "week":
            sizes.add(7)
        elif bucket == "month":
            sizes.add(calendar.monthrange(day.year, day.month)[1])
        else:
            first = 3 * ((day.month - 1) // 3) + 1
            sizes.add(sum(calendar.monthrange(day.year, m)[1] for m in range(first, first + 3)))
    return sorted(sizes)


class _SpyKernel:
    """Wraps the real index kernel; records each `derive_index_batch` call."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.calls: list[dict[str, Any]] = []

    def derive_index_batch(self, values: Any, **kwargs: Any) -> Any:
        self.calls.append({"rows": len(values), **kwargs})
        return self._real.derive_index_batch(values, **kwargs)

    def pool_sizes(self, namespace: str = NAMESPACE) -> list[int]:
        return [c["pool_size"] for c in self.calls if c["namespace"] == namespace]


def spy_index_kernel(monkeypatch: Any) -> _SpyKernel:
    """Replace the preflight index-kernel loader with one that returns a recording
    wrapper around the real kernel. Returns the wrapper (one instance for the run)."""
    from decoy_engine.execution.native import _dispatch

    spy = _SpyKernel(_dispatch.load_compiled_index_kernel())
    monkeypatch.setattr(_dispatch, "load_compiled_index_kernel", lambda: spy)
    return spy
