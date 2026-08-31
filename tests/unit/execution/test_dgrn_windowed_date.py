"""Phase 4 slice 1: durable global row offset (DGRN) -> windowed_date on the
chunked route (`docs/plans/2026-08-31-p4-slice1-dgrn-windowed-date.md`).

`windowed_date` is the first strategy admitted onto the chunked route by
consuming `base_row_offset` instead of being value-keyed. The hard rule
(binding, see the plan doc): the chunked stream MUST be byte-identical to the
full-frame pandas oracle. This module proves that plus the four correctness
traps the plan-gate caught:

1. `windowed_date` + `when:` is inadmissible (filtered enumeration).
2. `windowed_date` must stay OUT of `CHUNK_SAFE_STRATEGIES` (FK-child RI).
3. A null anchor raises the same `ValueError` on both routes and produces
   no `ExecutionResult`.
4. The DGRN domain (`0 <= i <= 2**64-1`) is guarded with a coded error, both
   at entry (`base_row_offset`) and per chunk.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import ExecutionError, run_mask_pipeline_chunked, run_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.determinism._derive import derive
from decoy_engine.execution import PolarsExecutionAdapter
from decoy_engine.execution._chunked import check_chunked_compatibility
from decoy_engine.execution._chunked_dgrn import (
    CHUNK_DGRN_STRATEGIES,
    ROW_OFFSET_DOMAIN_MAX,
    validate_base_row_offset,
    validate_chunk_row_offset_range,
)
from decoy_engine.execution._chunked_fk import CHUNK_SAFE_STRATEGIES
from decoy_engine.plan import PlanCompileError
from decoy_engine.transforms.windowed_date import (
    WindowedDateConfig,
    _sample_offset,
    apply_windowed_date,
)

_ENGINE_VERSION = "p4-slice1-dgrn-test"
_LOW_THRESHOLD = 10


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


def _anchor_frame(n: int) -> pd.DataFrame:
    return pd.DataFrame(
        {"start_date": [f"19{60 + (i % 40):02d}-03-{1 + (i % 28):02d}" for i in range(n)]}
    )


def _windowed_date_cfg(
    tmp_path, *, min_days: int = 0, max_days: int = 30, distribution: str = "uniform"
):
    columns = [
        {"name": "start_date", "strategy": "passthrough"},
        {
            "name": "end_date",
            "strategy": "windowed_date",
            "provider_config": {
                "anchor": "start_date",
                "min_days": min_days,
                "max_days": max_days,
                "distribution": distribution,
            },
        },
    ]
    return _config(tmp_path, columns)


def _chunks(df: pd.DataFrame, size: int) -> list[pa.Table]:
    return [
        pa.Table.from_pandas(df.iloc[i : i + size], preserve_index=False)
        for i in range(0, len(df), size)
    ]


# ---------------------------------------------------------------------------
# Handler unit: apply_windowed_date(row_offset=k) matches full-frame at k+j.
# ---------------------------------------------------------------------------


class TestHandlerUnitOffset:
    def test_chunk_with_offset_matches_full_frame_slice(self) -> None:
        full_df = _anchor_frame(50)
        cfg = WindowedDateConfig.from_dict(
            {"anchor": "start_date", "min_days": -10, "max_days": 40, "distribution": "early"}
        )
        seed = b"\xaa" * 8
        namespace = "windowed_date/end_date"

        full_result = apply_windowed_date(cfg, full_df, seed=seed, namespace=namespace)

        # A chunk starting at global row 17, 8 rows long.
        k, width = 17, 8
        chunk_df = full_df.iloc[k : k + width].reset_index(drop=True)
        chunk_result = apply_windowed_date(
            cfg, chunk_df, seed=seed, namespace=namespace, row_offset=k
        )

        assert chunk_result == full_result[k : k + width]

    def test_default_row_offset_is_zero_and_matches_plain_enumerate(self) -> None:
        df = _anchor_frame(10)
        cfg = WindowedDateConfig.from_dict({"anchor": "start_date", "max_days": 5})
        seed = b"\x22" * 8
        ns = "windowed_date/x"
        assert apply_windowed_date(cfg, df, seed=seed, namespace=ns) == apply_windowed_date(
            cfg, df, seed=seed, namespace=ns, row_offset=0
        )


class TestHandlerWritesRealValues:
    """A route-parity test alone cannot catch a handler that writes a
    constant instead of the real masked values: full-frame and chunked both
    run the SAME handler, so they would stay "equal" even if both wrote
    None into the column. This compares the handler's REAL output, through
    `PandasExecutionAdapter.run_single`, against a DIRECT `apply_windowed_date`
    call -- an oracle outside the handler dispatch path entirely."""

    def test_run_single_output_matches_direct_apply_windowed_date_oracle(self) -> None:
        from types import SimpleNamespace

        from decoy_engine.execution import PandasExecutionAdapter
        from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships._graph import RelationshipGraph
        from decoy_engine.relationships._namespace import NamespaceRegistry

        seed = (0x4242).to_bytes(8, "big")
        col_seed = ColumnSeed(
            namespace=None,
            strategy="windowed_date",
            provider=None,
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=False,
            provider_config=(("anchor", "start_date"), ("min_days", -4), ("max_days", 17)),
            coherent_with=(),
            when=None,
        )
        plan = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=seed,
                per_table=(
                    ("accounts", TableSeed(per_column=(("end_date", col_seed),), per_group=())),
                ),
            )
        )
        df = _anchor_frame(9)
        table = pa.Table.from_pandas(df, preserve_index=False)
        result = PandasExecutionAdapter().run_single(
            plan,
            table,
            registry=get_default_registry(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=NamespaceRegistry(bindings=()),
        )
        actual = result.output.column("end_date").to_pylist()

        oracle_cfg = WindowedDateConfig.from_dict(
            {"anchor": "start_date", "min_days": -4, "max_days": 17}
        )
        expected = apply_windowed_date(
            oracle_cfg, df, seed=seed, namespace="windowed_date/end_date"
        )
        assert actual == expected
        assert all(v is not None for v in actual)


# ---------------------------------------------------------------------------
# End-to-end parity via the REAL route: run_pipeline(auto_chunk=True).
# ---------------------------------------------------------------------------


class TestEndToEndParity:
    @pytest.mark.parametrize("chunk_size", [1, 7, 500])
    @pytest.mark.parametrize("distribution", ["uniform", "early", "late"])
    def test_chunked_equals_full_frame_across_matrix(
        self, tmp_path, chunk_size, distribution
    ) -> None:
        """Chunk sizes 1 (every row its own chunk), 7 (prime, does not divide
        60), and 500 (oversized -- a single chunk) x all three distributions,
        with a negative min_days window."""
        df = _anchor_frame(60)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _windowed_date_cfg(tmp_path, min_days=-15, max_days=45, distribution=distribution)
        sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}

        auto = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=chunk_size,
        )
        forced = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            auto_chunk=False,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=chunk_size,
        )

        assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked", (
            "windowed_date must actually route through the chunked entrypoint, "
            "not merely be admissible"
        )
        assert forced.quality_metrics["auto_chunk"]["mode"] == "full_frame"
        a, f = auto.outputs["accounts"], forced.outputs["accounts"]
        assert [str(fl.type) for fl in a.schema] == [str(fl.type) for fl in f.schema]
        assert a.equals(f), "chunked windowed_date output diverged from the full-frame oracle"


# ---------------------------------------------------------------------------
# Base-offset boundary.
# ---------------------------------------------------------------------------


class TestBaseOffsetBoundary:
    def test_small_positive_offset_matches_true_full_frame_suffix(self, tmp_path) -> None:
        """base_row_offset=N makes chunked local row 0 mask as global row N:
        proven against a TRUE full-frame run of an (N+rows)-row table, then
        selecting rows N: -- not against another offset-aware helper."""
        n_offset = 23
        n_chunk_rows = 12
        full_df = _anchor_frame(n_offset + n_chunk_rows)
        full_df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _windowed_date_cfg(tmp_path)

        full = run_pipeline(
            cfg,
            sources={"accounts": pa.Table.from_pandas(full_df, preserve_index=False)},
            engine_version=_ENGINE_VERSION,
        ).outputs["accounts"]
        expected_suffix = full.slice(n_offset, n_chunk_rows)

        chunk_df = full_df.iloc[n_offset:].reset_index(drop=True)
        chunked = list(
            run_mask_pipeline_chunked(
                cfg,
                _chunks(chunk_df, 5),
                table="accounts",
                engine_version=_ENGINE_VERSION,
                base_row_offset=n_offset,
            )
        )
        actual = pa.concat_tables(chunked).combine_chunks()
        assert actual.to_pylist() == expected_suffix.to_pylist()

    def test_offset_near_max_domain_matches_direct_derivation_oracle(self) -> None:
        """The 2**64-1 boundary: a full-frame table there is infeasible, so
        compare against a DIRECT derive()/default_rng() oracle built from the
        transform's own primitives (not another offset-aware helper)."""
        i = ROW_OFFSET_DOMAIN_MAX  # 2**64 - 1, the largest legal row index
        seed = b"\x07" * 8
        namespace = "windowed_date/end_date"
        anchor = "2024-06-15"
        min_days, max_days, distribution = -5, 60, "late"

        row_seed_int = int.from_bytes(derive(seed, namespace, i.to_bytes(8, "big"))[:8], "big")
        rng = np.random.default_rng(row_seed_int)
        offset = _sample_offset(rng, min_days, max_days, distribution)
        expected = (pd.Timestamp(anchor) + pd.Timedelta(days=offset)).strftime("%Y-%m-%d")

        df = pd.DataFrame({"start_date": [anchor]})
        cfg = WindowedDateConfig.from_dict(
            {
                "anchor": "start_date",
                "min_days": min_days,
                "max_days": max_days,
                "distribution": distribution,
            }
        )
        result = apply_windowed_date(cfg, df, seed=seed, namespace=namespace, row_offset=i)
        assert result == [expected]

    def test_max_domain_offset_admitted_by_chunked_route(self, tmp_path) -> None:
        """base_row_offset == ROW_OFFSET_DOMAIN_MAX with a single-row chunk is
        exactly at the boundary and must NOT raise."""
        df = _anchor_frame(1)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _windowed_date_cfg(tmp_path)
        out = list(
            run_mask_pipeline_chunked(
                cfg,
                _chunks(df, 1),
                table="accounts",
                engine_version=_ENGINE_VERSION,
                base_row_offset=ROW_OFFSET_DOMAIN_MAX,
            )
        )
        assert len(out) == 1
        assert out[0].num_rows == 1

    @pytest.mark.parametrize(
        "bad_offset",
        [-1, True, False, 1.5, "12", ROW_OFFSET_DOMAIN_MAX + 1],
    )
    def test_base_row_offset_out_of_domain_raises_coded_error(self, tmp_path, bad_offset) -> None:
        df = _anchor_frame(3)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _windowed_date_cfg(tmp_path)
        with pytest.raises(ExecutionError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg,
                    _chunks(df, 3),
                    table="accounts",
                    engine_version=_ENGINE_VERSION,
                    base_row_offset=bad_offset,
                )
            )
        assert exc.value.code == "chunked_row_offset_out_of_domain"

    def test_chunk_range_exceeding_domain_raises_before_masking(self, tmp_path) -> None:
        """A base_row_offset that is itself valid but whose chunk's range
        would cross the domain boundary must still raise the coded error."""
        df = _anchor_frame(3)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _windowed_date_cfg(tmp_path)
        with pytest.raises(ExecutionError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg,
                    _chunks(df, 3),  # one 3-row chunk
                    table="accounts",
                    engine_version=_ENGINE_VERSION,
                    base_row_offset=ROW_OFFSET_DOMAIN_MAX - 1,  # last row = MAX + 1
                )
            )
        assert exc.value.code == "chunked_row_offset_out_of_domain"

    def test_validate_base_row_offset_unit(self) -> None:
        validate_base_row_offset(0)
        validate_base_row_offset(ROW_OFFSET_DOMAIN_MAX)
        for bad in (-1, ROW_OFFSET_DOMAIN_MAX + 1, True, 2.0, "3"):
            with pytest.raises(ExecutionError) as exc:
                validate_base_row_offset(bad)  # type: ignore[arg-type]
            assert exc.value.code == "chunked_row_offset_out_of_domain"

    def test_validate_base_row_offset_bad_type_message_names_the_real_type(self) -> None:
        """Pins the message's `(type(base_row_offset).__name__)` fragment to
        the ACTUAL offending type, not an incidental constant."""
        with pytest.raises(ExecutionError) as exc:
            validate_base_row_offset(1.5)  # type: ignore[arg-type]
        assert "float" in exc.value.message
        assert "1.5" in exc.value.message
        with pytest.raises(ExecutionError) as exc2:
            validate_base_row_offset("12")  # type: ignore[arg-type]
        assert "str" in exc2.value.message

    def test_validate_base_row_offset_range_message_names_the_bound(self) -> None:
        with pytest.raises(ExecutionError) as exc:
            validate_base_row_offset(ROW_OFFSET_DOMAIN_MAX + 1)
        assert str(ROW_OFFSET_DOMAIN_MAX + 1) in exc.value.message
        assert str(ROW_OFFSET_DOMAIN_MAX) in exc.value.message

    def test_validate_chunk_row_offset_range_unit(self) -> None:
        validate_chunk_row_offset_range(0, 0)  # empty chunk never raises
        validate_chunk_row_offset_range(ROW_OFFSET_DOMAIN_MAX, 1)  # exactly the last row
        with pytest.raises(ExecutionError) as exc:
            validate_chunk_row_offset_range(ROW_OFFSET_DOMAIN_MAX, 2)
        assert exc.value.code == "chunked_row_offset_out_of_domain"
        assert str(ROW_OFFSET_DOMAIN_MAX) in exc.value.message
        assert str(ROW_OFFSET_DOMAIN_MAX + 1) in exc.value.message

    def test_validate_chunk_row_offset_range_single_row_chunk_still_checked(self) -> None:
        """A `num_rows == 1` chunk is not a special early-return case: it
        must still be checked against the domain like any other width."""
        with pytest.raises(ExecutionError) as exc:
            validate_chunk_row_offset_range(ROW_OFFSET_DOMAIN_MAX + 1, 1)
        assert exc.value.code == "chunked_row_offset_out_of_domain"


# ---------------------------------------------------------------------------
# `when:` rejection.
# ---------------------------------------------------------------------------


def _when_bearing_windowed_date_cfg(tmp_path) -> dict:
    """`when` is schema-forbidden on ColumnConfig (pydantic), but
    `run_pipeline`/`run_mask_pipeline_chunked`/`check_chunked_compatibility`
    do not re-validate their dict input and `when` is a shipped engine
    feature (`ColumnSeed.when`), so an SDK caller can hand one in; the gate
    must see it. Injected post-validation, mirroring
    `test_auto_chunk_routing.py`'s `_when_bearing_config`."""
    cfg = _windowed_date_cfg(tmp_path, max_days=10)
    cfg["tables"][0]["columns"][1]["when"] = "start_date != ''"
    return cfg


class TestWhenRejection:
    def test_windowed_date_with_when_rejected_by_check_chunked_compatibility(
        self, tmp_path
    ) -> None:
        cfg = _when_bearing_windowed_date_cfg(tmp_path)
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="accounts")
        assert exc.value.code == "chunked_windowed_date_when_not_supported"
        assert exc.value.path == "tables.accounts.columns"
        assert "end_date" in exc.value.message  # the offending column, by NAME
        assert "matching rows" in exc.value.message  # the filtered-enumeration reason

    def test_two_offending_columns_both_named_and_comma_joined(self, tmp_path) -> None:
        """Pins the exact `', '.join(...)` separator and that BOTH offending
        column names are read (not a hardcoded default)."""
        cfg = _config(
            tmp_path,
            [
                {"name": "start_date", "strategy": "passthrough"},
                {
                    "name": "end_date_a",
                    "strategy": "windowed_date",
                    "provider_config": {"anchor": "start_date", "max_days": 10},
                },
                {
                    "name": "end_date_b",
                    "strategy": "windowed_date",
                    "provider_config": {"anchor": "start_date", "max_days": 10},
                },
            ],
        )
        cfg["tables"][0]["columns"][1]["when"] = "start_date != ''"
        cfg["tables"][0]["columns"][2]["when"] = "start_date != ''"
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="accounts")
        assert "end_date_a, end_date_b" in exc.value.message

    def test_column_missing_name_key_falls_back_to_placeholder(self, tmp_path) -> None:
        """A malformed column entry with no `name` key must not crash the
        gate: it falls back to the `"?"` placeholder, same as every other
        name-reading gate in this module."""
        cfg = _config(
            tmp_path,
            [{"name": "start_date", "strategy": "passthrough"}],
        )
        cfg["tables"][0]["columns"].append(
            {
                "strategy": "windowed_date",
                "when": "1 == 1",
                "provider_config": {"anchor": "start_date", "max_days": 5},
            }
        )
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="accounts")
        assert "column(s) ? combine" in exc.value.message

    def test_windowed_date_with_when_rejected_by_manual_entrypoint(self, tmp_path) -> None:
        df = _anchor_frame(5)
        cfg = _when_bearing_windowed_date_cfg(tmp_path)
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg,
                    _chunks(df, 2),
                    table="accounts",
                    engine_version=_ENGINE_VERSION,
                )
            )
        assert exc.value.code == "chunked_windowed_date_when_not_supported"

    def test_windowed_date_with_when_stays_full_frame_on_auto_route(self, tmp_path) -> None:
        df = _anchor_frame(60)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _when_bearing_windowed_date_cfg(tmp_path)
        auto = run_pipeline(
            cfg,
            sources={"accounts": pa.Table.from_pandas(df, preserve_index=False)},
            engine_version=_ENGINE_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=7,
        )
        assert auto.quality_metrics["auto_chunk"]["mode"] == "full_frame"


# ---------------------------------------------------------------------------
# FK-child RI regression: windowed_date must stay OUT of CHUNK_SAFE_STRATEGIES.
# ---------------------------------------------------------------------------


class TestFkChildRIRegression:
    def test_windowed_date_not_in_chunk_safe_strategies(self) -> None:
        assert "windowed_date" not in CHUNK_SAFE_STRATEGIES
        assert "windowed_date" in CHUNK_DGRN_STRATEGIES
        assert CHUNK_SAFE_STRATEGIES.isdisjoint(CHUNK_DGRN_STRATEGIES)

    def test_matching_parent_child_windowed_date_rejected_as_fk_key(self, tmp_path) -> None:
        """Parent and child both configure windowed_date identically
        (same anchor shape, orphan_policy remap) -- this is the shape that
        would look admissible if windowed_date were folded into
        CHUNK_SAFE_STRATEGIES. It must still be rejected."""
        cfg = {
            "global_settings": {"seed": 7},
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {"name": "anchor", "strategy": "passthrough"},
                        {
                            "name": "id",
                            "strategy": "windowed_date",
                            "dtype": "string",
                            "provider_config": {"anchor": "anchor", "max_days": 10},
                        },
                    ],
                },
                {
                    "name": "orders",
                    "columns": [
                        {"name": "anchor", "strategy": "passthrough"},
                        {
                            "name": "customer_id",
                            "strategy": "windowed_date",
                            "dtype": "string",
                            "provider_config": {"anchor": "anchor", "max_days": 10},
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


# ---------------------------------------------------------------------------
# Null-anchor failure path.
# ---------------------------------------------------------------------------


class TestNullAnchorFailure:
    def test_null_anchor_raises_same_error_both_routes_no_result(self, tmp_path) -> None:
        df = _anchor_frame(10)
        df.loc[5, "start_date"] = None  # a chunk boundary when chunk_size_rows=5
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _windowed_date_cfg(tmp_path)
        sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}

        with pytest.raises(ValueError, match="strftime"):
            run_pipeline(
                cfg,
                sources=sources,
                engine_version=_ENGINE_VERSION,
                auto_chunk=False,
            )
        with pytest.raises(ValueError, match="strftime"):
            run_pipeline(
                cfg,
                sources=sources,
                engine_version=_ENGINE_VERSION,
                auto_chunk_threshold_rows=_LOW_THRESHOLD,
                chunk_size_rows=5,
            )

    def test_all_null_tail_chunk_raises_no_result(self, tmp_path) -> None:
        df = _anchor_frame(9)
        df.loc[6:, "start_date"] = None  # the last chunk of 3 is entirely null
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _windowed_date_cfg(tmp_path)
        sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}

        with pytest.raises(ValueError, match="strftime"):
            run_pipeline(
                cfg,
                sources=sources,
                engine_version=_ENGINE_VERSION,
                auto_chunk_threshold_rows=_LOW_THRESHOLD,
                chunk_size_rows=3,
            )

    def test_manual_chunked_entrypoint_null_anchor_raises_mid_iteration(self, tmp_path) -> None:
        """The generator raises during `list(...)` materialization, matching
        the full-frame route's raise-with-no-result contract exactly."""
        df = _anchor_frame(6)
        df.loc[4, "start_date"] = None
        cfg = _windowed_date_cfg(tmp_path)
        with pytest.raises(ValueError, match="strftime"):
            list(
                run_mask_pipeline_chunked(
                    cfg,
                    _chunks(df, 2),
                    table="accounts",
                    engine_version=_ENGINE_VERSION,
                )
            )


# ---------------------------------------------------------------------------
# Cross-substrate (polars pandas-fallback).
# ---------------------------------------------------------------------------


class TestCrossSubstrate:
    @pytest.mark.parametrize("chunk_size", [1, 6, 40])
    def test_polars_chunked_value_equals_pandas_full_frame(self, tmp_path, chunk_size) -> None:
        """windowed_date is never polars-native, so every polars-adapter
        chunk lands on `_run_via_pandas_oracle` -> `PandasExecutionAdapter.run`.
        Proves row_offset threads the FULL path: PolarsExecutionAdapter.run ->
        _run_via_pandas_oracle -> PandasExecutionAdapter.run -> StrategyContext.
        A missing forward anywhere on that path would reset `i` to 0 every
        chunk and only the multi-chunk cases here would catch it."""
        df = _anchor_frame(37)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _windowed_date_cfg(tmp_path, min_days=-8, max_days=20, distribution="early")

        full = run_pipeline(
            cfg,
            sources={"accounts": pa.Table.from_pandas(df, preserve_index=False)},
            engine_version=_ENGINE_VERSION,
        ).outputs["accounts"]

        polars_chunked = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg,
                    _chunks(df, chunk_size),
                    table="accounts",
                    engine_version=_ENGINE_VERSION,
                    adapter=PolarsExecutionAdapter(),
                )
            )
        ).combine_chunks()

        assert polars_chunked.column_names == full.column_names
        assert polars_chunked.to_pydict() == full.to_pydict()


# ---------------------------------------------------------------------------
# Mixed-strategy fixture.
# ---------------------------------------------------------------------------


class TestMixedStrategyFixture:
    @pytest.mark.parametrize("chunk_size", [1, 9, 200])
    def test_windowed_date_plus_hash_byte_identical(self, tmp_path, chunk_size) -> None:
        n = 44
        df = pd.DataFrame(
            {
                "start_date": [f"19{60 + (i % 40):02d}-03-{1 + (i % 28):02d}" for i in range(n)],
                "email": [f"user{i}@example.com" for i in range(n)],
            }
        )
        df.to_csv(tmp_path / "in.csv", index=False)
        columns = [
            {"name": "start_date", "strategy": "passthrough"},
            {
                "name": "end_date",
                "strategy": "windowed_date",
                "provider_config": {"anchor": "start_date", "min_days": -5, "max_days": 25},
            },
            {"name": "email", "strategy": "hash", "namespace": "email_ns"},
        ]
        cfg = _config(tmp_path, columns)
        sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}

        full = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION).outputs[
            "accounts"
        ]
        chunked = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg,
                    _chunks(df, chunk_size),
                    table="accounts",
                    engine_version=_ENGINE_VERSION,
                )
            )
        ).combine_chunks()
        assert chunked.to_pylist() == full.to_pylist()
