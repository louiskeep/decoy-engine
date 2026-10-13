"""Phase D / D2c-iii: SEQ bucket_perturb on the REAL compiled index kernel, against the Python
handler.

Needs the abi-3 companion (skipped without it). The oracle is the full-frame route, which runs
the shipped Python handler on the same job; the SEQ run must match it on every strict dimension.
There is no Unicode exemption: the format literal and the unparseable or preserved rows below
are non-ASCII, and the keyed offset is derived from NFC-normalized text on both sides.
"""

from __future__ import annotations

import datetime as dt
import random
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._index_ext import (
    _CompiledIndexKernel,
    load_compiled_index_kernel,
)
from tests.parity import _strict_compare as sc
from tests.parity._d2b_shapes import run_full, run_seq, strs
from tests.parity._d2c_bucket_data import (
    ADMITTED,
    bucket_payload_shape,
    bucket_seed,
)


def _companion_loads() -> bool:
    try:
        load_compiled_index_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


pytestmark = pytest.mark.skipif(not _companion_loads(), reason="needs the abi-3 companion")

NATIVE = "rust_companion"
# `e` + U+0301 (decomposed) and the precomposed U+00E9: one string after NFC.
DECOMPOSED = "é"
PRECOMPOSED = "é"


@pytest.fixture
def kernel_rows(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Row counts of every call that reaches the real compiled index kernel."""
    rows: list[int] = []
    real = _CompiledIndexKernel.derive_index_batch

    def spy(self: _CompiledIndexKernel, values: Any, **kwargs: Any) -> Any:
        rows.append(len(values))
        return real(self, values, **kwargs)

    monkeypatch.setattr(_CompiledIndexKernel, "derive_index_batch", spy)
    return rows


def _assert_strict(oracle: Any, seq: Any) -> None:
    for table in oracle.outputs:
        report = sc.compare_strict(oracle.outputs[table], seq.outputs[table])
        assert all(report.values()), (table, report)
    assert seq.quality_metrics.get("date_preserve") == oracle.quality_metrics.get("date_preserve")


def _backends(seq: Any) -> dict[str, str]:
    return {
        e["column"]: e["executed_backend"] for e in seq.quality_metrics["sequential_bucket_perturb"]
    }


def test_every_admitted_configuration_matches_the_python_handler_on_every_strict_dimension(
    kernel_rows: list[int],
) -> None:
    shape = bucket_payload_shape()
    oracle, seq = run_full(shape), run_seq(shape)
    _assert_strict(oracle, seq)
    assert kernel_rows
    assert _backends(seq) == dict.fromkeys(ADMITTED, NATIVE)


@pytest.mark.parametrize("bucket", ["week", "month", "quarter"])
def test_a_larger_random_column_matches_the_python_handler(
    bucket: str, kernel_rows: list[int]
) -> None:
    rng = random.Random(20261013)
    base = dt.date(1990, 1, 1)
    cells: list[str | None] = []
    for i in range(4000):
        if i % 19 == 0:
            cells.append(None)
        elif i % 23 == 0:
            cells.append(f"junk-{rng.randrange(99)}")
        else:
            cells.append((base + dt.timedelta(days=rng.randrange(0, 12000))).isoformat())
    shape = bucket_payload_shape({"d": (strs(cells), bucket_seed(bucket))})
    _assert_strict(run_full(shape), run_seq(shape))
    assert kernel_rows


def test_an_all_null_and_an_all_unparseable_column_match_the_python_handler(
    kernel_rows: list[int],
) -> None:
    shape = bucket_payload_shape(
        {
            "z": (strs([None] * 7), bucket_seed("month")),
            "u": (
                strs(["nope", "", "2024-99-99", "x" * 30, "31/12/2024", None, "?"]),
                bucket_seed("week"),
            ),
        }
    )
    oracle, seq = run_full(shape), run_seq(shape)
    _assert_strict(oracle, seq)
    assert kernel_rows == []
    assert _backends(seq) == {"z": "none", "u": "none"}


def test_a_near_boundary_date_declines_to_python_and_the_job_still_succeeds(
    kernel_rows: list[int],
) -> None:
    cells = ["2024-03-05", "2262-04-10", None, "1677-09-22", "2021-01-01"]
    shape = bucket_payload_shape({"b": (strs(cells), bucket_seed("month"))})
    oracle, seq = run_full(shape), run_seq(shape)
    _assert_strict(oracle, seq)
    assert kernel_rows == []
    [entry] = seq.quality_metrics["sequential_bucket_perturb"]
    assert entry["executed_backend"] == "python_scalar"
    assert entry["reason"] == "sequential_bucket_perturb_date_out_of_native_range"


@pytest.mark.parametrize("sentinel", ["2262-04-10", "1677-09-22", "9999-12-31", "2024-03-05"])
def test_preserved_sentinels_survive_and_the_count_matches_the_python_handler(
    sentinel: str, kernel_rows: list[int]
) -> None:
    cells = ["2024-03-05", "2262-04-10", "1677-09-22", None, "9999-12-31", "junk", "2023-07-19"]
    seed = bucket_seed("month", extra=(("preserve_values", [sentinel]),))
    shape = bucket_payload_shape({"b": (strs(cells), seed)})
    oracle, seq = run_full(shape), run_seq(shape)
    _assert_strict(oracle, seq)
    assert oracle.quality_metrics["date_preserve"][0]["preserved_rows"] == cells.count(sentinel)


def test_preserved_near_boundary_rows_keep_the_column_on_the_kernel(
    kernel_rows: list[int],
) -> None:
    cells = ["2024-03-05", "2262-04-10", "1677-09-22", None, "2023-07-19"]
    seed = bucket_seed("month", extra=(("preserve_values", ["2262-04-10", "1677-09-22"]),))
    shape = bucket_payload_shape({"b": (strs(cells), seed)})
    oracle, seq = run_full(shape), run_seq(shape)
    _assert_strict(oracle, seq)
    assert kernel_rows and sum(kernel_rows) == 2
    assert _backends(seq) == {"b": NATIVE}


def test_a_decomposed_unicode_format_literal_and_non_ascii_rows_are_byte_identical(
    kernel_rows: list[int],
) -> None:
    fmt = f"%Y-%m-%d {DECOMPOSED}"
    cells = [
        f"2024-03-05 {DECOMPOSED}",
        f"2024-03-06 {PRECOMPOSED}",  # precomposed literal where the format is decomposed
        f"2023-07-19 {DECOMPOSED}",
        f"2021-12-31 {DECOMPOSED}",
        f"2020-02-29 {DECOMPOSED}",
        "日本",
        DECOMPOSED,
        PRECOMPOSED,
        "",
        None,
        f"2022-05-05 {DECOMPOSED}",
        f"2022-05-05 {DECOMPOSED}",
    ]
    for bucket in ("week", "month", "quarter"):
        seed = bucket_seed(bucket, fmt)
        shape = bucket_payload_shape({"b": (strs(cells), seed)})
        _assert_strict(run_full(shape), run_seq(shape))
    assert kernel_rows


def test_non_ascii_text_beside_a_preserved_sentinel_is_byte_identical(
    kernel_rows: list[int],
) -> None:
    fmt = f"%Y-%m-%d {DECOMPOSED}"
    cells = [
        f"2024-03-05 {DECOMPOSED}",
        f"2023-07-19 {DECOMPOSED}",
        "日本語",
        f"2023-07-19 {PRECOMPOSED}",
        f"2021-01-01 {DECOMPOSED}",
    ]
    seed = bucket_seed("month", fmt, extra=(("preserve_values", ["2023-07-19"]),))
    shape = bucket_payload_shape({"b": (strs(cells), seed)})
    oracle, seq = run_full(shape), run_seq(shape)
    _assert_strict(oracle, seq)
    assert oracle.quality_metrics["date_preserve"][0]["preserved_rows"] >= 1
    assert kernel_rows


def test_a_large_string_source_declines_and_matches_the_python_handler(
    kernel_rows: list[int],
) -> None:
    source = pa.array(["2024-03-05", None, "2021-11-14"], pa.large_string())
    shape = bucket_payload_shape({"b": (source, bucket_seed("month"))})
    _assert_strict(run_full(shape), run_seq(shape))
    assert kernel_rows == []
