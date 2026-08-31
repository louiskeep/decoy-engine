"""Phase 4 slice 2: group_key on the chunked route
(`docs/plans/2026-08-31-p4-slice2-group-key-chunked.md`).

`group_key` derives a key from a SIBLING `group_by` column's value, so it is
admitted onto the chunked route through a separate `CHUNK_SIBLING_KEYED_
STRATEGIES` set (not `CHUNK_SAFE_STRATEGIES`, which the FK gate assumes is
value-own-keyed). This module proves the traps the plan-gate named:

1. Byte-identity to the pinned pandas oracle on the real
   `run_pipeline(auto_chunk=True)` route (chunk sizes, mixed strategies,
   FK-adjacent shapes).
2. Trap B: an int+null group_by must ingest losslessly on every route.
3. Trap C: the group_key output column's type is chunk-invariant (string)
   with no `when`.
4. Trap D: `group_key` + `when:` is rejected on the chunked route, both
   entrypoints.
5. Trap E: a group_by column whose EFFECTIVE type is not provably safe
   (floating/decimal cache collisions; a preceding dynamic-output or
   when-gated sibling mask) is rejected, work-order aware and per consumer.
6. `group_key` stays out of `CHUNK_SAFE_STRATEGIES` (FK RI).
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import run_mask_pipeline_chunked, run_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import PolarsExecutionAdapter
from decoy_engine.execution._chunked import check_chunked_compatibility
from decoy_engine.execution._chunked_fk import CHUNK_SAFE_STRATEGIES
from decoy_engine.execution._chunked_group_key import (
    CHUNK_SIBLING_KEYED_STRATEGIES,
    group_by_effective_type,
    group_by_type_is_safe,
    reject_group_key_when,
    reject_unsafe_group_key_group_by_dtype,
    unsafe_group_key_group_by_columns,
)
from decoy_engine.execution._chunked_profile import first_chunk_profile
from decoy_engine.execution._runner import WorkNode, build_work_list, order_work
from decoy_engine.plan import PlanCompileError, compile_plan
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships import RelationshipGraph
from decoy_engine.transforms.group_key import GroupKeyConfig, apply_group_key

_ENGINE_VERSION = "p4-slice2-group-key-test"
_LOW_THRESHOLD = 10
# Fixed seed so job_seed = bytes.fromhex("0102030405060708"), reused by every
# test that pins a literal expected key or builds a direct oracle.
_SEED_INT = 72623859790382856
_JOB_SEED = _SEED_INT.to_bytes(8, "big")


def _validated_dump(cfg: dict) -> dict:
    return PipelineConfig.model_validate(cfg).model_dump()


def _config(
    tmp_path,
    columns: list[dict],
    *,
    table: str = "people",
    relationships: list[dict] | None = None,
    seed: int = _SEED_INT,
    extra_tables: list[dict] | None = None,
) -> dict:
    tables = [{"name": table, "columns": columns}]
    sources = {table: {"type": "file", "format": "csv", "path": str(tmp_path / f"{table}.csv")}}
    targets = {table: {"type": "file", "format": "csv", "path": str(tmp_path / f"{table}_out.csv")}}
    for extra in extra_tables or []:
        tables.append(extra)
        name = extra["name"]
        sources[name] = {"type": "file", "format": "csv", "path": str(tmp_path / f"{name}.csv")}
        targets[name] = {"type": "file", "format": "csv", "path": str(tmp_path / f"{name}_out.csv")}
    cfg: dict = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": sources,
        "tables": tables,
        "targets": targets,
    }
    if relationships:
        cfg["relationships"] = relationships
    return _validated_dump(cfg)


def _pa_chunks(table: pa.Table, size: int) -> list[pa.Table]:
    return [table.slice(i, size) for i in range(0, table.num_rows, size)]


def _write_csv_stub(tmp_path, name: str, table: pa.Table) -> None:
    """Best-effort CSV mirror of `table` so the config's declared source path
    exists; the actual masking data always comes from the `sources=` kwarg,
    never a re-read of this file, so a lossy round-trip here is harmless."""
    try:
        table.to_pandas().to_csv(tmp_path / f"{name}.csv", index=False)
    except Exception:
        pd.DataFrame({c: [] for c in table.column_names}).to_csv(
            tmp_path / f"{name}.csv", index=False
        )


def _compile_test_plan(config: dict, source: pa.Table, *, table: str):
    """The same (profile, compile_plan) pair `run_mask_pipeline_chunked` uses,
    for tests that need a real `Plan` + registry + relationship_graph without
    going through the full chunked entrypoint."""
    profile = first_chunk_profile(source, table=table, engine_version=_ENGINE_VERSION)
    plan = compile_plan(config, profile, decoy_engine_version=_ENGINE_VERSION, no_profile=True)
    registry = get_default_registry()
    graph = RelationshipGraph(edges=(), ordering=())
    return plan, registry, graph


def _scalar_node(
    table: str,
    column: str,
    strategy: str,
    *,
    provider_config: tuple[tuple[str, object], ...] = (),
    when: str | None = None,
) -> WorkNode:
    """A minimal synthetic scalar WorkNode, for direct `group_by_effective_
    type` coverage of `_static_group_by_source_type`'s per-strategy branches
    without paying for a full config-validate + compile_plan round trip per
    case."""
    seed = ColumnSeed(
        namespace=None,
        strategy=strategy,
        provider=None,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=provider_config,
        coherent_with=(),
        when=when,
    )
    return WorkNode(
        table=table,
        columns=(column,),
        kind="scalar",
        strategy=strategy,
        provider=None,
        plan_slice=seed,
    )


# ---------------------------------------------------------------------------
# 1. Byte-identity across chunkings (household coherence survives chunking).
# ---------------------------------------------------------------------------


class TestEndToEndParity:
    @pytest.mark.parametrize("chunk_size", [1, 7, 500])
    def test_chunked_equals_full_frame_across_chunk_sizes(self, tmp_path, chunk_size) -> None:
        n = 60
        household_ids = [f"H-{i % 11}" for i in range(n)]  # repeats across chunk boundaries
        df = pd.DataFrame({"household_id": household_ids, "name": [f"p{i}" for i in range(n)]})
        table = pa.Table.from_pandas(df, preserve_index=False)
        columns = [
            {"name": "household_id", "strategy": "passthrough"},
            {"name": "name", "strategy": "passthrough"},
            {
                "name": "household_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "household_id"},
            },
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "people", table)
        sources = {"people": table}

        auto = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=chunk_size,
        )
        forced = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, auto_chunk=False
        )
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        assert forced.quality_metrics["auto_chunk"]["mode"] == "full_frame"
        a, f = auto.outputs["people"], forced.outputs["people"]
        assert [str(fl.type) for fl in a.schema] == [str(fl.type) for fl in f.schema]
        assert a.equals(f), "chunked group_key output diverged from the full-frame oracle"

        # Household coherence: same group_by value -> identical key, even
        # across a chunk boundary.
        keys_by_household: dict[str, set[str]] = {}
        for hh, key in zip(
            a.column("household_id").to_pylist(),
            a.column("household_key").to_pylist(),
            strict=True,
        ):
            keys_by_household.setdefault(hh, set()).add(key)
        assert all(len(keys) == 1 for keys in keys_by_household.values())


# ---------------------------------------------------------------------------
# 2. Literal pinned expected key (anti self-confirmation).
# ---------------------------------------------------------------------------


class TestLiteralPinnedKey:
    # Generated ONCE via `derive(bytes.fromhex("0102030405060708"),
    # "group_key/household_key", b"H-1001")[:8].hex()`; hardcoded here so the
    # assertion is not a derive(...) call recomputing the same thing.
    _EXPECTED_KEY = "0fd9b889d8553b2d"

    def test_direct_handler_matches_pinned_literal(self) -> None:
        config = GroupKeyConfig.from_dict({"group_by": "household_id"})
        df = pd.DataFrame({"household_id": ["H-1001"]})
        keys = apply_group_key(config, df, seed=_JOB_SEED, namespace="group_key/household_key")
        assert keys == [self._EXPECTED_KEY]

    def test_chunked_and_full_frame_both_equal_the_pinned_literal(self, tmp_path) -> None:
        n = 20
        # Row 5 (mid-stream) carries the pinned value; every other row is a
        # distinct filler value so the pinned row's key is unambiguous.
        household_ids = [f"filler-{i}" for i in range(n)]
        household_ids[5] = "H-1001"
        df = pd.DataFrame({"household_id": household_ids, "name": [f"p{i}" for i in range(n)]})
        table = pa.Table.from_pandas(df, preserve_index=False)
        columns = [
            {"name": "household_id", "strategy": "passthrough"},
            {"name": "name", "strategy": "passthrough"},
            {
                "name": "household_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "household_id"},
            },
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "people", table)
        sources = {"people": table}

        auto = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=6,
        )
        forced = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, auto_chunk=False
        )
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        assert auto.outputs["people"].column("household_key")[5].as_py() == self._EXPECTED_KEY
        assert forced.outputs["people"].column("household_key")[5].as_py() == self._EXPECTED_KEY


# ---------------------------------------------------------------------------
# 3. int+null group_by (Trap B): chunked == full-frame == exact-integer-keyed.
# ---------------------------------------------------------------------------


class TestIntNullGroupBy:
    def test_large_int_with_nulls_at_chunk_boundaries(self, tmp_path) -> None:
        large = 2**60
        household_ids: list[int | None] = [
            large,
            large + 1,
            None,  # chunk 1 (size 3) carries a null
            large + 1,
            large + 2,
            None,  # chunk 2 (size 3) also carries a null
            large + 2,
            large,  # chunk 3 (size 2) is null-free
        ]
        names = [f"p{i}" for i in range(len(household_ids))]
        table = pa.table({"household_id": pa.array(household_ids, type=pa.int64()), "name": names})
        columns = [
            {"name": "household_id", "strategy": "passthrough", "dtype": "int64"},
            {"name": "name", "strategy": "passthrough"},
            {
                "name": "household_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "household_id"},
            },
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "people", table)

        # The auto-chunk planner blanket-rejects ANY int+null column in the
        # whole table as "non-chunk-stable" (`_runtime_source_rejections`,
        # a stricter AUTO-ONLY safety net that does not know about the
        # lossless-ingest protection this slice adds), so this shape is
        # exercised via the MANUAL entrypoint the operator chooses
        # explicitly, matching top_code's own group_by tests.
        chunked = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg, _pa_chunks(table, 3), table="people", engine_version=_ENGINE_VERSION
                )
            )
        ).combine_chunks()
        forced = run_pipeline(
            cfg, sources={"people": table}, engine_version=_ENGINE_VERSION, auto_chunk=False
        ).outputs["people"]
        assert chunked.equals(forced)

        # Exact-integer-keyed oracle: nullable Int64 ingestion (never a bare
        # float64 widen), independent of chunking.
        oracle_cfg = GroupKeyConfig.from_dict({"group_by": "household_id"})
        oracle_df = pd.DataFrame({"household_id": pd.array(household_ids, dtype="Int64")})
        oracle_keys = apply_group_key(
            oracle_cfg, oracle_df, seed=_JOB_SEED, namespace="group_key/household_key"
        )
        assert chunked.column("household_key").to_pylist() == oracle_keys
        # A value >= 2**53 must key on its EXACT string, not a rounded
        # float repr ("1.152921504606847e+18" instead of the real int):
        # re-derive directly from the string the un-widened value produces.
        from decoy_engine.determinism._derive import derive

        expected0 = derive(_JOB_SEED, "group_key/household_key", str(large).encode())[:8].hex()
        assert oracle_keys[0] == expected0


# ---------------------------------------------------------------------------
# 4. Null-sentinel per dtype: chunked == full-frame for each ADMITTED type.
# ---------------------------------------------------------------------------


class TestNullSentinelPerDtype:
    def test_nullable_int_group_by_with_nulls(self, tmp_path) -> None:
        n = 24
        ids: list[int | None] = [i % 5 for i in range(n)]
        for i in range(2, n, 6):
            ids[i] = None
        table = pa.table(
            {"household_id": pa.array(ids, type=pa.int64()), "name": [f"p{i}" for i in range(n)]}
        )
        columns = [
            {"name": "household_id", "strategy": "passthrough"},
            {"name": "name", "strategy": "passthrough"},
            {
                "name": "household_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "household_id"},
            },
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "people", table)
        # Same AUTO-route int+null blanket rejection as TestIntNullGroupBy;
        # the manual entrypoint is the right route for this shape.
        chunked = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg, _pa_chunks(table, 4), table="people", engine_version=_ENGINE_VERSION
                )
            )
        ).combine_chunks()
        forced = run_pipeline(
            cfg, sources={"people": table}, engine_version=_ENGINE_VERSION, auto_chunk=False
        ).outputs["people"]
        assert chunked.equals(forced)

    def test_string_group_by_with_nulls(self, tmp_path) -> None:
        n = 24
        ids: list[str | None] = [f"H-{i % 5}" for i in range(n)]
        for i in range(2, n, 6):
            ids[i] = None
        table = pa.table(
            {"household_id": pa.array(ids, type=pa.string()), "name": [f"p{i}" for i in range(n)]}
        )
        columns = [
            {"name": "household_id", "strategy": "passthrough"},
            {"name": "name", "strategy": "passthrough"},
            {
                "name": "household_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "household_id"},
            },
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "people", table)
        sources = {"people": table}
        auto = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=4,
        )
        forced = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, auto_chunk=False
        )
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        assert auto.outputs["people"].equals(forced.outputs["people"])


# ---------------------------------------------------------------------------
# 5. Cache-collision (Trap E) -- decisive cases.
# ---------------------------------------------------------------------------


class TestCacheCollisionFloatSignedZero:
    def test_collision_is_real_via_direct_handler_baseline(self) -> None:
        """Whole-frame [0.0, 1.0, -0.0] vs chunked [0.0, 1.0] + [-0.0]: the
        cache is per-call, so the -0.0 row's key differs across the split
        (independent of the chunked route entirely)."""
        config = GroupKeyConfig.from_dict({"group_by": "v"})
        ns = "group_key/k"
        whole_df = pd.DataFrame({"v": [0.0, 1.0, -0.0]})
        whole_keys = apply_group_key(config, whole_df, seed=_JOB_SEED, namespace=ns)

        chunk_a = pd.DataFrame({"v": [0.0, 1.0]})
        chunk_b = pd.DataFrame({"v": [-0.0]})
        chunked_keys = apply_group_key(
            config, chunk_a, seed=_JOB_SEED, namespace=ns
        ) + apply_group_key(config, chunk_b, seed=_JOB_SEED, namespace=ns)

        assert whole_keys != chunked_keys
        assert whole_keys[2] != chunked_keys[2]

    def test_auto_route_selects_oracle_chunked_runner_never_invoked(self, tmp_path) -> None:
        n = 20
        values = [0.0 if i % 2 == 0 else -0.0 for i in range(n)]
        table = pa.table(
            {"amount": pa.array(values, type=pa.float64()), "name": [f"p{i}" for i in range(n)]}
        )
        columns = [
            {"name": "amount", "strategy": "passthrough"},
            {"name": "name", "strategy": "passthrough"},
            {
                "name": "amount_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "amount"},
            },
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "people", table)
        sources = {"people": table}

        with mock.patch(
            "decoy_engine.execution._chunked.run_mask_pipeline_chunked",
            wraps=run_mask_pipeline_chunked,
        ) as spy:
            result = run_pipeline(
                cfg,
                sources=sources,
                engine_version=_ENGINE_VERSION,
                auto_chunk_threshold_rows=_LOW_THRESHOLD,
                chunk_size_rows=4,
            )
        assert result.quality_metrics["auto_chunk"]["mode"] == "full_frame"
        spy.assert_not_called()

    def test_manual_entrypoint_raises_coded_error(self, tmp_path) -> None:
        n = 6
        values = [0.0, -0.0, 1.0, 2.0, -0.0, 0.0]
        table = pa.table(
            {"amount": pa.array(values, type=pa.float64()), "name": [f"p{i}" for i in range(n)]}
        )
        columns = [
            {"name": "amount", "strategy": "passthrough"},
            {"name": "name", "strategy": "passthrough"},
            {
                "name": "amount_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "amount"},
            },
        ]
        cfg = _config(tmp_path, columns)
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg,
                    _pa_chunks(table, 3),
                    table="people",
                    engine_version=_ENGINE_VERSION,
                )
            )
        assert exc.value.code == "chunked_group_key_group_by_dtype_unsupported"
        assert "amount_key" in exc.value.message


class TestTextRedactMaskedGroupByRoutesToOracle:
    """End-to-end guard against the Codex final-gate BLOCKER: a float group_by
    masked by a text_redact whose config makes it a passthrough (malformed
    detectors, or a non-string token) keeps the float type, so the auto route
    must fall back to the oracle rather than stream a byte-divergent job."""

    def _cfg(self, tmp_path, text_redact_pc: dict):
        n = 20
        values = [0.0 if i % 2 == 0 else -0.0 for i in range(n)]
        table = pa.table(
            {"amount": pa.array(values, type=pa.float64()), "name": [f"p{i}" for i in range(n)]}
        )
        columns = [
            {"name": "amount", "strategy": "text_redact", "provider_config": text_redact_pc},
            {"name": "name", "strategy": "passthrough"},
            {
                "name": "amount_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "amount"},
            },
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "people", table)
        return cfg, {"people": table}

    @pytest.mark.parametrize(
        "text_redact_pc",
        [
            {"token": "[X]", "detectors": 123},  # malformed detectors (Codex repro)
            {"token": 123},  # non-string token
        ],
    )
    def test_auto_route_falls_back_to_oracle(self, tmp_path, text_redact_pc) -> None:
        cfg, sources = self._cfg(tmp_path, text_redact_pc)
        with mock.patch(
            "decoy_engine.execution._chunked.run_mask_pipeline_chunked",
            wraps=run_mask_pipeline_chunked,
        ) as spy:
            result = run_pipeline(
                cfg,
                sources=sources,
                engine_version=_ENGINE_VERSION,
                auto_chunk_threshold_rows=_LOW_THRESHOLD,
                chunk_size_rows=4,
            )
        assert result.quality_metrics["auto_chunk"]["mode"] == "full_frame"
        spy.assert_not_called()


class TestCacheCollisionDecimalSignedZero:
    def test_collision_is_real_via_direct_handler_baseline(self) -> None:
        config = GroupKeyConfig.from_dict({"group_by": "v"})
        ns = "group_key/k"
        whole_df = pd.DataFrame({"v": [Decimal("0"), Decimal("1"), Decimal("-0")]})
        whole_keys = apply_group_key(config, whole_df, seed=_JOB_SEED, namespace=ns)

        chunk_a = pd.DataFrame({"v": [Decimal("0"), Decimal("1")]})
        chunk_b = pd.DataFrame({"v": [Decimal("-0")]})
        chunked_keys = apply_group_key(
            config, chunk_a, seed=_JOB_SEED, namespace=ns
        ) + apply_group_key(config, chunk_b, seed=_JOB_SEED, namespace=ns)

        assert whole_keys != chunked_keys
        assert whole_keys[2] != chunked_keys[2]

    def test_auto_route_selects_oracle_chunked_runner_never_invoked(self, tmp_path) -> None:
        n = 20
        values = [Decimal("0") if i % 2 == 0 else Decimal("-0") for i in range(n)]
        table = pa.table(
            {
                "amount": pa.array(values, type=pa.decimal128(10, 2)),
                "name": [f"p{i}" for i in range(n)],
            }
        )
        columns = [
            {"name": "amount", "strategy": "passthrough"},
            {"name": "name", "strategy": "passthrough"},
            {
                "name": "amount_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "amount"},
            },
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "people", table)
        sources = {"people": table}

        with mock.patch(
            "decoy_engine.execution._chunked.run_mask_pipeline_chunked",
            wraps=run_mask_pipeline_chunked,
        ) as spy:
            result = run_pipeline(
                cfg,
                sources=sources,
                engine_version=_ENGINE_VERSION,
                auto_chunk_threshold_rows=_LOW_THRESHOLD,
                chunk_size_rows=4,
            )
        assert result.quality_metrics["auto_chunk"]["mode"] == "full_frame"
        spy.assert_not_called()

    def test_manual_entrypoint_raises_coded_error(self, tmp_path) -> None:
        n = 4
        values = [Decimal("0"), Decimal("-0"), Decimal("1"), Decimal("2")]
        table = pa.table(
            {
                "amount": pa.array(values, type=pa.decimal128(10, 2)),
                "name": [f"p{i}" for i in range(n)],
            }
        )
        columns = [
            {"name": "amount", "strategy": "passthrough"},
            {"name": "name", "strategy": "passthrough"},
            {
                "name": "amount_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "amount"},
            },
        ]
        cfg = _config(tmp_path, columns)
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg,
                    _pa_chunks(table, 2),
                    table="people",
                    engine_version=_ENGINE_VERSION,
                )
            )
        assert exc.value.code == "chunked_group_key_group_by_dtype_unsupported"


class TestDictionaryGroupBy:
    """Direct unit coverage (types only): the guard reads Arrow SCHEMA types
    structurally, so a constructed `pa.dictionary(...)` type exercises the
    same code path a real chunked-route dictionary column would. A real
    dictionary-typed source is not built end-to-end here because the
    chunked route's pandas ingestion does not preserve Arrow dictionary
    typing (it round-trips through `to_pandas`), so an end-to-end version
    would not exercise a materially different path than this direct check."""

    def test_int_valued_dictionary_is_safe(self) -> None:
        assert group_by_type_is_safe(pa.dictionary(pa.int32(), pa.int64())) is True

    def test_string_valued_dictionary_is_safe(self) -> None:
        assert group_by_type_is_safe(pa.dictionary(pa.int32(), pa.string())) is True

    def test_float_valued_dictionary_is_unsafe(self) -> None:
        assert group_by_type_is_safe(pa.dictionary(pa.int32(), pa.float64())) is False

    def test_nested_dictionary_recurses(self) -> None:
        safe_nested = pa.dictionary(pa.int32(), pa.dictionary(pa.int32(), pa.string()))
        unsafe_nested = pa.dictionary(pa.int32(), pa.dictionary(pa.int32(), pa.float64()))
        assert group_by_type_is_safe(safe_nested) is True
        assert group_by_type_is_safe(unsafe_nested) is False


class TestOrderingRegressions:
    """Trap E note: the effective type is computed relative to the
    consuming group_key node's OWN position in `order_work`'s output.
    Independent scalar nodes (no FK edges) tie-break on sorted
    `(table, columns)`, so column NAMING controls the order deterministically.
    """

    def _plan_registry_graph(self, tmp_path, columns: list[dict], table_data: pa.Table):
        cfg = _config(tmp_path, columns)
        return _compile_test_plan(cfg, table_data, table="people")

    def test_unsafe_float_source_mask_scheduled_after_is_rejected(self, tmp_path) -> None:
        """group_key column "aaa_group_key" sorts BEFORE "amount", so
        amount's hash-mask (any strategy) is scheduled AFTER the consumer:
        the consumer sees the RAW (unsafe float) source type."""
        table = pa.table({"amount": pa.array([0.0, -0.0, 1.0], type=pa.float64())})
        columns = [
            {"name": "amount", "strategy": "hash", "namespace": "amount_ns"},
            {
                "name": "aaa_group_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "amount"},
            },
        ]
        plan, registry, graph = self._plan_registry_graph(tmp_path, columns, table)
        ordered = order_work(build_work_list(plan, registry), graph)
        keys = [n.key for n in ordered]
        assert keys.index(("people", ("aaa_group_key",))) < keys.index(("people", ("amount",)))

        group_key_node = next(n for n in ordered if n.key == ("people", ("aaa_group_key",)))
        effective = group_by_effective_type(
            ordered, table.schema, table="people", group_key_node=group_key_node
        )
        assert effective is not None
        assert pa.types.is_floating(effective)
        assert group_by_type_is_safe(effective) is False

        offending = unsafe_group_key_group_by_columns(ordered, table.schema, table="people")
        assert offending == ["aaa_group_key"]

    def test_unsafe_float_output_mask_scheduled_before_is_rejected(self, tmp_path) -> None:
        """amount (int64 source, SAFE) sorts before "zzz_group_key", and is
        masked via `redact` with a numeric `redact_with` -- a chunk-safe
        strategy whose STATIC output type is float64 here. The consumer must
        see the MASK's output type, not the safe source type, and reject."""
        table = pa.table({"amount": pa.array([1, 2, 3], type=pa.int64())})
        columns = [
            {
                "name": "amount",
                "strategy": "redact",
                "provider_config": {"redact_with": 0.0},
            },
            {
                "name": "zzz_group_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "amount"},
            },
        ]
        plan, registry, graph = self._plan_registry_graph(tmp_path, columns, table)
        ordered = order_work(build_work_list(plan, registry), graph)
        keys = [n.key for n in ordered]
        assert keys.index(("people", ("amount",))) < keys.index(("people", ("zzz_group_key",)))

        group_key_node = next(n for n in ordered if n.key == ("people", ("zzz_group_key",)))
        effective = group_by_effective_type(
            ordered, table.schema, table="people", group_key_node=group_key_node
        )
        assert effective is not None
        assert pa.types.is_floating(effective)

        offending = unsafe_group_key_group_by_columns(ordered, table.schema, table="people")
        assert offending == ["zzz_group_key"]

    def test_safe_hash_mask_scheduled_before_is_admitted_chunked_equals_full_frame(
        self, tmp_path
    ) -> None:
        n = 40
        ids = [i % 7 for i in range(n)]
        table = pa.table(
            {"amount": pa.array(ids, type=pa.int64()), "junk": [f"j{i}" for i in range(n)]}
        )
        columns = [
            {"name": "amount", "strategy": "hash", "namespace": "amount_ns"},
            {"name": "junk", "strategy": "passthrough"},
            {
                "name": "zzz_group_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "amount"},
            },
        ]
        plan, registry, graph = self._plan_registry_graph(tmp_path, columns, table)
        ordered = order_work(build_work_list(plan, registry), graph)
        keys = [n.key for n in ordered]
        assert keys.index(("people", ("amount",))) < keys.index(("people", ("zzz_group_key",)))
        assert unsafe_group_key_group_by_columns(ordered, table.schema, table="people") == []

        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "people", table)
        sources = {"people": table}
        auto = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=6,
        )
        forced = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, auto_chunk=False
        )
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        assert auto.outputs["people"].equals(forced.outputs["people"])
        # The masked (hashed) group_by value drove the key, not the raw int:
        # every row's group_key output must differ from a same-input oracle
        # keyed on the RAW int (proving the mask ran first).
        raw_oracle_cfg = GroupKeyConfig.from_dict({"group_by": "amount"})
        raw_oracle = apply_group_key(
            raw_oracle_cfg,
            pd.DataFrame({"amount": ids}),
            seed=_JOB_SEED,
            namespace="group_key/zzz_group_key",
        )
        assert auto.outputs["people"].column("zzz_group_key").to_pylist() != raw_oracle

    def test_multi_consumer_independent_effective_types(self, tmp_path) -> None:
        """Two group_key columns share one group_by ("amount", float64
        source): "aaa_group_key" sorts BEFORE amount's hash mask (sees the
        unsafe float source); "zzz_group_key" sorts AFTER it (sees the safe
        hashed-string output). Each consumer's judgment is independent."""
        table = pa.table({"amount": pa.array([1.5, 2.5, -0.0], type=pa.float64())})
        columns = [
            {"name": "amount", "strategy": "hash", "namespace": "amount_ns"},
            {
                "name": "aaa_group_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "amount"},
            },
            {
                "name": "zzz_group_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "amount"},
            },
        ]
        plan, registry, graph = self._plan_registry_graph(tmp_path, columns, table)
        ordered = order_work(build_work_list(plan, registry), graph)
        keys = [n.key for n in ordered]
        assert (
            keys.index(("people", ("aaa_group_key",)))
            < keys.index(("people", ("amount",)))
            < keys.index(("people", ("zzz_group_key",)))
        )

        aaa_node = next(n for n in ordered if n.key == ("people", ("aaa_group_key",)))
        zzz_node = next(n for n in ordered if n.key == ("people", ("zzz_group_key",)))
        aaa_type = group_by_effective_type(
            ordered, table.schema, table="people", group_key_node=aaa_node
        )
        zzz_type = group_by_effective_type(
            ordered, table.schema, table="people", group_key_node=zzz_node
        )
        assert aaa_type is not None and pa.types.is_floating(aaa_type)
        assert group_by_type_is_safe(aaa_type) is False
        assert zzz_type is not None and pa.types.is_string(zzz_type)
        assert group_by_type_is_safe(zzz_type) is True

        offending = unsafe_group_key_group_by_columns(ordered, table.schema, table="people")
        assert offending == ["aaa_group_key"]

    def test_preceding_when_gated_sibling_mask_is_rejected(self, tmp_path) -> None:
        table = pa.table({"amount": pa.array([1, 2, 3], type=pa.int64())})
        columns = [
            {"name": "amount", "strategy": "hash", "namespace": "amount_ns"},
            {
                "name": "zzz_group_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "amount"},
            },
        ]
        # `when` is schema-forbidden on ColumnConfig (pydantic), but neither
        # `check_chunked_compatibility` nor `run_mask_pipeline_chunked`
        # re-validates its dict input and `when` is a shipped engine field
        # (`ColumnSeed.when`), so an SDK caller can hand one in; the gate
        # must see it. Injected post-validation, mirroring the DGRN test's
        # `_when_bearing_windowed_date_cfg`.
        cfg = _config(tmp_path, columns)
        cfg["tables"][0]["columns"][0]["when"] = "amount > 0"

        plan, registry, graph = _compile_test_plan(cfg, table, table="people")
        ordered = order_work(build_work_list(plan, registry), graph)
        keys = [n.key for n in ordered]
        assert keys.index(("people", ("amount",))) < keys.index(("people", ("zzz_group_key",)))

        group_key_node = next(n for n in ordered if n.key == ("people", ("zzz_group_key",)))
        effective = group_by_effective_type(
            ordered, table.schema, table="people", group_key_node=group_key_node
        )
        assert effective is None

        with pytest.raises(PlanCompileError) as exc:
            reject_unsafe_group_key_group_by_dtype(
                plan, table.schema, table="people", registry=registry, relationship_graph=graph
            )
        assert exc.value.code == "chunked_group_key_group_by_dtype_unsupported"

        with pytest.raises(PlanCompileError) as exc2:
            list(
                run_mask_pipeline_chunked(
                    cfg, _pa_chunks(table, 2), table="people", engine_version=_ENGINE_VERSION
                )
            )
        assert exc2.value.code == "chunked_group_key_group_by_dtype_unsupported"


class TestDirectUnitCoverage:
    """Direct unit tests of `group_by_type_is_safe` / `group_by_effective_type`."""

    @pytest.mark.parametrize(
        ("arrow_type", "expected"),
        [
            (pa.int64(), True),
            (pa.int32(), True),
            (pa.bool_(), True),
            (pa.string(), True),
            (pa.large_string(), True),
            (pa.date32(), True),
            (pa.timestamp("us"), True),
            (pa.float64(), False),
            (pa.float32(), False),
            (pa.decimal128(10, 2), False),
            (pa.binary(), False),
            (pa.list_(pa.int64()), False),
        ],
    )
    def test_group_by_type_is_safe_matrix(self, arrow_type, expected) -> None:
        assert group_by_type_is_safe(arrow_type) is expected

    def test_effective_type_returns_source_when_no_preceding_mask(self, tmp_path) -> None:
        table = pa.table({"amount": pa.array([1, 2, 3], type=pa.int64())})
        columns = [
            {"name": "amount", "strategy": "passthrough"},
            {
                "name": "only_group_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "amount"},
            },
        ]
        cfg = _config(tmp_path, columns)
        plan, registry, graph = _compile_test_plan(cfg, table, table="people")
        ordered = order_work(build_work_list(plan, registry), graph)
        node = next(n for n in ordered if n.key == ("people", ("only_group_key",)))
        effective = group_by_effective_type(
            ordered, table.schema, table="people", group_key_node=node
        )
        assert effective == pa.int64()

    def test_effective_type_returns_mask_output_type_when_ordered_before(self, tmp_path) -> None:
        table = pa.table({"amount": pa.array([1, 2, 3], type=pa.int64())})
        columns = [
            {"name": "amount", "strategy": "hash", "namespace": "amount_ns"},
            {
                "name": "zzz_group_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "amount"},
            },
        ]
        cfg = _config(tmp_path, columns)
        plan, registry, graph = _compile_test_plan(cfg, table, table="people")
        ordered = order_work(build_work_list(plan, registry), graph)
        node = next(n for n in ordered if n.key == ("people", ("zzz_group_key",)))
        effective = group_by_effective_type(
            ordered, table.schema, table="people", group_key_node=node
        )
        assert effective == pa.string()

    def test_reject_unsafe_group_by_dtype_returns_on_safe(self, tmp_path) -> None:
        # The happy path of the raising wrapper (guard direction): a safe
        # (int) group_by effective type must NOT raise. Covered directly here
        # so it does not depend on the slow full-pipeline parity tests.
        table = pa.table({"amount": pa.array([1, 2, 3], type=pa.int64())})
        columns = [
            {"name": "amount", "strategy": "passthrough"},
            {
                "name": "gk",
                "strategy": "group_key",
                "provider_config": {"group_by": "amount"},
            },
        ]
        cfg = _config(tmp_path, columns)
        plan, registry, graph = _compile_test_plan(cfg, table, table="people")
        # Must simply return (no raise) for an admissible config.
        reject_unsafe_group_key_group_by_dtype(
            plan, table.schema, table="people", registry=registry, relationship_graph=graph
        )

    def test_reject_group_key_when_returns_without_when(self, tmp_path) -> None:
        # The happy path of the when gate: a group_key column WITHOUT a `when`
        # predicate must NOT raise. Direct (fast) coverage of the guard.
        columns = [
            {
                "name": "gk",
                "strategy": "group_key",
                "provider_config": {"group_by": "amount"},
            },
        ]
        cfg = _config(tmp_path, columns)
        reject_group_key_when(cfg["tables"][0], table="people")


class TestEffectiveTypeUnitBranches:
    """Fast direct coverage of `group_by_effective_type`'s work-order branches
    (built from synthetic WorkNodes so no full pipeline run is needed)."""

    def _gk_node(self, table: str = "people", group_by: str = "amount"):
        return _scalar_node(table, "gk", "group_key", provider_config=(("group_by", group_by),))

    def test_same_table_mask_found_past_an_other_table_node(self) -> None:
        # An other-table node ordered BEFORE the group_by mask must be skipped
        # (continue), not stop the search (break): the group_by mask still
        # counts, so the effective type is the mask output (string), not source.
        gk = self._gk_node()
        ordered = [
            _scalar_node("orders", "total", "hash"),  # other table, must be skipped
            _scalar_node("people", "amount", "hash"),  # the group_by mask, ordered before gk
            gk,
        ]
        schema = pa.schema([("amount", pa.int64())])
        eff = group_by_effective_type(ordered, schema, table="people", group_key_node=gk)
        assert eff == pa.string()

    def test_a_different_column_scalar_before_gk_is_not_the_mask(self) -> None:
        # A scalar node on a DIFFERENT column must not be mistaken for the
        # group_by mask (guards the `kind == scalar AND key == group_by_key`).
        gk = self._gk_node()
        ordered = [
            _scalar_node("people", "other", "redact"),  # scalar, but not group_by
            gk,
        ]
        schema = pa.schema([("amount", pa.int64())])
        eff = group_by_effective_type(ordered, schema, table="people", group_key_node=gk)
        assert eff == pa.int64()  # group_by unmasked -> source type

    def test_when_gated_preceding_mask_is_unsafe(self) -> None:
        # A preceding group_by mask carrying a `when` predicate -> mixed
        # effective domain -> None (unprovable).
        gk = self._gk_node()
        ordered = [
            _scalar_node("people", "amount", "hash", when="amount > 0"),
            gk,
        ]
        schema = pa.schema([("amount", pa.int64())])
        eff = group_by_effective_type(ordered, schema, table="people", group_key_node=gk)
        assert eff is None

    def test_composite_bundle_over_group_by_is_unsafe(self) -> None:
        # A composite/bundle node writing the group_by column -> not a single
        # static type -> None (fail closed).
        gk = self._gk_node()
        composite = WorkNode(
            table="people",
            columns=("amount", "city"),
            kind="composite",
            strategy="composite",
            provider=None,
            plan_slice=None,
        )
        ordered = [composite, gk]
        schema = pa.schema([("amount", pa.int64())])
        eff = group_by_effective_type(ordered, schema, table="people", group_key_node=gk)
        assert eff is None

    def test_composite_not_over_group_by_does_not_trigger_none(self) -> None:
        # A composite node that does NOT write the group_by column must not
        # force None (guards the `kind in composite AND group_by in columns`).
        gk = self._gk_node()
        composite = WorkNode(
            table="people",
            columns=("city", "zip"),  # not the group_by column
            kind="composite",
            strategy="composite",
            provider=None,
            plan_slice=None,
        )
        ordered = [composite, gk]
        schema = pa.schema([("amount", pa.int64())])
        eff = group_by_effective_type(ordered, schema, table="people", group_key_node=gk)
        assert eff == pa.int64()  # group_by unmasked -> source type

    def test_composite_fk_group_over_group_by_is_unsafe(self) -> None:
        # The composite_fk_group kind is also caught (guards that exact kind
        # string).
        gk = self._gk_node()
        composite = WorkNode(
            table="people",
            columns=("amount", "region"),
            kind="composite_fk_group",
            strategy="composite_fk_group",
            provider=None,
            plan_slice=None,
        )
        ordered = [composite, gk]
        schema = pa.schema([("amount", pa.int64())])
        eff = group_by_effective_type(ordered, schema, table="people", group_key_node=gk)
        assert eff is None

    def test_text_redact_non_string_token_keeps_source_type(self) -> None:
        # A text_redact mask with a NON-STRING token is a no-op passthrough
        # that keeps the source value/type, so a float64 group_by stays float64
        # (unsafe) -- it must NOT be treated as always-string. Guards the real
        # parity hole a "text_redact -> string" shortcut would open.
        gk = self._gk_node()
        ordered = [
            _scalar_node("people", "amount", "text_redact", provider_config=(("token", 123),)),
            gk,
        ]
        schema = pa.schema([("amount", pa.float64())])
        eff = group_by_effective_type(ordered, schema, table="people", group_key_node=gk)
        assert eff == pa.float64()
        # ...and the collector therefore rejects it (float is unsafe).
        assert unsafe_group_key_group_by_columns(ordered, schema, table="people") == ["gk"]

    def test_text_redact_string_token_is_string(self) -> None:
        # With a string token AND well-formed detectors, text_redact runs and
        # stringifies every cell -> string output.
        gk = self._gk_node()
        ordered = [
            _scalar_node(
                "people",
                "amount",
                "text_redact",
                provider_config=(("token", "[X]"), ("detectors", ("email",))),
            ),
            gk,
        ]
        schema = pa.schema([("amount", pa.float64())])
        eff = group_by_effective_type(ordered, schema, table="people", group_key_node=gk)
        assert eff == pa.string()

    def test_text_redact_malformed_detectors_keeps_source_type(self) -> None:
        # A malformed `detectors` (not None / list / tuple) is text_redact's
        # SECOND passthrough condition: the handler returns the frame unchanged,
        # so a float64 group_by stays float64 (unsafe). A string token alone
        # does not make it safe. (Codex final-gate BLOCKER repro.)
        gk = self._gk_node()
        ordered = [
            _scalar_node(
                "people",
                "amount",
                "text_redact",
                provider_config=(("token", "[X]"), ("detectors", 123)),
            ),
            gk,
        ]
        schema = pa.schema([("amount", pa.float64())])
        eff = group_by_effective_type(ordered, schema, table="people", group_key_node=gk)
        assert eff == pa.float64()
        assert unsafe_group_key_group_by_columns(ordered, schema, table="people") == ["gk"]


class TestUnsafeColumnsUnitBranches:
    """Fast direct coverage of `unsafe_group_key_group_by_columns`."""

    def _gk_node(self, column: str, group_by: str, table: str = "people"):
        return _scalar_node(table, column, "group_key", provider_config=(("group_by", group_by),))

    def test_unsafe_float_group_by_reports_the_column_name(self) -> None:
        # A float group_by is unsafe; the offending list carries the exact
        # group_key COLUMN name (guards columns[0], not None / columns[1]).
        gk = self._gk_node("gk_col", "amount")
        ordered = [gk]
        schema = pa.schema([("amount", pa.float64())])
        assert unsafe_group_key_group_by_columns(ordered, schema, table="people") == ["gk_col"]

    def test_safe_int_group_by_reports_nothing(self) -> None:
        gk = self._gk_node("gk_col", "amount")
        ordered = [gk]
        schema = pa.schema([("amount", pa.int64())])
        assert unsafe_group_key_group_by_columns(ordered, schema, table="people") == []

    def test_dynamic_effective_none_is_unsafe(self) -> None:
        # A preceding bucketize mask on the group_by yields a dynamic (None)
        # effective type, which must be treated as unsafe (guards the
        # `effective is None or not safe` OR).
        gk = self._gk_node("gk_col", "amount")
        ordered = [
            _scalar_node("people", "amount", "bucketize"),  # dynamic output -> None
            gk,
        ]
        schema = pa.schema([("amount", pa.int64())])
        assert unsafe_group_key_group_by_columns(ordered, schema, table="people") == ["gk_col"]

    def test_only_group_key_nodes_on_this_table_are_considered(self) -> None:
        # Nodes that are not group_key, or on another table, are skipped
        # (guards the table/kind/strategy skip predicate).
        gk = self._gk_node("gk_col", "amount")
        ordered = [
            _scalar_node("people", "amount", "hash"),  # not group_key
            _scalar_node("orders", "ogk", "group_key", provider_config=(("group_by", "x"),)),
            gk,
        ]
        schema = pa.schema([("amount", pa.int64())])
        # only the same-table safe group_key -> nothing offending
        assert unsafe_group_key_group_by_columns(ordered, schema, table="people") == []


class TestStaticOutputTypeMapCoverage:
    """Direct coverage of `_static_group_by_source_type`'s per-strategy
    branches (via the public `group_by_effective_type`), using synthetic
    WorkNodes so each strategy shape is a one-line fixture rather than a
    full config-validate + compile_plan round trip. Not itself in the
    plan's named 100%-grade list, but load-bearing for Trap E, so covered
    for real regardless of the formal mutation-grading target."""

    _SCHEMA_INT = pa.schema([("amount", pa.int64())])
    _SCHEMA_STR = pa.schema([("amount", pa.string())])

    def _effective(self, mask_node: WorkNode, schema: pa.Schema) -> pa.DataType | None:
        group_key_node = _scalar_node(
            "people", "gk", "group_key", provider_config=(("group_by", "amount"),)
        )
        ordered = [mask_node, group_key_node]
        return group_by_effective_type(
            ordered, schema, table="people", group_key_node=group_key_node
        )

    @pytest.mark.parametrize("strategy", ["fpe", "truncate", "text_redact"])
    def test_string_output_strategies(self, strategy) -> None:
        mask = _scalar_node("people", "amount", strategy)
        assert self._effective(mask, self._SCHEMA_STR) == pa.string()

    def test_redact_with_string_constant(self) -> None:
        mask = _scalar_node("people", "amount", "redact", provider_config=(("redact_with", "X"),))
        assert self._effective(mask, self._SCHEMA_STR) == pa.string()

    def test_redact_default_constant_is_string(self) -> None:
        mask = _scalar_node("people", "amount", "redact")
        assert self._effective(mask, self._SCHEMA_STR) == pa.string()

    def test_categorical_string_categories_is_safe(self) -> None:
        mask = _scalar_node(
            "people", "amount", "categorical", provider_config=(("categories", ["A", "B"]),)
        )
        effective = self._effective(mask, self._SCHEMA_INT)
        assert effective == pa.string()
        assert group_by_type_is_safe(effective) is True

    def test_categorical_numeric_categories_infer_numeric_type(self) -> None:
        # categorical returns the categories' inferred type, not always-string:
        # float categories -> float type -> unsafe (rejected), so a numeric
        # category set cannot admit a divergent job.
        mask = _scalar_node(
            "people", "amount", "categorical", provider_config=(("categories", [1.5, 2.5]),)
        )
        effective = self._effective(mask, self._SCHEMA_INT)
        assert pa.types.is_floating(effective)
        assert group_by_type_is_safe(effective) is False

    def test_categorical_no_categories_is_dynamic(self) -> None:
        mask = _scalar_node("people", "amount", "categorical")
        assert self._effective(mask, self._SCHEMA_INT) is None

    def test_date_shift_string_source_is_safe_string(self) -> None:
        mask = _scalar_node("people", "amount", "date_shift")
        assert self._effective(mask, self._SCHEMA_STR) == pa.string()

    def test_date_shift_non_string_source_is_dynamic(self) -> None:
        mask = _scalar_node("people", "amount", "date_shift")
        assert self._effective(mask, self._SCHEMA_INT) is None

    def test_windowed_date_is_safe_string(self) -> None:
        mask = _scalar_node("people", "amount", "windowed_date")
        assert self._effective(mask, self._SCHEMA_STR) == pa.string()

    def test_group_key_as_preceding_mask_is_safe_string(self) -> None:
        """A group_key column can itself be a preceding sibling mask for
        another group_key's group_by (a group_key chain)."""
        mask = _scalar_node(
            "people", "amount", "group_key", provider_config=(("group_by", "other"),)
        )
        assert self._effective(mask, self._SCHEMA_STR) == pa.string()

    @pytest.mark.parametrize("strategy", ["bucketize", "top_code", "faker"])
    def test_content_dependent_strategies_are_dynamic(self, strategy) -> None:
        mask = _scalar_node("people", "amount", strategy)
        assert self._effective(mask, self._SCHEMA_INT) is None

    @pytest.mark.parametrize("strategy", ["formula", "derived", "nested"])
    def test_unreachable_dynamic_strategies_are_dynamic(self, strategy) -> None:
        """formula/derived/nested can never reach this guard through the
        real chunked pipeline (check_chunked_compatibility's general
        strategy-admission loop rejects them first), but the map must
        still classify them DYNAMIC if it is ever asked -- defensive
        coverage, not a reachable route."""
        mask = _scalar_node("people", "amount", strategy)
        assert self._effective(mask, self._SCHEMA_INT) is None


# ---------------------------------------------------------------------------
# 6. FK exclusion + group_by-as-FK-key (Trap A).
# ---------------------------------------------------------------------------


class TestFkExclusion:
    def test_group_key_not_in_chunk_safe_strategies(self) -> None:
        assert "group_key" not in CHUNK_SAFE_STRATEGIES
        assert "group_key" in CHUNK_SIBLING_KEYED_STRATEGIES
        assert CHUNK_SAFE_STRATEGIES.isdisjoint(CHUNK_SIBLING_KEYED_STRATEGIES)

    def test_group_key_as_fk_parent_key_rejected(self, tmp_path) -> None:
        """A group_key column used AS an FK key (single-column edge, REMAP,
        so no other gate condition masks the intended rejection) must be
        rejected by the FK-self-mask gate, not silently admitted."""
        cfg = {
            "global_settings": {"seed": 7},
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {"name": "anchor", "strategy": "passthrough"},
                        {
                            "name": "id",
                            "strategy": "group_key",
                            "dtype": "string",
                            "provider_config": {"group_by": "anchor"},
                        },
                    ],
                },
                {
                    "name": "orders",
                    "columns": [
                        {"name": "anchor", "strategy": "passthrough"},
                        {
                            "name": "customer_id",
                            "strategy": "group_key",
                            "dtype": "string",
                            "provider_config": {"group_by": "anchor"},
                        },
                    ],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    "orphan_policy": "remap",
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="orders")
        assert exc.value.code == "chunked_fk_parent_strategy_not_safe"

    def test_group_by_sibling_is_fk_key_chunked_equals_full_frame(self, tmp_path) -> None:
        """A DIFFERENT case: the group_by column itself (not the group_key
        column) is an FK key, masked normally (hash). The manual entrypoint
        admits "customers" (an FK PARENT, not child, so
        `gate_fk_child_edges` does not constrain it), and the FK column
        ingests losslessly and group_key derives from it consistently."""
        n = 30
        household_ids = [i % 6 for i in range(n)]
        table = pa.table(
            {
                "household_id": pa.array(household_ids, type=pa.int64()),
                "name": [f"p{i}" for i in range(n)],
            }
        )
        columns = [
            {
                "name": "household_id",
                "strategy": "hash",
                "namespace": "hh_ns",
                "dtype": "int64",
            },
            {"name": "name", "strategy": "passthrough"},
            {
                "name": "household_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "household_id"},
            },
        ]
        relationships = [
            {
                "parent": {"table": "customers", "columns": ["household_id"]},
                "children": [{"table": "orders", "columns": ["household_id"]}],
                "orphan_policy": "remap",
            }
        ]
        cfg_with_fk = _config(
            tmp_path,
            columns,
            table="customers",
            relationships=relationships,
            extra_tables=[
                {
                    "name": "orders",
                    "columns": [
                        {
                            "name": "household_id",
                            "strategy": "hash",
                            "namespace": "hh_ns",
                            "dtype": "int64",
                        }
                    ],
                }
            ],
        )
        # check_chunked_compatibility admits "customers": it is an FK PARENT
        # only, which `gate_fk_child_edges` does not constrain.
        chunked = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg_with_fk,
                    _pa_chunks(table, 7),
                    table="customers",
                    engine_version=_ENGINE_VERSION,
                )
            )
        ).combine_chunks()

        # Oracle: the SAME columns on a config with NO relationships block
        # at all (hash/group_key do not read relationship state, so the
        # masked output of "customers" must be identical either way).
        cfg_no_fk = _config(tmp_path, columns, table="customers")
        _write_csv_stub(tmp_path, "customers", table)
        forced = run_pipeline(
            cfg_no_fk,
            sources={"customers": table},
            engine_version=_ENGINE_VERSION,
            auto_chunk=False,
        ).outputs["customers"]
        assert chunked.equals(forced)


# ---------------------------------------------------------------------------
# 7. group_by masked BEFORE group_key (the masked-group_by common case).
# ---------------------------------------------------------------------------


class TestGroupByMaskedBeforeGroupKey:
    def test_sibling_mask_runs_first_key_derives_from_masked_value(self, tmp_path) -> None:
        n = 30
        household_ids = [i % 6 for i in range(n)]
        table = pa.table(
            {
                "household_id": pa.array(household_ids, type=pa.int64()),
                "junk": [f"j{i}" for i in range(n)],
            }
        )
        columns = [
            {"name": "household_id", "strategy": "hash", "namespace": "hh_ns"},
            {"name": "junk", "strategy": "passthrough"},
            {
                "name": "zzz_household_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "household_id"},
            },
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "people", table)
        sources = {"people": table}
        auto = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=7,
        )
        forced = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, auto_chunk=False
        )
        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
        assert auto.outputs["people"].equals(forced.outputs["people"])

        # The derived key must come from the MASKED (hashed) household_id,
        # not the raw int: an oracle keyed on the raw int must differ.
        raw_oracle_cfg = GroupKeyConfig.from_dict({"group_by": "household_id"})
        raw_oracle = apply_group_key(
            raw_oracle_cfg,
            pd.DataFrame({"household_id": household_ids}),
            seed=_JOB_SEED,
            namespace="group_key/zzz_household_key",
        )
        actual = auto.outputs["people"].column("zzz_household_key").to_pylist()
        assert actual != raw_oracle


# ---------------------------------------------------------------------------
# 8. `when`-gated group_key is REJECTED (Trap D).
# ---------------------------------------------------------------------------


def _when_bearing_group_key_cfg(tmp_path, table_data: pa.Table) -> dict:
    columns = [
        {"name": "household_id", "strategy": "passthrough"},
        {
            "name": "household_key",
            "strategy": "group_key",
            "provider_config": {"group_by": "household_id"},
        },
    ]
    cfg = _config(tmp_path, columns)
    cfg["tables"][0]["columns"][1]["when"] = "household_id != ''"
    return cfg


class TestWhenRejection:
    def test_manual_entrypoint_raises_coded_error(self, tmp_path) -> None:
        table = pa.table({"household_id": pa.array(["H-1", "H-2", "H-1"], type=pa.string())})
        cfg = _when_bearing_group_key_cfg(tmp_path, table)
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg, _pa_chunks(table, 2), table="people", engine_version=_ENGINE_VERSION
                )
            )
        assert exc.value.code == "chunked_group_key_when_not_supported"

    def test_check_chunked_compatibility_raises_directly(self, tmp_path) -> None:
        table = pa.table({"household_id": pa.array(["H-1", "H-2"], type=pa.string())})
        cfg = _when_bearing_group_key_cfg(tmp_path, table)
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="people")
        assert exc.value.code == "chunked_group_key_when_not_supported"
        assert "household_key" in exc.value.message

    def test_when_gate_reject_function_directly(self, tmp_path) -> None:
        table = pa.table({"household_id": pa.array(["H-1"], type=pa.string())})
        cfg = _when_bearing_group_key_cfg(tmp_path, table)
        table_cfg = cfg["tables"][0]
        with pytest.raises(PlanCompileError) as exc:
            reject_group_key_when(table_cfg, table="people")
        assert exc.value.code == "chunked_group_key_when_not_supported"

    def test_non_string_target_would_mix_dtypes_documenting_the_rejection(self) -> None:
        """Documents WHY the rejection exists: on a non-string target column,
        a when-gated group_key would leave non-matching rows at their
        ORIGINAL (non-string) value while matching rows become the group_key
        STRING -- a mixed-type column no single Arrow dtype represents,
        which is exactly what `run_with_when_gate`'s `df.loc[mask, column] =
        sub_df[column]` produces (see `_when_gate.py`)."""
        df = pd.DataFrame({"household_id": ["H-1", "H-2"], "target": [1, 2]})
        mask = pd.Series([True, False])
        config = GroupKeyConfig.from_dict({"group_by": "household_id"})
        sub = df.loc[mask].copy()
        keys = apply_group_key(config, sub, seed=_JOB_SEED, namespace="group_key/target")
        sub["target"] = keys
        df.loc[mask, "target"] = sub["target"]
        # Row 0 is now a hex string; row 1 is still the original int -- two
        # Python types coexist in one pandas object column.
        assert isinstance(df["target"].iloc[0], str)
        assert isinstance(df["target"].iloc[1], int)

    def test_auto_route_rejects_via_the_when_planner_gate(self, tmp_path) -> None:
        table = pa.table(
            {"household_id": pa.array([f"H-{i % 5}" for i in range(30)], type=pa.string())}
        )
        cfg = _when_bearing_group_key_cfg(tmp_path, table)
        _write_csv_stub(tmp_path, "people", table)
        result = run_pipeline(
            cfg,
            sources={"people": table},
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=7,
        )
        assert result.quality_metrics["auto_chunk"]["mode"] == "full_frame"


# ---------------------------------------------------------------------------
# 9. Cross-substrate (polars).
# ---------------------------------------------------------------------------


class TestCrossSubstrate:
    @pytest.mark.parametrize("chunk_size", [1, 6, 40])
    def test_polars_chunked_value_equals_pandas_full_frame(self, tmp_path, chunk_size) -> None:
        n = 37
        household_ids = [f"H-{i % 9}" for i in range(n)]
        table = pa.table({"household_id": pa.array(household_ids, type=pa.string())})
        columns = [
            {"name": "household_id", "strategy": "passthrough"},
            {
                "name": "household_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "household_id"},
            },
        ]
        cfg = _config(tmp_path, columns)
        _write_csv_stub(tmp_path, "people", table)

        full = run_pipeline(cfg, sources={"people": table}, engine_version=_ENGINE_VERSION).outputs[
            "people"
        ]

        polars_chunked = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg,
                    _pa_chunks(table, chunk_size),
                    table="people",
                    engine_version=_ENGINE_VERSION,
                    adapter=PolarsExecutionAdapter(),
                )
            )
        ).combine_chunks()

        assert polars_chunked.column_names == full.column_names
        assert polars_chunked.to_pydict() == full.to_pydict()


# ---------------------------------------------------------------------------
# 10. Output dtype invariance, no-`when` (Trap C).
# ---------------------------------------------------------------------------


class TestOutputDtypeInvariance:
    def test_all_null_chunk_vs_mixed_chunk_no_schema_mismatch(self, tmp_path) -> None:
        from decoy_engine.execution._chunked import concat_masked_chunks

        household_ids: list[int | None] = [None, None, None, 1, 2, 3, None, 4]
        table = pa.table({"household_id": pa.array(household_ids, type=pa.int64())})
        columns = [
            {"name": "household_id", "strategy": "passthrough"},
            {
                "name": "household_key",
                "strategy": "group_key",
                "provider_config": {"group_by": "household_id"},
            },
        ]
        cfg = _config(tmp_path, columns)
        chunks = list(
            run_mask_pipeline_chunked(
                cfg, _pa_chunks(table, 3), table="people", engine_version=_ENGINE_VERSION
            )
        )
        assert len(chunks) == 3
        for chunk in chunks:
            assert chunk.schema.field("household_key").type == pa.string()
        # concat_masked_chunks must not raise chunked_schema_mismatch: every
        # chunk (including the fully-null first one) agrees on string type.
        combined = concat_masked_chunks(chunks, table="people")
        assert combined.schema.field("household_key").type == pa.string()
        # Every null household_id row -- across all three chunks -- derives
        # the SAME key (the nullable Int64 ingestion stringifies every null
        # identically, regardless of which chunk it landed in).
        null_keys = {
            key
            for hh, key in zip(
                combined.column("household_id").to_pylist(),
                combined.column("household_key").to_pylist(),
                strict=True,
            )
            if hh is None
        }
        assert len(null_keys) == 1
