"""Phase D / FK-key-native: fpe as a native FK key on the SEQ route, proven with stand-in kernels.

No companion is needed. The hash and fpe stand-ins prefix every derived value with a mark, so a
key counts as native only when the emitted table carries the marked value. Every shape and orphan
policy already proven for a hash key (`_d2b_shapes`, `_dcomp_shapes`) is rebuilt over the key
strategy {hash, fpe} from the same builder (`to_fpe`), then compared with the full-frame oracle,
which never reaches the SEQ kernels. The real-kernel parity lives in
`test_phase_d_fpe_key_seq_real.py`.

The crux is REMAP: an orphan child key is minted by re-running the PARENT column's strategy, so a
native fpe orphan must use the parent's namespace, config and tweak column, never the child's, or
the parent and child keys silently stop joining.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import _sequential_native
from decoy_engine.execution._errors import ExecutionError, StrategyError
from decoy_engine.execution._sequential_native import (
    REASON_KEY_NOT_INJECTIVE,
    SEQ_FPE_DISPOSITION_KEY,
    SEQ_HASH_DISPOSITION_KEY,
    SeqNativeRun,
)
from decoy_engine.execution._sequential_native_ops import BoundFpeKernel, FpeCall
from decoy_engine.execution._strategies import _fpe
from decoy_engine.execution.native._crypto_ext import FpeConfig
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _dcomp_shapes as d
from tests.parity import _strict_compare as sc
from tests.parity._d1b1_bucket_data import bucket_seed
from tests.parity._d2a_seq_data import RecordingSink, callable_sink
from tests.parity._d2c_bucket_data import BucketMarkingKernel, install_bucket
from tests.parity._d2c_cat_data import CatMarkingKernel, cat_seed, install_cat
from tests.parity._d2c_date_shift_data import (
    DateShiftMarkingKernel,
    date_shift_seed,
    install_date_shift,
)
from tests.parity._d2c_fpe_data import (
    FpeMarkingKernel,
    fpe_key_shape,
    fpe_seed,
    hide_fpe,
    install_fpe,
    warning_view,
)
from tests.parity._fpe_key_data import (
    assert_keys_declined,
    fpe_marked_expected,
    to_fpe,
)
from tests.parity._fpe_key_hash_baseline import BASELINE
from tests.parity._seq_witness import MARK, MarkingKernel, hide_companion, install, spy_python_hash

REMAP, PRESERVE, WARN, FAIL = (
    OrphanPolicy.REMAP,
    OrphanPolicy.PRESERVE,
    OrphanPolicy.WARN,
    OrphanPolicy.FAIL,
)
_ALL_TRUE = dict.fromkeys(sc.DIMENSIONS, True)
STRATEGIES = ["hash", "fpe"]

# (id, builder(policy)). The same builders the hash suites use; fpe is the same shape through
# `to_fpe`, so a shape cannot drift between the two strategies.
_SHAPES: dict[str, Callable[[OrphanPolicy], s.Shape]] = {
    "chain": s.chain_shape,
    "diamond": s.diamond_shape,
    "multi-parent": s.multi_parent_shape,
    "self-ref": s.self_ref_shape,
    "junction": s.junction_shape,
    "composite": s.composite_shape,
    "composite-int": lambda p: d.int_component_shape(
        p, parent_n=[1, 2, None], child_k=["a", "b", "q"], child_n=[1, 2, 7]
    ),
    "golden-composite": lambda p: d.golden_composite_shape(p, orphan=True),
}
# Rows of every kernel call, sorted: a parent derives once, each orphan REMAP adds one call, a
# matched child adds none. WARN keeps its orphans, so it prices like PRESERVE.
_ROWS: dict[tuple[str, OrphanPolicy], list[int]] = {
    ("chain", REMAP): [1, 1, 1],
    ("chain", PRESERVE): [1],
    ("diamond", PRESERVE): [2, 3, 3],
    ("diamond", REMAP): [1, 2, 3, 3],
    ("multi-parent", PRESERVE): [2, 2],
    ("multi-parent", REMAP): [1, 2, 2],
    ("self-ref", REMAP): [1, 4],
    ("self-ref", PRESERVE): [4],
    ("junction", PRESERVE): [2, 2],
    ("junction", REMAP): [1, 1, 2, 2],
    ("composite", REMAP): [1, 1, 3, 3],
    ("composite", PRESERVE): [3, 3],
}
_CELLS = [(n, p) for n in _SHAPES for p in (PRESERVE, REMAP, WARN)]
_CELL_IDS = [f"{n}-{p.name.lower()}" for n, p in _CELLS]


def _keyed(name: str, policy: OrphanPolicy, strategy: str) -> s.Shape:
    shape = _SHAPES[name](policy)
    return to_fpe(shape) if strategy == "fpe" else shape


def _install_both(mp: pytest.MonkeyPatch) -> tuple[MarkingKernel, FpeMarkingKernel]:
    return install(mp, MarkingKernel()), install_fpe(mp, FpeMarkingKernel())


def _hide_both(mp: pytest.MonkeyPatch) -> None:
    hide_companion(mp)
    hide_fpe(mp)


def _spy_python_fpe(mp: pytest.MonkeyPatch) -> list[int]:
    """Count the cells that reach the Python fpe cipher."""
    cells: list[int] = []
    real = _fpe.fpe_encrypt_value

    def spy(*args: Any, **kwargs: Any) -> Any:
        cells.append(1)
        return real(*args, **kwargs)

    mp.setattr(_fpe, "fpe_encrypt_value", spy)
    return cells


def _edge_columns(shape: s.Shape) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for e in shape.graph.edges:
        out.setdefault(e.parent_table, set()).update(e.parent_columns)
        out.setdefault(e.child_table, set()).update(e.child_columns)
    return out


def _expected(shape: s.Shape, strategy: str, oracle: dict[str, pa.Table]) -> dict[str, Any]:
    """The oracle's tables with the stand-in mark on every key cell the strategy derived."""
    if strategy == "hash":
        return {
            t: {c: s.marked_like(oracle[t], c, MARK) for c in oracle[t].column_names}
            for t in oracle
        }
    keys = _edge_columns(shape)
    return {t: fpe_marked_expected(oracle[t], shape.sources[t], keys.get(t, set())) for t in oracle}


def _emitted(result: Any) -> dict[str, Any]:
    return {t: out.to_pydict() for t, out in result.outputs.items()}


def _entries(result: Any, strategy: str) -> list[dict[str, Any]]:
    key = SEQ_HASH_DISPOSITION_KEY if strategy == "hash" else SEQ_FPE_DISPOSITION_KEY
    return list(result.quality_metrics.get(key, []))


# The shape x policy matrix. Each cell: oracle vs SEQ-native vs SEQ-python, per channel.


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize(("name", "policy"), _CELLS, ids=_CELL_IDS)
def test_each_shape_and_policy_native_matches_the_oracle_and_the_python_route(
    strategy: str, name: str, policy: OrphanPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = _keyed(name, policy, strategy)
    oracle = s.run_full(shape)
    with monkeypatch.context() as m:
        _hide_both(m)
        python = s.run_seq(shape)
    hash_kernel, fpe_kernel = _install_both(monkeypatch)
    python_hash_rows = spy_python_hash(monkeypatch)
    python_fpe_cells = _spy_python_fpe(monkeypatch)
    native = s.run_seq(shape)

    # Output tables: the Python route is strictly the oracle; the native route is the oracle
    # with the kernel's mark on exactly the derived key cells, with the oracle's Arrow types.
    for table in oracle.outputs:
        assert sc.compare_strict(oracle.outputs[table], python.outputs[table]) == _ALL_TRUE, table
        assert [f.type for f in native.outputs[table].schema] == [
            f.type for f in oracle.outputs[table].schema
        ]
    assert _emitted(native) == _expected(shape, strategy, oracle.outputs)
    # The other channels are route-neutral: warnings and row errors do not depend on the kernel.
    assert warning_view(native.warnings) == warning_view(python.warnings)
    # Route order of table-level warnings differs (a pre-existing, kernel-independent fact).
    assert sorted(map(repr, warning_view(native.warnings))) == sorted(
        map(repr, warning_view(oracle.warnings))
    )
    assert native.row_errors == python.row_errors == oracle.row_errors
    # Native provenance: the strategy's kernel ran for exactly the rows the shape owns, and the
    # Python implementation of that strategy never did.
    kernel = hash_kernel if strategy == "hash" else fpe_kernel
    other = fpe_kernel if strategy == "hash" else hash_kernel
    assert other.calls == []
    assert python_hash_rows == [] and python_fpe_cells == []
    priced = (name, PRESERVE if policy is WARN else policy)
    if priced in _ROWS:
        assert sorted(c.rows for c in kernel.calls) == _ROWS[priced]
    else:
        assert kernel.calls, name  # a Dcomp shape: priced by the hash/fpe equality below


@pytest.mark.parametrize(("name", "policy"), [(n, p) for n in _SHAPES for p in (REMAP, PRESERVE)])
def test_an_fpe_key_reports_exactly_the_dispositions_a_hash_key_does(
    name: str, policy: OrphanPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scalar parents, REMAP records and inherited children, with the same multiplicity."""
    hash_kernel, fpe_kernel = _install_both(monkeypatch)
    hash_result = s.run_seq(_keyed(name, policy, "hash"))
    fpe_result = s.run_seq(_keyed(name, policy, "fpe"))
    assert _entries(hash_result, "hash") == _entries(fpe_result, "fpe")
    assert _entries(hash_result, "hash")  # the shape has a key
    assert SEQ_HASH_DISPOSITION_KEY not in fpe_result.quality_metrics
    assert SEQ_FPE_DISPOSITION_KEY not in hash_result.quality_metrics
    assert sorted(c.rows for c in hash_kernel.calls) == sorted(c.rows for c in fpe_kernel.calls)


@pytest.mark.parametrize("strategy", STRATEGIES)
@pytest.mark.parametrize("name", ["chain", "diamond", "multi-parent", "self-ref", "junction"])
def test_a_genuine_orphan_under_fail_raises_identically_on_every_route(
    strategy: str, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = _keyed(name, FAIL, strategy)
    with pytest.raises(ExecutionError) as oracle:
        s.run_full(shape)
    with monkeypatch.context() as m:
        _hide_both(m)
        with pytest.raises(ExecutionError) as python:
            s.run_seq(shape)
    _install_both(monkeypatch)
    sink = RecordingSink()
    with pytest.raises(ExecutionError) as native:
        s.run_seq(shape, sink=sink)
    assert native.value.code == python.value.code == oracle.value.code == "orphan_fk_violation"
    assert sink.commits == 0 and sink.committed == {}
    published: dict[str, pa.Table] = {}
    with pytest.raises(ExecutionError):
        s.run_seq(shape, sink=callable_sink(published))
    assert set(published) < set(shape.sources)  # the failing child table was never delivered


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_fail_with_no_orphan_runs_native(strategy: str, monkeypatch: pytest.MonkeyPatch) -> None:
    base = s.chain_shape(FAIL, a=["a", "b"], b=["a", "b"], c=["a", "b"])
    shape = to_fpe(base) if strategy == "fpe" else base
    oracle = s.run_full(shape)
    _install_both(monkeypatch)
    assert _emitted(s.run_seq(shape)) == _expected(shape, strategy, oracle.outputs)


# Chain middle: a column that is both a parent key and a child.


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_a_chain_middle_gets_only_a_remap_decision_and_no_scalar_call(
    strategy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = _keyed("chain", REMAP, strategy)
    hash_kernel, fpe_kernel = _install_both(monkeypatch)
    kernel = hash_kernel if strategy == "hash" else fpe_kernel
    result = s.run_seq(shape)
    paths = {(e["table"], e["column"], e["path"]) for e in _entries(result, strategy)}
    # B.k is a child (inherits) and a parent key (REMAP); it is never rederived as a scalar.
    assert ("B", "k", "scalar") not in paths
    assert {("B", "k", "fk_inherited"), ("B", "k", "fk_remap")} <= paths
    assert paths == {
        ("A", "k", "scalar"),
        ("A", "k", "fk_remap"),
        ("B", "k", "fk_inherited"),
        ("B", "k", "fk_remap"),
        ("C", "k", "fk_inherited"),
    }
    # One scalar derivation (A) and one orphan per hop; a scalar pass over B would add a 2-row call.
    assert sorted(c.rows for c in kernel.calls) == [1, 1, 1]


# Mixed job: hash dispositions must not move or multiply when an fpe key shares the plan.


def _combine(left: s.Shape, right: s.Shape) -> s.Shape:
    assert not set(left.sources) & set(right.sources)
    plan = s.make_plan(
        {
            **dict(left.plan.seed_envelope.per_table),
            **dict(right.plan.seed_envelope.per_table),
        }
    )
    return s.Shape(
        plan,
        s.graph(*left.graph.edges, *right.graph.edges),
        {**left.sources, **right.sources},
    )


@pytest.mark.parametrize("policy", [PRESERVE, REMAP])
def test_a_mixed_hash_and_fpe_key_job_keeps_each_strategys_entries_apart(
    policy: OrphanPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    hash_part = s.diamond_shape(policy)  # tables R, L, M, D
    fpe_part = fpe_key_shape(policy)  # tables A, B, C
    _install_both(monkeypatch)
    mixed = s.run_seq(_combine(hash_part, fpe_part))
    hash_alone = s.run_seq(hash_part)
    fpe_alone = s.run_seq(fpe_part)
    # Value AND multiplicity: the very same list, in the same order, as the hash-only run.
    assert (
        _entries(mixed, "hash")
        == _entries(hash_alone, "hash")
        == BASELINE[f"diamond-{policy.name}"]
    )
    assert _entries(mixed, "fpe") == _entries(fpe_alone, "fpe")
    assert {e["table"] for e in _entries(mixed, "fpe")} == {"A", "B", "C"}
    assert {e["table"] for e in _entries(mixed, "hash")} == {"R", "L", "M", "D"}
    for e in _entries(mixed, "hash"):
        assert "fpe" not in str(e.get("reason", ""))


@pytest.mark.parametrize("key", sorted(BASELINE), ids=sorted(BASELINE))
def test_hash_entries_are_identical_to_the_pre_change_baseline(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Frozen from the code before the fpe-key slice: hash output is value-, order- and
    multiplicity-identical."""
    name, policy = key.rsplit("-", 1)
    builders: dict[str, Callable[[OrphanPolicy], s.Shape]] = {
        "chain": s.chain_shape,
        "diamond": s.diamond_shape,
        "multi": s.multi_parent_shape,
        "selfref": s.self_ref_shape,
        "junction": s.junction_shape,
        "composite": s.composite_shape,
    }
    _install_both(monkeypatch)
    result = s.run_seq(builders[name](OrphanPolicy[policy]))
    assert result.quality_metrics[SEQ_HASH_DISPOSITION_KEY] == BASELINE[key]
    assert SEQ_FPE_DISPOSITION_KEY not in result.quality_metrics


# fpe-specific: the REMAP tweak, namespace and config are the PARENT's.

_PARENT_CFG: dict[str, Any] = {}
_CHILD_CFG: dict[str, Any] = {"charset": "alphanum", "preserve_separators": False}
_ORPHAN = "5555555555"


def _edge_shape(
    parent_cfg: dict[str, Any],
    child_cfg: dict[str, Any],
    *,
    orphan: str = _ORPHAN,
    policy: OrphanPolicy = REMAP,
) -> s.Shape:
    """P.parent_id -> C.child_fk: distinct names, namespaces and fpe configs, one genuine orphan."""
    plan = s.make_plan(
        {
            "P": s.table_seed({"parent_id": fpe_seed("ns_p", **parent_cfg)}),
            "C": s.table_seed({"child_fk": fpe_seed("ns_c", **child_cfg)}),
        }
    )
    return s.Shape(
        plan,
        s.graph(s.edge("P", "parent_id", "C", "child_fk", "ns_p", policy)),
        {
            "P": pa.table({"parent_id": s.strs(["1000000001", "1000000002"])}),
            "C": pa.table({"child_fk": s.strs(["1000000001", orphan])}),
        },
    )


def _assert_remap_call_is_the_parents(kernel: FpeMarkingKernel, parent_cfg: dict[str, Any]) -> None:
    scalar, remap = kernel.calls
    assert (scalar.namespace, scalar.tweak_column, scalar.rows) == ("ns_p", "parent_id", 2)
    assert remap.rows == 1
    assert remap.namespace == "ns_p"
    assert remap.tweak_column == "parent_id"
    assert remap.config == FpeConfig.from_mapping(parent_cfg)
    assert remap.mask_key == scalar.mask_key


def _assert_orphan_ciphertext_is_the_python_parents(native: Any, python: Any, oracle: Any) -> None:
    py_orphan = python.outputs["C"].column("child_fk").to_pylist()[1]
    assert py_orphan == oracle.outputs["C"].column("child_fk").to_pylist()[1]
    assert py_orphan != _ORPHAN
    assert native.outputs["C"].column("child_fk").to_pylist()[1] == MARK + py_orphan


def _check_remap_uses_the_parent(
    mp: pytest.MonkeyPatch, parent_cfg: dict[str, Any], child_cfg: dict[str, Any]
) -> FpeMarkingKernel:
    shape = _edge_shape(parent_cfg, child_cfg)
    oracle = s.run_full(shape)
    with mp.context() as m:
        hide_fpe(m)
        python = s.run_seq(shape)
    kernel = install_fpe(mp, FpeMarkingKernel())
    native = s.run_seq(shape)
    _assert_remap_call_is_the_parents(kernel, parent_cfg)
    _assert_orphan_ciphertext_is_the_python_parents(native, python, oracle)
    return kernel


def test_a_remap_orphan_uses_the_parent_namespace_config_and_tweak_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _check_remap_uses_the_parent(monkeypatch, _PARENT_CFG, _CHILD_CFG)


def test_a_remap_orphan_uses_the_parents_join_group_not_the_childs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = {"fpe_join_group": "grp_parent"}
    child = {"fpe_join_group": "grp_child", "charset": "alphanum"}
    kernel = _check_remap_uses_the_parent(monkeypatch, parent, child)
    assert kernel.calls[1].config.join_group == "grp_parent"


def _child_tweak_mutant(mp: pytest.MonkeyPatch) -> None:
    """A wrong REMAP: the orphan is minted under the CHILD column name as the tweak."""
    real = SeqNativeRun._remap_tokens

    def wrong(
        self: SeqNativeRun, table: str, column: str, series: Any, strategy: str, call: Any
    ) -> Any:
        return real(self, table, column, series, strategy, replace(call, tweak_column="child_fk"))

    mp.setattr(SeqNativeRun, "_remap_tokens", wrong)


def test_the_tweak_check_catches_a_remap_minted_under_the_child_column_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = _edge_shape(_PARENT_CFG, _CHILD_CFG)
    oracle = s.run_full(shape)
    with monkeypatch.context() as m:
        hide_fpe(m)
        python = s.run_seq(shape)
    _child_tweak_mutant(monkeypatch)
    kernel = install_fpe(monkeypatch, FpeMarkingKernel())
    native = s.run_seq(shape)
    # Each of the two checks, on its own, fails the mutant.
    with pytest.raises(AssertionError):
        _assert_remap_call_is_the_parents(kernel, _PARENT_CFG)
    with pytest.raises(AssertionError):
        _assert_orphan_ciphertext_is_the_python_parents(native, python, oracle)


def test_a_remap_uses_the_parent_even_when_the_configs_match_but_the_names_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same config on both sides: only the tweak column tells parent from child.
    kernel = _check_remap_uses_the_parent(monkeypatch, {}, {})
    assert kernel.calls[1].tweak_column == "parent_id"


# fpe-specific: fail closed.


@pytest.mark.parametrize(
    ("orphan", "code"),
    [
        ("ABCDEFGHIJ", "fpe_unencryptable_value"),  # no character in the digits charset
        ("12", "fpe_unencryptable_domain"),  # in charset but below the FF1 domain floor
    ],
)
def test_a_bad_orphan_key_fails_the_native_remap_closed_like_python(
    orphan: str, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = _edge_shape(_PARENT_CFG, _CHILD_CFG, orphan=orphan)
    with pytest.raises(StrategyError) as oracle:
        s.run_full(shape)
    with monkeypatch.context() as m:
        hide_fpe(m)
        with pytest.raises(StrategyError) as python:
            s.run_seq(shape)
    kernel = install_fpe(monkeypatch, FpeMarkingKernel())
    sink = RecordingSink()
    with pytest.raises(StrategyError) as native:
        s.run_seq(shape, sink=sink)
    assert native.value.code == python.value.code == oracle.value.code == code
    assert native.value.strategy == "fpe"
    assert orphan not in str(native.value)
    # The failing call is the native REMAP of the parent column, not the scalar pass.
    assert [c.rows for c in kernel.calls] == [2, 1]
    assert kernel.calls[-1].tweak_column == "parent_id"
    assert sink.commits == 0 and sink.committed == {} and "C" not in sink.staged
    published: dict[str, pa.Table] = {}
    with pytest.raises(StrategyError):
        s.run_seq(shape, sink=callable_sink(published))
    assert "C" not in published


def test_the_remap_error_channel_is_read_before_any_orphan_key_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A kernel whose REMAP result carries an error row must not leak its (unusable) values.
    shape = _edge_shape(_PARENT_CFG, _CHILD_CFG, orphan="ABCDEFGHIJ")
    install_fpe(monkeypatch, FpeMarkingKernel())
    leaked: list[Any] = []
    real = SeqNativeRun._remap_tokens

    def record(self: SeqNativeRun, *args: Any) -> Any:
        out = real(self, *args)
        leaked.append(out)
        return out

    monkeypatch.setattr(SeqNativeRun, "_remap_tokens", record)
    with pytest.raises(StrategyError):
        s.run_seq(shape)
    assert leaked == []  # the call raised before returning a value


# fpe-specific: declines. The key stays Python with a caller-visible reason; output is the oracle's.


def _luhn(prefix: str) -> str:
    digits = [int(c) for c in prefix]
    total = sum(
        (d2 * 2 - 9 if d2 * 2 > 9 else d2 * 2) if i % 2 == 0 else d2
        for i, d2 in enumerate(reversed(digits))
    )
    return prefix + str((10 - total % 10) % 10)


def _decline_shape(
    parent_cfg: dict[str, Any], keys: list[Any], orphan: Any, typ: pa.DataType
) -> s.Shape:
    plan = s.make_plan(
        {
            "P": s.table_seed({"parent_id": fpe_seed("ns_p", **parent_cfg)}),
            "C": s.table_seed({"child_fk": fpe_seed("ns_c", **parent_cfg)}),
        }
    )
    return s.Shape(
        plan,
        s.graph(s.edge("P", "parent_id", "C", "child_fk", "ns_p", REMAP)),
        {
            "P": pa.table({"parent_id": pa.array(keys, typ)}),
            "C": pa.table({"child_fk": pa.array([keys[0], orphan], typ)}),
        },
    )


def _declines() -> list[tuple[str, s.Shape, str]]:
    luhn_keys = [_luhn("123456789"), _luhn("223456789")]
    return [
        (
            "checksum",
            _decline_shape({"checksum": "luhn"}, luhn_keys, _luhn("323456789"), pa.string()),
            "fpe_checksum_not_native:parent_id",
        ),
        (
            "non-string",
            _decline_shape({}, [1000000001, 1000000002], 1000000009, pa.int64()),
            "fpe_source_type_not_string:parent_id:int64",
        ),
        (
            "large-string",
            _decline_shape({}, ["1000000001", "1000000002"], "1000000009", pa.large_string()),
            "fpe_source_type_not_string:parent_id:large_string",
        ),
    ]


@pytest.mark.parametrize(("name", "shape", "reason"), _declines(), ids=[c[0] for c in _declines()])
def test_a_declined_fpe_key_stays_python_with_its_reason_and_matches_the_oracle(
    name: str, shape: s.Shape, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    oracle = s.run_full(shape)
    kernel = install_fpe(monkeypatch, FpeMarkingKernel())
    result = s.run_seq(shape)
    assert kernel.calls == []
    for table in oracle.outputs:
        assert sc.compare_strict(oracle.outputs[table], result.outputs[table]) == _ALL_TRUE, name
    by_path = {e["path"]: e for e in _entries(result, "fpe")}
    assert by_path["scalar"]["executed_backend"] == "python_scalar"
    assert by_path["scalar"]["reason"] == reason
    # The parent ran Python, so its orphan is minted in Python and says why.
    assert by_path["fk_remap"]["executed_backend"] == "python_scalar"
    assert by_path["fk_remap"]["reason"] == "fpe_remap_parent_stays_python:parent_id"


def test_without_the_companion_an_fpe_key_runs_python_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = fpe_key_shape(REMAP)
    oracle = s.run_full(shape)
    hide_fpe(monkeypatch)
    result = s.run_seq(shape)
    for table in oracle.outputs:
        assert sc.compare_strict(oracle.outputs[table], result.outputs[table]) == _ALL_TRUE
    backends = {
        e["executed_backend"] for e in _entries(result, "fpe") if e["path"] != "fk_inherited"
    }
    assert backends == {"python_scalar"}


def test_an_orphan_key_of_another_type_is_minted_in_python_with_a_value_free_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parent is a native string column, but the orphan keys it is asked to mint are ints:
    the REMAP call is admitted on its own, from the type of the keys it holds."""
    run = SeqNativeRun()
    kernel = FpeMarkingKernel()
    run._bound["fpe"] = BoundFpeKernel(kernel)
    run._load_attempted.add("fpe")
    run._remap_decisions[("P", "parent_id")] = _sequential_native.ColumnDecision(
        "P", "parent_id", "rust_companion", None, pa.string(), strategy="fpe"
    )
    call = FpeCall("ns_p", b"k" * 8, FpeConfig(), "parent_id")
    assert run._remap_tokens("P", "parent_id", pd.Series([1000000009]), "fpe", call) is None
    assert kernel.calls == []
    (entry,) = [r.entry() for r in run._remap.values()]
    assert entry["executed_backend"] == "python_scalar"
    assert entry["reason"] == "fpe_source_type_not_string:parent_id:int64"
    mixed = pd.Series(["1000000009", 7], dtype="object")
    assert run._remap_tokens("P", "parent_id", mixed, "fpe", call) is None
    assert run._remap["P", "parent_id"].reasons[-1] == "fpe_remap_input_not_arrow:parent_id"
    assert run._remap["P", "parent_id"].strategy == "fpe"


# Part B: a not-guaranteed-injective operator on an FK key stays Python and says why.


def _lossy_key_shape(
    seed: Callable[[str], Any], keys: list[str], payload: list[str], policy: OrphanPolicy
) -> s.Shape:
    """A(k key, p payload) -> B(k): `p` is NOT a key and must still go native."""
    plan = s.make_plan(
        {
            "A": s.table_seed({"k": seed("ns_ab"), "p": seed("ns_pay")}),
            "B": s.table_seed({"k": seed("ns_ab")}),
        }
    )
    return s.Shape(
        plan,
        s.graph(s.edge("A", "k", "B", "k", "ns_ab", policy)),
        {
            "A": pa.table({"k": s.strs(keys[:2]), "p": s.strs(payload)}),
            "B": pa.table({"k": s.strs(keys)}),
        },
    )


_POOL = [f"key{i}" for i in range(12)]
_DATES = ["2024-03-05", "2023-07-19", "2022-01-10"]
_LOSSY: dict[str, tuple[Callable[[str], Any], list[str], list[str], Any, Any]] = {
    "categorical": (
        lambda ns: cat_seed(ns, categories=_POOL),
        ["alpha", "beta", "gamma"],
        ["xray-val", "yankee-val"],
        CatMarkingKernel,
        install_cat,
    ),
    "bucket_perturb": (
        lambda ns: bucket_seed("month", namespace=ns),
        _DATES,
        ["2021-05-01", "2020-06-02"],
        BucketMarkingKernel,
        install_bucket,
    ),
    "date_shift": (
        lambda ns: date_shift_seed(namespace=ns),
        _DATES,
        ["2021-05-01", "2020-06-02"],
        DateShiftMarkingKernel,
        install_date_shift,
    ),
}
_DISPOSITION_KEY = {
    "categorical": "sequential_categorical",
    "bucket_perturb": "sequential_bucket_perturb",
    "date_shift": "sequential_date_shift",
}


@pytest.mark.parametrize("policy", [PRESERVE, REMAP])
@pytest.mark.parametrize("op", sorted(_LOSSY))
def test_a_lossy_operator_key_is_declined_but_its_payload_column_stays_native(
    op: str, policy: OrphanPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, keys, payload, kernel_cls, install_kernel = _LOSSY[op]
    shape = _lossy_key_shape(seed, keys, payload, policy)
    oracle = s.run_full(shape)
    kernel = install_kernel(monkeypatch, kernel_cls())
    hash_kernel, fpe_kernel = _install_both(monkeypatch)
    result = s.run_seq(shape)
    # B1: the key columns are the oracle's, byte for byte; the key kernel is never called for them.
    for table in oracle.outputs:
        assert result.outputs[table].column("k").equals(oracle.outputs[table].column("k")), table
    assert hash_kernel.calls == [] and fpe_kernel.calls == []
    # B2: the payload column still masks native (A.p's two rows only), beside the keys.
    assert sum(c.rows for c in kernel.calls) == 2
    entries = result.quality_metrics[_DISPOSITION_KEY[op]]
    payload_entries = [e for e in entries if e["column"] == "p"]
    assert [(e["path"], e["executed_backend"]) for e in payload_entries] == [
        ("scalar", "rust_companion")
    ]
    assert "reason" not in payload_entries[0]
    declined = [e for e in entries if e["column"] == "k"]
    assert {(e["table"], e["path"], e["executed_backend"]) for e in declined} == {
        ("A", "fk_key_declined", "python_scalar"),
        ("B", "fk_key_declined", "python_scalar"),
    }
    # B3: the reason carries the strategy, the column and the Arrow type, and no data.
    pattern = re.compile(rf"^{REASON_KEY_NOT_INJECTIVE}:{op}:k:string$")
    assert all(pattern.fullmatch(e["reason"]) for e in declined)
    text = repr(result.quality_metrics)
    for value in (*keys, *payload):
        assert value not in text


@pytest.mark.parametrize("op", sorted(_LOSSY))
def test_a_lossy_operator_key_alone_reports_the_decline_and_calls_no_kernel(
    op: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, keys, _, kernel_cls, install_kernel = _LOSSY[op]
    plan = s.make_plan(
        {"A": s.table_seed({"k": seed("ns_ab")}), "B": s.table_seed({"k": seed("ns_ab")})}
    )
    shape = s.Shape(
        plan,
        s.graph(s.edge("A", "k", "B", "k", "ns_ab", REMAP)),
        {"A": pa.table({"k": s.strs(keys[:2])}), "B": pa.table({"k": s.strs(keys)})},
    )
    oracle = s.run_full(shape)
    kernel = install_kernel(monkeypatch, kernel_cls())
    result = s.run_seq(shape)
    assert kernel.calls == []
    for table in oracle.outputs:
        assert result.outputs[table].equals(oracle.outputs[table]), table
    assert_keys_declined(result, shape, op, _DISPOSITION_KEY[op])


def test_the_decline_is_reporting_only_and_never_a_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, keys, payload, kernel_cls, install_kernel = _LOSSY["categorical"]
    run = SeqNativeRun()
    shape = _lossy_key_shape(seed, keys, payload, REMAP)
    schema = {t: src.schema for t, src in shape.sources.items()}
    for table in ("A", "B"):
        run.prepare_table(shape.plan, table, schema[table], shape.graph)
    assert {col for _, col in run._decisions} == {"p"}  # only the payload is decided
    assert run._remap_decisions == {} and run._inherited == []
    assert len(run._key_declines) == 2
