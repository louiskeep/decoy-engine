"""Phase D / D1b-i: frozen bucket_perturb fixture shared by the baseline, routing and native suites.

The `EXPECTED_*` dicts are hardcoded literals captured from the pre-D1b-i pandas-bridge
`apply_bucket_perturb` at base 899632ce, over ASCII date formats every Unicode version agrees
on. They are never derived from the output under test. The native route must reproduce them
byte for byte (the index kernel is KAT-pinned), so the same literals pin the Python baseline and
the native result. The Unicode `date_format` suffix whose NFC form differs between Python
versions is deliberately NOT here: it is the one accepted divergence and has its own test.

The literals were re-pinned under F13 (job_seed keying was removed): captured from the pandas
oracle keyed on `THROWAWAY_KEY_PROVIDER`, the key `run_chain` passes.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.determinism import derive_index
from decoy_engine.execution.out_of_core import _hash_run
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from tests.parity._d1a_hash_data import column_seed

NS = "ns_b"
ISO = "%Y-%m-%d"
DMY = "%d/%m/%Y"
STAMP = "%Y-%m-%d %H:%M:%S"

_BASE = [
    "2024-03-05",
    None,
    "not-a-date",
    "",
    "2024-13-45",
    "9999-12-31",
    "2299-01-01",
    "1900-01-01",
    "2024-02-29",
    "2023-12-31",
    "2024-03-05",
    "2024-01-01",
    "2024-03-31",
    "2023-01-01",
    "2023-03-31",
    "2021-11-14",
    "2020-02-29",
    "2024-12-30",
    "2000-01-01",
    "1999-12-31",
]
_START = dt.date(2019, 1, 1)


def _generated(fmt: str) -> list[str]:
    return [(_START + dt.timedelta(days=37 * i)).strftime(fmt) for i in range(16)]


def iso_column() -> pa.Array:
    """Nulls, unparseable text, out-of-range sentinels and leap-year quarter edges, then dates."""
    return pa.array([*_BASE, *_generated(ISO)], pa.string())


def dmy_column() -> pa.Array:
    base = [None if v is None or not v[:1].isdigit() else _dmy(v) for v in _BASE]
    return pa.array([*base, *_generated(DMY)], pa.string())


def _dmy(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def stamp_column() -> pa.Array:
    stamped = [None if v is None else (f"{v} 13:45:09" if v[:1].isdigit() else v) for v in _BASE]
    return pa.array([*stamped, *(f"{v} 00:00:00" for v in _generated(ISO))], pa.string())


def unparseable_column() -> pa.Array:
    """Present but never parseable under ISO: the output stays a string column."""
    text = ["nope", "", "2024-99-99", None, "x" * 40, "31/12/2024"]
    return pa.array([*text, *text], pa.string())


def all_null_column() -> pa.Array:
    return pa.array([None] * 12, pa.string())


def tail_null_column() -> pa.Array:
    """Populated rows first, then nulls: with a small batch the last batches are all-null."""
    return pa.array(
        ["2024-03-05", "2023-07-19", "2024-02-29", "2022-12-31", "2021-01-01", *[None] * 7],
        pa.string(),
    )


def head_null_column() -> pa.Array:
    return pa.array(
        [*[None] * 7, "2024-03-05", "2023-07-19", "2024-02-29", "2022-12-31", "2021-01-01"],
        pa.string(),
    )


def bucket_seed(
    bucket: str = "month",
    date_format: str | None = ISO,
    *,
    namespace: str | None = NS,
    extra: tuple[tuple[str, Any], ...] = (),
) -> Any:
    config: tuple[tuple[str, Any], ...] = (("bucket", bucket),)
    if date_format is not None:
        config = (*config, ("date_format", date_format))
    return column_seed("bucket_perturb", namespace=namespace, provider_config=(*config, *extra))


def declined_sources() -> dict[str, pa.Array]:
    """Sources/formats that clear preflight and mask on Python but that native must decline."""
    return {
        "d_date32": pa.array(
            [dt.date(2024, 3, 5), None, dt.date(2020, 1, 1), dt.date(1999, 12, 31)], pa.date32()
        ),
    }


# Formats that clear preflight and mask on Python but carry a tz directive, which native declines.
TZ_COLUMNS: dict[str, tuple[str, list[str | None]]] = {
    "d_tz_z": ("%Y-%m-%d %z", ["2024-03-05 +0100", None, "2024-03-05 junk", "2021-12-31 +0100"]),
    "d_tz_name": ("%Y-%m-%d %Z", ["2024-03-05 UTC", None, "2024-03-05 junk", "2021-12-31 UTC"]),
}


def boundary_column() -> pa.Array:
    """In-range parseable stamps within one bucket of the pandas Timestamp bounds."""
    return pa.array(
        [
            "1677-09-21 12:00:00",
            "1677-09-22 00:00:00",
            "1677-10-15 08:30:00",
            "2262-04-11 12:00:00",
            "2262-04-05 00:00:00",
            "2262-03-01 00:00:00",
            "2000-01-01 00:00:00",
        ],
        pa.string(),
    )


class PythonReferenceIndexKernel:
    """A stand-in for the compiled index kernel, computed with the Python keyed draw.

    Lets the routing machinery (decision, pinning, abort, disposition) run on a box without
    the companion; the real kernel is exercised by the companion-present suites.
    `fail_on_call` makes the Nth `derive_index_batch` raise, to model a mid-run kernel fault."""

    def __init__(self, fail_on_call: int | None = None) -> None:
        self.calls = 0
        self.fail_on_call = fail_on_call

    def derive_index_batch(
        self,
        values: Any,
        *,
        mask_key: bytes,
        namespace: str,
        pool_size: int,
        native_threads: int | None = None,
    ) -> pa.Array:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("injected kernel fault")
        drawn = [
            None
            if v is None
            else derive_index(mask_key, namespace, _canonicalize_source(v), pool_size=pool_size)
            for v in values.to_pylist()
        ]
        return pa.array(drawn, pa.uint64())


def install_index_companion(
    monkeypatch: pytest.MonkeyPatch, kernel: PythonReferenceIndexKernel | None = None
) -> dict[str, Any]:
    """Make the route's one-time index-kernel load return `kernel`; counts the loads."""
    state: dict[str, Any] = {"loads": 0, "kernel": kernel or PythonReferenceIndexKernel()}

    def load() -> PythonReferenceIndexKernel:
        state["loads"] += 1
        return state["kernel"]

    monkeypatch.setattr(_hash_run, "load_compiled_index_kernel", load)
    return state


MAIN_SEEDS: tuple[tuple[str, Any], ...] = (
    ("b_week", bucket_seed("week")),
    ("b_month", bucket_seed("month")),
    ("b_quarter", bucket_seed("quarter")),
    ("b_month_ns", bucket_seed("month", namespace="ns_other")),
    ("b_dmy", bucket_seed("month", DMY)),
    ("b_stamp", bucket_seed("month", STAMP)),
)
SHAPE_SEEDS: tuple[tuple[str, Any], ...] = (
    ("b_unparse", bucket_seed("week")),
    ("b_allnull", bucket_seed("week")),
    ("b_tail_null", bucket_seed("month")),
    ("b_head_null", bucket_seed("month")),
)


def main_payload() -> pa.Table:
    iso = iso_column()
    return pa.table(
        {
            "b_week": iso,
            "b_month": iso,
            "b_quarter": iso,
            "b_month_ns": iso,
            "b_dmy": dmy_column(),
            "b_stamp": stamp_column(),
        }
    )


def shape_payload() -> pa.Table:
    """Twelve-row columns whose per-batch shape (all-null, all-unparseable) differs by batch."""
    return pa.table(
        {
            "b_unparse": unparseable_column(),
            "b_allnull": all_null_column(),
            "b_tail_null": tail_null_column(),
            "b_head_null": head_null_column(),
        }
    )
