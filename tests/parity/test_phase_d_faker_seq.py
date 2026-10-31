"""Phase D / faker SEQ-native: admission, routing and FK-key boundary, with no companion needed.

The index kernel is a distinguishable stand-in (`CatMarkingKernel`: index 0 for every valid
row), so the native witness, the decline reasons, the evidence rendering and the routing hold in
an environment without the compiled companion. Real-kernel parity against the oracle lives in
`test_phase_d_faker_seq_real.py`.

Acceptance F5 to F8 of docs/plans/2026-10-13-phase-d-faker-seq-native.md.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import ConfigError
from decoy_engine.execution import _sequential_native
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution._pipeline_routing import decide_execution_route
from decoy_engine.execution._pipeline_routing_signals import out_of_core_admission
from decoy_engine.execution._sequential_native_decisions import ColumnDecision
from decoy_engine.execution._sequential_native_ops import (
    FAKER,
    SEQ_CATEGORICAL_DISPOSITION_KEY,
    SEQ_FAKER_DISPOSITION_KEY,
    SEQ_HASH_DISPOSITION_KEY,
    faker_op,
)
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._index_ext import load_compiled_index_kernel
from decoy_engine.execution.native._requirements import (
    faker_pool_precondition_met,
    faker_pool_precondition_met_seed,
)
from decoy_engine.generation.pool import GenerationError
from decoy_engine.relationships._graph import OrphanPolicy
from tests.native._chunked_faker_support import oracle_pool_for
from tests.parity import _d2b_shapes as s
from tests.parity._d1a_hash_data import JOB_SEED, MASK_KEY
from tests.parity._d2b_shapes import hashed, make_plan, run_full, run_seq, strs, table_seed
from tests.parity._d2c_cat_data import (
    CatMarkingKernel,
    cat_seed,
    hide_cat,
    install_cat,
    payload_shape,
)
from tests.parity._faker_seq_data import (
    FIRST,
    NS,
    PASSTHROUGH,
    POOL,
    SRC,
    assert_python_ran,
    cells,
    faker_entries,
    faker_entry,
    faker_seed,
    gated,
    positional_seed,
)
from tests.parity._fpe_key_data import assert_keys_declined

_OOC_CODE = "out_of_core_faker_pool_unsupported"
_SEQUENTIAL = ("sequential", "pure_mask_fk")
# B4 made the deterministic-REUSE faker shape out-of-core-eligible, so a faker job now
# takes a bounded out-of-core route under the two auto paths and the explicit mode.
_OUT_OF_CORE_BYTE = ("out_of_core", "byte_estimate_bounded_out_of_core")
_OUT_OF_CORE_LARGE = ("out_of_core", "out_of_core_large_fk")
_OOC_FAKER_KEY = "out_of_core_faker"


def _companion_loads() -> bool:
    try:
        load_compiled_index_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


# The backend a sequential faker payload reports through run_pipeline: the companion when it
# loads, else the Python handler.
_SEQ_BACKEND = "rust_companion" if _companion_loads() else "python_scalar"
# The backend the out-of-core faker route (B4) reports: the compiled pool-select kernel when the
# companion loads, else the KAT-equal Python reference the pool context feeds.
_OOC_FAKER_BACKEND = "rust_pool_select" if _companion_loads() else "python_scalar"


def _expected_value(seed: Any) -> str:
    """What the index-0 stand-in picks: the first value of the pool the handler builds."""
    return str(oracle_pool_for(seed, JOB_SEED).values[0])


# The native witness with a stand-in kernel: the pool, the keys and the evidence.


def test_deterministic_leg_hands_the_kernel_the_source_keyed_on_the_mask_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    seed = faker_seed()
    out = run_seq(payload_shape({"f": (strs(SRC), seed)}))
    [call] = [c for c in kernel.calls if c.namespace == NS]
    assert (call.rows, call.arrow_type, call.pool_size, call.mask_key) == (
        len(SRC),
        pa.string(),
        POOL,
        MASK_KEY,
    )
    first = _expected_value(seed)
    assert out.outputs["P"].column("f").to_pylist() == [None if v is None else first for v in SRC]
    entry = faker_entry(out, "f")
    assert entry == {
        "table": "P",
        "column": "f",
        "path": "scalar",
        "executed_backend": "rust_companion",
        "native_calls": 1,
    }


def test_positional_leg_hands_the_kernel_dense_position_keys_under_the_default_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    seed = positional_seed(None)
    out = run_seq(payload_shape({"f": (strs(SRC), seed)}))
    default_ns = "faker-nd/1:P/1:f"
    [call] = [c for c in kernel.calls if c.namespace == default_ns]
    assert (call.rows, call.arrow_type, call.mask_key) == (len(SRC), pa.uint64(), JOB_SEED)
    first = _expected_value(seed)
    assert out.outputs["P"].column("f").to_pylist() == [None if v is None else first for v in SRC]
    assert faker_entry(out, "f")["native_calls"] == 1


def test_positional_leg_under_a_gate_hands_the_kernel_only_the_selected_positions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    flag = [0, 1, 0, 1, 0, 0, 0]
    shape = gated({"f": (strs(SRC), positional_seed())}, flag, "flag == 1")
    out = run_seq(shape).outputs["P"].column("f").to_pylist()
    [call] = [c for c in kernel.calls if c.namespace == NS]
    assert call.rows == 2
    assert out[0] == SRC[0] and out[2] is None and out[4] == SRC[4]  # unselected rows untouched


# F5: declines (with a stand-in kernel, so they hold with or without the companion).


def _declines(seed: Any, source: pa.Array, reason: str, monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    shape = payload_shape({"f": (source, seed)})
    result = run_seq(shape)
    assert_python_ran(result, "f", reason)
    assert [c for c in kernel.calls if c.namespace == NS] == []
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])
    assert "alice" not in repr(result.quality_metrics)


@pytest.mark.parametrize("mode", ["unique", "match_source_cardinality", "scale_source_cardinality"])
def test_f5a_whole_column_modes_decline(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    seed = positional_seed(mode=mode, pool_size=200)
    _declines(seed, cells(30), f"faker_nondeterministic_mode_stays_python:f:{mode}", monkeypatch)


@pytest.mark.parametrize("deterministic", [True, False])
def test_f5b_a_provider_outside_the_allowlist_declines(
    deterministic: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = faker_seed(provider="person_email", deterministic=deterministic)
    _declines(seed, cells(30), "faker_provider_not_native:f:person_email", monkeypatch)


@pytest.mark.parametrize("deterministic", [True, False])
def test_f5c_a_non_string_source_declines(
    deterministic: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = faker_seed(deterministic=deterministic)
    ints = pa.array(list(range(20)), pa.int64())
    _declines(seed, ints, "faker_source_type_not_string:f:int64", monkeypatch)
    wide = pa.array([f"v{i}" for i in range(20)], pa.large_string())
    _declines(seed, wide, "faker_source_type_not_string:f:large_string", monkeypatch)


def test_f5d_no_companion_reports_the_unavailable_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    hide_cat(monkeypatch)
    for seed in (faker_seed(), positional_seed()):
        shape = payload_shape({"f": (cells(30), seed)})
        result = run_seq(shape)
        assert_python_ran(result, "f", "crypto_extension_unavailable")
        assert result.outputs["P"].equals(run_full(shape).outputs["P"])


def test_f5f_a_deterministic_faker_without_a_pool_size_declines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _declines(
        faker_seed(pool_size=None),
        cells(30),
        "faker_not_deterministic_reuse_variant:f",
        monkeypatch,
    )


def test_f5c_missing_namespace_is_the_oracles_error_not_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    shape = payload_shape({"f": (cells(30), faker_seed(None))})
    with pytest.raises(GenerationError) as oracle:
        run_full(shape)
    with pytest.raises(GenerationError) as seq:
        run_seq(shape)
    assert oracle.value.code == seq.value.code == "deterministic_requires_source_and_namespace"
    assert kernel.calls == []


@pytest.mark.parametrize("deterministic", [True, False])
def test_f5b_an_out_of_grammar_when_predicate_is_rejected_for_both_routes(
    deterministic: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Invalid config, not a native-to-Python fallback: the `when:` gate refuses it up front.
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    seed = faker_seed(deterministic=deterministic)
    shape = gated({"f": (cells(12), seed)}, [1] * 12, "__import__('os').system('x') == 1")
    codes = []
    for run in (run_full, run_seq):
        with pytest.raises(Exception) as exc:
            run(shape)
        codes.append(getattr(exc.value, "code", None))
    assert codes == ["when_outside_closed_grammar"] * 2
    assert kernel.calls == []


def test_the_seed_level_precondition_is_the_node_level_one() -> None:
    cases = [
        faker_seed(),
        faker_seed(None),
        faker_seed(pool_size=None),
        faker_seed(mode="unique"),
        positional_seed(),
        faker_seed(""),
    ]
    for seed in cases:
        node = SimpleNamespace(plan_slice=seed)
        assert faker_pool_precondition_met(node) is faker_pool_precondition_met_seed(seed)
    expected = [True, False, False, False, False, False]
    assert [faker_pool_precondition_met_seed(c) for c in cases] == expected
    # A group slice is never a ColumnSeed: the node wrapper keeps its guard.
    assert faker_pool_precondition_met(SimpleNamespace(plan_slice=object())) is False


def test_the_admit_reasons_are_coded_and_value_free() -> None:
    admit = faker_op(lambda _t: None).admit
    string = pa.string()
    assert admit("c", string, faker_seed()) is None
    assert admit("c", string, positional_seed()) is None
    assert admit("c", string, positional_seed(None, pool_size=None)) is None
    assert admit("c", string, faker_seed(provider="person_email")) == (
        "faker_provider_not_native:c:person_email"
    )
    assert admit("c", string, replace(faker_seed(), provider=None)) == (
        "faker_provider_not_native:c:None"
    )
    assert admit("c", string, faker_seed(None)) == "faker_not_deterministic_reuse_variant:c"
    assert (
        admit("c", string, faker_seed(mode="unique")) == "faker_not_deterministic_reuse_variant:c"
    )
    assert admit("c", string, positional_seed(mode="unique")) == (
        "faker_nondeterministic_mode_stays_python:c:unique"
    )
    assert admit("c", pa.int32(), faker_seed()) == "faker_source_type_not_string:c:int32"
    assert admit("c", pa.large_string(), positional_seed()) == (
        "faker_source_type_not_string:c:large_string"
    )


def test_faker_is_registered_and_is_not_a_key_strategy() -> None:
    assert FAKER in _sequential_native._OPS
    assert _sequential_native._OPS[FAKER].disposition_key == SEQ_FAKER_DISPOSITION_KEY
    assert FAKER not in _sequential_native._KEY_STRATEGIES
    assert _sequential_native._OPS[FAKER].remap_input_reason is None


# F6: faker as an FK key declines through the key-injectivity gate.


def _faker_key_shape(policy: OrphanPolicy, *, payload: bool = True) -> s.Shape:
    a = {
        "k": faker_seed("ns_ab"),
        **({"p": positional_seed("ns_p"), "h": hashed("ns_h")} if payload else {}),
    }
    plan = make_plan(
        {
            "A": table_seed(a),
            "B": table_seed({"k": faker_seed("ns_bc")}),
            "C": table_seed({"k": faker_seed("ns_bc")}),
        }
    )
    a_cols: dict[str, pa.Array] = {"k": strs(["alpha", "beta"])}
    if payload:
        a_cols |= {"p": strs(["x", "y"]), "h": strs(["u", "v"])}
    return s.Shape(
        plan,
        s.graph(
            s.edge("A", "k", "B", "k", "ns_ab", policy),
            s.edge("B", "k", "C", "k", "ns_bc", policy),
        ),
        {
            "A": pa.table(a_cols),
            "B": pa.table({"k": strs(["alpha", "beta", "gamma"])}),
            "C": pa.table({"k": strs(["alpha", "beta", "gamma", "delta"])}),
        },
    )


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.REMAP])
def test_f6_a_faker_key_stays_python_and_reports_the_decline(
    policy: OrphanPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    shape = _faker_key_shape(policy, payload=False)
    result = run_seq(shape)
    oracle = run_full(shape)
    for table in ("A", "B", "C"):
        assert result.outputs[table].equals(oracle.outputs[table]), table
    assert kernel.calls == []
    assert_keys_declined(result, shape, "faker", SEQ_FAKER_DISPOSITION_KEY)


def test_f6_a_faker_payload_beside_faker_keys_stays_native(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    shape = _faker_key_shape(OrphanPolicy.REMAP)
    result = run_seq(shape)
    entries = result.quality_metrics[SEQ_FAKER_DISPOSITION_KEY]
    by_path = {(e["column"], e["path"]): e["executed_backend"] for e in entries}
    assert by_path[("p", "scalar")] == "rust_companion"
    assert {e["path"] for e in entries if e["column"] == "k"} == {"fk_key_declined"}
    assert [c.namespace for c in kernel.calls].count("ns_p") == 1
    assert SEQ_HASH_DISPOSITION_KEY in result.quality_metrics


# F8: empty versus all-null evidence, with a stand-in kernel.


def test_f8_empty_table_books_zero_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    for seed in (faker_seed(), positional_seed()):
        result = run_seq(payload_shape({"f": (pa.array([], pa.string()), seed)}))
        entry = faker_entry(result, "f")
        assert entry["native_calls"] == 0
        assert entry["executed_backend"] == "none"
        assert entry["reason"] == "no_rows_hashed"
    assert [c for c in kernel.calls if c.namespace == NS] == []


def test_f8_all_null_populated_column_books_exactly_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    for seed in (faker_seed(), positional_seed()):
        shape = payload_shape({"f": (strs([None] * 9), seed)})
        result = run_seq(shape)
        entry = faker_entry(result, "f")
        assert entry["native_calls"] == 1 and entry["executed_backend"] == "rust_companion"
        assert "reason" not in entry
        assert result.outputs["P"].column("f").to_pylist() == [None] * 9
    assert len([c for c in kernel.calls if c.rows == 9 and c.namespace == NS]) == 2


def test_the_faker_entry_renders_its_numeric_call_count() -> None:
    entry = ColumnDecision("t", "c", "rust_companion", None, pa.string(), 5, FAKER, 2).entry()
    assert entry["native_calls"] == 2
    declined = ColumnDecision("t", "c", "python_scalar", "r", pa.string(), 5, FAKER).entry()
    assert declined["native_calls"] == 0
    other = ColumnDecision("t", "c", "rust_companion", None, pa.string(), 5, "categorical").entry()
    assert "native_calls" not in other  # no other strategy's fragment changes shape


# F7: a mixed job reports each strategy under its own key (stand-in kernels, no companion).


def test_f7_faker_beside_categorical_and_hash_does_not_disturb_their_fragments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_cat(monkeypatch, CatMarkingKernel())
    parts = {
        "c": (strs(SRC), cat_seed()),
        "f": (strs(SRC), faker_seed()),
        "g": (strs(SRC), positional_seed()),
    }
    both = run_seq(payload_shape(parts)).quality_metrics
    cat_only = run_seq(payload_shape({"c": parts["c"]})).quality_metrics
    assert sorted(e["column"] for e in both[SEQ_FAKER_DISPOSITION_KEY]) == ["f", "g"]
    assert repr(both[SEQ_CATEGORICAL_DISPOSITION_KEY]) == repr(
        cat_only[SEQ_CATEGORICAL_DISPOSITION_KEY]
    )
    assert repr(both[SEQ_HASH_DISPOSITION_KEY]) == repr(cat_only[SEQ_HASH_DISPOSITION_KEY])
    assert SEQ_FAKER_DISPOSITION_KEY not in cat_only


# F7 / F7b: where a faker-bearing job runs, through production routing.


def _shape() -> s.Shape:
    return payload_shape({"f": (strs(SRC), faker_seed()), "p": (strs(SRC), PASSTHROUGH)})


def _route(shape: s.Shape, **kw: Any) -> tuple[str, str]:
    compatible, code = out_of_core_admission(shape.plan, registry=s.REGISTRY, graph=shape.graph)
    return decide_execution_route(
        SimpleNamespace(relationships=(object(),), tables=()),
        has_generate_table=False,
        has_mask_table=True,
        validators=[],
        fidelity_report=False,
        vault_writer=None,
        execution_mode=kw.pop("execution_mode", "auto"),
        graph=shape.graph,
        out_of_core_compatible=compatible,
        out_of_core_reject_code=code,
        largest_table_rows=kw.pop("largest_table_rows", 10),
        **kw,
    )


def test_the_real_gate_admits_a_deterministic_reuse_faker_for_the_out_of_core_route() -> None:
    # B4 ported the deterministic-REUSE person-name variant, so the real gate now admits it
    # on its plan shape (the exact-string source requirement fires later, with the schema in hand).
    shape = payload_shape({"f": (strs(SRC), faker_seed())})
    assert out_of_core_admission(shape.plan, registry=s.REGISTRY, graph=shape.graph) == (True, None)


def test_the_real_gate_declines_a_positional_faker_for_the_out_of_core_route() -> None:
    # A positional faker is not the ported deterministic-REUSE shape, so it still declines and
    # keeps the sequential/full-frame route.
    shape = payload_shape({"f": (strs(SRC), positional_seed())})
    assert out_of_core_admission(shape.plan, registry=s.REGISTRY, graph=shape.graph) == (
        False,
        _OOC_CODE,
    )


@pytest.mark.parametrize(
    "estimate",
    [True, False, None],
    ids=["fits", "does-not-fit", "unpriceable"],
)
def test_byte_estimate_routing_for_a_faker_job(estimate: bool | None) -> None:
    # B4: the deterministic-REUSE faker job is bounded-eligible, so byte-estimate routing sends
    # it to the out-of-core route whether the full-frame fit estimate is a fit, a miss, or
    # unpriceable -- a fit no longer pulls a bounded-eligible job onto full_frame, and the row
    # threshold is not consulted in this mode.
    assert (
        _route(
            _shape(),
            use_byte_estimate_routing=True,
            use_probe_routing=False,
            full_frame_fits_estimate=estimate,
        )
        == _OUT_OF_CORE_BYTE
    )


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (4_999_999, _SEQUENTIAL),
        (5_000_000, _OUT_OF_CORE_LARGE),
        (50_000_000, _OUT_OF_CORE_LARGE),
    ],
    ids=["below", "at", "far-above"],
)
def test_f7b_flag_off_row_threshold_routes_by_size(rows: int, expected: Any) -> None:
    # `use_byte_estimate_routing=False` falls to the row-threshold path. The deterministic-REUSE
    # faker job is out-of-core compatible (B4), so below the threshold it stays sequential and at
    # or above it diverts to the out-of-core route.
    assert (
        _route(
            _shape(),
            use_byte_estimate_routing=False,
            full_frame_fits_estimate=True,  # must be ignored with the flag off
            largest_table_rows=rows,
            out_of_core_threshold_rows=5_000_000,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("sequential", _SEQUENTIAL),
        ("full_frame", ("full_frame", "override_full_frame")),
    ],
)
def test_explicit_execution_mode_is_honoured(mode: str, expected: Any) -> None:
    assert _route(_shape(), execution_mode=mode, full_frame_fits_estimate=True) == expected


def test_explicit_out_of_core_on_a_deterministic_faker_job_is_honoured() -> None:
    # B4: the deterministic-REUSE faker job is out-of-core-eligible, so the explicit mode runs it
    # there instead of failing closed.
    assert _route(_shape(), execution_mode="out_of_core") == ("out_of_core", "override_out_of_core")


def test_explicit_out_of_core_on_a_positional_faker_job_fails_closed() -> None:
    # A positional faker is not out-of-core-eligible, so the explicit mode still fails closed.
    shape = payload_shape({"f": (strs(SRC), positional_seed()), "p": (strs(SRC), PASSTHROUGH)})
    with pytest.raises(ConfigError, match=_OOC_CODE):
        _route(shape, execution_mode="out_of_core")


# End to end through run_pipeline on a real faker config.


def _config(tmp_path: Path, deterministic: bool) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    parent = pa.table({"f": ["a", "b", None, "c"], "k": ["k1", "k2", "k3", "k4"]})
    child = pa.table({"k": ["k1", "k2", "k9"]})
    for name, table in (("P", parent), ("C", child)):
        pq.write_table(table, tmp_path / f"{name}.parquet")
    column: dict[str, Any] = {
        "name": "f",
        "strategy": "faker",
        "provider": FIRST,
        "deterministic": deterministic,
        "pool_size": 40,
        "namespace": "ns_f",
    }
    config: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "faker-route", "seed": 7},
        "sources": {
            t: {"type": "file", "path": str(tmp_path / f"{t}.parquet"), "format": "parquet"}
            for t in ("P", "C")
        },
        "targets": {
            t: {"type": "file", "path": str(tmp_path / f"{t}.out.parquet"), "format": "parquet"}
            for t in ("P", "C")
        },
        "tables": [
            {
                "name": "P",
                "columns": [column, {"name": "k", "strategy": "hash", "namespace": "ns_k"}],
            },
            {"name": "C", "columns": [{"name": "k", "strategy": "hash", "namespace": "ns_k"}]},
        ],
        "relationships": [
            {
                "parent": {"table": "P", "columns": ["k"]},
                "children": [{"table": "C", "columns": ["k"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_k",
            }
        ],
    }
    return config, {"P": parent, "C": child}


def _run(tmp_path: Path, deterministic: bool = True, **kw: Any) -> Any:
    config, sources = _config(tmp_path, deterministic)
    return run_pipeline(
        config, sources, engine_version="0.1.0", out_of_core_budget_bytes=1 << 30, **kw
    )


def _visible(result: Any) -> tuple[str, str]:
    execution = result.quality_metrics["execution"]
    return execution["execution_mode"], execution["route_reason"]


def test_default_routing_runs_a_small_deterministic_faker_job_out_of_core(tmp_path: Path) -> None:
    # B4: a deterministic-REUSE faker job is bounded-eligible, so default (byte-estimate) routing
    # sends it to the out-of-core route regardless of size; the faker column runs there (B3/B4),
    # not the full-frame oracle.
    result = _run(tmp_path)
    assert _visible(result) == _OUT_OF_CORE_BYTE
    [entry] = result.quality_metrics[_OOC_FAKER_KEY]
    assert (entry["column"], entry["path"], entry["executed_backend"]) == (
        "f",
        "mask_batch",
        _OOC_FAKER_BACKEND,
    )


@pytest.mark.parametrize("deterministic", [True, False])
@pytest.mark.parametrize(
    "kw",
    [{"use_byte_estimate_routing": False}, {"execution_mode": "sequential"}],
    ids=["flag-off", "explicit-sequential"],
)
def test_sequential_route_reports_its_reason_and_its_faker_backend(
    tmp_path: Path, kw: dict[str, Any], deterministic: bool
) -> None:
    result = _run(tmp_path, deterministic, **kw)
    assert _visible(result) == _SEQUENTIAL
    [entry] = faker_entries(result)
    assert (entry["column"], entry["path"], entry["executed_backend"]) == (
        "f",
        "scalar",
        _SEQ_BACKEND,
    )
    assert entry["native_calls"] == (1 if _SEQ_BACKEND == "rust_companion" else 0)


@pytest.mark.parametrize("threshold", [3, 4, 5], ids=["below", "at", "above"])
def test_f7b_flag_off_threshold_routes_a_faker_job_by_size_and_runs_it_native(
    tmp_path: Path, threshold: int
) -> None:
    # The flag disables byte-estimate ROUTING, not native EXECUTION. The largest table has 4
    # rows, so thresholds 3 and 4 sit at or below it (out-of-core) and 5 above it (sequential).
    # Either way the faker column runs native (B4 out-of-core, or the B-phase sequential seam),
    # never the oracle.
    result = _run(tmp_path, use_byte_estimate_routing=False, out_of_core_threshold_rows=threshold)
    if threshold <= 4:
        assert _visible(result) == _OUT_OF_CORE_LARGE
        [entry] = result.quality_metrics[_OOC_FAKER_KEY]
        assert entry["executed_backend"] == _OOC_FAKER_BACKEND
    else:
        assert _visible(result) == _SEQUENTIAL
        [entry] = faker_entries(result)
        assert entry["executed_backend"] == _SEQ_BACKEND


def test_explicit_full_frame_is_reported_and_out_of_core_is_honoured(tmp_path: Path) -> None:
    assert _visible(_run(tmp_path, execution_mode="full_frame")) == (
        "full_frame",
        "override_full_frame",
    )
    # B4: the deterministic-REUSE faker job is out-of-core-eligible, so the explicit out-of-core
    # mode now runs it there instead of being refused.
    assert _visible(_run(tmp_path, execution_mode="out_of_core")) == (
        "out_of_core",
        "override_out_of_core",
    )


@pytest.mark.parametrize("deterministic", [True, False])
def test_full_frame_and_sequential_routes_return_strictly_equal_faker_tables(
    tmp_path: Path, deterministic: bool
) -> None:
    from tests.parity import _strict_compare as sc

    full = _run(tmp_path, deterministic, execution_mode="full_frame")
    sequential = _run(tmp_path, deterministic, execution_mode="sequential")
    for table in ("P", "C"):
        assert sc.compare_strict(full.outputs[table], sequential.outputs[table]) == dict.fromkeys(
            sc.DIMENSIONS, True
        )
