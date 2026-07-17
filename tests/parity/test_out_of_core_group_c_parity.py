"""SC4 parity: out-of-core Group (c) strategies vs the pandas oracle.

`test_out_of_core_group_b_parity.py` pins SC3's `fpe/text_redact/categorical`.
This file pins the SC4 widening: the batch-local Group (c) strategies ported onto
the out-of-core kernel (`text_mask`, `code_set` in mask mode, `bucket_perturb`
with an explicit date_format) are byte-identical to `PandasExecutionAdapter.run`
(the oracle) when masking payload columns of an FK job -- including under a FORCED
SMALL BATCH SIZE, which is where a non-batch-local port would diverge -- and the
deliberately-deferred strategies (`geo_generalize`, `formula`, `derived`,
`nested`) plus the unsupported config shapes (code_set gen/chapter_preserve,
bucket_perturb auto-detect, Group (c) as an FK parent key) are a fail-closed gate
MISS (the route never runs, so it never emits divergent output; the job falls
back to full-frame).
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_NS = NamespaceRegistry(bindings=())
_JOB_SEED = b"\x11" * 8


def _seed(
    strategy: str,
    *,
    namespace: str | None = None,
    provider_config: tuple[tuple[str, Any], ...] = (),
    deterministic: bool | None = None,
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=(namespace is not None) if deterministic is None else deterministic,
        provider_config=provider_config,
        coherent_with=(),
    )


def _plan(per_table: tuple[tuple[str, TableSeed], ...]) -> Any:
    return SimpleNamespace(seed_envelope=SeedEnvelope(job_seed=_JOB_SEED, per_table=per_table))


def _fold(v: object) -> object:
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _comparable(table: pa.Table) -> dict[str, list[object]]:
    return {name: [_fold(v) for v in col] for name, col in table.to_pydict().items()}


def _assert_value_equal(oracle: pa.Table, ooc: pa.Table, label: str) -> None:
    got, want = _comparable(ooc), _comparable(oracle)
    assert set(got) == set(want), f"{label}: column mismatch {set(got)} vs {set(want)}"
    for name in want:
        assert got[name] == want[name], (
            f"{label}: column {name!r} diverges\n oracle={want[name]}\n    ooc={got[name]}"
        )


def _gate_admits(plan: Any, graph: RelationshipGraph) -> bool:
    work = order_work(build_work_list(plan, _REG), graph)
    return check_out_of_core_compatibility(plan, work, graph).accepted


def _gate_codes(plan: Any, graph: RelationshipGraph) -> set[str]:
    work = order_work(build_work_list(plan, _REG), graph)
    return {r.code for r in check_out_of_core_compatibility(plan, work, graph).rejections}


# ---------------------------------------------------------------------------
# Payload-column parity: parent + child each carry a Group (c) payload column,
# the FK key stays hash (an SC1-supported key). Orphans + nulls included, and
# the run is forced through a SMALL batch size so batch-locality is exercised.
# ---------------------------------------------------------------------------

# Each entry: (payload seed, source values). Values are chosen so the transform
# does visible work (text_mask splices a span, code_set remaps to a corpus code,
# bucket_perturb snaps within the bucket) rather than trivially passing through.
_PAYLOADS: dict[str, tuple[ColumnSeed, list[str | None]]] = {
    "text_mask": (
        _seed("text_mask", provider_config=(("token", "[X]"),)),
        [
            "call me at 415-555-1234 today",
            "ssn 123-45-6789 on file",
            None,
            "email a@b.com please",
            "no pii here at all",
            "reach 202-555-0147 asap",
        ],
    ),
    "text_mask_passthrough": (
        _seed(
            "text_mask",
            provider_config=(("unmatched_span_policy", "passthrough"), ("token", "[R]")),
        ),
        [
            "ssn 123-45-6789 here",
            "phone 415-555-1234 now",
            None,
            "nothing to see",
            "card 4111111111111111 ok",
            "plain text only",
        ],
    ),
    "code_set_mask": (
        _seed("code_set", namespace="cs", provider_config=(("code_set", "mcc"),)),
        ["alpha", "beta", None, "gamma", "delta", "epsilon"],
    ),
    "bucket_perturb_month": (
        _seed(
            "bucket_perturb",
            namespace="bp",
            provider_config=(("bucket", "month"), ("date_format", "%Y-%m-%d")),
        ),
        ["2021-03-15", "2020-11-02", None, "2019-07-28", "2022-01-09", "2023-05-19"],
    ),
    "bucket_perturb_quarter": (
        _seed(
            "bucket_perturb",
            namespace="bpq",
            provider_config=(("bucket", "quarter"), ("date_format", "%Y-%m-%d")),
        ),
        ["2021-03-15", "2020-11-02", None, "2019-07-28", "2022-01-09", "2023-05-19"],
    ),
}


def _payload_edge_job(
    payload_seed: ColumnSeed, payload_vals: list[str | None], *, policy: OrphanPolicy
) -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    n = len(payload_vals)
    key = _seed("hash", namespace="kns")
    parent = pa.table(
        {
            "pk": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
            "pay": pa.array(payload_vals, type=pa.string()),
        }
    )
    child_fk = [f"p{i}" for i in range(n)]
    if policy is not OrphanPolicy.FAIL:
        child_fk[1] = "orphan-x"
    child = pa.table(
        {
            "fk": pa.array(child_fk, type=pa.string()),
            "cpay": pa.array(list(reversed(payload_vals)), type=pa.string()),
        }
    )
    plan = _plan(
        (
            ("parent", TableSeed(per_column=(("pk", key), ("pay", payload_seed)), per_group=())),
            ("child", TableSeed(per_column=(("fk", key), ("cpay", payload_seed)), per_group=())),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace="kns",
                orphan_policy=policy,
            ),
        ),
        ordering=(),
    )
    return plan, {"parent": parent, "child": child}, graph


@pytest.mark.parametrize("kind", list(_PAYLOADS))
@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.WARN, OrphanPolicy.FAIL])
@pytest.mark.parametrize("batch_rows", [None, 2, 1])
def test_group_c_payload_parity(kind: str, policy: OrphanPolicy, batch_rows: int | None) -> None:
    payload_seed, payload_vals = _PAYLOADS[kind]
    plan, sources, graph = _payload_edge_job(payload_seed, payload_vals, policy=policy)
    assert _gate_admits(plan, graph), f"{kind}/{policy.name}: expected gate to admit"
    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    ooc = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, batch_rows=batch_rows
    )
    for table in oracle.outputs:
        _assert_value_equal(
            oracle.outputs[table],
            ooc.outputs[table],
            f"{kind}/{policy.name}/batch={batch_rows}:{table}",
        )


@pytest.mark.parametrize("kind", list(_PAYLOADS))
def test_group_c_payload_actually_transforms(kind: str) -> None:
    """Guard against a no-op port: the masked payload must differ from the source
    for at least one non-null row (else a broken port returning the input would
    still pass parity against a matching oracle)."""
    payload_seed, payload_vals = _PAYLOADS[kind]
    plan, sources, graph = _payload_edge_job(
        payload_seed, payload_vals, policy=OrphanPolicy.PRESERVE
    )
    ooc = run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph)
    src = sources["parent"].column("pay").to_pylist()
    out = ooc.outputs["parent"].column("pay").to_pylist()
    changed = [o for s, o in zip(src, out, strict=True) if s is not None and o != s]
    assert changed, f"{kind}: masked payload never differs from source (no-op port?)"


@pytest.mark.parametrize("kind", list(_PAYLOADS))
@pytest.mark.parametrize("batch_rows", [None, 2, 1])
def test_group_c_all_null_column_parity(kind: str, batch_rows: int | None) -> None:
    """An entirely-null payload column must reconcile null-typed/all-null against
    the oracle, at whole-column, multi-batch, and single-row batch sizes -- a
    fully-null batch has no non-null value to drive the per-value kernel, which
    is where a batch-local assumption (e.g. an implicit dtype inferred from the
    first non-null value) would silently diverge from the oracle."""
    payload_seed, payload_vals = _PAYLOADS[kind]
    all_null_vals: list[str | None] = [None] * len(payload_vals)
    plan, sources, graph = _payload_edge_job(
        payload_seed, all_null_vals, policy=OrphanPolicy.PRESERVE
    )
    assert _gate_admits(plan, graph), f"{kind}: expected gate to admit an all-null payload"
    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    ooc = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, batch_rows=batch_rows
    )
    for table in oracle.outputs:
        _assert_value_equal(
            oracle.outputs[table], ooc.outputs[table], f"{kind}/all_null/batch={batch_rows}:{table}"
        )


# ---------------------------------------------------------------------------
# Fail-closed MISS: Group (c) as an FK PARENT KEY, deferred strategies, and the
# conditionally-unsupported config shapes of the ported strategies.
# ---------------------------------------------------------------------------


def _key_strategy_job(key_seed: ColumnSeed) -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    parent = pa.table({"pk": pa.array(["100", "200", "300"], type=pa.string())})
    child = pa.table({"fk": pa.array(["100", "200", "300"], type=pa.string())})
    plan = _plan(
        (
            ("parent", TableSeed(per_column=(("pk", key_seed),), per_group=())),
            ("child", TableSeed(per_column=(("fk", key_seed),), per_group=())),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace=key_seed.namespace or "kns",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )
    return plan, {"parent": parent, "child": child}, graph


@pytest.mark.parametrize(
    "key_seed",
    [
        _seed("text_mask"),
        _seed("code_set", namespace="cs", provider_config=(("code_set", "mcc"),)),
        _seed(
            "bucket_perturb",
            namespace="bp",
            provider_config=(("bucket", "month"), ("date_format", "%Y-%m-%d")),
        ),
    ],
)
def test_group_c_as_parent_key_is_gate_miss(key_seed: ColumnSeed) -> None:
    plan, _sources, graph = _key_strategy_job(key_seed)
    assert not _gate_admits(plan, graph)
    assert "out_of_core_parent_strategy_unsupported" in _gate_codes(plan, graph)


@pytest.mark.parametrize(
    ("strategy", "provider_config", "code"),
    [
        ("geo_generalize", (), "out_of_core_whole_column_aggregation_unsupported"),
        ("formula", (("formula", "value"),), "out_of_core_dynamic_output_type_unsupported"),
        ("derived", (("expression", "a"),), "out_of_core_dynamic_output_type_unsupported"),
        (
            "nested",
            (("target", "$.x"), ("strategy", "redact")),
            "out_of_core_child_dispatch_unsupported",
        ),
    ],
)
def test_deferred_group_c_is_documented_gate_miss(
    strategy: str, provider_config: tuple[tuple[str, Any], ...], code: str
) -> None:
    seed = _seed(strategy, namespace="dns", provider_config=provider_config)
    plan, _sources, graph = _payload_edge_job(seed, ["1", "2", "3"], policy=OrphanPolicy.PRESERVE)
    assert not _gate_admits(plan, graph)
    assert code in _gate_codes(plan, graph)


@pytest.mark.parametrize(
    ("provider_config", "code"),
    [
        (
            (("code_set", "mcc"), ("mode", "gen")),
            "out_of_core_code_set_shape_unsupported",
        ),
        (
            (("code_set", "mcc"), ("chapter_preserve", True)),
            "out_of_core_code_set_shape_unsupported",
        ),
    ],
)
def test_code_set_unsupported_shape_is_gate_miss(
    provider_config: tuple[tuple[str, Any], ...], code: str
) -> None:
    seed = _seed("code_set", namespace="cs", provider_config=provider_config)
    plan, _sources, graph = _payload_edge_job(seed, ["a", "b", "c"], policy=OrphanPolicy.PRESERVE)
    assert not _gate_admits(plan, graph)
    assert code in _gate_codes(plan, graph)


def test_bucket_perturb_autodetect_is_gate_miss() -> None:
    # No explicit date_format -> whole-column format detection -> fail-closed MISS.
    seed = _seed("bucket_perturb", namespace="bp", provider_config=(("bucket", "month"),))
    plan, _sources, graph = _payload_edge_job(
        seed, ["2021-03-15", "2020-11-02", "2019-07-28"], policy=OrphanPolicy.PRESERVE
    )
    assert not _gate_admits(plan, graph)
    assert "out_of_core_bucket_perturb_autodetect_unsupported" in _gate_codes(plan, graph)


# ---------------------------------------------------------------------------
# HIGH-2 remediation: the out-of-core route must surface the same
# code_set_corpora provenance evidence the pandas/sequential routes merge
# into ExecutionResult.quality_metrics -- the exact route the large-healthcare
# (70k ICD) case HC-1 exists for actually takes.
# ---------------------------------------------------------------------------


class TestOutOfCoreCodeSetCorporaEvidence:
    def test_ooc_code_set_masking_surfaces_code_set_corpora(self) -> None:
        payload_seed, payload_vals = _PAYLOADS["code_set_mask"]
        plan, sources, graph = _payload_edge_job(
            payload_seed, payload_vals, policy=OrphanPolicy.PRESERVE
        )
        oracle = PandasExecutionAdapter().run(
            plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
        )
        ooc = run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph)

        oracle_corpora = oracle.quality_metrics.get("code_set_corpora")
        ooc_corpora = ooc.quality_metrics.get("code_set_corpora")
        assert oracle_corpora is not None and ooc_corpora is not None

        # Both routes stamp one entry per code_set column ("pay" on the parent,
        # "cpay" on the child, both configured for the "mcc" corpus); parity in
        # SHAPE (sorted by code_set-column identity), not raw list order.
        def _key(entries: list[dict[str, Any]]) -> list[tuple[str, int]]:
            return sorted((e["code_set"], e["row_count"]) for e in entries)

        assert _key(ooc_corpora) == _key(oracle_corpora)
        for entry in ooc_corpora:
            assert entry["code_set"] == "mcc"
            assert entry["row_count"] > 0
            # Counts + identifiers only -- no raw codes leak into evidence.
            assert "codes" not in entry
            assert "rows" not in entry

    def test_ooc_quality_metrics_omits_code_set_corpora_when_no_code_set_columns(self) -> None:
        payload_seed, payload_vals = _PAYLOADS["text_mask"]
        plan, sources, graph = _payload_edge_job(
            payload_seed, payload_vals, policy=OrphanPolicy.PRESERVE
        )
        ooc = run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph)
        assert "code_set_corpora" not in ooc.quality_metrics

    def test_ooc_sink_path_also_surfaces_code_set_corpora(self, tmp_path: Any) -> None:
        """The sink branch (`ExecutionResult(outputs={}, ...)`) must carry the
        same evidence as the in-memory branch -- both return sites were fixed."""
        from decoy_engine.execution import ParquetTransactionalSink

        payload_seed, payload_vals = _PAYLOADS["code_set_mask"]
        plan, sources, graph = _payload_edge_job(
            payload_seed, payload_vals, policy=OrphanPolicy.PRESERVE
        )
        ooc = run_fk_out_of_core(
            plan,
            sources,
            registry=_REG,
            relationship_graph=graph,
            sink=ParquetTransactionalSink(tmp_path / "published"),
        )
        corpora = ooc.quality_metrics.get("code_set_corpora")
        assert corpora is not None and len(corpora) == 2
        assert {e["code_set"] for e in corpora} == {"mcc"}
