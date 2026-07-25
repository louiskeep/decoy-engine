"""Chunked mask execution (capability-gaps WS4, 2026-06-12).

`run_mask_pipeline_chunked` masks a table chunk-by-chunk for inputs too
large to hold in memory. The non-negotiable gate is BYTE PARITY: for
any chunking of the rows, concatenated chunked output equals the
full-frame `run_pipeline` output exactly. That holds because chunked
mode only admits VALUE-KEYED strategies (every output cell is a pure
function of its input cell + config + seed, never of row position or
neighboring rows); `check_chunked_compatibility` rejects everything
else at compile time.

Deferred follow-up 2 (2026-06-12): faker and categorical are admitted
on their deterministic value-keyed paths when every whole-run input is
declared in config (explicit pool_size / categories); the same parity
gate covers them below.
"""

from __future__ import annotations

import datetime

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import run_mask_pipeline_chunked
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.plan import PlanCompileError

_ENGINE_VERSION = "ws4-test"


def _config(tmp_path, columns: list[dict], table: str = "accounts") -> dict:
    cfg = {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {table: {"type": "file", "format": "csv", "path": str(tmp_path / "in.csv")}},
        "tables": [{"name": table, "columns": columns}],
        "targets": {table: {"type": "file", "format": "csv", "path": str(tmp_path / "out.csv")}},
    }
    return PipelineConfig.model_validate(cfg).model_dump()


_SAFE_COLUMNS = [
    {
        "name": "ssn",
        "strategy": "fpe",
        "namespace": "ssn_ns",
        "provider_config": {"charset": "digits"},
    },
    {"name": "email", "strategy": "hash", "namespace": "email_ns"},
    {"name": "notes", "strategy": "text_redact"},
    {
        "name": "dob",
        "strategy": "date_shift",
        "namespace": "dob_ns",
        "provider_config": {"min_days": -30, "max_days": 30},
    },
    {"name": "zip", "strategy": "truncate", "provider_config": {"length": 3}},
    {"name": "secret", "strategy": "redact"},
    {
        "name": "contact",
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": "contact_ns",
        "cardinality_mode": "reuse",
        "provider_config": {"pool_size": 50},
    },
    {
        "name": "tier",
        "strategy": "categorical",
        "deterministic": True,
        "namespace": "tier_ns",
        "provider_config": {"categories": ["free", "pro", "team"], "weights": [0.6, 0.3, 0.1]},
    },
    {
        "name": "region",
        "strategy": "categorical",
        "deterministic": True,
        "namespace": "region_ns",
        "provider_config": {"categories": ["na", "emea", "apac"]},
    },
]


def _frame(n: int = 100) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ssn": [f"{i:09d}" for i in range(n)],
            "email": [f"user{i}@example.com" for i in range(n)],
            "notes": [f"contact user{i}@example.com today" for i in range(n)],
            "dob": [f"19{60 + (i % 40):02d}-03-{1 + (i % 28):02d}" for i in range(n)],
            "zip": [f"{10000 + i:05d}" for i in range(n)],
            "secret": [f"secret-{i}" for i in range(n)],
            "contact": [f"person{i}@source.example" for i in range(n)],
            "tier": [["bronze", "silver", "gold"][i % 3] for i in range(n)],
            "region": [f"district-{i % 11}" for i in range(n)],
        }
    )


def _chunks(df: pd.DataFrame, size: int) -> list[pa.Table]:
    return [
        pa.Table.from_pandas(df.iloc[i : i + size], preserve_index=False)
        for i in range(0, len(df), size)
    ]


class TestChunkParity:
    @pytest.mark.parametrize("chunk_size", [1, 7, 33, 100, 250])
    def test_chunked_equals_full_frame(self, tmp_path, chunk_size: int) -> None:
        df = _frame(100)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(tmp_path, _SAFE_COLUMNS)

        full = run_pipeline(
            cfg,
            sources={"accounts": pa.Table.from_pandas(df, preserve_index=False)},
            engine_version=_ENGINE_VERSION,
        ).outputs["accounts"]

        out_chunks = list(
            run_mask_pipeline_chunked(
                cfg,
                _chunks(df, chunk_size),
                table="accounts",
                engine_version=_ENGINE_VERSION,
            )
        )
        chunked = pa.concat_tables(out_chunks)
        assert chunked.to_pylist() == full.to_pylist()

    def test_nulls_preserved(self, tmp_path) -> None:
        df = _frame(10)
        df.loc[3, "ssn"] = None
        df.loc[5, "notes"] = None
        df.loc[2, "contact"] = None
        df.loc[7, "tier"] = None
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(tmp_path, _SAFE_COLUMNS)
        out = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg, _chunks(df, 4), table="accounts", engine_version=_ENGINE_VERSION
                )
            )
        )
        assert out.column("ssn").to_pylist()[3] is None
        assert out.column("notes").to_pylist()[5] is None
        assert out.column("contact").to_pylist()[2] is None
        assert out.column("tier").to_pylist()[7] is None

    def test_deterministic_across_calls(self, tmp_path) -> None:
        df = _frame(30)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(tmp_path, _SAFE_COLUMNS)
        a = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg, _chunks(df, 7), table="accounts", engine_version=_ENGINE_VERSION
                )
            )
        )
        b = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg, _chunks(df, 7), table="accounts", engine_version=_ENGINE_VERSION
                )
            )
        )
        assert a.equals(b)


class TestChunkParityGroupBy:
    """HC-3a: date_shift + group_by chunk parity, including patients split
    ACROSS chunk boundaries. group_by adds no whole-column state (it's a
    per-row lookup into the same-row's sibling value, same as the date cell
    itself), so a group's offset is identical whether all its rows land in
    one chunk or are split across several -- the parity gate below is the
    proof."""

    _COLUMNS = [
        {
            "name": "dob",
            "strategy": "date_shift",
            "namespace": "dob_ns",
            "provider_config": {"min_days": -30, "max_days": 30, "group_by": "patient_id"},
        },
        {"name": "patient_id", "strategy": "passthrough"},
    ]

    def _frame(self, n: int) -> pd.DataFrame:
        # Patients repeat every 3 rows, so most chunk sizes below split at
        # least one patient's rows across a chunk boundary.
        return pd.DataFrame(
            {
                "dob": [f"19{60 + (i % 40):02d}-03-{1 + (i % 28):02d}" for i in range(n)],
                "patient_id": [f"patient-{i // 3}" for i in range(n)],
            }
        )

    @pytest.mark.parametrize("chunk_size", [1, 7, 33, 100, 250])
    def test_chunked_equals_full_frame(self, tmp_path, chunk_size: int) -> None:
        df = self._frame(100)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(tmp_path, self._COLUMNS)

        full = run_pipeline(
            cfg,
            sources={"accounts": pa.Table.from_pandas(df, preserve_index=False)},
            engine_version=_ENGINE_VERSION,
        ).outputs["accounts"]

        out_chunks = list(
            run_mask_pipeline_chunked(
                cfg, _chunks(df, chunk_size), table="accounts", engine_version=_ENGINE_VERSION
            )
        )
        chunked = pa.concat_tables(out_chunks)
        assert chunked.to_pylist() == full.to_pylist()

    def test_same_patient_same_offset_across_chunk_boundary(self, tmp_path) -> None:
        """CORE guarantee: a patient's two dates land in DIFFERENT chunks
        (chunk_size=2 splits patient pA's second row from its first) but
        still shift by the same offset -- the interval survives chunking."""
        df = pd.DataFrame(
            {
                "dob": ["1980-01-01", "1980-01-15", "1990-05-01"],
                "patient_id": ["pA", "pA", "pB"],
            }
        )
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(tmp_path, self._COLUMNS)
        out = (
            pa.concat_tables(
                list(
                    run_mask_pipeline_chunked(
                        cfg, _chunks(df, 2), table="accounts", engine_version=_ENGINE_VERSION
                    )
                )
            )
            .column("dob")
            .to_pylist()
        )

        d0 = datetime.date.fromisoformat("1980-01-01")
        d1 = datetime.date.fromisoformat("1980-01-15")
        o0 = datetime.datetime.strptime(out[0], "%Y-%m-%d").date()
        o1 = datetime.datetime.strptime(out[1], "%Y-%m-%d").date()
        assert (o1 - o0).days == (d1 - d0).days


class TestChunkedCompatibility:
    def test_shuffle_rejected(self, tmp_path) -> None:
        cfg = _config(tmp_path, [{"name": "ssn", "strategy": "shuffle"}])
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(cfg, [], table="accounts", engine_version=_ENGINE_VERSION)
            )
        assert exc.value.code == "strategy_not_chunk_safe"
        assert "shuffle" in str(exc.value)

    def test_faker_without_pool_size_rejected(self, tmp_path) -> None:
        """Deterministic faker without the explicit capacity declaration:
        the non-chunked 10k default is never applied silently here."""
        cfg = _config(
            tmp_path,
            [
                {
                    "name": "email",
                    "strategy": "faker",
                    "provider": "person_email",
                    "deterministic": True,
                    "namespace": "e_ns",
                }
            ],
        )
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(cfg, [], table="accounts", engine_version=_ENGINE_VERSION)
            )
        assert exc.value.code == "chunked_strategy_conditions_unmet"
        assert "pool_size" in str(exc.value)

    def test_faker_non_deterministic_rejected(self, tmp_path) -> None:
        cfg = _config(
            tmp_path,
            [
                {
                    "name": "email",
                    "strategy": "faker",
                    "provider": "person_email",
                    "namespace": "e_ns",
                    "provider_config": {"pool_size": 10},
                }
            ],
        )
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(cfg, [], table="accounts", engine_version=_ENGINE_VERSION)
            )
        assert exc.value.code == "chunked_strategy_conditions_unmet"
        assert "deterministic" in str(exc.value)

    def test_faker_source_cardinality_mode_rejected(self, tmp_path) -> None:
        cfg = _config(
            tmp_path,
            [
                {
                    "name": "email",
                    "strategy": "faker",
                    "provider": "person_email",
                    "deterministic": True,
                    "namespace": "e_ns",
                    "cardinality_mode": "match_source_cardinality",
                    "provider_config": {"pool_size": 10},
                }
            ],
        )
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(cfg, [], table="accounts", engine_version=_ENGINE_VERSION)
            )
        assert exc.value.code == "chunked_strategy_conditions_unmet"
        assert "cardinality_mode" in str(exc.value)

    def test_categorical_from_profile_rejected(self, tmp_path) -> None:
        cfg = _config(
            tmp_path,
            [
                {
                    "name": "tier",
                    "strategy": "categorical",
                    "deterministic": True,
                    "namespace": "t_ns",
                    "provider_config": {"from_profile": True},
                }
            ],
        )
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(cfg, [], table="accounts", engine_version=_ENGINE_VERSION)
            )
        assert exc.value.code == "chunked_strategy_conditions_unmet"
        assert "from_profile" in str(exc.value)

    def test_relationships_with_preserve_policy_rejected(self, tmp_path) -> None:
        # PRESERVE policy cannot be reproduced by child self-masking (detecting an
        # orphan requires the parent key set resident). Gate raises a specific code
        # rather than the old blanket chunked_relationships_unsupported.
        cfg = _config(tmp_path, [{"name": "ssn", "strategy": "hash", "namespace": "n"}])
        cfg["relationships"] = [
            {
                "parent": {"table": "accounts", "columns": ["ssn"]},
                "children": [{"table": "accounts", "columns": ["ssn"]}],
                "orphan_policy": "preserve",
                "namespace": "n",
            }
        ]
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(cfg, [], table="accounts", engine_version=_ENGINE_VERSION)
            )
        assert exc.value.code == "chunked_fk_orphan_policy_not_remap"

    def test_generate_tables_rejected(self, tmp_path) -> None:
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "synth",
                    "row_count": 5,
                    "generate_columns": [{"name": "n", "type": "sequence", "start": 1}],
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            list(run_mask_pipeline_chunked(cfg, [], table="synth", engine_version=_ENGINE_VERSION))
        assert exc.value.code == "chunked_generate_unsupported"

    def test_unknown_table_rejected(self, tmp_path) -> None:
        cfg = _config(tmp_path, [{"name": "ssn", "strategy": "hash", "namespace": "n"}])
        with pytest.raises(PlanCompileError) as exc:
            list(run_mask_pipeline_chunked(cfg, [], table="nope", engine_version=_ENGINE_VERSION))
        assert exc.value.code == "chunked_table_unknown"

    def test_empty_iterator_yields_nothing(self, tmp_path) -> None:
        cfg = _config(tmp_path, [{"name": "ssn", "strategy": "hash", "namespace": "n"}])
        assert (
            list(
                run_mask_pipeline_chunked(cfg, [], table="accounts", engine_version=_ENGINE_VERSION)
            )
            == []
        )


class TestConditionalStrategies:
    def test_more_distincts_than_pool_size_keeps_parity(self, tmp_path) -> None:
        """derive_index maps any value into [0, pool_size) independent of
        chunk arrival: 100 distincts through a 7-slot pool stay
        byte-identical to the full-frame run, with collisions."""
        n = 100
        df = pd.DataFrame({"contact": [f"person{i}@source.example" for i in range(n)]})
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(
            tmp_path,
            [
                {
                    "name": "contact",
                    "strategy": "faker",
                    "provider": "person_email",
                    "deterministic": True,
                    "namespace": "contact_ns",
                    "provider_config": {"pool_size": 7},
                }
            ],
        )
        full = run_pipeline(
            cfg,
            sources={"accounts": pa.Table.from_pandas(df, preserve_index=False)},
            engine_version=_ENGINE_VERSION,
        ).outputs["accounts"]
        chunked = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg, _chunks(df, 9), table="accounts", engine_version=_ENGINE_VERSION
                )
            )
        )
        assert chunked.to_pylist() == full.to_pylist()
        masked = chunked.column("contact").to_pylist()
        assert len(set(masked)) <= 7

    def test_faker_pool_cache_consulted_across_runs(self, tmp_path) -> None:
        """The handler consults ctx.pool_cache: a second adapter run with
        the same cache hits the pool built by the first instead of
        rebuilding, with identical output bytes."""
        from decoy_engine.execution._chunked_profile import first_chunk_profile
        from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
        from decoy_engine.generation.pool import PoolCache
        from decoy_engine.plan import compile_plan
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships import RelationshipGraph, build_namespace_registry

        df = _frame(20)
        cfg = _config(tmp_path, _SAFE_COLUMNS)
        source_tbl = pa.Table.from_pandas(df, preserve_index=False)
        profile = first_chunk_profile(source_tbl, table="accounts", engine_version=_ENGINE_VERSION)
        plan = compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION, no_profile=True)
        registry = get_default_registry()
        ns_registry = build_namespace_registry(cfg, profile)
        graph = RelationshipGraph(edges=(), ordering=())
        adapter = PandasExecutionAdapter()
        cache = PoolCache()

        kwargs = dict(
            registry=registry,
            pool_cache=cache,
            relationship_graph=graph,
            namespace_registry=ns_registry,
        )
        first = adapter.run(plan, {"accounts": source_tbl}, **kwargs).outputs["accounts"]
        after_first = cache.stats()
        second = adapter.run(plan, {"accounts": source_tbl}, **kwargs).outputs["accounts"]
        after_second = cache.stats()

        assert first.equals(second)
        assert after_first.entries >= 1
        assert after_second.hits > after_first.hits
        assert after_second.misses == after_first.misses


class TestChunkedRowErrorsFailClosed:
    """HIGH H1 remediation (dennis review 2026-07-04).

    The chunked/streaming entry point has no quarantine machinery. A
    bucketize/date_shift chunk that produces per-row `format_error`s used to
    silently discard `ExecutionResult.row_errors`, keeping the raw source
    value in the streamed output. `run_mask_pipeline_chunked` now FAILS
    CLOSED: any chunk with non-empty row_errors raises
    `RowErrorsFailedError` (fail-closed is correct and cheap here; the
    chunked path has no opt-in quarantine).
    """

    def _row_error_config(self, tmp_path, strategy: str) -> dict:
        if strategy == "bucketize":
            columns = [{"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}}]
        else:
            columns = [
                {
                    "name": "age",
                    "strategy": "date_shift",
                    "namespace": "age_ns",
                    "provider_config": {"min_days": 30, "max_days": 60},
                }
            ]
        return _config(tmp_path, columns)

    @pytest.mark.parametrize("strategy", ["bucketize", "date_shift"])
    def test_uncoercible_cell_fails_closed(self, tmp_path, strategy: str) -> None:
        from decoy_engine.errors import RowErrorsFailedError

        cfg = self._row_error_config(tmp_path, strategy)
        # A chunk whose second row is uncoercible under the strategy.
        bad_value = "not-a-number" if strategy == "bucketize" else "not-a-date"
        good = "23" if strategy == "bucketize" else "2020-01-01"
        chunk = pa.table({"age": pa.array([good, bad_value], type=pa.string())})

        with pytest.raises(RowErrorsFailedError) as exc_info:
            list(
                run_mask_pipeline_chunked(
                    cfg, [chunk], table="accounts", engine_version=_ENGINE_VERSION
                )
            )
        assert exc_info.value.records[0].trigger == "format_error"
        # THE LEAK TEST: no cell value leaks into the error message.
        assert bad_value not in str(exc_info.value)

    def test_clean_chunks_still_stream(self, tmp_path) -> None:
        """A fully-coercible run is unaffected (no false positives)."""
        cfg = self._row_error_config(tmp_path, "bucketize")
        chunk = pa.table({"age": pa.array(["23", "47"], type=pa.string())})
        out = list(
            run_mask_pipeline_chunked(
                cfg, [chunk], table="accounts", engine_version=_ENGINE_VERSION
            )
        )
        assert pa.concat_tables(out).column("age").to_pylist() == ["20", "40"]
