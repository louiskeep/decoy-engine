"""Phase D / D2c-iv: SEQ date_shift on the REAL compiled index kernel, against the Python handler.

Needs the abi-3 companion (skipped without it). Clean columns are compared with the full-frame
route, which runs the shipped Python handler on the same job. Error-bearing columns are compared
with Python-SEQ (the same run with the index companion hidden) under a quarantine config, because
the SEQ pipeline owns the fail-or-quarantine rule for both routes. There is no Unicode exemption:
the format literal and the unparseable rows below are non-ASCII, and dates are NFC-normalized
before the keyed draw on both sides.
"""

from __future__ import annotations

import datetime as dt
import random
import unicodedata
from pathlib import Path
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
from tests.parity._d2c_date_shift_data import (
    ADMITTED,
    MIXED,
    MIXED_ERROR_ROWS,
    date_payload_shape,
    date_shift_seed,
    hide_date_shift,
    run_seq_quarantine,
)


def _companion_loads() -> bool:
    try:
        load_compiled_index_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


pytestmark = pytest.mark.skipif(not _companion_loads(), reason="needs the abi-3 companion")

NATIVE = "rust_companion"
# `e` + U+0301 (decomposed) and the precomposed U+00E9: distinct byte sequences that agree only
# after NFC. Written as explicit escapes so the source editor cannot silently normalize them to
# the same literal (which would make the decomposed-vs-precomposed coverage vacuous).
DECOMPOSED = "é"
PRECOMPOSED = "é"
assert DECOMPOSED != PRECOMPOSED, "the two forms must differ in bytes to exercise normalization"
assert unicodedata.normalize("NFC", DECOMPOSED) == unicodedata.normalize("NFC", PRECOMPOSED), (
    "the two forms must agree under NFC"
)


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
        e["column"]: e["executed_backend"] for e in seq.quality_metrics["sequential_date_shift"]
    }


def _assert_python_seq_parity(
    shape: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, Any]:
    native = run_seq_quarantine(shape, tmp_path / "native.jsonl")
    hide_date_shift(monkeypatch)
    python = run_seq_quarantine(shape, tmp_path / "python.jsonl")
    assert native.row_errors == python.row_errors
    for table in python.outputs:
        report = sc.compare_strict(python.outputs[table], native.outputs[table])
        assert all(report.values()), (table, report)
    assert native.quality_metrics.get("date_preserve") == python.quality_metrics.get(
        "date_preserve"
    )
    return native, python


def test_every_admitted_configuration_matches_the_python_handler_on_every_strict_dimension(
    kernel_rows: list[int],
) -> None:
    shape = date_payload_shape()
    oracle, seq = run_full(shape), run_seq(shape)
    _assert_strict(oracle, seq)
    assert kernel_rows
    assert _backends(seq) == dict.fromkeys(ADMITTED, NATIVE)


@pytest.mark.parametrize(("lo", "hi"), [(-7, 7), (-30, 30), (1, 90), (-365, -1)])
def test_a_larger_random_column_matches_the_python_handler_on_week_and_month_scale_shifts(
    lo: int, hi: int, kernel_rows: list[int]
) -> None:
    rng = random.Random(20261013)
    base = dt.date(1990, 1, 1)
    cells: list[str | None] = []
    for i in range(4000):
        if i % 19 == 0:
            cells.append(None)
        else:
            cells.append((base + dt.timedelta(days=rng.randrange(0, 12000))).isoformat())
    shape = date_payload_shape({"d": (strs(cells), date_shift_seed(lo, hi))})
    _assert_strict(run_full(shape), run_seq(shape))
    assert kernel_rows


def test_an_all_null_column_matches_the_python_handler_and_makes_no_kernel_call(
    kernel_rows: list[int],
) -> None:
    shape = date_payload_shape({"z": (strs([None] * 7), date_shift_seed())})
    _assert_strict(run_full(shape), run_seq(shape))
    assert kernel_rows == []
    assert _backends(run_seq(shape)) == {"z": "none"}


def test_preserved_sentinels_survive_and_the_count_matches_the_python_handler(
    kernel_rows: list[int],
) -> None:
    cells = ["2024-03-05", "2000-01-01", "2023-07-19", None, "2000-01-01", "2021-11-14"]
    seed = date_shift_seed(extra=(("preserve_values", ["2000-01-01"]),))
    shape = date_payload_shape({"d": (strs(cells), seed)})
    oracle, seq = run_full(shape), run_seq(shape)
    _assert_strict(oracle, seq)
    assert oracle.quality_metrics["date_preserve"][0]["preserved_rows"] == 2
    assert _backends(seq) == {"d": NATIVE}


def test_a_decomposed_unicode_format_literal_and_non_ascii_rows_are_byte_identical(
    kernel_rows: list[int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    ]
    shape = date_payload_shape({"d": (strs(cells), date_shift_seed(-45, 45, fmt))})
    native, _python = _assert_python_seq_parity(shape, tmp_path, monkeypatch)
    assert kernel_rows
    # The non-ASCII and empty rows can never parse under this format.
    assert {5, 6, 7, 8} <= {e.row_index for e in native.row_errors}


def test_real_kernel_row_errors_match_python_seq_under_quarantine(
    kernel_rows: list[int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = date_payload_shape({"d": (strs(MIXED), date_shift_seed())})
    native, python = _assert_python_seq_parity(shape, tmp_path, monkeypatch)
    assert kernel_rows == [3]
    assert [e.row_index for e in native.row_errors] == MIXED_ERROR_ROWS
    assert native.outputs["P"].num_rows == len(MIXED) - len(MIXED_ERROR_ROWS)
    assert python.outputs["P"].column("d").type == pa.string()


def test_real_kernel_row_errors_after_a_preserved_row_keep_their_full_frame_positions(
    kernel_rows: list[int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cells = ["2000-01-01", "2024-03-05", "junk", "2000-01-01", None, "bad", "2023-07-19"]
    seed = date_shift_seed(extra=(("preserve_values", ["2000-01-01"]),))
    shape = date_payload_shape({"d": (strs(cells), seed)})
    native, _python = _assert_python_seq_parity(shape, tmp_path, monkeypatch)
    assert [e.row_index for e in native.row_errors] == [2, 5]
    assert sum(kernel_rows) == 2
