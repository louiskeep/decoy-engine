"""P3 (job-performance sprints): auto-select chunked execution in `run_pipeline`.

P3 routes ELIGIBLE large single-table mask jobs through the existing
`run_mask_pipeline_chunked` entrypoint instead of the full-frame adapter
call, so peak memory drops while output stays byte-identical. The
routing decision is the P2 planner's `chunked` classification extended
with the P3 gates (size threshold, pandas-substrate pin, composite-work
guard, fpe join-group warning parity, chunk-stable source dtypes), plus
the `auto_chunk` kill switch on `run_pipeline`.

The load-bearing contract pinned here, in order of importance:

1. BYTE IDENTITY: for every strategy admitted to auto-routing, the
   auto-chunked output equals the same job forced full-frame
   (`auto_chunk=False`) exactly -- values AND schema.
2. FAIL-CLOSED: every non-eligible shape (non-chunk-safe strategy,
   multi-table, generation, relationships, polars substrate, below
   threshold, unstable dtypes, join-group fpe) takes the unchanged
   full-frame path with a recorded reason.
3. VAULT: a vault-collecting eligible job produces the same vault
   entries chunked as full-frame.
4. The P1 golden holds: a plain default small run stamps nothing
   (`quality_metrics == {}`).
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import ExecutionError, run_pipeline

_ENGINE_VERSION = "p3-auto-chunk-test"

# Small-run knobs used across this module: a 60-row frame routes chunked
# once the threshold knob is lowered to 10 (4 chunks of 16 with an
# uneven tail), keeping the matrix fast while exercising real chunking.
_ROWS = 60
_LOW_THRESHOLD = 10
_CHUNK = 16


def _validated_dump(cfg: dict) -> dict:
    return PipelineConfig.model_validate(cfg).model_dump()


def _config(tmp_path, columns: list[dict], table: str = "accounts") -> dict:
    return _validated_dump(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {table: {"type": "file", "format": "csv", "path": str(tmp_path / "in.csv")}},
            "tables": [{"name": table, "columns": columns}],
            "targets": {
                table: {"type": "file", "format": "csv", "path": str(tmp_path / "out.csv")}
            },
        }
    )


# One column spec + source values per strategy admitted to auto-routing.
# This is the byte-identity matrix: every strategy the routing can admit
# must prove chunked == full-frame at the run_pipeline level.
_STRATEGY_MATRIX: dict[str, tuple[dict[str, Any], list[Any]]] = {
    "hash": (
        {"name": "val", "strategy": "hash", "namespace": "hash_ns"},
        [f"user{i}@example.com" for i in range(_ROWS)],
    ),
    "fpe": (
        {
            "name": "val",
            "strategy": "fpe",
            "namespace": "fpe_ns",
            "provider_config": {"charset": "digits"},
        },
        [f"{i:09d}" for i in range(_ROWS)],
    ),
    "redact": (
        {"name": "val", "strategy": "redact"},
        [f"secret-{i}" for i in range(_ROWS)],
    ),
    "truncate": (
        {"name": "val", "strategy": "truncate", "provider_config": {"length": 3}},
        [f"{10000 + i:05d}" for i in range(_ROWS)],
    ),
    "text_redact": (
        {"name": "val", "strategy": "text_redact"},
        [f"contact user{i}@example.com or 123-45-6789 today" for i in range(_ROWS)],
    ),
    "date_shift": (
        # date_format is explicit because auto-routing requires it: without
        # it the strategy detects the format from whole-column samples, which
        # is chunk-variant (see TestChunkStateGates).
        {
            "name": "val",
            "strategy": "date_shift",
            "namespace": "dob_ns",
            "provider_config": {"min_days": -30, "max_days": 30, "date_format": "%Y-%m-%d"},
        },
        [f"19{60 + (i % 40):02d}-03-{1 + (i % 28):02d}" for i in range(_ROWS)],
    ),
    "bucketize": (
        {"name": "val", "strategy": "bucketize", "provider_config": {"width": 50}},
        [i * 13 % 997 for i in range(_ROWS)],
    ),
    "passthrough": (
        {"name": "val", "strategy": "passthrough"},
        [f"keep-{i}" for i in range(_ROWS)],
    ),
    "faker_deterministic": (
        {
            "name": "val",
            "strategy": "faker",
            "provider": "person_email",
            "deterministic": True,
            "namespace": "contact_ns",
            "cardinality_mode": "reuse",
            "provider_config": {"pool_size": 20},
        },
        [f"person{i}@source.example" for i in range(_ROWS)],
    ),
    "categorical_deterministic": (
        {
            "name": "val",
            "strategy": "categorical",
            "deterministic": True,
            "namespace": "tier_ns",
            "provider_config": {"categories": ["free", "pro", "team"], "weights": [0.6, 0.3, 0.1]},
        },
        [["bronze", "silver", "gold"][i % 3] for i in range(_ROWS)],
    ),
}


def _single_column_job(tmp_path, spec_name: str) -> tuple[dict, dict[str, pa.Table]]:
    column, values = _STRATEGY_MATRIX[spec_name]
    df = pd.DataFrame({"val": values})
    df.to_csv(tmp_path / "in.csv", index=False)
    cfg = _config(tmp_path, [column])
    return cfg, {"accounts": pa.Table.from_pandas(df, preserve_index=False)}


def _run_pair(cfg, sources, monkeypatch, **kwargs):
    """Run the same job auto-chunked and forced full-frame; return both results.

    The auto run lowers the threshold knob so the small fixture routes;
    the forced run flips only the kill switch, so any output difference
    is the routing itself.
    """
    monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
    auto = run_pipeline(
        cfg,
        sources=sources,
        engine_version=_ENGINE_VERSION,
        auto_chunk_threshold_rows=_LOW_THRESHOLD,
        chunk_size_rows=_CHUNK,
        **kwargs,
    )
    forced = run_pipeline(
        cfg,
        sources=sources,
        engine_version=_ENGINE_VERSION,
        auto_chunk=False,
        auto_chunk_threshold_rows=_LOW_THRESHOLD,
        chunk_size_rows=_CHUNK,
        **kwargs,
    )
    return auto, forced


def _assert_row_errors_fail_identically_both_routes(cfg, sources, monkeypatch, **kwargs):
    """S1 honesty pack x S3 auto-chunk reconciliation: a source that
    triggers a row-level format/mask error (S1, D7/D8) fails loud on
    BOTH the auto-routed and forced-full-frame calls -- since routing
    correctly declines to chunk this shape (whole-column-state gate),
    both take the identical full-frame path and both raise the same
    `RowErrorsFailedError` before any output is produced. This is the
    fail-closed-parity form of the byte-identity contract for a source
    shape that S1 (landed after these fixtures were authored) turned
    from a silent pass-through into a hard failure.
    """
    monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
    with pytest.raises(RowErrorsFailedError) as auto_exc:
        run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=_CHUNK,
            **kwargs,
        )
    with pytest.raises(RowErrorsFailedError) as forced_exc:
        run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk=False,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=_CHUNK,
            **kwargs,
        )
    auto_counts = sorted((r.table, r.column, r.trigger) for r in auto_exc.value.records)
    forced_counts = sorted((r.table, r.column, r.trigger) for r in forced_exc.value.records)
    assert auto_counts == forced_counts
    assert len(auto_exc.value.records) == len(forced_exc.value.records)


def _composite_job_config(tmp_path) -> tuple[dict, dict[str, pa.Table]]:
    """Composite (coherent_with) faker job whose per-column config satisfies
    the chunked faker admission conditions, exposing the bundle-state gap
    the planner's composite gate closes."""
    df = pd.DataFrame(
        {
            "first_name": [f"F{i}" for i in range(_ROWS)],
            "last_name": [f"L{i}" for i in range(_ROWS)],
            "email": [f"e{i}@x.com" for i in range(_ROWS)],
        }
    )
    df.to_csv(tmp_path / "in.csv", index=False)

    def col(name: str, others: list[str]) -> dict[str, Any]:
        return {
            "name": name,
            "strategy": "faker",
            "provider": "composite_name_email",
            "deterministic": True,
            "namespace": "ne",
            "coherent_with": others,
            "cardinality_mode": "reuse",
            "provider_config": {"pool_size": 20},
        }

    cfg = _config(
        tmp_path,
        [
            col("first_name", ["last_name", "email"]),
            col("last_name", ["first_name", "email"]),
            col("email", ["first_name", "last_name"]),
        ],
    )
    return cfg, {"accounts": pa.Table.from_pandas(df, preserve_index=False)}


# --------------------------------------------------------------------------
# Byte identity across the full admitted strategy set
# --------------------------------------------------------------------------


class TestByteIdentityMatrix:
    @pytest.mark.parametrize("spec_name", sorted(_STRATEGY_MATRIX))
    def test_strategy_byte_identity(self, tmp_path, monkeypatch, spec_name):
        """Every admitted strategy: auto-chunked output == forced full-frame
        output, values and schema both (the P3 core correctness gate)."""
        cfg, sources = _single_column_job(tmp_path, spec_name)
        auto, forced = _run_pair(cfg, sources, monkeypatch)
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked", (
            f"{spec_name} did not auto-route; the matrix must exercise the chunked path"
        )
        assert forced.quality_metrics["auto_chunk"]["mode"] == "full_frame"
        a, f = auto.outputs["accounts"], forced.outputs["accounts"]
        assert [str(fl.type) for fl in a.schema] == [str(fl.type) for fl in f.schema]
        assert a.equals(f), f"{spec_name}: chunked output diverged from full-frame"

    def test_all_admitted_strategies_together_byte_identity(self, tmp_path, monkeypatch):
        """The whole admitted set in one table, uneven tail chunk included."""
        columns = []
        data: dict[str, list[Any]] = {}
        for name, (column, values) in sorted(_STRATEGY_MATRIX.items()):
            col = dict(column)
            col["name"] = name
            columns.append(col)
            data[name] = values
        df = pd.DataFrame(data)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(tmp_path, columns)
        sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}
        auto, forced = _run_pair(cfg, sources, monkeypatch)
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        assert auto.outputs["accounts"].equals(forced.outputs["accounts"])

    def test_nulls_preserved_byte_identically(self, tmp_path, monkeypatch):
        """String-column nulls (including a fully-null tail chunk) survive
        chunked routing byte-identically to full-frame."""
        values = [f"user{i}@example.com" for i in range(_ROWS)]
        for i in (3, 17, *range(48, _ROWS)):  # the last chunk is all-null
            values[i] = None
        df = pd.DataFrame({"val": pd.array(values, dtype="string").astype(object)})
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(tmp_path, [{"name": "val", "strategy": "hash", "namespace": "hash_ns"}])
        sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}
        auto, forced = _run_pair(cfg, sources, monkeypatch)
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        a, f = auto.outputs["accounts"], forced.outputs["accounts"]
        assert [str(fl.type) for fl in a.schema] == [str(fl.type) for fl in f.schema]
        assert a.equals(f)


# --------------------------------------------------------------------------
# Routing mechanics: the chunked entrypoint really runs, knobs are recorded
# --------------------------------------------------------------------------


class TestRoutingMechanics:
    def test_eligible_job_routes_through_chunked_entrypoint(self, tmp_path, monkeypatch):
        from decoy_engine.execution import _chunked

        cfg, sources = _single_column_job(tmp_path, "hash")
        calls: list[dict[str, Any]] = []
        real = _chunked.run_mask_pipeline_chunked

        def spy(config, chunks, **kwargs):
            chunk_list = list(chunks)
            calls.append({"n_chunks": len(chunk_list), **kwargs})
            return real(config, chunk_list, **kwargs)

        monkeypatch.setattr(_chunked, "run_mask_pipeline_chunked", spy)
        auto, _forced = _run_pair(cfg, sources, monkeypatch)
        assert len(calls) == 1
        assert calls[0]["table"] == "accounts"
        assert calls[0]["n_chunks"] == 4  # 60 rows / 16-row chunks
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"

    def test_auto_chunk_stamp_records_mode_size_and_reason(self, tmp_path, monkeypatch):
        cfg, sources = _single_column_job(tmp_path, "hash")
        auto, _ = _run_pair(cfg, sources, monkeypatch)
        block = auto.quality_metrics["auto_chunk"]
        assert block["mode"] == "chunked"
        assert block["chunk_size_rows"] == _CHUNK
        assert block["threshold_rows"] == _LOW_THRESHOLD
        assert block["source_rows"] == _ROWS
        assert block["chunk_count"] == 4
        assert isinstance(block["reason"], str) and block["reason"]

    def test_decision_is_deterministic_across_runs(self, tmp_path, monkeypatch):
        cfg, sources = _single_column_job(tmp_path, "hash")
        first, _ = _run_pair(cfg, sources, monkeypatch)
        second, _ = _run_pair(cfg, sources, monkeypatch)
        assert first.quality_metrics["auto_chunk"] == second.quality_metrics["auto_chunk"]
        assert first.outputs["accounts"].equals(second.outputs["accounts"])

    def test_explain_plan_mode_matches_routing(self, tmp_path, monkeypatch):
        """The stamped execution_plan and the actual route agree: one
        classification drives both (no drift between explain and execute)."""
        cfg, sources = _single_column_job(tmp_path, "hash")
        auto, _ = _run_pair(cfg, sources, monkeypatch, explain_plan=True)
        assert auto.quality_metrics["execution_plan"]["mode"] == "chunked"
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"


# --------------------------------------------------------------------------
# Fail-closed: non-eligible shapes stay on the unchanged full-frame path
# --------------------------------------------------------------------------


def _assert_full_frame_and_identical(auto, forced, table: str, rejection: str | None = None):
    """The job did not chunk, and its output equals the forced full-frame run."""
    block = auto.quality_metrics.get("auto_chunk")
    if block is not None:
        assert block["mode"] == "full_frame"
    plan_block = auto.quality_metrics.get("execution_plan")
    if rejection is not None and plan_block is not None:
        assert rejection in plan_block["rejections"]["chunked"]
    assert auto.outputs[table].equals(forced.outputs[table])


class TestFailClosed:
    def test_shuffle_strategy_stays_full_frame(self, tmp_path, monkeypatch):
        """shuffle is a whole-column permutation (not value-keyed) and is
        also nondeterministic across runs, so identity is asserted on the
        PATH (chunked entrypoint must never run) rather than on output
        bytes; the output is still checked to be a permutation."""
        from decoy_engine.execution import _chunked

        def bomb(*args, **kwargs):
            raise AssertionError("chunked entrypoint ran for a non-chunk-safe strategy")

        monkeypatch.setattr(_chunked, "run_mask_pipeline_chunked", bomb)
        df = pd.DataFrame({"val": [f"v{i}" for i in range(_ROWS)]})
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(tmp_path, [{"name": "val", "strategy": "shuffle"}])
        sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}
        auto, _forced = _run_pair(cfg, sources, monkeypatch, explain_plan=True)
        assert auto.quality_metrics["auto_chunk"]["mode"] == "full_frame"
        plan_block = auto.quality_metrics["execution_plan"]
        assert "strategy_not_chunk_safe" in plan_block["rejections"]["chunked"]
        assert sorted(auto.outputs["accounts"].column("val").to_pylist()) == sorted(df["val"])

    def test_multi_mask_table_stays_full_frame(self, tmp_path, monkeypatch):
        df = pd.DataFrame({"val": [f"v{i}" for i in range(_ROWS)]})
        cfg = _validated_dump(
            {
                "version": 1,
                "global_settings": {"seed": 42},
                "sources": {
                    name: {"type": "file", "format": "csv", "path": str(tmp_path / f"{name}.csv")}
                    for name in ("a", "b")
                },
                "tables": [
                    {
                        "name": name,
                        "columns": [{"name": "val", "strategy": "hash", "namespace": f"{name}_ns"}],
                    }
                    for name in ("a", "b")
                ],
                "targets": {
                    name: {
                        "type": "file",
                        "format": "csv",
                        "path": str(tmp_path / f"{name}_out.csv"),
                    }
                    for name in ("a", "b")
                },
            }
        )
        for name in ("a", "b"):
            df.to_csv(tmp_path / f"{name}.csv", index=False)
        table = pa.Table.from_pandas(df, preserve_index=False)
        sources = {"a": table, "b": table}
        auto, forced = _run_pair(cfg, sources, monkeypatch, explain_plan=True)
        _assert_full_frame_and_identical(auto, forced, "a", "one table per run")
        _assert_full_frame_and_identical(auto, forced, "b")

    def test_generate_table_stays_full_frame(self, tmp_path, monkeypatch):
        df = pd.DataFrame({"val": [f"v{i}" for i in range(_ROWS)]})
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _validated_dump(
            {
                "version": 1,
                "global_settings": {"seed": 42},
                "sources": {
                    "accounts": {"type": "file", "format": "csv", "path": str(tmp_path / "in.csv")}
                },
                "tables": [
                    {
                        "name": "accounts",
                        "columns": [{"name": "val", "strategy": "hash", "namespace": "ns"}],
                    },
                    {
                        "name": "gen",
                        "row_count": 5,
                        "generate_columns": [{"name": "id", "type": "sequence", "start": 1}],
                    },
                ],
                "targets": {
                    "accounts": {
                        "type": "file",
                        "format": "csv",
                        "path": str(tmp_path / "out.csv"),
                    },
                    "gen": {
                        "type": "file",
                        "format": "csv",
                        "path": str(tmp_path / "gen_out.csv"),
                    },
                },
            }
        )
        sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}
        auto, forced = _run_pair(cfg, sources, monkeypatch, explain_plan=True)
        _assert_full_frame_and_identical(auto, forced, "accounts", "generat")
        assert auto.outputs["gen"].equals(forced.outputs["gen"])

    def test_relationships_stay_full_frame(self, tmp_path, monkeypatch):
        customers = pd.DataFrame({"id": [f"C{i}" for i in range(_ROWS)]})
        orders = pd.DataFrame({"customer_id": [f"C{i % 10}" for i in range(_ROWS)]})
        customers.to_csv(tmp_path / "customers.csv", index=False)
        orders.to_csv(tmp_path / "orders.csv", index=False)
        cfg = _validated_dump(
            {
                "version": 1,
                "global_settings": {"seed": 42},
                "sources": {
                    "customers": {
                        "type": "file",
                        "format": "csv",
                        "path": str(tmp_path / "customers.csv"),
                    },
                    "orders": {
                        "type": "file",
                        "format": "csv",
                        "path": str(tmp_path / "orders.csv"),
                    },
                },
                "tables": [
                    {
                        "name": "customers",
                        "columns": [{"name": "id", "strategy": "hash", "namespace": "id_ns"}],
                    },
                    {
                        "name": "orders",
                        "columns": [
                            {"name": "customer_id", "strategy": "hash", "namespace": "id_ns"}
                        ],
                    },
                ],
                "relationships": [
                    {
                        "parent": {"table": "customers", "columns": ["id"]},
                        "children": [{"table": "orders", "columns": ["customer_id"]}],
                        "orphan_policy": "preserve",
                        "namespace": "id_ns",
                    }
                ],
                "targets": {
                    name: {
                        "type": "file",
                        "format": "csv",
                        "path": str(tmp_path / f"{name}_out.csv"),
                    }
                    for name in ("customers", "orders")
                },
            }
        )
        sources = {
            "customers": pa.Table.from_pandas(customers, preserve_index=False),
            "orders": pa.Table.from_pandas(orders, preserve_index=False),
        }
        auto, forced = _run_pair(cfg, sources, monkeypatch, explain_plan=True)
        _assert_full_frame_and_identical(auto, forced, "customers", "relationship")
        _assert_full_frame_and_identical(auto, forced, "orders")

    def test_polars_substrate_stays_on_polars_adapter(self, tmp_path, monkeypatch):
        """A polars-substrate job must not be silently moved to the pandas
        chunked path: the executed substrate is part of the job's contract.
        Every chunk-safe strategy is also polars-native, so this job
        classifies polars_native and never reaches the chunked mode."""
        cfg, sources = _single_column_job(tmp_path, "hash")
        auto, forced = _run_pair(cfg, sources, monkeypatch, substrate="polars", explain_plan=True)
        assert auto.quality_metrics["auto_chunk"]["mode"] == "full_frame"
        assert auto.quality_metrics["execution_plan"]["mode"] == "polars_native"
        # The polars adapter really ran (its per-strategy substrate-of-record).
        assert auto.quality_metrics["executed_substrate"] == {"hash": "polars"}
        assert auto.outputs["accounts"].equals(forced.outputs["accounts"])

    def test_planner_substrate_gate_rejects_chunked_for_polars(self, tmp_path, monkeypatch):
        """Defense-in-depth: when the chunked mode IS evaluated under a
        polars substrate (reachable via non-polars-native work, e.g. a
        composite bundle), the rejection names the substrate pin."""
        from decoy_engine.execution._planner import classify_job
        from decoy_engine.plan import compile_plan
        from decoy_engine.plan._seed import _normalize_job_seed_int
        from decoy_engine.profile import profile_source
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships import RelationshipGraph

        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg, sources = _composite_job_config(tmp_path)
        profile = profile_source(cfg, seed=_normalize_job_seed_int(cfg))
        plan = compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION)
        decision = classify_job(
            cfg,
            plan=plan,
            registry=get_default_registry(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            substrate="polars",
            source_tables=sources,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
        )
        assert decision.mode != "chunked"
        assert "polars" in decision.rejections["chunked"]

    def test_below_threshold_default_run_stamps_nothing(self, tmp_path, monkeypatch):
        """The P1 golden extended: a small eligible job under ALL-DEFAULT
        knobs stays full-frame and stamps no auto-chunk / adapter-selection
        quality metrics. S2 (landed on the integration branch ahead of
        this P3 test) unconditionally stamps `quality_metrics["execution"]`
        honesty telemetry on every full_frame run, so the original verbatim
        `== {}` golden no longer holds; the load-bearing claim -- a
        below-threshold job stamps nothing chunk/adapter-specific -- still
        does."""
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg, sources = _single_column_job(tmp_path, "hash")
        result = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
        assert set(result.quality_metrics) == {"execution"}
        forced = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, auto_chunk=False
        )
        assert result.outputs["accounts"].equals(forced.outputs["accounts"])

    def test_below_threshold_reason_recorded_via_explain(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg, sources = _single_column_job(tmp_path, "hash")
        result = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, explain_plan=True
        )
        block = result.quality_metrics["execution_plan"]
        assert block["mode"] == "pandas_fallback"
        assert "threshold" in block["rejections"]["chunked"]

    def test_int_column_with_nulls_stays_full_frame(self, tmp_path, monkeypatch):
        """pandas widens int+null to float PER FRAME, so per-chunk conversion
        is not byte-stable; the routing must fail closed on that shape."""
        vals: list[Any] = [i for i in range(_ROWS)]
        vals[7] = None
        df = pd.DataFrame(
            {
                "val": [f"v{i}" for i in range(_ROWS)],
                "amount": pd.array(vals, dtype="Int64"),
            }
        )
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(
            tmp_path,
            [
                {"name": "val", "strategy": "hash", "namespace": "ns"},
                {"name": "amount", "strategy": "passthrough"},
            ],
        )
        sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}
        auto, forced = _run_pair(cfg, sources, monkeypatch, explain_plan=True)
        _assert_full_frame_and_identical(auto, forced, "accounts", "amount")

    def test_int_column_without_nulls_still_routes(self, tmp_path, monkeypatch):
        """The dtype gate is about int+NULL, not int: a null-free int
        column chunk-converts stably and must not block the route."""
        df = pd.DataFrame(
            {
                "val": [f"v{i}" for i in range(_ROWS)],
                "amount": list(range(_ROWS)),
            }
        )
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(
            tmp_path,
            [
                {"name": "val", "strategy": "hash", "namespace": "ns"},
                {"name": "amount", "strategy": "passthrough"},
            ],
        )
        sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}
        auto, forced = _run_pair(cfg, sources, monkeypatch)
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        assert auto.outputs["accounts"].equals(forced.outputs["accounts"])

    def test_fpe_join_group_stays_full_frame_and_keeps_warning(self, tmp_path, monkeypatch):
        """fpe with fpe_join_group emits a security-relevant QualityWarning
        the chunked entrypoint cannot carry; the route fails closed so the
        warning survives. (A join group needs two member columns.)"""
        df = pd.DataFrame(
            {
                "phone": [f"{i:09d}" for i in range(_ROWS)],
                "mobile": [f"{i + 7:09d}" for i in range(_ROWS)],
            }
        )
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(
            tmp_path,
            [
                {
                    "name": name,
                    "strategy": "fpe",
                    "namespace": "phone_ns",
                    "provider_config": {"charset": "digits", "fpe_join_group": "phone_e164"},
                }
                for name in ("phone", "mobile")
            ],
        )
        sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}
        auto, forced = _run_pair(cfg, sources, monkeypatch, explain_plan=True)
        _assert_full_frame_and_identical(auto, forced, "accounts", "fpe_join_group")
        assert any(w.code == "fpe_join_group_active" for w in auto.warnings)

    def test_extra_loaded_source_table_stays_full_frame(self, tmp_path, monkeypatch):
        """The full-frame adapter echoes EVERY loaded source frame in its
        outputs; the chunked route only yields the mask table, so extra
        loaded frames fail the route closed to keep outputs identical."""
        cfg, sources = _single_column_job(tmp_path, "hash")
        sources = dict(sources)
        sources["extra"] = pa.Table.from_pandas(
            pd.DataFrame({"x": [1, 2, 3]}), preserve_index=False
        )
        auto, forced = _run_pair(cfg, sources, monkeypatch, explain_plan=True)
        _assert_full_frame_and_identical(auto, forced, "accounts", "extra")
        assert auto.outputs["extra"].equals(forced.outputs["extra"])

    def test_composite_provider_rejected_by_planner(self, tmp_path, monkeypatch):
        """A composite (coherent_with) provider that satisfies the per-column
        faker admission conditions still carries bundle state; the planner
        must reject it for chunked (the per-column compat gate cannot see
        bundle-ness)."""
        from decoy_engine.execution._planner import classify_job
        from decoy_engine.plan import compile_plan
        from decoy_engine.plan._seed import _normalize_job_seed_int
        from decoy_engine.profile import profile_source
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships import RelationshipGraph

        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg, sources = _composite_job_config(tmp_path)
        profile = profile_source(cfg, seed=_normalize_job_seed_int(cfg))
        plan = compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION)
        decision = classify_job(
            cfg,
            plan=plan,
            registry=get_default_registry(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            substrate="pandas",
            source_tables=sources,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
        )
        assert decision.mode != "chunked"
        assert "composite" in decision.rejections["chunked"]

    def test_missing_loaded_source_rejected_by_planner(self, tmp_path, monkeypatch):
        from decoy_engine.execution._planner import classify_job
        from decoy_engine.plan import compile_plan
        from decoy_engine.plan._seed import _normalize_job_seed_int
        from decoy_engine.profile import profile_source
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships import RelationshipGraph

        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg, _sources = _single_column_job(tmp_path, "hash")
        profile = profile_source(cfg, seed=_normalize_job_seed_int(cfg))
        plan = compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION)
        decision = classify_job(
            cfg,
            plan=plan,
            registry=get_default_registry(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            substrate="pandas",
            source_tables={},
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
        )
        assert decision.mode != "chunked"
        assert "no loaded source" in decision.rejections["chunked"]


# --------------------------------------------------------------------------
# Whole-column-state gates (P3 remediation): strategies whose output depends
# on whole-column state may only auto-route when that state is pinned in
# config (date_shift) or provably absent from the source (bucketize); `when`
# predicates are frame-scoped and never auto-route.
# --------------------------------------------------------------------------


def _classify(cfg, sources, *, substrate="pandas"):
    from decoy_engine.execution._planner import classify_job
    from decoy_engine.plan import compile_plan
    from decoy_engine.plan._seed import _normalize_job_seed_int
    from decoy_engine.profile import profile_source
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships import RelationshipGraph

    profile = profile_source(cfg, seed=_normalize_job_seed_int(cfg))
    plan = compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION)
    return classify_job(
        cfg,
        plan=plan,
        registry=get_default_registry(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        substrate=substrate,
        source_tables=sources,
        auto_chunk_threshold_rows=_LOW_THRESHOLD,
    )


class TestChunkStateGates:
    def test_date_shift_mixed_format_without_explicit_format_stays_full_frame(
        self, tmp_path, monkeypatch
    ):
        """P3 remediation BLOCKER 1: date_shift without an explicit
        date_format detects the format from whole-column samples; per-chunk
        detection can lock a DIFFERENT format (the reviewer's mixed ISO +
        dd/mm/YYYY counterexample), so the job must fail closed."""
        values = [f"20{10 + i % 5:02d}-03-{(i % 28) + 1:02d}" for i in range(16)]
        values += [f"{13 + (i % 15):02d}/03/20{10 + i % 5:02d}" for i in range(16, _ROWS)]
        df = pd.DataFrame({"val": values})
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(
            tmp_path,
            [
                {
                    "name": "val",
                    "strategy": "date_shift",
                    "namespace": "dob_ns",
                    "provider_config": {"min_days": -30, "max_days": 30},
                }
            ],
        )
        sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}
        # S1's honesty pack (landed after this fixture was authored) turns
        # the dd/mm/YYYY rows' unparseable-as-ISO cells into row errors on
        # the full-frame date_shift pass; both routes must decline to
        # chunk this shape and fail identically (see the helper's docstring).
        _assert_row_errors_fail_identically_both_routes(
            cfg, sources, monkeypatch, explain_plan=True
        )

    def test_date_shift_with_explicit_format_still_routes_byte_identical(
        self, tmp_path, monkeypatch
    ):
        """An explicit date_format removes the whole-column dependency, so
        the job routes and stays byte-identical."""
        cfg, sources = _single_column_job(tmp_path, "date_shift")
        auto, forced = _run_pair(cfg, sources, monkeypatch)
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        a, f = auto.outputs["accounts"], forced.outputs["accounts"]
        assert [str(fl.type) for fl in a.schema] == [str(fl.type) for fl in f.schema]
        assert a.equals(f)

    def _bucketize_job(self, tmp_path, values, provider_config=None):
        df = pd.DataFrame({"val": values})
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(
            tmp_path,
            [
                {
                    "name": "val",
                    "strategy": "bucketize",
                    "provider_config": provider_config or {"width": 2.5, "format": "range"},
                }
            ],
        )
        return cfg, {"accounts": pa.Table.from_pandas(df, preserve_index=False)}

    def test_bucketize_null_in_one_chunk_stays_full_frame(self, tmp_path, monkeypatch):
        """P3 remediation BLOCKER 2: bucketize output dtype depends on chunk
        content when nulls are present (null-bearing chunks fall through to
        the original value), so a null-bearing source fails closed."""
        values: list[Any] = [i * 1.5 for i in range(_ROWS)]
        values[5] = None
        cfg, sources = self._bucketize_job(tmp_path, values)
        auto, forced = _run_pair(cfg, sources, monkeypatch, explain_plan=True)
        _assert_full_frame_and_identical(auto, forced, "accounts", "bucketize")

    def test_bucketize_all_null_tail_chunk_stays_full_frame(self, tmp_path, monkeypatch):
        """The reviewer's all-null tail-chunk counterexample: the tail chunk
        yields a null-typed column that a promoting concat would silently
        widen into a schema the full-frame run never produces."""
        values = [(-1) ** i * (i * 7.3) for i in range(48)] + [None] * 12
        cfg, sources = self._bucketize_job(tmp_path, values)
        auto, forced = _run_pair(cfg, sources, monkeypatch, explain_plan=True)
        _assert_full_frame_and_identical(auto, forced, "accounts", "bucketize")

    def test_bucketize_non_numeric_source_stays_full_frame(self, tmp_path, monkeypatch):
        """Non-numeric bucketize source coerces per chunk (parseable strings
        become numbers, the rest fall through), so it fails closed."""
        values = [f"abc{i}" for i in range(_ROWS)]
        cfg, sources = self._bucketize_job(tmp_path, values, {"width": 10})
        # S1's honesty pack (landed after this fixture was authored) turns
        # the non-numeric bucketize source's mask errors into row errors on
        # the full-frame pass; both routes must decline to chunk this shape
        # and fail identically (see the helper's docstring).
        _assert_row_errors_fail_identically_both_routes(
            cfg, sources, monkeypatch, explain_plan=True
        )

    def test_bucketize_null_free_float_still_routes(self, tmp_path, monkeypatch):
        cfg, sources = self._bucketize_job(tmp_path, [(-1) ** i * (i * 7.3) for i in range(_ROWS)])
        auto, forced = _run_pair(cfg, sources, monkeypatch)
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        a, f = auto.outputs["accounts"], forced.outputs["accounts"]
        assert [str(fl.type) for fl in a.schema] == [str(fl.type) for fl in f.schema]
        assert a.equals(f)

    def test_bucketize_null_free_int_still_routes(self, tmp_path, monkeypatch):
        cfg, sources = _single_column_job(tmp_path, "bucketize")
        auto, forced = _run_pair(cfg, sources, monkeypatch)
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        assert auto.outputs["accounts"].equals(forced.outputs["accounts"])

    def _when_bearing_config(self, tmp_path):
        """`when` is schema-forbidden on ColumnConfig, but run_pipeline does
        not re-validate its dict input and `when` is a shipped engine
        feature (ColumnSeed.when), so an SDK caller can hand one in; the
        gate must see it. Injected post-validation to model that caller."""
        df = pd.DataFrame(
            {
                "val": [f"v{i}" for i in range(_ROWS)],
                "amount": list(range(_ROWS)),
            }
        )
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(
            tmp_path,
            [
                {"name": "val", "strategy": "redact"},
                {"name": "amount", "strategy": "passthrough"},
            ],
        )
        cfg["tables"][0]["columns"][0]["when"] = "amount > 30"
        return cfg, {"accounts": pa.Table.from_pandas(df, preserve_index=False)}

    def test_when_bearing_column_stays_full_frame(self, tmp_path, monkeypatch):
        """A `when` predicate is evaluated per frame; per-chunk evaluation
        is a latent divergence (e.g. index-referencing predicates reset per
        chunk), so any when-bearing column fails the route closed."""
        cfg, sources = self._when_bearing_config(tmp_path)
        auto, forced = _run_pair(cfg, sources, monkeypatch, explain_plan=True)
        _assert_full_frame_and_identical(auto, forced, "accounts", "when")

    def test_when_bearing_column_rejected_by_planner(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg, sources = self._when_bearing_config(tmp_path)
        decision = _classify(cfg, sources)
        assert decision.mode != "chunked"
        assert "when" in decision.rejections["chunked"]


# --------------------------------------------------------------------------
# Strict chunk concatenation (defense-in-depth behind the gates)
# --------------------------------------------------------------------------


class TestStrictChunkConcat:
    def test_diverging_chunk_schemas_raise_coded_error(self):
        """A schema disagreement between masked chunks means an eligibility
        gate admitted a chunk-variant job; that must surface as a coded
        error, never as a silent promotion."""
        from decoy_engine.execution._chunked import concat_masked_chunks

        chunks = [
            pa.table({"val": pa.array(["a", "b"], type=pa.string())}),
            pa.table({"val": pa.array(["c", "d"], type=pa.large_string())}),
        ]
        with pytest.raises(ExecutionError) as exc:
            concat_masked_chunks(chunks, table="accounts")
        assert exc.value.code == "chunked_schema_mismatch"
        assert "val" in str(exc.value)

    def test_all_null_chunk_casts_to_column_type(self):
        """The ONE promotion full-frame inference also performs: an all-null
        chunk's null-typed column takes the type the non-null chunks agree
        on (the full frame contains those non-null values, so its inference
        lands on the same type)."""
        from decoy_engine.execution._chunked import concat_masked_chunks

        chunks = [
            pa.table({"val": pa.array(["a", "b"], type=pa.large_string())}),
            pa.table({"val": pa.array([None, None], type=pa.null())}),
        ]
        out = concat_masked_chunks(chunks, table="accounts")
        assert out.schema.field("val").type == pa.large_string()
        assert out.column("val").to_pylist() == ["a", "b", None, None]

    def test_all_chunks_null_column_stays_null(self):
        from decoy_engine.execution._chunked import concat_masked_chunks

        chunks = [
            pa.table({"val": pa.array([None], type=pa.null())}),
            pa.table({"val": pa.array([None, None], type=pa.null())}),
        ]
        out = concat_masked_chunks(chunks, table="accounts")
        assert out.schema.field("val").type == pa.null()
        assert out.num_rows == 3

    def test_column_name_mismatch_raises_coded_error(self):
        from decoy_engine.execution._chunked import concat_masked_chunks

        chunks = [
            pa.table({"val": pa.array(["a"], type=pa.string())}),
            pa.table({"other": pa.array(["b"], type=pa.string())}),
        ]
        with pytest.raises(ExecutionError) as exc:
            concat_masked_chunks(chunks, table="accounts")
        assert exc.value.code == "chunked_schema_mismatch"


# --------------------------------------------------------------------------
# Routed-result surface: warnings and timings must not be dropped
# --------------------------------------------------------------------------


class TestRoutedResultSurface:
    def test_routed_job_surfaces_chunk_warnings_union_order_stable(self, tmp_path, monkeypatch):
        """Per-chunk QualityWarnings must reach the routed ExecutionResult:
        duplicates collapse (same warning re-emitted per chunk), distinct
        warnings keep first-emission order."""
        import dataclasses

        from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
        from decoy_engine.generation.pool._events import QualityWarning

        cfg, sources = _single_column_job(tmp_path, "hash")
        constant = QualityWarning(code="test_constant", provider="test", column="val")
        counter = {"n": 0}
        real = PandasExecutionAdapter.run

        def injecting_run(self, plan, run_sources, **kwargs):
            result = real(self, plan, run_sources, **kwargs)
            unique = QualityWarning(
                code="test_unique",
                provider="test",
                column="val",
                detail={"chunk": counter["n"]},
            )
            counter["n"] += 1
            return dataclasses.replace(result, warnings=(*result.warnings, constant, unique))

        monkeypatch.setattr(PandasExecutionAdapter, "run", injecting_run)
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        auto = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=_CHUNK,
        )
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        codes = [(w.code, w.detail.get("chunk")) for w in auto.warnings]
        assert codes == [
            ("test_constant", None),
            ("test_unique", 0),
            ("test_unique", 1),
            ("test_unique", 2),
            ("test_unique", 3),
        ]

    def test_routed_job_stamps_timings_and_conversion_ms(self, tmp_path, monkeypatch):
        """The routed path must not silently blank the ExecutionResult
        timing surface: per-column strategy timings and a non-zero boundary
        conversion figure survive routing."""
        cfg, sources = _single_column_job(tmp_path, "hash")
        auto, forced = _run_pair(cfg, sources, monkeypatch)
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        assert auto.boundary_conversion_ms > 0.0
        auto_keys = {(t.strategy_type, t.column) for t in auto.timings}
        forced_keys = {(t.strategy_type, t.column) for t in forced.timings}
        assert auto_keys == forced_keys
        assert auto_keys == {("hash", "val")}


# --------------------------------------------------------------------------
# Kill switch
# --------------------------------------------------------------------------


class TestKillSwitch:
    def test_auto_chunk_false_forces_full_frame_with_reason(self, tmp_path, monkeypatch):
        from decoy_engine.execution import _chunked

        cfg, sources = _single_column_job(tmp_path, "hash")

        def bomb(*args, **kwargs):
            raise AssertionError("chunked entrypoint ran despite auto_chunk=False")

        monkeypatch.setattr(_chunked, "run_mask_pipeline_chunked", bomb)
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        result = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk=False,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
        )
        block = result.quality_metrics["auto_chunk"]
        assert block["mode"] == "full_frame"
        assert "disabled" in block["reason"]


# --------------------------------------------------------------------------
# Vault correctness on the chunked route
# --------------------------------------------------------------------------


class TestVault:
    def _vault_job(self, tmp_path):
        df = pd.DataFrame({"val": [f"user{i}@example.com" for i in range(_ROWS)]})
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(
            tmp_path,
            [{"name": "val", "strategy": "hash", "namespace": "hash_ns", "vault": True}],
        )
        return cfg, {"accounts": pa.Table.from_pandas(df, preserve_index=False)}

    def test_vault_entries_identical_between_chunked_and_full_frame(self, tmp_path, monkeypatch):
        from decoy_engine.vault import vault_writer_for_config

        cfg, sources = self._vault_job(tmp_path)
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        auto_writer = vault_writer_for_config(cfg)
        forced_writer = vault_writer_for_config(cfg)
        auto = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            vault_writer=auto_writer,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=_CHUNK,
        )
        forced = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            vault_writer=forced_writer,
            auto_chunk=False,
        )
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        assert auto.outputs["accounts"].equals(forced.outputs["accounts"])
        # The accumulated (namespace, masked, source) sets are the vault's
        # deterministic content; the encrypted file is IV-random by design.
        assert auto_writer._entries == forced_writer._entries
        assert len(auto_writer._entries) == _ROWS

    def test_vault_file_round_trip_identical(self, tmp_path, monkeypatch):
        pytest.importorskip("cryptography")
        from decoy_engine.plan._seed import _normalize_job_seed
        from decoy_engine.vault import load_vault, vault_writer_for_config

        cfg, sources = self._vault_job(tmp_path)
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        maps = {}
        for label, kwargs in (
            ("auto", {"auto_chunk_threshold_rows": _LOW_THRESHOLD, "chunk_size_rows": _CHUNK}),
            ("forced", {"auto_chunk": False}),
        ):
            writer = vault_writer_for_config(cfg)
            run_pipeline(
                cfg,
                sources=sources,
                engine_version=_ENGINE_VERSION,
                vault_writer=writer,
                **kwargs,
            )
            path = tmp_path / f"{label}.vault"
            writer.write(path)
            maps[label], ambiguous = load_vault(path, _normalize_job_seed(cfg))
            assert ambiguous == 0
        assert maps["auto"] == maps["forced"]
        assert len(maps["auto"]) == _ROWS


# --------------------------------------------------------------------------
# Knob validation (fail-early, typed, before any profiling work)
# --------------------------------------------------------------------------


class TestKnobValidation:
    @pytest.fixture
    def forbid_profiling(self, monkeypatch):
        import decoy_engine.profile as profile_mod

        def bomb(*args, **kwargs):
            raise AssertionError("profile_source ran before knob validation")

        monkeypatch.setattr(profile_mod, "profile_source", bomb)

    @pytest.mark.parametrize(
        ("knob", "value"),
        [
            ("auto_chunk", "false"),
            ("auto_chunk", 1),
            ("chunk_size_rows", 0),
            ("chunk_size_rows", -5),
            ("chunk_size_rows", True),
            ("auto_chunk_threshold_rows", 0),
            ("auto_chunk_threshold_rows", False),
        ],
    )
    def test_invalid_auto_chunk_knob_fails_early_with_typed_error(
        self, tmp_path, monkeypatch, forbid_profiling, knob, value
    ):
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg, sources = _single_column_job(tmp_path, "hash")
        with pytest.raises(ExecutionError) as exc:
            run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION, **{knob: value})
        assert exc.value.code == "invalid_execution_knob"
        assert knob in str(exc.value)
