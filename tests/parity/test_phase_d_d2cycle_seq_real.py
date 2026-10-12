"""Phase D / D2-cycle: the REAL compiled companion on cyclic jobs.

Needs the abi-3 companion; skipped (not weakened) on a box without it, where the stand-in suite
(`test_phase_d_d2cycle_seq.py`) covers the same routing. The checks here are the ones only the
real kernel can show: native SEQ cycles equal the full-frame oracle on every strict dimension,
equal the SEQ Python path byte for byte, hash natively in both passes, and carry the one accepted
Unicode divergence through a whole cycle.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from typing import Any

import pytest

from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _strict_compare as sc
from tests.parity._d2a_seq_data import table_digest
from tests.parity._d2cycle_shapes import (
    chain_cycle_shape,
    composite_cycle_shape,
    mixed_topology_shape,
    run_full,
    run_seq_cycle,
    self_ref_in_cycle_shape,
    three_table_cycle_shape,
    two_table_cycle_shape,
)
from tests.parity._seq_witness import hide_companion, spy_python_hash


def _companion_loads() -> bool:
    try:
        load_compiled_crypto_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _companion_loads(),
    reason="decoy-engine-native abi-3 companion not loadable; the companion-present job covers this",
)

PRESERVE, REMAP = OrphanPolicy.PRESERVE, OrphanPolicy.REMAP
_ALL_TRUE = dict.fromkeys(sc.DIMENSIONS, True)
_SENSITIVE = "a\u0897\u0316"
_ORDERED = "a\u0316\u0897"

_CASES: list[tuple[str, Callable[[], s.Shape]]] = [
    ("two-preserve", lambda: two_table_cycle_shape(PRESERVE)),
    ("two-remap", lambda: two_table_cycle_shape(REMAP)),
    ("three-remap", lambda: three_table_cycle_shape(REMAP)),
    ("chain-remap", lambda: chain_cycle_shape(REMAP)),
    ("mixed-remap", lambda: mixed_topology_shape(REMAP)),
    ("composite-remap", lambda: composite_cycle_shape(REMAP)),
    ("self-ref-remap", lambda: self_ref_in_cycle_shape(REMAP)),
]
_IDS = [c[0] for c in _CASES]


@pytest.mark.parametrize(("name", "build"), _CASES, ids=_IDS)
def test_native_cycle_agrees_with_the_oracle_on_every_strict_dimension(
    name: str, build: Callable[[], s.Shape], monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = build()
    full = run_full(shape).outputs
    python_rows = spy_python_hash(monkeypatch)
    seq = run_seq_cycle(shape).outputs
    assert python_rows == [], name
    for table in full:
        assert sc.compare_strict(full[table], seq[table]) == _ALL_TRUE, (name, table)


@pytest.mark.parametrize(("name", "build"), _CASES, ids=_IDS)
def test_native_and_python_seq_cycles_are_byte_identical(
    name: str, build: Callable[[], s.Shape], monkeypatch: pytest.MonkeyPatch
) -> None:
    native = run_seq_cycle(build())
    with monkeypatch.context() as m:
        hide_companion(m)
        python = run_seq_cycle(build())
    assert set(native.outputs) == set(python.outputs)
    for table in python.outputs:
        assert table_digest(native.outputs[table]) == table_digest(python.outputs[table]), (
            name,
            table,
        )
    backends = {
        e["executed_backend"]
        for e in native.quality_metrics["sequential_hash"]
        if e["path"] == "scalar"
    }
    assert backends == {"rust_companion"}


def test_the_accepted_unicode_divergence_is_carried_through_a_whole_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cycle(key: str) -> dict[str, Any]:
        shape = two_table_cycle_shape(
            PRESERVE, a_id=[key, "a2"], b_mid=[key, "a2"], a_back=[key, "a2"]
        )
        return run_seq_cycle(shape).outputs

    def first_tokens(outputs: dict[str, Any]) -> set[str]:
        cells = (("A", "id"), ("A", "back"), ("B", "mid"))
        return {outputs[t][c].to_pylist()[0] for t, c in cells}

    sensitive, ordered = cycle(_SENSITIVE), cycle(_ORDERED)
    # Native NFC maps both spellings to one token, and every column of the cycle carries it.
    tokens = first_tokens(sensitive) | first_tokens(ordered)
    assert len(tokens) == 1
    assert tuple(int(p) for p in unicodedata.unidata_version.split(".")) < (16, 0, 0)
    with monkeypatch.context() as m:
        hide_companion(m)
        python = [first_tokens(cycle(_SENSITIVE)).pop(), first_tokens(cycle(_ORDERED)).pop()]
    # The Python path keeps the older normalization, so the two spellings differ there.
    assert python[0] != python[1] or unicodedata.unidata_version != "14.0.0"
    assert tokens != {python[0]}


def test_an_orphan_minted_in_the_key_pass_matches_the_one_the_table_pass_emits() -> None:
    """The key pass and the table pass must mint a REMAP orphan on the same backend, or the
    child that inherits the key-pass value would disagree with its parent's emitted value."""
    shape = two_table_cycle_shape(
        REMAP, a_id=["a1", "a2"], b_mid=["a1", _SENSITIVE], a_back=["a1", _SENSITIVE]
    )
    out = run_seq_cycle(shape).outputs
    assert out["A"]["back"].to_pylist() == out["B"]["mid"].to_pylist()
    assert out["B"]["mid"].to_pylist()[1] != _SENSITIVE


@pytest.mark.parametrize(("name", "build"), _CASES, ids=_IDS)
def test_every_remap_in_a_cycle_runs_on_the_kernel(name: str, build: Callable[[], s.Shape]) -> None:
    entries = run_seq_cycle(build()).quality_metrics["sequential_hash"]
    remaps = [e for e in entries if e["path"] == "fk_remap"]
    assert all(e["executed_backend"] == "rust_companion" for e in remaps), (name, remaps)
