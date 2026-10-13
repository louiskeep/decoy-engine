"""Phase D / FK-key-native: the REAL compiled companions across the fpe-key SEQ shapes.

Needs the abi-3 companion; skipped (not weakened) on a box without it, where the stand-in suite
(`test_phase_d_fpe_key_seq.py`) covers the same routing. These tests check what only the real
kernels can show: native SEQ output equals the full-frame oracle and the SEQ Python route on every
strict dimension for each shape and policy, the REMAP orphan key equals what the Python parent
handler mints (so the FK join holds), and the kernel's tweak sensitivity is real, which is what
makes a wrong tweak observable.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.errors import DecoyError
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)
from decoy_engine.execution.native._fpe_ext import load_compiled_fpe_kernel
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _strict_compare as sc
from tests.parity._d2a_seq_data import RecordingSink, table_digest
from tests.parity._d2c_fpe_data import hide_fpe, install_fpe
from tests.parity._seq_witness import hide_companion, spy_python_hash
from tests.parity.test_phase_d_fpe_key_seq import (
    _CELL_IDS,
    _CELLS,
    _ORPHAN,
    _PARENT_CFG,
    _ROWS,
    STRATEGIES,
    _child_tweak_mutant,
    _edge_shape,
    _keyed,
    _spy_python_fpe,
)

REMAP, PRESERVE, FAIL = OrphanPolicy.REMAP, OrphanPolicy.PRESERVE, OrphanPolicy.FAIL
_ALL_TRUE = dict.fromkeys(sc.DIMENSIONS, True)


def _companions_load() -> bool:
    try:
        load_compiled_crypto_kernel()
        load_compiled_fpe_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _companions_load(),
    reason="decoy-engine-native abi-3 companion not loadable; the companion-present job covers this",
)


class RecordingRealFpe:
    """Delegates to the real compiled fpe kernel and records each call's row count."""

    def __init__(self) -> None:
        self._real = load_compiled_fpe_kernel()
        self.rows: list[int] = []

    def run(self, values: Any, **kwargs: Any) -> Any:
        self.rows.append(len(values))
        return self._real.run(values, **kwargs)


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize(("name", "policy"), _CELLS, ids=_CELL_IDS)
def test_native_seq_agrees_with_the_oracle_on_every_strict_dimension(
    strategy: str, name: str, policy: OrphanPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = _keyed(name, policy, strategy)
    oracle = s.run_full(shape).outputs
    python_hash_rows = spy_python_hash(monkeypatch)
    python_fpe_cells = _spy_python_fpe(monkeypatch)
    spy = install_fpe(monkeypatch, RecordingRealFpe())
    native = s.run_seq(shape)
    assert python_hash_rows == [] and python_fpe_cells == [], name
    for table in oracle:
        assert sc.compare_strict(oracle[table], native.outputs[table]) == _ALL_TRUE, (name, table)
    if strategy == "fpe":
        priced = (name, PRESERVE if policy is OrphanPolicy.WARN else policy)
        if priced in _ROWS:
            assert sorted(spy.rows) == _ROWS[priced]
        assert spy.rows, name  # the real fpe kernel ran for the key


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize(("name", "policy"), _CELLS, ids=_CELL_IDS)
def test_native_and_python_seq_are_byte_identical_per_shape(
    strategy: str, name: str, policy: OrphanPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = _keyed(name, policy, strategy)
    native = s.run_seq(shape)
    with monkeypatch.context() as m:
        hide_companion(m)
        hide_fpe(m)
        python = s.run_seq(shape)
    assert set(native.outputs) == set(python.outputs)
    for table in python.outputs:
        assert table_digest(native.outputs[table]) == table_digest(python.outputs[table]), (
            name,
            table,
        )
    key = "sequential_hash" if strategy == "hash" else "sequential_fpe"
    backends = {
        e["executed_backend"] for e in native.quality_metrics[key] if e["path"] != "fk_inherited"
    }
    assert backends == {"rust_companion"}  # the companion really ran, on every key


@pytest.mark.parametrize("name", ["chain", "diamond", "multi-parent", "self-ref", "junction"])
def test_a_genuine_orphan_under_fail_raises_the_same_error_as_the_oracle(name: str) -> None:
    shape = _keyed(name, FAIL, "fpe")
    with pytest.raises(ExecutionError) as oracle:
        s.run_full(shape)
    sink = RecordingSink()
    with pytest.raises(ExecutionError) as native:
        s.run_seq(shape, sink=sink)
    assert native.value.code == oracle.value.code == "orphan_fk_violation"
    assert sink.commits == 0 and sink.committed == {}


# The orphan key the real kernel mints is the Python parent handler's, so parent and child join.


@pytest.mark.parametrize(
    ("parent_cfg", "child_cfg"),
    [
        (_PARENT_CFG, {"charset": "alphanum", "preserve_separators": False}),
        ({"fpe_join_group": "grp_parent"}, {"fpe_join_group": "grp_child", "charset": "alphanum"}),
        ({}, {}),
    ],
    ids=["distinct-config", "join-group", "same-config"],
)
def test_the_real_remap_orphan_key_equals_the_python_parents(
    parent_cfg: dict[str, Any], child_cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = _edge_shape(parent_cfg, child_cfg)
    oracle = s.run_full(shape).outputs
    spy = install_fpe(monkeypatch, RecordingRealFpe())
    native = s.run_seq(shape).outputs
    assert spy.rows == [2, 1]  # the parent scalar pass, then one native orphan
    for table in oracle:
        assert sc.compare_strict(oracle[table], native[table]) == _ALL_TRUE, table
    orphan = native["C"].column("child_fk").to_pylist()[1]
    assert orphan != _ORPHAN
    # The matched child holds the parent's masked key: the join this slice must not break.
    assert (
        native["C"].column("child_fk").to_pylist()[0]
        == native["P"].column("parent_id").to_pylist()[0]
    )


def test_the_real_kernel_tells_a_child_tweak_from_the_parents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the real cipher, minting under the child column name gives a different key, so the
    parity check above does catch a wrong tweak."""
    shape = _edge_shape(_PARENT_CFG, _PARENT_CFG)
    oracle = s.run_full(shape).outputs
    _child_tweak_mutant(monkeypatch)
    install_fpe(monkeypatch, RecordingRealFpe())
    mutated = s.run_seq(shape).outputs
    assert (
        mutated["C"].column("child_fk").to_pylist()[1]
        != oracle["C"].column("child_fk").to_pylist()[1]
    )


@pytest.mark.parametrize("orphan", ["ABCDEFGHIJ", "12"])
def test_a_bad_orphan_key_fails_the_real_native_remap_like_python(
    orphan: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = _edge_shape(_PARENT_CFG, _PARENT_CFG, orphan=orphan)
    with pytest.raises(DecoyError) as oracle:
        s.run_full(shape)
    install_fpe(monkeypatch, RecordingRealFpe())
    sink = RecordingSink()
    with pytest.raises(DecoyError) as native:
        s.run_seq(shape, sink=sink)
    assert native.value.code == oracle.value.code
    assert orphan not in str(native.value)
    assert sink.commits == 0 and "C" not in sink.staged


def test_a_published_native_fpe_key_table_is_not_a_python_recompute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = _keyed("chain", REMAP, "fpe")
    cells = _spy_python_fpe(monkeypatch)
    out: dict[str, pa.Table] = s.run_seq(shape).outputs
    assert cells == []  # no cell reached the Python cipher
    assert out["A"].schema.field("k").type == pa.string()
