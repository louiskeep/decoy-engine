"""Phase D / D1b-ii: frozen categorical fixture shared by the baseline, routing and native suites.

`EXPECTED` and `EXPECTED_NON_STRING` are hardcoded literals captured from the pre-D1b-ii
pure-Python `categorical_array` at base c75d6f70, over input every Unicode version agrees on.
They are never derived from the output under test. The native route must reproduce them byte
for byte (the index kernel is KAT-pinned), so the same literals pin the Python baseline and
the native result. The post-Python-version normalization case is deliberately NOT here: it is
the one accepted divergence and lives in its own characterization test.

The literals (except `c_secret`) were re-pinned under F13 because job_seed keying was removed:
captured from the pandas oracle keyed on `THROWAWAY_KEY_PROVIDER`, the key `run_chain` passes.
"""

from __future__ import annotations

import datetime as dt
import decimal
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.determinism import derive_index
from decoy_engine.execution.out_of_core import _hash_run
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from tests.parity._d1a_hash_data import column_seed

NS = "ns_c"
CATEGORIES = ("red", "green", "blue", "amber", "violet")
# Includes a weight below one in ten so the CDF/searchsorted bands are not all equal width.
WEIGHTS = (5, 1, 2, 0.5, 8)

# Composed and decomposed e-acute are both present: NFC makes them one source value.
SOURCE_TEXT = [
    "alice",
    "Bob",
    "",
    "héllo",
    "é",
    "é",
    None,
    "日本語",
    "\U0001f600",
    "a" * 300,
    *[f"v{i}" for i in range(30)],
]


def source_column() -> pa.Array:
    return pa.array(SOURCE_TEXT, pa.string())


def categorical_seed(
    *,
    namespace: str | None = NS,
    categories: tuple[Any, ...] = CATEGORIES,
    weights: tuple[Any, ...] | None = None,
) -> Any:
    config: tuple[tuple[str, Any], ...] = (("categories", categories),)
    if weights is not None:
        config = (*config, ("weights", weights))
    return column_seed("categorical", namespace=namespace, provider_config=config)


def non_string_sources() -> dict[str, pa.Array]:
    """Sources the compiled index kernel does not admit, with a value-per-row mix of nulls.

    `n_tz_nat` holds the tick pandas reads back as NaT: the Python path reports it missing
    where the native path would select a category, which is why stage B declines every
    non-string source instead of trying to admit the safe ones."""
    utc = dt.timezone.utc
    return {
        "n_date": pa.array(
            [dt.date(2020, 1, 1), None, dt.date(2021, 5, 5), dt.date(1999, 12, 31)], pa.date32()
        ),
        "n_decimal": pa.array(
            [
                decimal.Decimal("1.50"),
                None,
                decimal.Decimal("2.25"),
                decimal.Decimal("-3.00"),
            ],
            pa.decimal128(10, 2),
        ),
        "n_tz": pa.array(
            [
                dt.datetime(2020, 1, 1, tzinfo=utc),
                None,
                dt.datetime(2022, 2, 2, tzinfo=utc),
                dt.datetime(2023, 3, 3, tzinfo=utc),
            ],
            pa.timestamp("us", tz="UTC"),
        ),
        "n_tz_nat": pa.array([-(2**63), 5, None, 7], pa.int64()).cast(pa.timestamp("ns", tz="UTC")),
    }


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


EXPECTED: dict[str, list[str | None]] = {
    "c_uniform": [
        "blue",
        "amber",
        "amber",
        "blue",
        "blue",
        "blue",
        None,
        "red",
        "green",
        "green",
        "red",
        "amber",
        "blue",
        "violet",
        "amber",
        "green",
        "violet",
        "violet",
        "red",
        "blue",
        "violet",
        "red",
        "amber",
        "red",
        "violet",
        "blue",
        "amber",
        "amber",
        "violet",
        "violet",
        "amber",
        "red",
        "red",
        "green",
        "green",
        "amber",
        "blue",
        "green",
        "blue",
        "red",
    ],
    "c_weighted": [
        "violet",
        "green",
        "violet",
        "blue",
        "violet",
        "violet",
        None,
        "violet",
        "violet",
        "blue",
        "violet",
        "blue",
        "amber",
        "violet",
        "violet",
        "violet",
        "red",
        "violet",
        "red",
        "violet",
        "violet",
        "red",
        "violet",
        "red",
        "violet",
        "violet",
        "red",
        "red",
        "violet",
        "red",
        "violet",
        "red",
        "violet",
        "violet",
        "red",
        "violet",
        "violet",
        "blue",
        "violet",
        "violet",
    ],
    "c_other_ns": [
        "violet",
        "green",
        "red",
        "violet",
        "red",
        "red",
        None,
        "blue",
        "amber",
        "violet",
        "green",
        "amber",
        "green",
        "blue",
        "violet",
        "blue",
        "blue",
        "blue",
        "red",
        "blue",
        "amber",
        "blue",
        "violet",
        "blue",
        "red",
        "violet",
        "amber",
        "amber",
        "green",
        "green",
        "green",
        "red",
        "red",
        "violet",
        "red",
        "amber",
        "blue",
        "blue",
        "violet",
        "red",
    ],
    "c_secret": [
        "blue",
        "violet",
        "red",
        "red",
        "violet",
        "violet",
        None,
        "blue",
        "red",
        "red",
        "blue",
        "red",
        "violet",
        "red",
        "violet",
        "red",
        "red",
        "violet",
        "green",
        "violet",
        "amber",
        "red",
        "violet",
        "violet",
        "violet",
        "red",
        "blue",
        "violet",
        "red",
        "red",
        "violet",
        "violet",
        "red",
        "violet",
        "violet",
        "blue",
        "violet",
        "red",
        "violet",
        "red",
    ],
}
EXPECTED_NON_STRING: dict[str, list[str | None]] = {
    "n_date": [
        "green",
        None,
        "red",
        "green",
    ],
    "n_decimal": [
        "blue",
        None,
        "blue",
        "green",
    ],
    "n_tz": [
        "green",
        None,
        "green",
        "green",
    ],
    "n_tz_nat": [
        None,
        "blue",
        None,
        "blue",
    ],
}
