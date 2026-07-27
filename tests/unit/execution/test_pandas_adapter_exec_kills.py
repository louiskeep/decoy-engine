"""Mutation-kill coverage for the full-frame execution + strategy-dispatch
cluster of `PandasExecutionAdapter` (execution/_pandas_adapter.py):
`run`, `_dispatch_mask_node`, and `run_sequential`.

Every test drives an observable machine field -- masked output bytes, the
strategy actually dispatched (via the timing record label), coded errors,
row-error records + FK cascade, the packaged `ExecutionResult` fields, and
the threaded kwargs (key_provider / relationship_graph / group-anchor
snapshots / lossless FK typing) -- never wall-clock magnitude (tq-findings
#18) and never message prose (house style: message is non-contractual).

Fixtures/patterns reuse the proven ones in test_pandas_adapter.py,
test_hash_bucketize.py, test_de03_output_projection.py,
test_run_pipeline_substrate.py, test_orphan_fk.py, test_de10_fk_lossless_typing.py,
test_code_set_cross_substrate_evidence.py, and tests/perf_fixtures/fk_relational.py.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ExecutionError, PandasExecutionAdapter
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._runner import WorkNode
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.instrumentation.timing import TimingCollector, use_collector
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.plan._types import ColumnSeed, GroupSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from tests.perf_fixtures.fk_relational import build_fk_relational

_REG = get_default_registry()
_EMPTY_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = (0x55).to_bytes(8, "big")
_SECRET = SecretKeyProvider(b"a-strong-32B+-managed-secret-value!!", key_version="v1")


# ---------------------------------------------------------------------------
# Config-compile helper (mirrors run_pipeline's compile block; used where a
# real date_shift / bucketize plan is needed).
# ---------------------------------------------------------------------------
def _compile(config: dict[str, Any]):
    from decoy_engine.plan import compile_plan
    from decoy_engine.plan._seed import _normalize_job_seed_int
    from decoy_engine.profile import profile_source
    from decoy_engine.relationships import (
        build_namespace_registry,
        build_relationship_graph,
        check_orphan_fk_policy_completeness,
    )

    job_seed = _normalize_job_seed_int(config)
    profile = profile_source(config, seed=job_seed)
    plan = compile_plan(config, profile, decoy_engine_version="0.1.0")
    ns = build_namespace_registry(config, profile)
    if profile.relationships:
        lookup = check_orphan_fk_policy_completeness(config, profile.relationships)
        graph = build_relationship_graph(
            profile.relationships, namespace_registry=ns, orphan_policy_lookup=lookup
        )
    else:
        graph = RelationshipGraph(edges=(), ordering=())
    return plan, graph, ns, get_default_registry()


def _sources(config: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}


def _loader(sources: dict[str, pa.Table]):
    def load(table: str) -> pa.Table:
        return sources[table]

    return load


def _write(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _faker_col(name: str, namespace: str) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": namespace,
    }


# ===========================================================================
# run: single-column-scalar strategy dispatch label (mut_87/88 in _dispatch)
# ===========================================================================
def _scalar_seed(strategy: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy=strategy,
        provider="x_nobackend",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(),
        coherent_with=(),
    )


def _scalar_plan(strategy: str, col: str = "a") -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                ("t", TableSeed(per_column=((col, _scalar_seed(strategy)),), per_group=())),
            ),
        )
    )


def _run(plan, sources, graph=_EMPTY_GRAPH, **kw):
    return PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS, **kw
    )


class TestScalarDispatchTiming:
    def test_scalar_node_records_strategy_label_and_column(self) -> None:
        # _dispatch stamps timed_strategy(node.strategy, ",".join(node.columns)).
        # mut_87 (strategy_type=None) / mut_88 (column=None) show up as a missing
        # (redact, "a") timing record.
        res = _run(_scalar_plan("redact"), {"t": pa.table({"a": ["x@y.com"]})})
        labels = {(r.strategy_type, r.column) for r in res.timings}
        assert ("redact", "a") in labels
        assert res.output.column("a").to_pylist() == ["REDACTED"]


# ===========================================================================
# run: composite-node dispatch label (mut_44/45/48/49/51 in _dispatch)
# ===========================================================================
def _composite_col(coherent_with: tuple[str, ...]) -> ColumnSeed:
    return ColumnSeed(
        namespace="nm_ns",
        strategy="<composite>",
        provider="composite_name_email",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=coherent_with,
    )


def _composite_ns() -> NamespaceRegistry:
    from decoy_engine.relationships._namespace import NamespaceBinding

    group = ("email", "first_name", "last_name")
    return NamespaceRegistry(
        bindings=(NamespaceBinding(namespace="nm_ns", declared_by=(("people", group),)),)
    )


def _composite_plan_single() -> Any:
    people = TableSeed(
        per_column=(
            ("first_name", _composite_col(("last_name", "email"))),
            ("last_name", _composite_col(("first_name", "email"))),
            ("email", _composite_col(("first_name", "last_name"))),
        ),
        per_group=(),
    )
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(job_seed=_SEED, per_table=(("people", people),))
    )


class TestCompositeDispatchTiming:
    def test_composite_node_records_composite_label_and_joined_columns(self) -> None:
        # The composite branch stamps timed_strategy("composite", ",".join(cols)).
        # The column string is the sorted coherent tuple joined by "," -- kills the
        # label mutants (None / XX / UPPER, mut_44/48/49), the None-column (mut_45),
        # and the "XX,XX"-separator mutant (mut_51, observable only multi-column).
        sources = {
            "people": pa.table(
                {
                    "first_name": ["Ann", "Bob"],
                    "last_name": ["Lee", "Ray"],
                    "email": ["a@x.com", "b@x.com"],
                }
            )
        }
        res = PandasExecutionAdapter().run(
            _composite_plan_single(),
            sources,
            registry=_REG,
            relationship_graph=_EMPTY_GRAPH,
            namespace_registry=_composite_ns(),
        )
        comp = [r for r in res.timings if r.strategy_type == "composite"]
        assert len(comp) == 1
        assert comp[0].column == "email,first_name,last_name"


# ===========================================================================
# run + _dispatch: FK resolution label + RI (single-col fk_resolve)
# mut_10/11/12 (parents_of), mut_15/16/19/20 (fk_resolve label)
# ===========================================================================
class TestFkResolveTimingAndRi:
    def test_single_col_fk_resolve_label_and_child_ri(self) -> None:
        fx = build_fk_relational(rows=80, width=1, orphan_frac=0.0)
        graph = fx.graph(OrphanPolicy.PRESERVE)
        res = PandasExecutionAdapter().run(
            fx.plan,
            fx.sources,
            registry=fx.registry,
            relationship_graph=graph,
            namespace_registry=fx.namespace_registry,
        )
        fk = [r for r in res.timings if r.strategy_type == "fk_resolve"]
        # Exact label + exact single-column string (kills mut_15/16/19/20).
        cols = {r.column for r in fk}
        assert "parent_id" in cols
        assert "child_id" in cols
        # RI holds: masked child FK values are a subset of masked parent keys.
        # A dropped/mis-routed parents_of (mut_10/11/12) masks the child by its
        # own strategy instead, breaking this subset relation (or crashes).
        parent_ids = set(res.outputs["parent"].column("id").to_pylist())
        child_fk = set(res.outputs["child"].column("parent_id").to_pylist())
        assert child_fk <= parent_ids


# ===========================================================================
# run + _dispatch: composite-FK resolve label (multi-col separator, mut_22)
# Reuses test_orphan_fk.py's composite-key fixture.
# ===========================================================================
def _hash_col(namespace: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="hash",
        provider="hash",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


_COMPOSITE_COLS = ("member_id", "plan_id", "effective_date")


def _composite_fk_plan() -> Any:
    parent_cols = tuple((c, _hash_col(f"enr_{c}")) for c in _COMPOSITE_COLS)
    group = GroupSeed(namespace="enr", coherent_columns=_COMPOSITE_COLS)
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                ("enrollments", TableSeed(per_column=parent_cols, per_group=())),
                ("claims", TableSeed(per_column=(), per_group=(("member_id__plan_id", group),))),
            ),
        )
    )


def _composite_fk_graph() -> RelationshipGraph:
    edge = RelationshipEdge(
        parent_table="enrollments",
        parent_columns=_COMPOSITE_COLS,
        child_table="claims",
        child_columns=_COMPOSITE_COLS,
        namespace="enr",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    return RelationshipGraph(edges=(edge,), ordering=())


class TestCompositeFkResolveSeparator:
    def test_composite_fk_resolve_joins_columns_with_comma(self) -> None:
        sources = {
            "enrollments": pa.table(
                {
                    "member_id": ["m1", "m2"],
                    "plan_id": ["p1", "p2"],
                    "effective_date": ["2020", "2021"],
                }
            ),
            "claims": pa.table(
                {
                    "member_id": ["m2", "m1"],
                    "plan_id": ["p2", "p1"],
                    "effective_date": ["2021", "2020"],
                }
            ),
        }
        res = PandasExecutionAdapter().run(
            _composite_fk_plan(),
            sources,
            registry=_REG,
            relationship_graph=_composite_fk_graph(),
            namespace_registry=_NS,
        )
        fk = [r for r in res.timings if r.strategy_type == "fk_resolve"]
        assert len(fk) == 1
        # Joined by "," -- the "XX,XX" separator (mut_22) only shows up here,
        # on a genuinely multi-column FK key.
        assert fk[0].column == "member_id,plan_id,effective_date"


# ===========================================================================
# run: use_collector(collector) is the real collector (mut_96) + timings
# packaging (mut_162) + boundary_conversion_ms packaging (mut_163)
# ===========================================================================
class TestResultTelemetryPackaging:
    def test_timings_nonempty_and_conversion_positive_bounded(self) -> None:
        res = _run(_scalar_plan("redact"), {"t": pa.table({"a": ["x", "y"]})})
        # mut_96 binds use_collector(None) -> no records captured -> empty timings.
        # mut_162 drops the timings kwarg -> the () default.
        assert isinstance(res.timings, tuple)
        assert len(res.timings) > 0
        # mut_163 drops boundary_conversion_ms -> the 0.0 default. Real conversions
        # are strictly positive; the < 1000 bound catches a perf_counter epoch leak
        # (mut_41 `+ t0`, mut_153 `+ t1`) without any wall-clock magnitude assertion.
        assert isinstance(res.boundary_conversion_ms, float)
        assert 0.0 < res.boundary_conversion_ms < 1000.0

    def test_clean_job_row_errors_is_empty_tuple(self) -> None:
        # mut_160 sets row_errors=None on a clean job.
        res = _run(_scalar_plan("redact"), {"t": pa.table({"a": ["x"]})})
        assert res.row_errors == ()


# ===========================================================================
# run: code_set corpus-provenance metrics (mut_165) + current_table stamping
# in _dispatch (mut_3/7/8). Reuses test_code_set_cross_substrate_evidence.py.
# ===========================================================================
def _direct_code_set_col(code_set: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="code_set",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(("code_set", code_set), ("mode", "mask")),
        coherent_with=(),
    )


def _code_set_plan(table: str, col: str) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    table,
                    TableSeed(per_column=((col, _direct_code_set_col("icd10")),), per_group=()),
                ),
            ),
        )
    )


class TestCodeSetEvidence:
    def test_code_set_metrics_carry_stamped_table_identity(self) -> None:
        res = _run(
            _code_set_plan("t", "diag"),
            {"t": pa.table({"diag": pa.array(["I10", "E11.9"], type=pa.string())})},
        )
        # mut_165 drops quality_metrics -> {} (no corpora block at all).
        corpora = res.quality_metrics.get("code_set_corpora")
        assert corpora is not None and len(corpora) == 1
        entry = corpora[0]
        # _dispatch stamps ctx.current_table = node.table before the handler runs;
        # CodeSetHandler keys its evidence on it. mut_3 (None) / mut_7 / mut_8
        # (misspelled attr -> stays "") make this identity wrong.
        assert entry["table"] == "t"
        assert entry["column"] == "diag"


# ===========================================================================
# run: FK key-error EXCLUDE-then-CASCADE. Threads key_error_rows /
# errored_keys_cache and the per-node drain/fold.
# Kills run mut_91/92/98(part)/107/108/116/117/119/120/124/125-132 and
# _dispatch mut_28/30/31/39/40 (node_by_key + key-error threading to
# _resolve_fk_node).
# ===========================================================================
_KEY_DATES = ["2020-01-01", "2020-02-01", "2020-03-01", "2020-04-01", "2020-05-01", "notadate"]
_IDS = ["p0", "p1", "p2", "p3", "p4", "p5"]


def _fk_key_error_config(tmp_path: Path) -> dict[str, Any]:
    parent = pa.table({"id": pa.array(_KEY_DATES, type=pa.string())})
    child = pa.table(
        {
            "id": pa.array([f"c{i}" for i in range(len(_KEY_DATES))], type=pa.string()),
            "parent_id": pa.array(_KEY_DATES, type=pa.string()),
        }
    )
    return {
        "version": 1,
        "global_settings": {"job_name": "run-kills-keyerror", "seed": 42},
        "sources": {
            "parent": {
                "type": "file",
                "path": _write(tmp_path, parent, "parent"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": _write(tmp_path, child, "child"),
                "format": "parquet",
            },
        },
        "targets": {
            "parent": {
                "type": "file",
                "path": str(tmp_path / "p.out.parquet"),
                "format": "parquet",
            },
            "child": {"type": "file", "path": str(tmp_path / "c.out.parquet"), "format": "parquet"},
        },
        "tables": [
            {
                "name": "parent",
                "columns": [
                    {
                        "name": "id",
                        "strategy": "date_shift",
                        "provider_config": {"min_days": 1, "max_days": 30},
                        "namespace": "parent_ns",
                    }
                ],
            },
            {"name": "child", "columns": [_faker_col("parent_id", "parent_ns")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "parent_ns",
            }
        ],
    }


class TestFullFrameKeyErrorCascade:
    def test_run_excludes_errored_key_and_cascades_trigger(self, tmp_path: Path) -> None:
        config = _fk_key_error_config(tmp_path)
        plan, graph, ns, registry = _compile(config)
        res = PandasExecutionAdapter().run(
            plan,
            _sources(config),
            registry=registry,
            relationship_graph=graph,
            namespace_registry=ns,
        )
        child_parent_ids = res.outputs["child"].column("parent_id").to_pylist()
        # The raw errored key never leaks through the parent map to the child.
        # Any broken threading (None/dropped key_error_rows or errored_keys_cache,
        # a mis-built key_error_rows index) re-leaks "notadate" to the child or
        # crashes.
        assert "notadate" not in child_parent_ids
        # (Full-frame `run` does not quarantine: the parent row keeps its own
        # errored value; the row error is still recorded below. The cascade
        # exclusion is what protects the CHILD from the raw parent key.)
        # The parent's date_shift row error is recorded and table-attributed
        # (mut_120 table=None); the child cascade carries the parent's trigger
        # (mut_124 stores None instead of rec.trigger).
        parent_errs = [r for r in res.row_errors if r.table == "parent"]
        child_errs = [r for r in res.row_errors if r.table == "child"]
        assert len(parent_errs) == 1
        assert parent_errs[0].column == "id"
        assert parent_errs[0].trigger == "format_error"
        assert len(child_errs) == 1
        assert child_errs[0].trigger == "format_error"


# ===========================================================================
# run: row_errors packaging on a job that HAS row errors (mut_166 drop ->
# the () default would swallow the records) + drain source arg (mut_119).
# ===========================================================================
def _bucketize_row_error_plan() -> Any:
    seed = ColumnSeed(
        namespace=None,
        strategy="bucketize",
        provider="bucketize",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(("width", 10),),
        coherent_with=(),
    )
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(("t", TableSeed(per_column=(("age", seed),), per_group=())),),
        )
    )


class TestRowErrorsPackaged:
    def test_run_reports_row_error_records_with_table(self) -> None:
        # "abc" is uncoercible -> one format_error row error, drained + attributed.
        res = _run(_bucketize_row_error_plan(), {"t": pa.table({"age": ["10", "abc"]})})
        recs = res.row_errors
        assert len(recs) == 1  # mut_166 drop -> () default -> would be 0
        assert recs[0].table == "t"  # mut_120 table=None
        assert recs[0].column == "age"
        assert recs[0].trigger == "format_error"


# ===========================================================================
# run: node_by_key threading to REMAP orphans (mut_77, mut_105 in run;
# mut_28 in _dispatch). REMAP mints a synthetic parent key via node_by_key.
# ===========================================================================
class TestRemapOrphanNodeByKey:
    def test_remap_orphans_resolve_via_node_by_key(self) -> None:
        # Orphan child rows under REMAP are minted through the parent strategy,
        # which needs node_by_key. A None/dropped node_by_key crashes or leaves
        # the orphan unremapped (raw key leaks into the child).
        fx = build_fk_relational(rows=120, width=1, orphan_frac=0.1)
        graph = fx.graph(OrphanPolicy.REMAP)
        res = PandasExecutionAdapter().run(
            fx.plan,
            fx.sources,
            registry=fx.registry,
            relationship_graph=graph,
            namespace_registry=fx.namespace_registry,
        )
        # REMAP mints a synthetic masked key for each orphan through the parent
        # column's own strategy, which the remap closure reaches via node_by_key.
        # A None/dropped node_by_key makes that closure raise (orphans present),
        # so the run completing at all + no raw orphan token surviving proves the
        # mint ran. (A minted key is deliberately NOT an existing parent key.)
        child_fk = res.outputs["child"].column("parent_id").to_pylist()
        assert res.outputs["child"].num_rows > 0
        assert not any(isinstance(v, str) and v.startswith("orphan") for v in child_fk)


# ===========================================================================
# run: lossless FK integer typing (mut_26/28). A big int64 FK key beside a
# null must survive byte-exact; an unprotected column widens to float64 and
# rounds. DE-10 fixture shape.
# ===========================================================================
_BIG_KEY = 9007199254740993  # 2**53 + 1: does NOT round-trip through float64


def _de10_plan() -> Any:
    seed = ColumnSeed(
        namespace="de10",
        strategy="passthrough",
        provider="passthrough",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                ("parent", TableSeed(per_column=(("pk", seed),), per_group=())),
                ("child", TableSeed(per_column=(("fk", seed),), per_group=())),
            ),
        )
    )


def _de10_graph() -> RelationshipGraph:
    edge = RelationshipEdge(
        parent_table="parent",
        parent_columns=("pk",),
        child_table="child",
        child_columns=("fk",),
        namespace="de10",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    return RelationshipGraph(edges=(edge,), ordering=())


class TestLosslessFkTyping:
    def test_big_fk_key_beside_null_survives_exact(self) -> None:
        # fk_columns_for_table feeds the FK-safe (lossless) ingestion set. With
        # table=None (mut_28) or the set intersected away (mut_26), the FK columns
        # widen to float64 and the big key rounds.
        parent = pa.table({"pk": pa.array([1, _BIG_KEY], type=pa.int64())})
        child = pa.table({"fk": pa.array([1, None, _BIG_KEY], type=pa.int64())})
        res = _run(_de10_plan(), {"parent": parent, "child": child}, graph=_de10_graph())
        out = res.outputs["child"].column("fk")
        assert out.type == pa.int64()
        assert out.to_pylist() == [1, None, _BIG_KEY]


# ===========================================================================
# _dispatch_mask_node direct calls: the defensive raise branches.
# ===========================================================================
def _dispatch_ctx() -> StrategyContext:
    return StrategyContext(
        registry=_REG,
        pool_cache=PoolCache(),
        relationship_graph=_EMPTY_GRAPH,
        namespace_registry=_NS,
        job_seed=_SEED,
        mask_key=_SEED,
    )


class TestDispatchDefensiveRaises:
    def test_composite_fk_group_without_edge_raises_coded(self) -> None:
        # A composite_fk_group node with no matching relationship edge hits the
        # "graph has no edge for it" raise. Kills the code mutants (None / drop /
        # XX / UPPER, mut_62/64/66/67); the message is prose (equivalent).
        import pandas as pd

        node = WorkNode(
            table="t",
            columns=("a", "b"),
            kind="composite_fk_group",
            strategy="<group>",
            provider="<group>",
            plan_slice=GroupSeed(namespace="g", coherent_columns=("a", "b")),
        )
        adapter = PandasExecutionAdapter()
        with pytest.raises(ExecutionError) as exc:
            adapter._dispatch_mask_node(
                node,
                {"t": pd.DataFrame({"a": ["x"], "b": ["y"]})},
                _EMPTY_GRAPH,
                {},
                {},
                {},
                _dispatch_ctx(),
            )
        assert exc.value.code == "composite_fk_group_no_edge"

    def test_no_handler_for_strategy_raises_coded(self) -> None:
        import pandas as pd

        node = WorkNode(
            table="t",
            columns=("a",),
            kind="scalar",
            strategy="not_a_real_strategy",
            provider=None,
            plan_slice=ColumnSeed(
                namespace=None,
                strategy="not_a_real_strategy",
                provider=None,
                backend_type="faker",
                backend_version="v",
                cardinality_mode="reuse",
                deterministic=False,
                provider_config=(),
                coherent_with=(),
            ),
        )
        adapter = PandasExecutionAdapter()
        with pytest.raises(ExecutionError) as exc:
            adapter._dispatch_mask_node(
                node, {"t": pd.DataFrame({"a": ["x"]})}, _EMPTY_GRAPH, {}, {}, {}, _dispatch_ctx()
            )
        assert exc.value.code == "unsupported_strategy"

    def test_scalar_node_with_non_columnseed_slice_raises_coded(self) -> None:
        # A scalar node whose strategy HAS a handler but whose plan_slice is not a
        # ColumnSeed hits the narrowing guard. Kills the code mutants
        # (None / drop / XX / UPPER, mut_81/83/85/86); message is prose.
        import pandas as pd

        node = WorkNode(
            table="t",
            columns=("a",),
            kind="scalar",
            strategy="redact",
            provider=None,
            plan_slice=GroupSeed(namespace="g", coherent_columns=("a",)),
        )
        adapter = PandasExecutionAdapter()
        with pytest.raises(ExecutionError) as exc:
            adapter._dispatch_mask_node(
                node, {"t": pd.DataFrame({"a": ["x"]})}, _EMPTY_GRAPH, {}, {}, {}, _dispatch_ctx()
            )
        assert exc.value.code == "unsupported_strategy"

    def test_current_table_stamped_before_dispatch(self) -> None:
        # _dispatch stamps ctx.current_table = node.table (mut_3 None, mut_7/8 wrong
        # attr name -> stays ""). Observe the attribute directly after a scalar
        # dispatch.
        import pandas as pd

        node = WorkNode(
            table="mytable",
            columns=("a",),
            kind="scalar",
            strategy="redact",
            provider=None,
            plan_slice=ColumnSeed(
                namespace=None,
                strategy="redact",
                provider="x_nobackend",
                backend_type="faker",
                backend_version="v",
                cardinality_mode="reuse",
                deterministic=False,
                provider_config=(),
                coherent_with=(),
            ),
        )
        ctx = _dispatch_ctx()
        with use_collector(TimingCollector()):
            PandasExecutionAdapter()._dispatch_mask_node(
                node, {"mytable": pd.DataFrame({"a": ["x"]})}, _EMPTY_GRAPH, {}, {}, {}, ctx
            )
        assert ctx.current_table == "mytable"


# ===========================================================================
# _dispatch_mask_node direct: fk_resolve threading to _resolve_fk_node.
# mut_28 (node_by_key None) is covered by the REMAP test above; here we pin
# the key-error kwarg threading (mut_30/31/39/40) via the full-frame cascade
# already (they route the same caches). No extra test needed.
# ===========================================================================


# ===========================================================================
# run: group_by date_shift pre-mask anchor snapshot (mut_43/45/46/56/63).
# Empty snapshots -> date_shift group_by fails closed.
# ===========================================================================
def _group_by_config(tmp_path: Path) -> dict[str, Any]:
    src = pa.table(
        {
            "d": pa.array(
                ["2020-01-01", "2020-01-15", "2019-05-01", "2019-05-20"], type=pa.string()
            ),
            "patient_id": pa.array(["p1", "p1", "p2", "p2"], type=pa.string()),
        }
    )
    return {
        "version": 1,
        "global_settings": {"job_name": "run-kills-groupby", "seed": 7},
        "sources": {"t": {"type": "file", "path": _write(tmp_path, src, "t"), "format": "parquet"}},
        "targets": {
            "t": {"type": "file", "path": str(tmp_path / "t.out.parquet"), "format": "parquet"}
        },
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": "d",
                        "strategy": "date_shift",
                        "provider_config": {
                            "min_days": -100,
                            "max_days": 100,
                            "date_format": "%Y-%m-%d",
                            "group_by": "patient_id",
                        },
                        "namespace": "dates",
                    },
                    {"name": "patient_id", "strategy": "hash", "namespace": "pid"},
                ],
            }
        ],
    }


class TestGroupByAnchorSnapshot:
    def test_group_by_uses_pre_mask_snapshot_full_frame(self, tmp_path: Path) -> None:
        import datetime

        config = _group_by_config(tmp_path)
        plan, graph, ns, registry = _compile(config)
        res = PandasExecutionAdapter().run(
            plan,
            _sources(config),
            registry=registry,
            relationship_graph=graph,
            namespace_registry=ns,
        )
        out = res.outputs["t"]
        dates = out.column("d").to_pylist()
        src_dates = [
            datetime.date.fromisoformat(v)
            for v in ["2020-01-01", "2020-01-15", "2019-05-01", "2019-05-20"]
        ]
        out_dates = [datetime.datetime.strptime(v, "%Y-%m-%d").date() for v in dates]
        # Same-entity interval preserved only if the anchor came from the pre-mask
        # snapshot; empty snapshots (mut_43/45/46/56/63) fail closed with
        # date_shift_group_anchor_snapshot_missing.
        assert (out_dates[1] - out_dates[0]).days == (src_dates[1] - src_dates[0]).days
        assert (out_dates[3] - out_dates[2]).days == (src_dates[3] - src_dates[2]).days
        assert (out_dates[0] - src_dates[0]).days != (out_dates[2] - src_dates[2]).days


# ===========================================================================
# run: a node on a table absent from `sources` is SKIPPED (continue), and
# later nodes on present tables still mask (mut_98 continue -> break).
# ===========================================================================
class TestAbsentTableNodeSkipped:
    def test_absent_table_node_does_not_abort_the_loop(self) -> None:
        # "a_absent" has a mask node but no source; it is skipped. "b" comes after
        # it in work order and must still mask. `break` (mut_98) would leave b raw.
        plan = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=_SEED,
                per_table=(
                    (
                        "a_absent",
                        TableSeed(per_column=(("x", _scalar_seed("redact")),), per_group=()),
                    ),
                    ("b", TableSeed(per_column=(("y", _scalar_seed("redact")),), per_group=())),
                ),
            )
        )
        res = _run(plan, {"b": pa.table({"y": ["secret"]})})
        assert res.outputs["b"].column("y").to_pylist() == ["REDACTED"]


# ===========================================================================
# run: lossless typing of a date_shift group_by ANCHOR column (mut_25/31).
# An int+null anchor must stay Int64 through ingestion; widened to float64 the
# anchor id no longer canonicalizes and the run fails closed.
# ===========================================================================
def _int_anchor_plan() -> Any:
    d = ColumnSeed(
        namespace="dates",
        strategy="date_shift",
        provider="date_shift",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(
            ("min_days", -100),
            ("max_days", 100),
            ("date_format", "%Y-%m-%d"),
            ("group_by", "pid"),
        ),
        coherent_with=(),
    )
    pid = ColumnSeed(
        namespace=None,
        strategy="passthrough",
        provider="passthrough",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(),
        coherent_with=(),
    )
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=(0x07).to_bytes(8, "big"),
            per_table=(("t", TableSeed(per_column=(("d", d), ("pid", pid)), per_group=())),),
        )
    )


class TestIntNullAnchorLosslessTyping:
    def test_int_null_anchor_stays_lossless_and_run_succeeds(self) -> None:
        import datetime

        # pid is an int64 anchor with a null (row 2). group_anchor_cols must be
        # unioned into the FK-safe set (mut_25 intersects it away, mut_31 looks it
        # up under table=None), or pid widens to float64 and the run fails closed.
        src = pa.table(
            {
                "d": pa.array(["2020-01-01", "2020-01-15", "2019-05-01"], type=pa.string()),
                "pid": pa.array([1, 1, None], type=pa.int64()),
            }
        )
        res = _run(_int_anchor_plan(), {"t": src})
        dates = res.outputs["t"].column("d").to_pylist()
        out_dates = [datetime.datetime.strptime(v, "%Y-%m-%d").date() for v in dates]
        src_dates = [datetime.date.fromisoformat(v) for v in ["2020-01-01", "2020-01-15"]]
        # Same entity (pid=1) -> same offset -> intra-entity interval preserved.
        assert (out_dates[1] - out_dates[0]).days == (src_dates[1] - src_dates[0]).days
        # The int anchor round-trips unwidened.
        assert res.outputs["t"].column("pid").to_pylist() == [1, 1, None]


# ===========================================================================
# run_sequential: argument threading to the delegate (execution._sequential).
# ===========================================================================
class TestRunSequentialThreading:
    def test_byte_parity_threads_core_args(self) -> None:
        # A clean FK byte-parity run exercises self/plan/source_loader/registry/
        # relationship_graph/namespace_registry: any None/dropped one crashes or
        # diverges from the full-frame oracle.
        adapter = PandasExecutionAdapter()
        fx = build_fk_relational(rows=200, width=1, orphan_frac=0.05)
        graph = fx.graph(OrphanPolicy.PRESERVE)
        full = adapter.run(
            fx.plan,
            fx.sources,
            registry=fx.registry,
            relationship_graph=graph,
            namespace_registry=fx.namespace_registry,
        )
        seq = adapter.run_sequential(
            fx.plan,
            _loader(fx.sources),
            registry=fx.registry,
            relationship_graph=graph,
            namespace_registry=fx.namespace_registry,
        )
        assert set(seq.outputs) == set(full.outputs)
        for table in full.outputs:
            assert seq.outputs[table].equals(full.outputs[table]), f"{table} differs"

    def test_namespace_registry_threads_to_delegate(self) -> None:
        # A composite column resolves its group's namespace binding through
        # ctx.namespace_registry. Forcing it to None (mut_7/18) leaves the
        # composite handler unable to resolve the binding (it fails closed),
        # so a successful masked run proves the registry reached the delegate.
        sources = {
            "people": pa.table(
                {
                    "first_name": ["Ann", "Bob", "Cy"],
                    "last_name": ["Lee", "Ray", "Ng"],
                    "email": ["a@x.com", "b@x.com", "c@x.com"],
                }
            )
        }
        res = PandasExecutionAdapter().run_sequential(
            _composite_plan_single(),
            _loader(sources),
            registry=_REG,
            relationship_graph=_EMPTY_GRAPH,
            namespace_registry=_composite_ns(),
        )
        masked = res.outputs["people"].column("email").to_pylist()
        assert masked != ["a@x.com", "b@x.com", "c@x.com"]

    def test_key_provider_threads_to_delegate(self) -> None:
        adapter = PandasExecutionAdapter()
        fx = build_fk_relational(rows=120, width=1, orphan_frac=0.0)
        graph = fx.graph(OrphanPolicy.PRESERVE)
        unkeyed = adapter.run_sequential(
            fx.plan,
            _loader(fx.sources),
            registry=fx.registry,
            relationship_graph=graph,
            namespace_registry=fx.namespace_registry,
        )
        keyed = adapter.run_sequential(
            fx.plan,
            _loader(fx.sources),
            registry=fx.registry,
            relationship_graph=graph,
            namespace_registry=fx.namespace_registry,
            key_provider=_SECRET,
        )
        # mut_11/22 force key_provider=None -> keyed collapses back to job_seed.
        assert not keyed.outputs["parent"].equals(unkeyed.outputs["parent"])

    def test_sink_threads_to_delegate(self) -> None:
        adapter = PandasExecutionAdapter()
        fx = build_fk_relational(rows=150, width=1, orphan_frac=0.0)
        seen: dict[str, pa.Table] = {}

        def sink(table: str, out: pa.Table) -> None:
            seen[table] = out

        res = adapter.run_sequential(
            fx.plan,
            _loader(fx.sources),
            registry=fx.registry,
            relationship_graph=fx.graph(OrphanPolicy.PRESERVE),
            namespace_registry=fx.namespace_registry,
            sink=sink,
        )
        # mut_8/19 force sink=None -> outputs accumulate instead of streaming out.
        assert set(seen) == set(fx.sources)
        assert res.outputs == {}

    def test_quarantine_config_threads_to_delegate(self, tmp_path: Path) -> None:
        # With a covering quarantine config the errored row is filtered; forcing
        # it to None (mut_9/20) makes the delegate fail loud instead.

        parent = pa.table(
            {
                "id": pa.array(_IDS, type=pa.string()),
                "age": pa.array(["10", "20", "30", "40", "50", "badX"], type=pa.string()),
            }
        )
        child = pa.table(
            {
                "id": pa.array([f"c{i}" for i in range(len(_IDS))], type=pa.string()),
                "parent_id": pa.array(_IDS, type=pa.string()),
            }
        )
        qpath = str(tmp_path / "q.jsonl")
        config = {
            "version": 1,
            "global_settings": {"job_name": "run-seq-quar", "seed": 42},
            "sources": {
                "parent": {
                    "type": "file",
                    "path": _write(tmp_path, parent, "parent"),
                    "format": "parquet",
                },
                "child": {
                    "type": "file",
                    "path": _write(tmp_path, child, "child"),
                    "format": "parquet",
                },
            },
            "targets": {
                "parent": {
                    "type": "file",
                    "path": str(tmp_path / "p.out.parquet"),
                    "format": "parquet",
                },
                "child": {
                    "type": "file",
                    "path": str(tmp_path / "c.out.parquet"),
                    "format": "parquet",
                },
            },
            "tables": [
                {
                    "name": "parent",
                    "columns": [
                        _faker_col("id", "parent_ns"),
                        {"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}},
                    ],
                },
                {"name": "child", "columns": [_faker_col("parent_id", "parent_ns")]},
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["parent_id"]}],
                    "orphan_policy": "preserve",
                    "namespace": "parent_ns",
                }
            ],
        }
        plan, graph, ns, registry = _compile(config)
        quar = {"enabled": True, "output_path": qpath, "triggers": ["format_error"]}
        res = PandasExecutionAdapter().run_sequential(
            plan,
            _loader(_sources(config)),
            registry=registry,
            relationship_graph=graph,
            namespace_registry=ns,
            quarantine_config=quar,
        )
        assert res.outputs["parent"].num_rows == 5
        assert "badX" not in res.outputs["parent"].column("age").to_pylist()
        assert res.quality_metrics["quarantine"]["total_quarantined"] == 1

    def test_unconfigured_column_policy_threads_to_delegate(self, tmp_path: Path) -> None:
        # policy="error" must raise on an undeclared child column; forcing None
        # (mut_10/21) resolves to the pre-GA warn default -> no raise.
        parent = pa.table({"pk": ["p0", "p1", "p2"]})
        child = pa.table({"fk": ["p0", "p1", "p2"], "payload_leak": ["r0", "r1", "r2"]})
        plan = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=_SEED,
                per_table=(
                    ("parent", TableSeed(per_column=(("pk", _hash_col("ns_p")),), per_group=())),
                    ("child", TableSeed(per_column=(("fk", _hash_col("ns_p")),), per_group=())),
                ),
            )
        )
        graph = RelationshipGraph(
            edges=(
                RelationshipEdge(
                    parent_table="parent",
                    parent_columns=("pk",),
                    child_table="child",
                    child_columns=("fk",),
                    namespace="ns_p",
                    orphan_policy=OrphanPolicy.PRESERVE,
                ),
            ),
            ordering=(),
        )
        sources = {"parent": parent, "child": child}
        with pytest.raises(ExecutionError) as exc:
            PandasExecutionAdapter().run_sequential(
                plan,
                _loader(sources),
                registry=_REG,
                relationship_graph=graph,
                namespace_registry=_NS,
                unconfigured_column_policy="error",
            )
        assert exc.value.code == "undeclared_output_columns"
