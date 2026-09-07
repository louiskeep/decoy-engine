"""Parity harness for the standalone sequential reorder driver (P4-A Task 6).

`_stream_driver.stream_table` drives `StreamFkJoiner.run_ordered_join` (the
bounded-sorter order-restore path) instead of `_batch_join.ChildFkBatchJoiner`
(the live `_batch_join` route `run_fk_out_of_core` drives). This module is not
wired into any route yet (that is Task 7); `_stream_driver_harness.
run_stream_driver` drives tables through it in topological order, mirroring
`_runner.run_fk_out_of_core`'s own outer loop.

Tests #1-#4, #7, #8 of `docs/plans/2026-09-02-p4-task6-reorder-driver.md`
section 4. #5, #6, #7b live in
`tests/unit/execution/test_stream_driver_lifecycle.py`.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import HealthCheck, given, settings

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
from decoy_engine.execution.out_of_core._external_sort import BoundedExternalSorter
from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from tests.parity.test_out_of_core_fk_parity import (
    _assert_value_equal,
    _build_chain,
    _build_chain_n,
    _build_fanout,
    _build_single_edge,
    _chain_case,
    _fanout_case,
    _gate_admits,
    _run_oracle,
    _single_edge_case,
)
from tests.unit.execution._stream_driver_harness import run_stream_driver

_REG = get_default_registry()
_NS = NamespaceRegistry(bindings=())
_SEED = b"\x55" * 8


def _col(strategy: str, *, namespace: str | None = None) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=(),
        coherent_with=(),
    )


def _assert_strong_equal(oracle: pa.Table, driver: pa.Table, label: str) -> None:
    """The STRONG contract (plan section 3): schema + Arrow type + metadata,
    row order, values, nulls -- not merely `to_pydict()` value equality.

    `pa.Table.equals` is bitwise (IEEE 754: NaN != NaN), so a byte-identical
    NaN payload in BOTH tables fails it even though the two routes agree
    exactly; fall back to a per-column compare that folds NaN-at-the-same-
    position to equal (a real value divergence, NaN or not, still fails).
    """
    assert driver.schema.equals(oracle.schema, check_metadata=True), (
        f"{label}: schema diverges\n oracle={oracle.schema}\n driver={driver.schema}"
    )
    if driver.equals(oracle, check_metadata=True):
        return
    assert driver.num_rows == oracle.num_rows, f"{label}: row count diverges"
    for name in driver.schema.names:
        d_col = driver.column(name).combine_chunks()
        o_col = oracle.column(name).combine_chunks()
        if d_col.equals(o_col):
            continue
        if not pa.types.is_floating(d_col.type):
            raise AssertionError(
                f"{label}: column {name!r} diverges\n oracle={o_col}\n driver={d_col}"
            )
        for i, (dv, ov) in enumerate(zip(d_col.to_pylist(), o_col.to_pylist(), strict=True)):
            same = dv == ov or (
                isinstance(dv, float)
                and isinstance(ov, float)
                and math.isnan(dv)
                and math.isnan(ov)
            )
            assert same, f"{label}: column {name!r}[{i}] diverges: driver={dv!r} oracle={ov!r}"


def _warning_key(warning: Any) -> tuple[object, ...]:
    return (warning.code, warning.provider, warning.column, warning.detail)


# ---------------------------------------------------------------------------
# #1: differential STRONG parity vs `run_fk_out_of_core` (_batch_join route)
# ---------------------------------------------------------------------------


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(_single_edge_case())
def test_reorder_matches_batch_join_single_edge(
    case: tuple[Any, dict[str, pa.Table], RelationshipGraph, str],
) -> None:
    plan, sources, graph, label = case
    if not _gate_admits(plan, graph):
        return
    try:
        batch_join_res = run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph)
    except Exception:
        return  # a fail-closed rejection on this shape is out of this test's scope
    driver_res = run_stream_driver(plan, sources, graph, temp_dir=_tmp_for(label))
    for table in batch_join_res.outputs:
        _assert_strong_equal(
            batch_join_res.outputs[table], driver_res.outputs[table], f"{label}:{table}"
        )
    assert [_warning_key(w) for w in driver_res.warnings] == [
        _warning_key(w) for w in batch_join_res.warnings
    ]


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(_chain_case())
def test_reorder_matches_batch_join_chain(
    case: tuple[Any, dict[str, pa.Table], RelationshipGraph, str],
) -> None:
    plan, sources, graph, label = case
    if not _gate_admits(plan, graph):
        return
    try:
        batch_join_res = run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph)
    except Exception:
        return
    driver_res = run_stream_driver(plan, sources, graph, temp_dir=_tmp_for(label))
    for table in batch_join_res.outputs:
        _assert_strong_equal(
            batch_join_res.outputs[table], driver_res.outputs[table], f"{label}:{table}"
        )
    assert [_warning_key(w) for w in driver_res.warnings] == [
        _warning_key(w) for w in batch_join_res.warnings
    ]


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(_fanout_case())
def test_reorder_matches_batch_join_fanout(
    case: tuple[Any, dict[str, pa.Table], RelationshipGraph, str],
) -> None:
    plan, sources, graph, label = case
    if not _gate_admits(plan, graph):
        return
    try:
        batch_join_res = run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph)
    except Exception:
        return
    driver_res = run_stream_driver(plan, sources, graph, temp_dir=_tmp_for(label))
    for table in batch_join_res.outputs:
        _assert_strong_equal(
            batch_join_res.outputs[table], driver_res.outputs[table], f"{label}:{table}"
        )
    assert [_warning_key(w) for w in driver_res.warnings] == [
        _warning_key(w) for w in batch_join_res.warnings
    ]


_TMP_COUNTER = [0]


def _tmp_for(label: str) -> Path:
    """A fresh temp dir per hypothesis example (pytest's `tmp_path` fixture is
    function-scoped, not example-scoped, so property tests roll their own)."""
    _TMP_COUNTER[0] += 1
    safe = "".join(c if c.isalnum() else "_" for c in label)
    return Path(tempfile.mkdtemp(prefix=f"reorder-parity-{_TMP_COUNTER[0]}-{safe}-"))


# ---------------------------------------------------------------------------
# #2: anchor to the pandas oracle directly, four shapes, value parity under
# the suite's documented normalizations (NaN<->null, decimal scale).
# ---------------------------------------------------------------------------


def test_reorder_matches_pandas_oracle_single_edge(tmp_path: Any) -> None:
    plan, sources, graph = _build_single_edge(
        parent_kind="str",
        child_kind="str",
        strategy="hash",
        policy=OrphanPolicy.PRESERVE,
        parent_rows=5,
        child_refs=[0, 1, -1, 2, None, 0],
        parent_nan_row=None,
    )
    oracle = _run_oracle(plan, sources, graph)
    driver_res = run_stream_driver(plan, sources, graph, temp_dir=tmp_path)
    for table in oracle.outputs:
        _assert_value_equal(
            oracle.outputs[table], driver_res.outputs[table], f"single_edge:{table}"
        )


def test_reorder_matches_pandas_oracle_chain(tmp_path: Any) -> None:
    plan, sources, graph = _build_chain(
        strategy="hash",
        policy=OrphanPolicy.WARN,
        child_refs=[0, 1, -1, None, 2, 0],
        gc_refs=[0, -1, None, 2, 3],
    )
    oracle = _run_oracle(plan, sources, graph)
    driver_res = run_stream_driver(plan, sources, graph, temp_dir=tmp_path)
    for table in oracle.outputs:
        _assert_value_equal(oracle.outputs[table], driver_res.outputs[table], f"chain:{table}")


def test_reorder_matches_pandas_oracle_deep_chain(tmp_path: Any) -> None:
    plan, sources, graph = _build_chain_n(
        strategy="passthrough",
        policy=OrphanPolicy.PRESERVE,
        depth=4,
        refs=[[0, 1, -1, None], [0, -1, 1, None], [0, 1, None, -1]],
    )
    oracle = _run_oracle(plan, sources, graph)
    driver_res = run_stream_driver(plan, sources, graph, temp_dir=tmp_path)
    for table in oracle.outputs:
        _assert_value_equal(oracle.outputs[table], driver_res.outputs[table], f"deep_chain:{table}")


def test_reorder_matches_pandas_oracle_fanout(tmp_path: Any) -> None:
    plan, sources, graph = _build_fanout(
        strategy="truncate",
        policy=OrphanPolicy.REMAP,
        parent_rows=4,
        refs_a=[0, 1, -1, None, 2],
        refs_b=[3, -1, 0, None],
    )
    oracle = _run_oracle(plan, sources, graph)
    driver_res = run_stream_driver(plan, sources, graph, temp_dir=tmp_path)
    for table in oracle.outputs:
        _assert_value_equal(oracle.outputs[table], driver_res.outputs[table], f"fanout:{table}")


# ---------------------------------------------------------------------------
# #3: orphan policies -- matched / FAIL / WARN / PRESERVE / REMAP, null FK,
# empty child, duplicate child keys, cross-batch and cross-run boundaries.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy", list(OrphanPolicy))
def test_orphan_policies_match_oracle(policy: OrphanPolicy, tmp_path: Any) -> None:
    """Matched, orphan (unless FAIL), null FK, and duplicate child keys, run
    with a small batch_rows and a small run_bytes_cap so the boundary spans
    multiple payload batches AND multiple sorter runs (cross-batch,
    cross-run)."""
    plan, sources, graph = _build_single_edge(
        parent_kind="str",
        child_kind="str",
        strategy="passthrough",
        policy=policy,
        parent_rows=6,
        # matched rows (dup keys included), nulls, and (for non-FAIL) orphans.
        child_refs=[0, 0, 1, None, 2, None]
        if policy is OrphanPolicy.FAIL
        else [0, 0, 1, None, -1, 2, -1, None, 3],
        parent_nan_row=None,
    )
    if policy is OrphanPolicy.FAIL:
        oracle = _run_oracle(plan, sources, graph)
        driver_res = run_stream_driver(
            plan, sources, graph, temp_dir=tmp_path, batch_rows=2, run_bytes_cap=256, merge_fan_in=2
        )
        for table in oracle.outputs:
            _assert_value_equal(
                oracle.outputs[table], driver_res.outputs[table], f"{policy}:{table}"
            )
        return
    oracle = _run_oracle(plan, sources, graph)
    driver_res = run_stream_driver(
        plan, sources, graph, temp_dir=tmp_path, batch_rows=2, run_bytes_cap=256, merge_fan_in=2
    )
    for table in oracle.outputs:
        _assert_value_equal(oracle.outputs[table], driver_res.outputs[table], f"{policy}:{table}")


def test_empty_child_matches_oracle(tmp_path: Any) -> None:
    plan, sources, graph = _build_single_edge(
        parent_kind="str",
        child_kind="str",
        strategy="passthrough",
        policy=OrphanPolicy.PRESERVE,
        parent_rows=3,
        child_refs=[],
        parent_nan_row=None,
    )
    oracle = _run_oracle(plan, sources, graph)
    driver_res = run_stream_driver(plan, sources, graph, temp_dir=tmp_path)
    for table in oracle.outputs:
        _assert_value_equal(
            oracle.outputs[table], driver_res.outputs[table], f"empty_child:{table}"
        )


def test_fail_policy_orphan_raises_same_as_oracle(tmp_path: Any) -> None:
    plan, sources, graph = _build_single_edge(
        parent_kind="str",
        child_kind="str",
        strategy="passthrough",
        policy=OrphanPolicy.FAIL,
        parent_rows=3,
        child_refs=[0, -1, 1],
        parent_nan_row=None,
    )
    with pytest.raises(ExecutionError):
        _run_oracle(plan, sources, graph)
    with pytest.raises(ExecutionError):
        run_stream_driver(plan, sources, graph, temp_dir=tmp_path)


# ---------------------------------------------------------------------------
# #4: combined decisive case -- multi-edge x REMAP x overlapping+distinct
# child columns (later-edge overwrite) x mismatched payload/join boundaries
# x a run_bytes_cap small enough to force multiple sorter runs per edge.
# ---------------------------------------------------------------------------


def _overlapping_remap_fixture() -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                ("parent_a", TableSeed(per_column=(("id", _col("passthrough")),), per_group=())),
                (
                    "parent_b",
                    TableSeed(
                        per_column=(("cid", _col("passthrough")), ("wid", _col("passthrough"))),
                        per_group=(),
                    ),
                ),
                ("child", TableSeed(per_column=(), per_group=())),
            ),
        )
    )
    edge_a = RelationshipEdge(
        parent_table="parent_a",
        parent_columns=("id",),
        child_table="child",
        child_columns=("a_id",),
        namespace="ns_a",
        orphan_policy=OrphanPolicy.REMAP,
    )
    # edge_b OVERLAPS edge_a on a_id and adds a DISTINCT column b_id: the
    # later edge (edge_b) must win a_id, matching the whole-child contract.
    edge_b = RelationshipEdge(
        parent_table="parent_b",
        parent_columns=("cid", "wid"),
        child_table="child",
        child_columns=("a_id", "b_id"),
        namespace="ns_b",
        orphan_policy=OrphanPolicy.REMAP,
    )
    graph = RelationshipGraph(edges=(edge_a, edge_b), ordering=())

    n_parent = 6
    parent_a = pa.table({"id": [f"a{i}" for i in range(n_parent)]})
    parent_b = pa.table(
        {"cid": [f"a{i}" for i in range(n_parent)], "wid": [f"w{i}" for i in range(n_parent)]}
    )
    n_child = 30
    a_ids: list[str] = []
    b_ids: list[str] = []
    for i in range(n_child):
        a_ids.append(f"orphanA{i}" if i % 5 == 0 else f"a{i % n_parent}")
        b_ids.append(f"orphanB{i}" if i % 4 == 0 else f"w{i % n_parent}")
    child = pa.table({"a_id": a_ids, "b_id": b_ids})
    return plan, {"parent_a": parent_a, "parent_b": parent_b, "child": child}, graph


def test_combined_multi_edge_remap_overlap_and_multi_run(tmp_path: Any) -> None:
    plan, sources, graph = _overlapping_remap_fixture()
    oracle = _run_oracle(plan, sources, graph)
    # batch_rows (payload boundary) deliberately differs from the sorter's own
    # merge output batching; run_bytes_cap/merge_fan_in are small enough (per
    # a calibration run) to force multiple BoundedExternalSorter flushes.
    driver_res = run_stream_driver(
        plan, sources, graph, temp_dir=tmp_path, batch_rows=4, run_bytes_cap=600, merge_fan_in=2
    )
    for table in oracle.outputs:
        _assert_value_equal(oracle.outputs[table], driver_res.outputs[table], f"combined:{table}")
    # Later-edge overwrite: a_id reflects edge_b's resolution (composite
    # parent_b), not edge_a's, on every row -- the oracle enforces the same
    # whole-child contract, so matching it above already proves this, but the
    # explicit check pins the exact regression the plan calls out.
    assert (
        driver_res.outputs["child"].column("a_id").to_pylist()
        == oracle.outputs["child"].column("a_id").to_pylist()
    )


def test_combined_case_forces_multiple_sorter_runs(tmp_path: Any, monkeypatch: Any) -> None:
    flush_calls: list[int] = []
    orig_flush = BoundedExternalSorter._flush

    def spy_flush(self: BoundedExternalSorter) -> None:
        flush_calls.append(1)
        return orig_flush(self)

    monkeypatch.setattr(BoundedExternalSorter, "_flush", spy_flush)
    plan, sources, graph = _overlapping_remap_fixture()
    run_stream_driver(
        plan, sources, graph, temp_dir=tmp_path, batch_rows=4, run_bytes_cap=600, merge_fan_in=2
    )
    assert len(flush_calls) > 1, "expected more than one BoundedExternalSorter flush"


# ---------------------------------------------------------------------------
# #7: degenerate all-null masked outgoing parent key -> correct outgoing
# parent-relation type (the ported masked_observed_types plumbing).
# ---------------------------------------------------------------------------


def test_all_null_redact_outgoing_key_sink_matches_batch_join(tmp_path: Any) -> None:
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                ("parent", TableSeed(per_column=(("key", _col("redact")),), per_group=())),
                ("child", TableSeed(per_column=(), per_group=())),
            ),
        )
    )
    edge = RelationshipEdge(
        parent_table="parent",
        parent_columns=("key",),
        child_table="child",
        child_columns=("fk",),
        namespace="ns",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    graph = RelationshipGraph(edges=(edge,), ordering=())
    sources = {
        "parent": pa.table({"key": pa.array([None, None], type=pa.int64())}),
        "child": pa.table({"fk": pa.array([7, 8], type=pa.int64())}),
    }

    batch_join_target = tmp_path / "batch-join"
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(batch_join_target),
        temp_dir=tmp_path / "work-batch-join",
    )
    driver_target = tmp_path / "driver"
    run_stream_driver(
        plan,
        sources,
        graph,
        temp_dir=tmp_path / "work-driver",
        sink=ParquetTransactionalSink(driver_target),
    )
    batch_join_child = pq.read_table(batch_join_target / "child.parquet")
    driver_child = pq.read_table(driver_target / "child.parquet")
    assert driver_child.column("fk").type == batch_join_child.column("fk").type
    assert driver_child.to_pydict() == batch_join_child.to_pydict()


# ---------------------------------------------------------------------------
# #8: route evidence -- the reorder path provably ran `run_ordered_join`, not
# `iter_join_rows` / `_batch_join`.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# code_set stamp parity: the phase-1 missing/NaN detection + the deferred
# `code_set_corpora` commit (docs/plans/2026-09-02-p4-task6-reorder-driver.md
# section 4's driver-specific code_set plumbing) are exercised by NO other
# driver test -- `run_stream_driver` never threaded `code_set_corpora` and no
# driver fixture used a `code_set` column, so `_code_set_records_and_evidence_
# for_table` returned empty and the withhold-vs-stamp branch was dead code
# under test. One column masks real values (earns its stamp); its FK-linked
# sibling is entirely null (withholds it) -- the same masked_any parity
# `test_out_of_core_group_c_parity.TestOutOfCoreCodeSetCorporaAllNullEvidence
# Parity` pins for the live `_batch_join` route.
# ---------------------------------------------------------------------------


def _code_set_col(*, namespace: str, code_set: str = "mcc") -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="code_set",
        provider="code_set",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(("code_set", code_set),),
        coherent_with=(),
    )


def _code_set_missing_vs_normal_fixture() -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "parent",
                    TableSeed(
                        per_column=(
                            ("pk", _col("hash", namespace="kns")),
                            ("pay", _code_set_col(namespace="cs")),
                        ),
                        per_group=(),
                    ),
                ),
                (
                    "child",
                    TableSeed(
                        per_column=(
                            ("fk", _col("hash", namespace="kns")),
                            ("cpay", _code_set_col(namespace="cs")),
                        ),
                        per_group=(),
                    ),
                ),
            ),
        )
    )
    edge = RelationshipEdge(
        parent_table="parent",
        parent_columns=("pk",),
        child_table="child",
        child_columns=("fk",),
        namespace="kns",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    graph = RelationshipGraph(edges=(edge,), ordering=())
    parent = pa.table(
        {
            "pk": pa.array(["p0", "p1", "p2"], type=pa.string()),
            # normal: at least one non-missing value -> earns the stamp.
            "pay": pa.array(["alpha", "beta", None], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "fk": pa.array(["p0", "p1", "p2"], type=pa.string()),
            # all-missing (all-null) -> masks zero values, withholds the stamp.
            "cpay": pa.array([None, None, None], type=pa.string()),
        }
    )
    return plan, {"parent": parent, "child": child}, graph


def test_code_set_stamp_parity_missing_vs_normal_column(tmp_path: Any) -> None:
    plan, sources, graph = _code_set_missing_vs_normal_fixture()
    oracle = _run_oracle(plan, sources, graph)
    code_set_corpora: dict[tuple[str, str], dict[str, Any]] = {}
    driver_res = run_stream_driver(
        plan, sources, graph, temp_dir=tmp_path, code_set_corpora=code_set_corpora
    )
    for table in oracle.outputs:
        _assert_value_equal(oracle.outputs[table], driver_res.outputs[table], f"code_set:{table}")

    # The oracle (pandas route) stamps only the non-missing column.
    oracle_corpora = oracle.quality_metrics.get("code_set_corpora")
    assert oracle_corpora is not None and len(oracle_corpora) == 1
    assert oracle_corpora[0]["table"] == "parent"
    assert oracle_corpora[0]["column"] == "pay"

    # Driver-side parity, straight off the threaded sink: the all-null sibling
    # withholds its stamp even though it shares the SAME edge and corpus as
    # the column that earns one.
    assert set(code_set_corpora) == {("parent", "pay")}
    driver_res_metrics = driver_res.quality_metrics.get("code_set_corpora")
    assert driver_res_metrics is not None and len(driver_res_metrics) == 1
    assert driver_res_metrics[0]["table"] == "parent"
    assert driver_res_metrics[0]["column"] == "pay"


def test_route_evidence_uses_run_ordered_join_not_batch_join(
    tmp_path: Any, monkeypatch: Any
) -> None:
    def _forbidden_batch_join_init(self: Any, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("the reorder driver must never construct ChildFkBatchJoiner")

    monkeypatch.setattr(ChildFkBatchJoiner, "__init__", _forbidden_batch_join_init)

    ordered_calls: list[int] = []
    orig_ordered = StreamFkJoiner.run_ordered_join

    def spy_ordered(self: StreamFkJoiner, *args: Any, **kwargs: Any) -> Any:
        ordered_calls.append(1)
        return orig_ordered(self, *args, **kwargs)

    monkeypatch.setattr(StreamFkJoiner, "run_ordered_join", spy_ordered)

    iter_calls: list[int] = []
    orig_iter = StreamFkJoiner.iter_join_rows

    def spy_iter(self: StreamFkJoiner, *args: Any, **kwargs: Any) -> Any:
        iter_calls.append(1)
        return orig_iter(self, *args, **kwargs)

    monkeypatch.setattr(StreamFkJoiner, "iter_join_rows", spy_iter)

    plan, sources, graph = _build_single_edge(
        parent_kind="str",
        child_kind="str",
        strategy="passthrough",
        policy=OrphanPolicy.PRESERVE,
        parent_rows=3,
        child_refs=[0, 1, -1],
        parent_nan_row=None,
    )
    result = run_stream_driver(plan, sources, graph, temp_dir=tmp_path)

    assert ordered_calls, "run_ordered_join was never called"
    assert not iter_calls, "iter_join_rows must never run on the reorder route"
    assert result.outputs["child"].num_rows == 3


__all__: list[str] = []
