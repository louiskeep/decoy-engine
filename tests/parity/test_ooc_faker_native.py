"""B4 acceptance: deterministic-REUSE faker payload on the out-of-core route.

Plan: docs/plans/2026-10-26-substrate-route-coverage.md (B4). The route declined
faker entirely before this slice (`out_of_core_faker_pool_unsupported`), so every
positive test here FAILS on the base commit (d613c804): forcing the out-of-core
route with a faker column raised at preflight there.

Compared here: the out-of-core native route vs the full-frame pandas oracle
(`PandasExecutionAdapter.run`, the reference), across batch splits and all three
drivers (resident, sink-batch, sink-reorder); repeated values, nulls, unicode and
empties; both allowlisted providers; one pool build reused across batches; the
compiled index selection actually consumed; a non-string source and the
unsupported shapes declined/fail-closed with caller-visible reasons; a late
kernel fault publishing nothing; and the companion-absent reference path.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._operator_registry import PYTHON_SCALAR, RUST_POOL_SELECT
from tests.parity import _strict_compare as sc
from tests.parity._d1a_hash_data import chain_graph, chain_plan, chain_sources, run_chain
from tests.parity._ooc_faker_data import (
    FAKER_KEY,
    SOURCE,
    FakerConsumedWitness,
    faker_seed,
    hide_companion,
    install_marking,
    kernel_loads,
)
from tests.parity.test_out_of_core_fk_parity import _run_oracle
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs

MODES = ["resident", "sink-batch", "sink-reorder"]
_PROVIDERS = ["person_first_name", "person_last_name"]
# Null positions in SOURCE, preserved by every faker mode.
_NULL_POS = [i for i, v in enumerate(SOURCE) if v is None]

pytestmark = pytest.mark.skipif(
    not kernel_loads(),
    reason="decoy-engine-native index kernel not loadable; the companion-present job covers this",
)


def _payload(values: list[str | None] = SOURCE) -> pa.Table:
    return pa.table({"a": pa.array(values, pa.string())})


def _seeds(provider: str = "person_first_name", **kw: object) -> tuple[tuple[str, object], ...]:
    return (("a", faker_seed(provider, **kw)),)  # type: ignore[arg-type]


def _oracle_col(payload: pa.Table, seeds: object) -> pa.Array:
    out = _run_oracle(chain_plan(seeds), chain_sources(payload), chain_graph()).outputs
    return out["t"].column("a").combine_chunks()


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("batch_rows", [None, 1, 3, 7])
def test_forced_ooc_faker_is_the_oracle_and_is_consumed(
    mode: str, batch_rows: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeds = _seeds()
    kernel = install_marking(monkeypatch)
    witness = FakerConsumedWitness()
    with witness.observe():
        run = run_chain(_payload(), seeds, batch_rows=batch_rows, **mode_kwargs(mode, tmp_path))
    witness.assert_consumed(kernel)
    got = run.tables["t"].column("a").combine_chunks()
    assert got.type == pa.string()
    oracle = _oracle_col(_payload(), seeds)
    assert got.to_pylist() == oracle.to_pylist()
    for pos in _NULL_POS:
        assert got[pos].as_py() is None
    disposition = {e["column"]: e for e in run.result.quality_metrics[FAKER_KEY]}
    assert disposition["a"]["executed_backend"] == RUST_POOL_SELECT
    assert "reason" not in disposition["a"]


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_both_allowlisted_providers_match_the_oracle(provider: str) -> None:
    seeds = _seeds(provider)
    got = run_chain(_payload(), seeds, batch_rows=3).tables["t"].column("a").to_pylist()
    assert got == _oracle_col(_payload(), seeds).to_pylist()


def test_repeated_values_are_deterministic_across_batch_boundaries() -> None:
    seeds = _seeds()
    whole = run_chain(_payload(), seeds).tables["t"].column("a").to_pylist()
    split = run_chain(_payload(), seeds, batch_rows=1).tables["t"].column("a").to_pylist()
    assert whole == split  # a batch boundary never changes a source-keyed selection
    # "alice" at 0 and 2, "Bob" at 1 and 7: a repeated source maps to one name.
    assert whole[0] == whole[2]
    assert whole[1] == whole[7]
    assert whole[5] is None and whole[8] is None


def test_one_pool_build_is_reused_across_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.generation.pool import _builder

    real = _builder.PoolBuilder.build
    calls = {"n": 0}

    def counting(self: object, provider: str, **kw: object) -> object:
        calls["n"] += 1
        return real(self, provider, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(_builder.PoolBuilder, "build", counting)
    run_chain(_payload(), _seeds(), batch_rows=2)  # several batches, one table
    assert calls["n"] == 1


def test_two_columns_with_one_identity_share_one_build(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.generation.pool import _builder

    real = _builder.PoolBuilder.build
    calls = {"n": 0}

    def counting(self: object, provider: str, **kw: object) -> object:
        calls["n"] += 1
        return real(self, provider, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(_builder.PoolBuilder, "build", counting)
    seeds = (("a", faker_seed()), ("b", faker_seed()))
    payload = pa.table({"a": pa.array(SOURCE, pa.string()), "b": pa.array(SOURCE, pa.string())})
    run = run_chain(payload, seeds, batch_rows=3)
    assert calls["n"] == 1  # identical identity => one pool
    assert run.tables["t"].column("a").to_pylist() == run.tables["t"].column("b").to_pylist()


def test_all_three_drivers_agree_with_the_oracle_byte_for_byte(tmp_path: Path) -> None:
    seeds = _seeds()
    oracle = _oracle_col(_payload(), seeds)
    for mode in MODES:
        got = (
            run_chain(_payload(), seeds, batch_rows=3, **mode_kwargs(mode, tmp_path / mode))
            .tables["t"]
            .column("a")
            .combine_chunks()
        )
        report = sc.compare_strict(
            pa.table({"a": oracle}), pa.table({"a": got})
        )
        assert {dim for dim, agree in report.items() if not agree} <= {"field_metadata"}, mode


def test_forced_ooc_nonstring_source_refuses_before_any_output(tmp_path: Path) -> None:
    # run_chain forces the out-of-core route; the preflight refuses a non-string
    # faker source before any scratch/sink write (a FORCED route checks refusal).
    seeds = _seeds()  # admitted shape, but the source column is int
    payload = pa.table({"a": pa.array([1, 2, 1, 4, 5, None, 7, 2, None, 10], pa.int64())})
    with pytest.raises(ExecutionError) as exc:
        run_chain(payload, seeds, out_dir=tmp_path / "out")
    assert exc.value.code == "out_of_core_faker_source_type_unsupported"
    assert not (tmp_path / "out" / "t.parquet").exists()  # no partial publication
    # The full-frame oracle masks this exact int-source faker to names WITHOUT
    # error, so it is a real config: a forced out-of-core job refuses it, but an
    # auto-routed job MUST reroute to the oracle (next test), never abort. This
    # assertion fails if the oracle also raised (which would make the refusal a
    # non-regression) -- it does not, which is why the routing gate below matters.
    oracle = _oracle_col(payload, seeds)
    assert oracle.type == pa.string()
    assert oracle.to_pylist()[0] is not None  # a real masked name, not a null/raw value


def test_routing_reroutes_a_nonstring_faker_source_instead_of_admitting_it() -> None:
    # The no-regression fix: with the schema in hand, routing declines a
    # non-string faker source so the job reroutes to full-frame/sequential (which
    # masks it) rather than being admitted to OOC and aborting mid-run.
    from decoy_engine.execution._pipeline_routing_signals import out_of_core_admission
    from decoy_engine.providers_v2 import get_default_registry

    plan = chain_plan(_seeds())
    graph = chain_graph()
    reg = get_default_registry()
    ok_string, _ = out_of_core_admission(
        plan, registry=reg, graph=graph, source_types={("t", "a"): pa.string()}
    )
    assert ok_string  # exact-string source is admitted to OOC
    ok_int, code_int = out_of_core_admission(
        plan, registry=reg, graph=graph, source_types={("t", "a"): pa.int64()}
    )
    assert not ok_int  # non-string reroutes
    assert code_int == "out_of_core_faker_source_type_unsupported"
    ok_large, code_large = out_of_core_admission(
        plan, registry=reg, graph=graph, source_types={("t", "a"): pa.large_string()}
    )
    assert not ok_large  # large_string is not exact string -> reroute, not abort
    assert code_large == "out_of_core_faker_source_type_unsupported"
    # With no schema (the capacity estimator), admission is shape-only.
    ok_blind, _ = out_of_core_admission(plan, registry=reg, graph=graph, source_types=None)
    assert ok_blind


@pytest.mark.parametrize(
    "seed_kwargs",
    [
        pytest.param({"deterministic": False}, id="positional-reuse"),
        pytest.param({"provider": "address_city"}, id="non-allowlisted-provider"),
        pytest.param({"pool_size": None}, id="no-pool-size"),
        pytest.param({"cardinality_mode": "unique"}, id="unique-mode"),
        pytest.param({"namespace": None}, id="no-namespace"),
    ],
)
def test_unsupported_faker_shapes_decline_with_a_caller_visible_reason(
    seed_kwargs: dict[str, object],
) -> None:
    seeds = (("a", faker_seed(**seed_kwargs)),)  # type: ignore[arg-type]
    with pytest.raises(ExecutionError) as exc:
        run_chain(_payload(), seeds)
    assert exc.value.code == "out_of_core_faker_pool_unsupported"


def test_a_late_kernel_fault_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_marking(monkeypatch, fail_on_call=1)  # the first selection raises
    with pytest.raises(RuntimeError, match="injected faker kernel fault"):
        run_chain(_payload(), _seeds(), out_dir=tmp_path / "out")
    assert not (tmp_path / "out" / "t.parquet").exists()


def test_companion_absent_matches_the_oracle_and_records_the_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hide_companion(monkeypatch)
    seeds = _seeds()
    run = run_chain(_payload(), seeds, batch_rows=3)
    got = run.tables["t"].column("a").to_pylist()
    # The oracle keeps the compiled kernel; the reference kernel is KAT-equal.
    assert got == _oracle_col(_payload(), seeds).to_pylist()
    disposition = {e["column"]: e for e in run.result.quality_metrics[FAKER_KEY]}
    assert disposition["a"]["executed_backend"] == PYTHON_SCALAR
