"""Phase D / D2c-ii: SEQ categorical on the REAL compiled index kernel, against the Python handler.

Needs the abi-3 companion (skipped without it). The oracle is the full-frame route, which runs
the shipped Python categorical handler on the same job; the SEQ run must match it on every
strict dimension, except the one accepted divergence (Unicode normalization of post-version
codepoints, shared with hash).
"""

from __future__ import annotations

import random
import string
import unicodedata
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._index_ext import (
    _CompiledIndexKernel,
    load_compiled_index_kernel,
)
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _strict_compare as sc
from tests.parity._d2b_shapes import run_full, run_seq, strs
from tests.parity._d2c_cat_data import (
    ADMITTED,
    SRC,
    cat_key_shape,
    cat_seed,
    hide_cat,
    payload_shape,
)


def _companion_loads() -> bool:
    try:
        load_compiled_index_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


pytestmark = pytest.mark.skipif(not _companion_loads(), reason="needs the abi-3 companion")

# `a` + U+0897 + U+0316 and its reordering: one string under Unicode >= 16, two under 14.
_SENSITIVE = "a̖ࢗ"
_ORDERED = "a̖ࢗ"


@pytest.fixture
def kernel_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Row counts of every call that reaches the real compiled index kernel."""
    rows: list[int] = []
    real = _CompiledIndexKernel.derive_index_batch

    def spy(self: _CompiledIndexKernel, values: Any, **kwargs: Any) -> Any:
        rows.append(len(values))
        return real(self, values, **kwargs)

    monkeypatch.setattr(_CompiledIndexKernel, "derive_index_batch", spy)
    return rows


def _assert_strict(oracle: pa.Table, seq: pa.Table) -> None:
    report = sc.compare_strict(oracle, seq)
    assert all(report.values()), report


def test_every_admitted_configuration_matches_the_python_handler_on_every_strict_dimension(
    kernel_calls: list[int],
) -> None:
    # c_nfc holds only text every Unicode version agrees on, so it must match byte for byte.
    shape = payload_shape()
    oracle, seq = run_full(shape), run_seq(shape)
    for table in ("P", "C"):
        _assert_strict(oracle.outputs[table], seq.outputs[table])
    assert kernel_calls == [len(SRC)] * len(ADMITTED)
    assert {e["executed_backend"] for e in seq.quality_metrics["sequential_categorical"]} == {
        "rust_companion"
    }


def test_a_profile_weighted_cdf_matches_the_python_handler(kernel_calls: list[int]) -> None:
    # `from_profile` is resolved to categories + weights before execution.
    pool = [f"label{i}" for i in range(9)]
    weights = [40, 22, 15, 9, 6, 4, 2, 1.5, 0.5]
    seed = cat_seed(categories=pool, weights=weights, from_profile=True)
    rng = random.Random(7)
    cells = [None if i % 11 == 0 else f"v{rng.randrange(400)}" for i in range(2500)]
    shape = payload_shape({"p": (strs(cells), seed)})
    _assert_strict(run_full(shape).outputs["P"], run_seq(shape).outputs["P"])
    assert kernel_calls == [2500]


def test_a_larger_random_column_matches_the_python_handler(kernel_calls: list[int]) -> None:
    rng = random.Random(20261012)
    n = 5000
    cells = [
        None
        if i % 17 == 0
        else "".join(rng.choice(string.ascii_letters) for _ in range(rng.randint(0, 12)))
        for i in range(n)
    ]
    shape = payload_shape(
        {
            "u": (strs(cells), cat_seed()),
            "w": (strs(cells), cat_seed(weights=[3, 1, 4, 1, 5, 9, 2])),
        }
    )
    _assert_strict(run_full(shape).outputs["P"], run_seq(shape).outputs["P"])
    assert kernel_calls == [n, n]


def test_an_all_null_column_matches_the_python_handler_and_is_recorded_as_a_kernel_call(
    kernel_calls: list[int],
) -> None:
    shape = payload_shape({"z": (strs([None] * 7), cat_seed())})
    oracle, seq = run_full(shape), run_seq(shape)
    _assert_strict(oracle.outputs["P"], seq.outputs["P"])
    assert kernel_calls == [7]
    [entry] = seq.quality_metrics["sequential_categorical"]
    assert entry["executed_backend"] == "rust_companion"


def test_an_empty_table_runs_no_kernel_call(kernel_calls: list[int]) -> None:
    shape = payload_shape({"e": (pa.array([], pa.string()), cat_seed())})
    oracle, seq = run_full(shape), run_seq(shape)
    assert oracle.outputs["P"].equals(seq.outputs["P"])
    assert kernel_calls == []
    [entry] = seq.quality_metrics["sequential_categorical"]
    assert entry["executed_backend"] == "none"


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.REMAP])
def test_categorical_key_columns_match_the_python_oracle_and_never_reach_the_kernel(
    policy: OrphanPolicy, kernel_calls: list[int]
) -> None:
    shape = cat_key_shape(policy)
    oracle, seq = run_full(shape), run_seq(shape)
    for table in ("A", "B", "C"):
        _assert_strict(oracle.outputs[table], seq.outputs[table])
    assert kernel_calls == []
    assert "sequential_categorical" not in seq.quality_metrics


def test_a_categorical_payload_beside_a_hash_key_matches_across_an_fk_edge(
    kernel_calls: list[int],
) -> None:
    shape = payload_shape({"c": (strs(SRC), cat_seed())}, policy=OrphanPolicy.REMAP)
    oracle, seq = run_full(shape), run_seq(shape)
    for table in ("P", "C"):
        _assert_strict(oracle.outputs[table], seq.outputs[table])
    assert kernel_calls == [len(SRC)]


# The one accepted divergence: the same kernel as D1a hashing, so the same Unicode tables.


def test_post_python_version_text_follows_the_native_normalization(
    kernel_calls: list[int],
) -> None:
    pool = [f"cat{i}" for i in range(500)]
    shape = payload_shape(
        {
            "s": (
                strs([_SENSITIVE, _ORDERED, "plain", None, "é", "é", "x"]),
                cat_seed(categories=pool),
            )
        }
    )
    got = run_seq(shape).outputs["P"].column("s").to_pylist()
    # Native normalizes both spellings to one string, so they pick the same category.
    assert got[0] == got[1] and got[0] in pool
    # Text every Unicode version agrees on is unaffected.
    assert got[4] == got[5]
    assert kernel_calls == [7]


def test_the_python_handler_keeps_the_older_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if tuple(int(p) for p in unicodedata.unidata_version.split(".")) >= (16, 0, 0):
        pytest.skip("Python's Unicode tables already match the companion's")
    pool = [f"cat{i}" for i in range(500)]
    cells = [_SENSITIVE, _ORDERED]
    shape = payload_shape({"s": (strs(cells), cat_seed(categories=pool))})
    python = run_full(shape).outputs["P"].column("s").to_pylist()
    native = run_seq(shape).outputs["P"].column("s").to_pylist()
    assert python[0] != python[1]  # Python < 16 sees two different strings
    assert native[0] == native[1]  # the companion sees one
    assert python != native  # the accepted, documented divergence
    hide_cat(monkeypatch)
    assert run_seq(shape).outputs["P"].column("s").to_pylist() == python
