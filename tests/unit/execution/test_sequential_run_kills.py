"""Mutation-kill coverage for `run_sequential` (execution/_sequential.py).

These tests target the sequential FK execution route directly (via
`PandasExecutionAdapter.run_sequential`), driving every observable surface it
owns: quarantine config parsing, the fail-loud/quarantine per-table
classification, key-error bookkeeping + FK cascade, the transactional-sink
commit/abort protocol, group-anchor pre-mask snapshots, keyed-mask wiring,
and the packaged `ExecutionResult` fields (row_errors, quality_metrics,
timings, boundary_conversion_ms).

Jobs are compiled from a config dict with the same helpers `run_pipeline`
uses (`compile_job` below), then handed straight to `run_sequential` so a
test can control `quarantine_config` byte-for-byte (including omitting keys)
and read the raw `ExecutionResult` the pipeline would otherwise repackage.
The masking configs (bucketize / date_shift row errors, FK-key cascade)
mirror the proven integration fixtures in
tests/integration/test_fk_sequential_row_error_leak.py and
tests/integration/test_quarantine_commit_fate.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import ExecutionError, PandasExecutionAdapter
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.relationships._graph import OrphanPolicy
from tests.perf_fixtures.fk_relational import build_fk_relational

_AGE = ["10", "20", "30", "40", "50", "badX"]
_IDS = ["p0", "p1", "p2", "p3", "p4", "p5"]
_KEY_DATES = ["2020-01-01", "2020-02-01", "2020-03-01", "2020-04-01", "2020-05-01", "notadate"]


# ---------------------------------------------------------------------------
# Helpers: compile a config dict into (plan, graph, ns_registry, registry) so
# run_sequential can be called directly (mirrors run_pipeline's compile block).
# ---------------------------------------------------------------------------


def compile_job(config: dict[str, Any]):
    from decoy_engine.plan import compile_plan
    from decoy_engine.plan._seed import _normalize_job_seed_int
    from decoy_engine.profile import profile_source
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships import (
        RelationshipGraph,
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


def _loader(sources: dict[str, pa.Table]):
    def load(table: str) -> pa.Table:
        return sources[table]

    return load


def _sources(config: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}


def _write_source(tmp_path: Path, table: pa.Table, name: str) -> str:
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


def _fk_bucketize_config(tmp_path: Path, quarantine: dict[str, Any] | None) -> dict[str, Any]:
    """Parent has a `bucketize` `age` column with one uncoercible cell ("badX"
    at full-table index 5); a child FK references the parent. This is the
    integration suite's format_error shape."""
    parent = pa.table(
        {"id": pa.array(_IDS, type=pa.string()), "age": pa.array(_AGE, type=pa.string())}
    )
    child = pa.table(
        {
            "id": pa.array([f"c{i}" for i in range(len(_IDS))], type=pa.string()),
            "parent_id": pa.array(_IDS, type=pa.string()),
        }
    )
    cfg: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "seq-kills-bucketize", "seed": 42},
        "sources": {
            "parent": {
                "type": "file",
                "path": _write_source(tmp_path, parent, "parent"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": _write_source(tmp_path, child, "child"),
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
    if quarantine is not None:
        cfg["quarantine"] = quarantine
    return cfg


def _fk_key_error_config(tmp_path: Path, quarantine: dict[str, Any] | None) -> dict[str, Any]:
    """The FK KEY column (`id`) is masked by `date_shift` (row-error-emitting);
    one uncoercible key ("notadate"). The child `parent_id` carries the same
    values so it leaks the raw errored key 1:1 without the cascade fix."""
    parent = pa.table({"id": pa.array(_KEY_DATES, type=pa.string())})
    child = pa.table(
        {
            "id": pa.array([f"c{i}" for i in range(len(_KEY_DATES))], type=pa.string()),
            "parent_id": pa.array(_KEY_DATES, type=pa.string()),
        }
    )
    id_col = {
        "name": "id",
        "strategy": "date_shift",
        "provider_config": {"min_days": 1, "max_days": 30},
        "namespace": "parent_ns",
    }
    cfg: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "seq-kills-keyerror", "seed": 42},
        "sources": {
            "parent": {
                "type": "file",
                "path": _write_source(tmp_path, parent, "parent"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": _write_source(tmp_path, child, "child"),
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
            {"name": "parent", "columns": [id_col]},
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
    if quarantine is not None:
        cfg["quarantine"] = quarantine
    return cfg


# ===========================================================================
# Cluster 1: quarantine covers the row error (in-memory, no sink).
# Exercises q_cfg parsing, per-table classification, compute_quarantine,
# counts_by_trigger, row_error_counts, and every ExecutionResult field.
# ===========================================================================


def test_quarantine_covers_row_error_filters_and_records(tmp_path: Path) -> None:
    qpath = str(tmp_path / "q.jsonl")
    config = _fk_bucketize_config(
        tmp_path, {"enabled": True, "output_path": qpath, "triggers": ["format_error"]}
    )
    plan, graph, ns, registry = compile_job(config)
    res = PandasExecutionAdapter().run_sequential(
        plan,
        _loader(_sources(config)),
        registry=registry,
        relationship_graph=graph,
        namespace_registry=ns,
        quarantine_config={"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
    )

    # (a) leak closed + innocent row preserved: exactly one row removed.
    out_age = res.outputs["parent"].column("age").to_pylist()
    assert "badX" not in out_age
    assert "30" in out_age
    assert res.outputs["parent"].num_rows == 5

    # (b) row_errors field carries the drained record, table-attributed, full
    #     table index (5), correct trigger.
    recs = [r for r in res.row_errors if r.table == "parent"]
    assert len(recs) == 1
    assert recs[0].column == "age"
    assert recs[0].row_index == 5
    assert recs[0].trigger == "format_error"

    # (c) quality_metrics["row_errors"] folds the count under the exact key.
    assert res.quality_metrics["row_errors"] == {"parent.age[format_error]": 1}

    # (d) quality_metrics["quarantine"] evidence manifest + counts_by_trigger.
    qm = res.quality_metrics["quarantine"]
    assert qm["total_quarantined"] == 1
    assert qm["counts_by_trigger"] == {"format_error": 1}

    # (e) the JSONL sidecar carries the real bad value under the right trigger.
    records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["age"] == "badX"
    assert records[0]["_quarantine_trigger"] == "format_error"


# ===========================================================================
# Cluster 2: fail-loud when NO quarantine trigger covers the row error.
# ===========================================================================


def test_no_quarantine_fails_loud_before_any_output(tmp_path: Path) -> None:
    config = _fk_bucketize_config(tmp_path, None)
    plan, graph, ns, registry = compile_job(config)
    with pytest.raises(RowErrorsFailedError) as exc:
        PandasExecutionAdapter().run_sequential(
            plan,
            _loader(_sources(config)),
            registry=registry,
            relationship_graph=graph,
            namespace_registry=ns,
            quarantine_config=None,
        )
    recs = [r for r in exc.value.records if r.table == "parent"]
    assert len(recs) == 1
    assert recs[0].row_index == 5
    assert recs[0].trigger == "format_error"
    assert "badX" not in str(exc.value)


def test_trigger_present_but_not_matching_still_fails_loud(tmp_path: Path) -> None:
    """quarantine enabled with a NON-matching trigger (mask_error) does not
    cover a format_error, so the run still fails loud. Pins the membership
    test `r.trigger in q_triggers` and the `q_enabled and ...` conjunction."""
    qpath = str(tmp_path / "q.jsonl")
    quar = {"enabled": True, "output_path": qpath, "triggers": ["mask_error"]}
    config = _fk_bucketize_config(tmp_path, quar)
    plan, graph, ns, registry = compile_job(config)
    with pytest.raises(RowErrorsFailedError):
        PandasExecutionAdapter().run_sequential(
            plan,
            _loader(_sources(config)),
            registry=registry,
            relationship_graph=graph,
            namespace_registry=ns,
            quarantine_config=quar,
        )


# ===========================================================================
# Cluster 3: quarantine config guardrail + key parsing (direct control).
# ===========================================================================


def test_missing_enabled_key_defaults_to_disabled(tmp_path: Path) -> None:
    """quarantine_config with triggers+output_path but NO 'enabled' key: the
    default is False, so the row error is uncovered and the run fails loud.
    Kills the mutant that flips the `.get('enabled', False)` default to True."""
    qpath = str(tmp_path / "q.jsonl")
    config = _fk_bucketize_config(tmp_path, None)
    plan, graph, ns, registry = compile_job(config)
    with pytest.raises(RowErrorsFailedError):
        PandasExecutionAdapter().run_sequential(
            plan,
            _loader(_sources(config)),
            registry=registry,
            relationship_graph=graph,
            namespace_registry=ns,
            quarantine_config={"output_path": qpath, "triggers": ["format_error"]},
        )


def test_enabled_triggers_without_output_path_refuses_to_run(tmp_path: Path) -> None:
    """quarantine enabled with a trigger but NO output_path must raise a
    fail-closed ValueError (a quarantined row would be silently dropped).
    Kills mutants that fabricate a non-empty output_path default."""
    config = _fk_bucketize_config(tmp_path, None)
    plan, graph, ns, registry = compile_job(config)
    with pytest.raises(ValueError) as exc:
        PandasExecutionAdapter().run_sequential(
            plan,
            _loader(_sources(config)),
            registry=registry,
            relationship_graph=graph,
            namespace_registry=ns,
            quarantine_config={"enabled": True, "triggers": ["format_error"]},
        )
    # House style: the machine field here is the ValueError TYPE (pinned above) +
    # the identifying config key `output_path`; the explanatory prose is
    # equivalent (mutants 28/30/31 that XX-wrap / re-case the sentence carry no
    # machine contract). The condition mutants (25/26/27/29) are killed by the
    # guardrail firing at all.
    assert "output_path" in str(exc.value)


def test_quarantine_enabled_but_no_triggers_runs_clean() -> None:
    # enabled=True but EMPTY triggers (and no output_path): the guardrail
    # `q_enabled AND q_triggers AND not q_output_path` is False (no triggers), so
    # the job runs normally. The `and`->`or` mutant (mut_25) makes it
    # `q_enabled OR (q_triggers and not q_output_path)` = True and wrongly refuses
    # a job that has nothing to quarantine. (Killable only with this shape -- the
    # missing-output_path test above fires the guardrail under both operators.)
    adapter = PandasExecutionAdapter()
    fx = build_fk_relational(rows=50, width=2, orphan_frac=0.0)
    seq = adapter.run_sequential(
        fx.plan,
        _loader(fx.sources),
        registry=fx.registry,
        relationship_graph=fx.graph(OrphanPolicy.PRESERVE),
        namespace_registry=fx.namespace_registry,
        quarantine_config={"enabled": True, "triggers": []},
    )
    assert set(seq.outputs)  # completed, no fail-closed ValueError


# ===========================================================================
# Cluster 4: transactional-sink commit protocol + quarantine sidecar.
# ===========================================================================


def test_quarantine_with_transactional_sink_commits_and_publishes(tmp_path: Path) -> None:
    qpath = str(tmp_path / "q.jsonl")
    quar = {"enabled": True, "output_path": qpath, "triggers": ["format_error"]}
    config = _fk_bucketize_config(tmp_path, quar)
    plan, graph, ns, registry = compile_job(config)
    target = tmp_path / "out"
    res = PandasExecutionAdapter().run_sequential(
        plan,
        _loader(_sources(config)),
        registry=registry,
        relationship_graph=graph,
        namespace_registry=ns,
        sink=ParquetTransactionalSink(target),
        quarantine_config=quar,
    )
    # With a sink, outputs are streamed out, not accumulated.
    assert res.outputs == {}
    # Both tables committed via the sink; the bad value is gone from parent.
    assert (target / "parent.parquet").exists()
    assert (target / "child.parquet").exists()
    out_age = pq.read_table(target / "parent.parquet").column("age").to_pylist()
    assert "badX" not in out_age
    # Quarantine sidecar published after commit; manifest recorded.
    assert Path(qpath).exists()
    records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["age"] == "badX"
    assert res.quality_metrics["quarantine"]["total_quarantined"] == 1


def test_sink_aborts_and_publishes_nothing_on_fail_loud(tmp_path: Path) -> None:
    """No covering trigger -> fail-loud raise BEFORE commit -> the sink aborts
    and nothing (tables or quarantine) is published."""
    config = _fk_bucketize_config(tmp_path, None)
    plan, graph, ns, registry = compile_job(config)
    target = tmp_path / "out"
    with pytest.raises(RowErrorsFailedError):
        PandasExecutionAdapter().run_sequential(
            plan,
            _loader(_sources(config)),
            registry=registry,
            relationship_graph=graph,
            namespace_registry=ns,
            sink=ParquetTransactionalSink(target),
            quarantine_config=None,
        )
    assert not target.exists()


# ===========================================================================
# Cluster 5: FK key-error EXCLUDE-then-CASCADE (key_error_rows + cache).
# ===========================================================================


def test_key_error_cascades_to_child_and_is_recorded(tmp_path: Path) -> None:
    qpath = str(tmp_path / "q.jsonl")
    quar = {"enabled": True, "output_path": qpath, "triggers": ["format_error"]}
    config = _fk_key_error_config(tmp_path, quar)
    plan, graph, ns, registry = compile_job(config)
    res = PandasExecutionAdapter().run_sequential(
        plan,
        _loader(_sources(config)),
        registry=registry,
        relationship_graph=graph,
        namespace_registry=ns,
        quarantine_config=quar,
    )
    parent_ids = res.outputs["parent"].column("id").to_pylist()
    child_parent_ids = res.outputs["child"].column("parent_id").to_pylist()
    # Raw errored key absent from BOTH parent and the cascaded child.
    assert "notadate" not in parent_ids
    assert "notadate" not in child_parent_ids
    assert len(parent_ids) == 5
    assert len(child_parent_ids) == 5

    records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
    parent_recs = [r for r in records if r["_source_table"] == "parent"]
    child_recs = [r for r in records if r["_source_table"] == "child"]
    assert len(parent_recs) == 1
    assert parent_recs[0]["id"] == "notadate"
    assert len(child_recs) == 1
    assert child_recs[0]["parent_id"] is None
    assert "parent-key" in child_recs[0]["_quarantine_reason"]


# ===========================================================================
# Cluster 6: multi-table single quarantine JSONL (both tables' entries kept).
# ===========================================================================


def _multi_table_quarantine_config(tmp_path: Path, qpath: str) -> dict[str, Any]:
    parent = pa.table(
        {"id": pa.array(_IDS, type=pa.string()), "age": pa.array(_AGE, type=pa.string())}
    )
    child = pa.table(
        {
            "id": pa.array([f"c{i}" for i in range(len(_IDS))], type=pa.string()),
            "parent_id": pa.array(_IDS, type=pa.string()),
            "note": pa.array(["1", "2", "3", "4", "5", "badY"], type=pa.string()),
        }
    )
    return {
        "version": 1,
        "global_settings": {"job_name": "seq-kills-multi", "seed": 42},
        "sources": {
            "parent": {
                "type": "file",
                "path": _write_source(tmp_path, parent, "parent"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": _write_source(tmp_path, child, "child"),
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
                    _faker_col("id", "parent_ns"),
                    {"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}},
                ],
            },
            {
                "name": "child",
                "columns": [
                    _faker_col("parent_id", "parent_ns"),
                    {"name": "note", "strategy": "bucketize", "provider_config": {"width": 10}},
                ],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "parent_ns",
            }
        ],
        "quarantine": {"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
    }


def test_multi_table_quarantine_keeps_both_tables_entries(tmp_path: Path) -> None:
    qpath = str(tmp_path / "q.jsonl")
    config = _multi_table_quarantine_config(tmp_path, qpath)
    plan, graph, ns, registry = compile_job(config)
    quar = {"enabled": True, "output_path": qpath, "triggers": ["format_error"]}
    res = PandasExecutionAdapter().run_sequential(
        plan,
        _loader(_sources(config)),
        registry=registry,
        relationship_graph=graph,
        namespace_registry=ns,
        quarantine_config=quar,
    )
    records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
    parent_recs = [r for r in records if r["_source_table"] == "parent"]
    child_recs = [r for r in records if r["_source_table"] == "child"]
    assert len(parent_recs) == 1
    assert parent_recs[0]["age"] == "badX"
    assert len(child_recs) == 1
    assert child_recs[0]["note"] == "badY"
    # counts_by_trigger accumulates across BOTH tables (2 total), and the
    # per-key row_error fold covers both columns.
    assert res.quality_metrics["quarantine"]["counts_by_trigger"] == {"format_error": 2}
    assert res.quality_metrics["quarantine"]["total_quarantined"] == 2
    assert res.quality_metrics["row_errors"] == {
        "parent.age[format_error]": 1,
        "child.note[format_error]": 1,
    }


# ===========================================================================
# Cluster 7: byte parity + eviction (clean FK chain, no row errors, no sink).
# ===========================================================================


def _full_frame(adapter, plan, sources, graph, ns, registry):
    return adapter.run(
        plan, sources, registry=registry, relationship_graph=graph, namespace_registry=ns
    )


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.REMAP])
def test_byte_parity_with_full_frame(policy) -> None:
    adapter = PandasExecutionAdapter()
    fx = build_fk_relational(rows=400, width=2, orphan_frac=0.05)
    graph = fx.graph(policy)
    full = _full_frame(adapter, fx.plan, fx.sources, graph, fx.namespace_registry, fx.registry)
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


def test_fail_policy_aborts(tmp_path: Path) -> None:
    adapter = PandasExecutionAdapter()
    fx = build_fk_relational(rows=300, width=2, orphan_frac=0.05)
    with pytest.raises(ExecutionError) as exc:
        adapter.run_sequential(
            fx.plan,
            _loader(fx.sources),
            registry=fx.registry,
            relationship_graph=fx.graph(OrphanPolicy.FAIL),
            namespace_registry=fx.namespace_registry,
        )
    assert exc.value.code == "orphan_fk_violation"


# ===========================================================================
# Cluster 8: sink streaming (callable sink, no quarantine).
# ===========================================================================


def test_callable_sink_streams_each_table_once(tmp_path: Path) -> None:
    adapter = PandasExecutionAdapter()
    fx = build_fk_relational(rows=200, width=2, orphan_frac=0.0)
    seen: dict[str, pa.Table] = {}

    def sink(table: str, out: pa.Table) -> None:
        assert table not in seen, f"{table} emitted twice"
        seen[table] = out

    res = adapter.run_sequential(
        fx.plan,
        _loader(fx.sources),
        registry=fx.registry,
        relationship_graph=fx.graph(OrphanPolicy.PRESERVE),
        namespace_registry=fx.namespace_registry,
        sink=sink,
    )
    assert set(seen) == set(fx.sources)
    assert res.outputs == {}
    full = _full_frame(
        adapter,
        fx.plan,
        fx.sources,
        fx.graph(OrphanPolicy.PRESERVE),
        fx.namespace_registry,
        fx.registry,
    )
    for table in full.outputs:
        assert seen[table].equals(full.outputs[table]), f"{table} sink differs"


# ===========================================================================
# Cluster 9: keyed-mask wiring (mask_key is load-bearing).
# ===========================================================================


def test_keyed_mask_differs_from_unkeyed(tmp_path: Path) -> None:
    """Passing a SecretKeyProvider makes StrategyContext.mask_key the 32-byte
    secret (not job_seed), so keyed hash output differs from the unkeyed run.
    Kills mutants that drop/None the mask_key threading (it collapses back to
    job_seed and keyed == unkeyed)."""
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
        key_provider=SecretKeyProvider(b"a-strong-32B+-managed-secret-value!!", key_version="v1"),
    )
    # The masked parent key column must change under a real secret.
    assert not keyed.outputs["parent"].equals(unkeyed.outputs["parent"])


# ===========================================================================
# Cluster 10: date_shift group_by pre-mask anchor snapshot (single table).
# ===========================================================================


def _group_by_config(tmp_path: Path) -> dict[str, Any]:
    """A single table `t` where `d` is date_shift-anchored to a MASKED entity
    column `patient_id` (hash). The date_shift handler must read the entity
    from the PRE-MASK snapshot; if the snapshot is not taken, it fails closed
    with `date_shift_group_anchor_snapshot_missing`."""
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
        "global_settings": {"job_name": "seq-kills-groupby", "seed": 7},
        "sources": {
            "t": {"type": "file", "path": _write_source(tmp_path, src, "t"), "format": "parquet"}
        },
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


def test_group_by_anchor_uses_pre_mask_snapshot(tmp_path: Path) -> None:
    import datetime

    config = _group_by_config(tmp_path)
    plan, graph, ns, registry = compile_job(config)
    # Force sequential to require group_by tables to be included in the graph
    # table set; a graph with no edges still drives table_topo_order via the
    # plan's per_table, so run_sequential loads/masks `t`.
    res = PandasExecutionAdapter().run_sequential(
        plan,
        _loader(_sources(config)),
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
    # Same patient -> same offset -> intra-entity interval preserved. This only
    # holds if the entity anchor came from the pre-mask snapshot: masking
    # patient_id (hash) does not disturb the anchoring.
    assert (out_dates[1] - out_dates[0]).days == (src_dates[1] - src_dates[0]).days
    assert (out_dates[3] - out_dates[2]).days == (src_dates[3] - src_dates[2]).days
    # Different patients get different offsets.
    assert (out_dates[0] - src_dates[0]).days != (out_dates[2] - src_dates[2]).days


# ===========================================================================
# Cluster 11: ExecutionResult telemetry fields (timings + conversion_ms).
# ===========================================================================


def test_result_reports_conversion_and_timings(tmp_path: Path) -> None:
    adapter = PandasExecutionAdapter()
    fx = build_fk_relational(rows=60, width=1, orphan_frac=0.0)
    res = adapter.run_sequential(
        fx.plan,
        _loader(fx.sources),
        registry=fx.registry,
        relationship_graph=fx.graph(OrphanPolicy.PRESERVE),
        namespace_registry=fx.namespace_registry,
    )
    # boundary_conversion_ms is accumulated from real (small, positive) Arrow
    # <-> pandas conversions: strictly > 0 (catches a dropped kwarg / None),
    # and bounded well below a wall-clock absolute reading (catches a `+ t0`
    # or `+ t1` leak that would fold the perf_counter epoch into the total,
    # producing a value in the millions of ms).
    assert isinstance(res.boundary_conversion_ms, float)
    assert res.boundary_conversion_ms > 0.0
    assert res.boundary_conversion_ms < 1000.0
    # timings is the collector's records tuple (non-empty: each masked node
    # records one), never None and never the empty default.
    assert isinstance(res.timings, tuple)
    assert len(res.timings) > 0
    # row_errors defaults to an empty tuple on a clean job (never None).
    assert res.row_errors == ()
    assert isinstance(res.quality_metrics, dict)


# ===========================================================================
# Cluster 12: clean single-edge FK child RI (pre-mask parent-key snapshot).
# ===========================================================================


def _clean_fk_config(tmp_path: Path) -> dict[str, Any]:
    """A single FK edge (parent.id -> child.parent_id), faker-masked in the
    same namespace so referential integrity holds. No row errors. The child's
    parent_id resolves through the parent key map, which is built from the
    PRE-MASK source snapshot of parent.id -- so a broken snapshot loop shows
    up as child RI drift versus the full-frame path."""
    parent = pa.table({"id": pa.array(_IDS, type=pa.string())})
    child = pa.table(
        {
            "id": pa.array([f"c{i}" for i in range(len(_IDS))], type=pa.string()),
            "parent_id": pa.array(_IDS, type=pa.string()),
        }
    )
    return {
        "version": 1,
        "global_settings": {"job_name": "seq-kills-clean-fk", "seed": 42},
        "sources": {
            "parent": {
                "type": "file",
                "path": _write_source(tmp_path, parent, "parent"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": _write_source(tmp_path, child, "child"),
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
            {"name": "parent", "columns": [_faker_col("id", "parent_ns")]},
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


def test_clean_fk_child_ri_matches_full_frame(tmp_path: Path) -> None:
    config = _clean_fk_config(tmp_path)
    plan, graph, ns, registry = compile_job(config)
    adapter = PandasExecutionAdapter()
    sources = _sources(config)
    full = adapter.run(
        plan, sources, registry=registry, relationship_graph=graph, namespace_registry=ns
    )
    seq = adapter.run_sequential(
        plan,
        _loader(sources),
        registry=registry,
        relationship_graph=graph,
        namespace_registry=ns,
    )
    # Byte parity, and specifically the child's masked parent_id must equal the
    # full-frame child (RI resolved through the pre-mask parent-key snapshot).
    assert seq.outputs["parent"].equals(full.outputs["parent"])
    assert seq.outputs["child"].equals(full.outputs["child"])
    # The child's masked FK values are exactly the masked parent keys (RI).
    parent_ids = set(seq.outputs["parent"].column("id").to_pylist())
    child_fk = set(seq.outputs["child"].column("parent_id").to_pylist())
    assert child_fk <= parent_ids


# ===========================================================================
# Cluster 13: self-referential FK key-error cascade (intra-table _dispatch
# threading of key_error_rows / source_snapshots).
# ===========================================================================


def _self_fk_config(tmp_path: Path, qpath: str) -> dict[str, Any]:
    ids = ["notadate", "2020-01-01", "2020-03-01"]
    manager_ids = [None, "2020-01-01", "notadate"]
    employees = pa.table(
        {
            "id": pa.array(ids, type=pa.string()),
            "manager_id": pa.array(manager_ids, type=pa.string()),
        }
    )
    return {
        "version": 1,
        "global_settings": {"job_name": "seq-kills-selfref", "seed": 42},
        "sources": {
            "employees": {
                "type": "file",
                "path": _write_source(tmp_path, employees, "employees"),
                "format": "parquet",
            }
        },
        "targets": {
            "employees": {
                "type": "file",
                "path": str(tmp_path / "e.out.parquet"),
                "format": "parquet",
            }
        },
        "tables": [
            {
                "name": "employees",
                "columns": [
                    {
                        "name": "id",
                        "strategy": "date_shift",
                        "provider_config": {"min_days": 1, "max_days": 30},
                        "namespace": "employee_ns",
                    },
                    _faker_col("manager_id", "employee_ns"),
                ],
            }
        ],
        "relationships": [
            {
                "parent": {"table": "employees", "columns": ["id"]},
                "children": [{"table": "employees", "columns": ["manager_id"]}],
                "orphan_policy": "preserve",
                "namespace": "employee_ns",
            }
        ],
        "quarantine": {"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
    }


def test_self_ref_fk_key_error_cascades(tmp_path: Path) -> None:
    qpath = str(tmp_path / "q.jsonl")
    config = _self_fk_config(tmp_path, qpath)
    plan, graph, ns, registry = compile_job(config)
    quar = {"enabled": True, "output_path": qpath, "triggers": ["format_error"]}
    res = PandasExecutionAdapter().run_sequential(
        plan,
        _loader(_sources(config)),
        registry=registry,
        relationship_graph=graph,
        namespace_registry=ns,
        quarantine_config=quar,
    )
    out = res.outputs["employees"]
    ids = out.column("id").to_pylist()
    manager_ids = out.column("manager_id").to_pylist()
    # The raw errored key never leaks through the self-FK: the per-node drain +
    # fold makes the manager_id (self-FK child) see the id node's error in the
    # SAME table iteration.
    assert "notadate" not in ids
    assert "notadate" not in manager_ids
    # Only the clean, self-referencing row survives (row 0's own key errored;
    # row 2 references the errored key and cascades).
    assert out.num_rows == 1
    assert ids[0] == manager_ids[0]


# ===========================================================================
# Cluster 14: diamond FK (one parent, two children) parent-map retention.
# ===========================================================================


def _diamond_config(tmp_path: Path) -> dict[str, Any]:
    """One parent, two children referencing it. The parent's key map must be
    retained until BOTH children consume it; a mutant that frees it after the
    first child breaks the second child's RI (its source snapshot is already
    evicted, so the map cannot be rebuilt)."""
    parent = pa.table({"id": pa.array(_IDS, type=pa.string())})
    child_a = pa.table(
        {
            "a_id": pa.array([f"a{i}" for i in range(len(_IDS))], type=pa.string()),
            "pa_id": pa.array(_IDS, type=pa.string()),
        }
    )
    child_b = pa.table(
        {
            "b_id": pa.array([f"b{i}" for i in range(len(_IDS))], type=pa.string()),
            "pb_id": pa.array(_IDS, type=pa.string()),
        }
    )
    return {
        "version": 1,
        "global_settings": {"job_name": "seq-kills-diamond", "seed": 42},
        "sources": {
            "parent": {
                "type": "file",
                "path": _write_source(tmp_path, parent, "parent"),
                "format": "parquet",
            },
            "child_a": {
                "type": "file",
                "path": _write_source(tmp_path, child_a, "child_a"),
                "format": "parquet",
            },
            "child_b": {
                "type": "file",
                "path": _write_source(tmp_path, child_b, "child_b"),
                "format": "parquet",
            },
        },
        "targets": {
            "parent": {
                "type": "file",
                "path": str(tmp_path / "p.out.parquet"),
                "format": "parquet",
            },
            "child_a": {
                "type": "file",
                "path": str(tmp_path / "a.out.parquet"),
                "format": "parquet",
            },
            "child_b": {
                "type": "file",
                "path": str(tmp_path / "b.out.parquet"),
                "format": "parquet",
            },
        },
        "tables": [
            {"name": "parent", "columns": [_faker_col("id", "parent_ns")]},
            {"name": "child_a", "columns": [_faker_col("pa_id", "parent_ns")]},
            {"name": "child_b", "columns": [_faker_col("pb_id", "parent_ns")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [
                    {"table": "child_a", "columns": ["pa_id"]},
                    {"table": "child_b", "columns": ["pb_id"]},
                ],
                "orphan_policy": "preserve",
                "namespace": "parent_ns",
            }
        ],
    }


def test_diamond_second_child_ri_matches_full_frame(tmp_path: Path) -> None:
    config = _diamond_config(tmp_path)
    plan, graph, ns, registry = compile_job(config)
    adapter = PandasExecutionAdapter()
    sources = _sources(config)
    full = adapter.run(
        plan, sources, registry=registry, relationship_graph=graph, namespace_registry=ns
    )
    seq = adapter.run_sequential(
        plan,
        _loader(sources),
        registry=registry,
        relationship_graph=graph,
        namespace_registry=ns,
    )
    # BOTH children must match the full-frame output. A parent-map freed after
    # the first child drifts the second child's FK values.
    for table in ("parent", "child_a", "child_b"):
        assert seq.outputs[table].equals(full.outputs[table]), f"{table} differs"


# ===========================================================================
# Cluster 15: per-key row-error count accumulation (two errors, one key).
# ===========================================================================


def test_row_error_count_accumulates_same_key(tmp_path: Path) -> None:
    """Two uncoercible cells in the SAME column produce a count of 2 under the
    one `table.column[trigger]` key. Kills a mutant that looks the running
    count up under the wrong key (never accumulating past 1)."""
    qpath = str(tmp_path / "q.jsonl")
    parent = pa.table(
        {
            "id": pa.array(_IDS, type=pa.string()),
            "age": pa.array(["10", "bad1", "30", "bad2", "50", "60"], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "id": pa.array([f"c{i}" for i in range(len(_IDS))], type=pa.string()),
            "parent_id": pa.array(_IDS, type=pa.string()),
        }
    )
    config = {
        "version": 1,
        "global_settings": {"job_name": "seq-kills-twoerr", "seed": 42},
        "sources": {
            "parent": {
                "type": "file",
                "path": _write_source(tmp_path, parent, "parent"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": _write_source(tmp_path, child, "child"),
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
        "quarantine": {"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
    }
    plan, graph, ns, registry = compile_job(config)
    quar = {"enabled": True, "output_path": qpath, "triggers": ["format_error"]}
    res = PandasExecutionAdapter().run_sequential(
        plan,
        _loader(_sources(config)),
        registry=registry,
        relationship_graph=graph,
        namespace_registry=ns,
        quarantine_config=quar,
    )
    assert res.quality_metrics["row_errors"] == {"parent.age[format_error]": 2}
    assert res.quality_metrics["quarantine"]["counts_by_trigger"] == {"format_error": 2}
    assert res.outputs["parent"].num_rows == 4


# ===========================================================================
# Cluster 16: transactional-sink quarantine commit-fate edge cases.
# ===========================================================================


class _AbortTrackingSink:
    def __init__(self, inner: ParquetTransactionalSink) -> None:
        self._inner = inner
        self.abort_called = False

    def write(self, table: str, data: pa.Table) -> None:
        self._inner.write(table, data)

    def commit(self) -> None:
        self._inner.commit()

    def abort(self) -> None:
        self.abort_called = True
        self._inner.abort()


def test_sink_quarantine_preexisting_file_refuses_to_publish(tmp_path: Path) -> None:
    """A genuine TransactionalSink stages then exclusive-create publishes the
    quarantine sidecar. A pre-existing file at output_path must make the
    publish refuse (loud ValueError), not silently overwrite. Kills a mutant
    that routes a genuine sink through the plain direct-write branch."""
    qpath_file = tmp_path / "q.jsonl"
    qpath_file.write_text("pre-existing content, not a quarantine record")
    qpath = str(qpath_file)
    quar = {"enabled": True, "output_path": qpath, "triggers": ["format_error"]}
    config = _fk_bucketize_config(tmp_path, quar)
    plan, graph, ns, registry = compile_job(config)
    with pytest.raises(ValueError, match="refusing to publish quarantine"):
        PandasExecutionAdapter().run_sequential(
            plan,
            _loader(_sources(config)),
            registry=registry,
            relationship_graph=graph,
            namespace_registry=ns,
            sink=ParquetTransactionalSink(tmp_path / "out"),
            quarantine_config=quar,
        )
    # The masked tables committed; the pre-existing file is untouched.
    assert (tmp_path / "out" / "parent.parquet").exists()
    assert qpath_file.read_text() == "pre-existing content, not a quarantine record"


def test_sink_quarantine_alias_committed_table_refuses(tmp_path: Path) -> None:
    """output_path aliasing a committed table's artifact must be refused by the
    name-based alias guard (which needs the real sink, not None). Kills a
    mutant that passes sink=None into finalize_committed_quarantine."""
    target = tmp_path / "out"
    qpath = str(target / "parent.parquet")  # aliases the committed table
    quar = {"enabled": True, "output_path": qpath, "triggers": ["format_error"]}
    config = _fk_bucketize_config(tmp_path, quar)
    plan, graph, ns, registry = compile_job(config)
    with pytest.raises(ValueError, match="aliases the output artifact"):
        PandasExecutionAdapter().run_sequential(
            plan,
            _loader(_sources(config)),
            registry=registry,
            relationship_graph=graph,
            namespace_registry=ns,
            sink=ParquetTransactionalSink(target),
            quarantine_config=quar,
        )
    # The masked table is untouched: still masked Parquet, no raw cell.
    assert "badX" not in pq.read_table(target / "parent.parquet").column("age").to_pylist()


def test_sink_post_commit_publish_failure_does_not_abort(tmp_path: Path) -> None:
    """A publish failure AFTER a successful commit (a pre-existing directory at
    output_path) must NOT call the sink's abort(): the tables are already
    durably committed. Kills mutants that mis-track `_committed` or widen the
    abort condition so abort fires post-commit."""
    qpath_dir = tmp_path / "q.jsonl"
    qpath_dir.mkdir()
    (qpath_dir / "occupant.txt").write_text("pre-existing")
    qpath = str(qpath_dir)
    quar = {"enabled": True, "output_path": qpath, "triggers": ["format_error"]}
    config = _fk_bucketize_config(tmp_path, quar)
    plan, graph, ns, registry = compile_job(config)
    sink = _AbortTrackingSink(ParquetTransactionalSink(tmp_path / "out"))
    with pytest.raises(ValueError, match="refusing to publish quarantine"):
        PandasExecutionAdapter().run_sequential(
            plan,
            _loader(_sources(config)),
            registry=registry,
            relationship_graph=graph,
            namespace_registry=ns,
            sink=sink,
            quarantine_config=quar,
        )
    assert sink.abort_called is False
    # Commit already succeeded: the masked tables are published.
    assert (tmp_path / "out" / "parent.parquet").exists()
