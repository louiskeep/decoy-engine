"""Phase D / D2c-i: SEQ fpe on the REAL compiled kernel, against the Python handler.

Needs the abi-3 companion (skipped without it). The oracle is the full-frame route, which
runs the shipped Python fpe handler on the same job; the SEQ run must match it on every
strict dimension, including the security warnings.
"""

from __future__ import annotations

import random
import string
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._fpe_ext import _CompiledFpeKernel, load_compiled_fpe_kernel
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _strict_compare as sc
from tests.parity._d2b_shapes import run_full, run_seq, strs
from tests.parity._d2c_fpe_data import (
    NFC,
    fpe_key_shape,
    fpe_seed,
    payload_columns,
    payload_shape,
    warning_view,
)


def _companion_loads() -> bool:
    try:
        load_compiled_fpe_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


pytestmark = pytest.mark.skipif(not _companion_loads(), reason="needs the abi-3 companion")


@pytest.fixture
def kernel_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Row counts of every call that reaches the real compiled FF1 kernel."""
    rows: list[int] = []
    real = _CompiledFpeKernel.run

    def spy(self: _CompiledFpeKernel, values: Any, **kwargs: Any) -> Any:
        rows.append(len(values))
        return real(self, values, **kwargs)

    monkeypatch.setattr(_CompiledFpeKernel, "run", spy)
    return rows


def _assert_strict(oracle: pa.Table, seq: pa.Table) -> None:
    report = sc.compare_strict(oracle, seq)
    assert all(report.values()), report


def test_every_admitted_configuration_matches_the_python_handler_on_every_strict_dimension(
    kernel_calls: list[int],
) -> None:
    shape = payload_shape()
    oracle, seq = run_full(shape), run_seq(shape)
    for table in ("P", "C"):
        _assert_strict(oracle.outputs[table], seq.outputs[table])
    assert kernel_calls == [len(NFC)] * len(payload_columns())  # every fpe column ran native
    assert {e["executed_backend"] for e in seq.quality_metrics["sequential_fpe"]} == {
        "rust_companion"
    }


def test_the_security_warnings_match_the_python_handler(kernel_calls: list[int]) -> None:
    shape = payload_shape()
    oracle, seq = run_full(shape), run_seq(shape)
    assert warning_view(seq.warnings) == warning_view(oracle.warnings)
    assert {"fpe_partial_plaintext_disclosure", "fpe_join_group_active"} <= {
        w.code for w in seq.warnings
    }
    assert kernel_calls


def test_fpe_is_nfc_agnostic_composed_and_decomposed_text_is_byte_identical(
    kernel_calls: list[int],
) -> None:
    # Unlike hash, fpe does not normalize: both forms keep their out-of-charset characters
    # verbatim, so native and Python agree byte for byte.
    shape = payload_shape({"f_nfc": (strs(NFC), fpe_seed())})
    native = run_seq(shape).outputs["P"].column("f_nfc").to_pylist()
    python = run_full(shape).outputs["P"].column("f_nfc").to_pylist()
    assert [v.encode() if v is not None else None for v in native] == [
        v.encode() if v is not None else None for v in python
    ]
    assert native[0] != native[1]  # the two forms stay distinct inputs
    assert native[0][0] == "é" and native[3].startswith("日本")
    assert kernel_calls == [len(NFC)]


def test_a_larger_random_column_matches_the_python_handler(kernel_calls: list[int]) -> None:
    rng = random.Random(20261012)
    n = 3000
    alphabet = string.digits + "-/ "
    cells: list[str | None] = []
    for i in range(n):
        if i % 17 == 0:
            cells.append(None)
        elif i % 29 == 0:
            cells.append("")
        else:
            cells.append("".join(rng.choice(alphabet) for _ in range(rng.randint(8, 24))) + "7777")
    big = payload_shape({"c": (strs(cells), fpe_seed())})
    _assert_strict(run_full(big).outputs["P"], run_seq(big).outputs["P"])
    assert kernel_calls == [n]


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.REMAP])
def test_fpe_key_columns_match_the_python_oracle_and_reach_the_real_kernel(
    policy: OrphanPolicy, kernel_calls: list[int]
) -> None:
    shape = fpe_key_shape(policy)
    oracle, seq = run_full(shape), run_seq(shape)
    for table in ("A", "B", "C"):
        _assert_strict(oracle.outputs[table], seq.outputs[table])
    # A.k derives once; REMAP adds one native call per hop's orphan. B.k, a chain middle, is
    # never rederived as a scalar.
    assert sorted(kernel_calls) == ([1, 1, 2] if policy is OrphanPolicy.REMAP else [2])
    assert "sequential_fpe" in seq.quality_metrics


@pytest.mark.parametrize(
    ("cell", "code"),
    [
        ("12ab34", "fpe_unencryptable_value"),
        ("123", "fpe_unencryptable_domain"),
        ("1" * 300, "fpe_unencryptable_length"),
    ],
)
def test_the_real_kernels_row_errors_raise_the_oracles_code(cell: str, code: str) -> None:
    config = {"preserve_separators": False} if code == "fpe_unencryptable_value" else {}
    cells = ["1234567890", cell, "1234567890", None, "1234567890", "1234567890", "1234567890"]
    shape = payload_shape({"bad": (strs(cells), fpe_seed(**config))})
    with pytest.raises(StrategyError) as native:
        run_seq(shape)
    with pytest.raises(StrategyError) as python:
        run_full(shape)
    assert native.value.code == python.value.code == code
    assert cell not in str(native.value)
