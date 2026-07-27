"""Mutation-kill oracles for `execution/_chunked.py` (TQ substrate sweep).

`_chunked.py` is the chunked mask-execution route: `run_mask_pipeline_chunked`
masks one table chunk-by-chunk for out-of-memory inputs, under a byte-parity
contract with the full-frame path. This is a substrate module (route/gate +
streaming), not crypto/RI, so the LOGIC bar is 77.91%.

Each test targets specific surviving mutants by asserting on MACHINE fields --
the byte-parity output, the compatibility verdict CODE + `.path` + the offending
column/table NAMES carried in the message (data, not prose), the aggregated
timing values, and the pool-cache state the warmer's contract specifies -- never
on explanatory message prose. Message wording (XX-wrap / upper-case of a literal)
is left EQUIVALENT and documented in
`docs/quality/mutation-ledgers/execution_chunked.md`.

Expected values are hardcoded or computed by the full-frame engine path once and
compared; no module constant is imported and recomputed into an oracle.
"""

from __future__ import annotations

import types

import pandas as pd
import pyarrow as pa
import pytest

import decoy_engine.generation.pool as poolmod
import decoy_engine.release as release
from decoy_engine import run_mask_pipeline_chunked
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._chunked import (
    _conditional_admission_failures,
    _warm_faker_pools,
    aggregate_chunk_timings,
    check_chunked_compatibility,
    concat_masked_chunks,
)
from decoy_engine.execution._chunked_profile import first_chunk_profile
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import FK_KEY_DTYPE_UNSUPPORTED_CODE
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.generation.pool import PoolCache
from decoy_engine.instrumentation.timing import StrategyTimingRecord
from decoy_engine.keyprovider import KeyedStrategyRequiresSecret
from decoy_engine.plan import PlanCompileError, compile_plan
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships import RelationshipGraph, build_namespace_registry

_EV = "chunked-mutkill"


def _validated(cfg: dict) -> dict:
    return PipelineConfig.model_validate(cfg).model_dump()


def _config(tmp_path, columns: list[dict], table: str = "accounts") -> dict:
    return _validated(
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


def _chunks(df: pd.DataFrame, size: int) -> list[pa.Table]:
    return [
        pa.Table.from_pandas(df.iloc[i : i + size], preserve_index=False)
        for i in range(0, len(df), size)
    ]


# ==========================================================================
# _conditional_admission_failures -- which faker/categorical admission
# conditions are unmet (data), not the wording of each explanation.
# ==========================================================================


class TestConditionalAdmissionFailures:
    def test_top_level_pool_size_admits_faker(self) -> None:
        """A faker column with pool_size at the TOP LEVEL (not provider_config)
        is admitted: no pool_size failure. Kills the mutations that break the
        `col_entry.get("pool_size")` read (nulled key / renamed key), which
        would wrongly flag the top-level declaration as missing."""
        col = {
            "name": "c",
            "strategy": "faker",
            "provider": "person_email",
            "deterministic": True,
            "namespace": "n",
            "pool_size": 20,
            "provider_config": {},
        }
        assert _conditional_admission_failures(col) == []

    def test_missing_namespace_names_that_condition(self) -> None:
        """A deterministic faker with pool_size but NO namespace reports exactly
        the namespace condition. Kills `append(None)` (the list becomes [None])
        and the upper-cased literal (the lowercase phrase disappears); the
        XX-wrapped literal keeps the phrase and stays EQUIVALENT."""
        col = {
            "name": "c",
            "strategy": "faker",
            "provider": "person_email",
            "deterministic": True,
            "provider_config": {"pool_size": 8},
        }
        failures = _conditional_admission_failures(col)
        assert len(failures) == 1
        assert failures[0] is not None
        assert "requires a namespace" in failures[0]

    def test_categorical_missing_categories_names_that_condition(self) -> None:
        """A deterministic categorical with a namespace but no categories (and
        not from_profile) reports exactly the categories condition. Kills
        `append(None)` and the upper-cased literal."""
        col = {
            "name": "t",
            "strategy": "categorical",
            "deterministic": True,
            "namespace": "tn",
            "provider_config": {},
        }
        failures = _conditional_admission_failures(col)
        assert len(failures) == 1
        assert failures[0] is not None
        assert "requires explicit provider_config.categories" in failures[0]


# ==========================================================================
# check_chunked_compatibility -- reject/admit verdict CODE, `.path`, and the
# offending column/table NAMES. Called directly with raw config dicts so
# nameless columns and non-dict entries (unreachable through PipelineConfig
# validation) can be exercised.
# ==========================================================================


class TestCheckCompatibilityVerdict:
    def test_unknown_table_reports_code_path_and_known_tables(self) -> None:
        """Querying a table absent from a two-table config raises
        chunked_table_unknown with path `tables.<name>` and the sorted list of
        KNOWN table names in the message. Kills: `known=None`, the nulled/renamed
        `t.get("name","?")` reads (the known names vanish or become "?"), the
        no-default `.get("?")` (sorted() of Nones raises), `path=None`, and
        `message=None`."""
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [{"name": "aaa", "columns": []}, {"name": "bbb", "columns": []}],
        }
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="nope")
        assert exc.value.code == "chunked_table_unknown"
        assert exc.value.path == "tables.nope"
        assert exc.value.message is not None
        assert "aaa" in exc.value.message
        assert "bbb" in exc.value.message

    def test_generate_table_reports_code_and_path(self) -> None:
        """A generate-kind table raises chunked_generate_unsupported with path
        `tables.<name>` and a non-None message. Kills `path=None` and
        `message=None`."""
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
            check_chunked_compatibility(cfg, table="synth")
        assert exc.value.code == "chunked_generate_unsupported"
        assert exc.value.path == "tables.synth"
        assert exc.value.message is not None
        assert "generate" in exc.value.message

    def test_nonsafe_after_safe_column_still_rejects(self) -> None:
        """A chunk-safe column FIRST then a non-safe column: the loop must keep
        scanning past the safe column. Kills the safe-branch `continue`->`break`
        (which would stop before the shuffle and admit the job)."""
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "accounts",
                    "columns": [
                        {"name": "ok", "strategy": "hash", "namespace": "n"},
                        {"name": "ssn", "strategy": "shuffle"},
                    ],
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="accounts")
        assert exc.value.code == "strategy_not_chunk_safe"

    def test_nonsafe_after_nondict_entry_still_rejects(self) -> None:
        """A non-dict column entry FIRST then a non-safe column: the loop must
        skip the non-dict and keep scanning. Kills the isinstance-guard
        `continue`->`break`."""
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "accounts",
                    "columns": ["not-a-dict", {"name": "ssn", "strategy": "shuffle"}],
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="accounts")
        assert exc.value.code == "strategy_not_chunk_safe"

    def test_nonsafe_after_admitted_conditional_still_rejects(self) -> None:
        """An admitted faker (conditional) column FIRST then a non-safe column:
        the loop must keep scanning past the conditional. Kills the
        conditional-branch `continue`->`break`."""
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "accounts",
                    "columns": [
                        {
                            "name": "em",
                            "strategy": "faker",
                            "provider": "person_email",
                            "deterministic": True,
                            "namespace": "e",
                            "provider_config": {"pool_size": 5},
                        },
                        {"name": "ssn", "strategy": "shuffle"},
                    ],
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="accounts")
        assert exc.value.code == "strategy_not_chunk_safe"

    def test_offending_columns_named_and_joined(self) -> None:
        """Two named non-safe columns: both NAMES appear, comma-joined, with the
        error path. Kills the offending-name mutations (str(None) / nulled or
        renamed `.get`, which corrupt the shown name) and the `", "` list
        separator (asserted as the exact joined substring)."""
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "accounts",
                    "columns": [
                        {"name": "sa", "strategy": "shuffle"},
                        {"name": "sb", "strategy": "shuffle"},
                    ],
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="accounts")
        assert exc.value.code == "strategy_not_chunk_safe"
        assert exc.value.path == "tables.accounts.columns"
        assert "sa (shuffle), sb (shuffle)" in exc.value.message

    def test_offending_nameless_column_uses_question_mark(self) -> None:
        """A non-safe column with no `name` key shows the "?" fallback. Kills the
        default-value mutations (get("name",None)/get("name")/get("name","XX?XX")
        -> "None"/"XX?XX") that only differ when the name is absent."""
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [{"name": "accounts", "columns": [{"strategy": "shuffle"}]}],
        }
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="accounts")
        assert exc.value.code == "strategy_not_chunk_safe"
        assert "? (shuffle)" in exc.value.message

    def test_conditional_unmet_named_and_path(self) -> None:
        """A named faker missing its namespace: the column NAME appears in the
        conditions_unmet message with path `tables.<t>.columns`. Kills the
        conditions-unmet name mutations (name corrupted) and `path=None`."""
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "accounts",
                    "columns": [
                        {
                            "name": "em",
                            "strategy": "faker",
                            "provider": "person_email",
                            "deterministic": True,
                            "provider_config": {"pool_size": 5},
                        }
                    ],
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="accounts")
        assert exc.value.code == "chunked_strategy_conditions_unmet"
        assert exc.value.path == "tables.accounts.columns"
        assert "em" in exc.value.message

    def test_conditional_unmet_nameless_uses_question_mark(self) -> None:
        """A nameless faker missing its namespace shows the "?" fallback for the
        column in the conditions_unmet message. Kills the default-only name
        mutations (differ only when `name` is absent)."""
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "accounts",
                    "columns": [
                        {
                            "strategy": "faker",
                            "provider": "person_email",
                            "deterministic": True,
                            "provider_config": {"pool_size": 5},
                        }
                    ],
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="accounts")
        assert exc.value.code == "chunked_strategy_conditions_unmet"
        assert "? (faker" in exc.value.message

    def test_two_conditional_unmet_columns_joined(self) -> None:
        """Two conditional columns each with an unmet condition: both NAMES
        appear, joined by the reason separator. Kills the `"; "` separator
        between the per-column entries (asserted as the exact joined substring)."""
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "accounts",
                    "columns": [
                        {
                            "name": "ca",
                            "strategy": "faker",
                            "provider": "person_email",
                            "deterministic": True,
                            "provider_config": {"pool_size": 5},
                        },
                        {
                            "name": "cb",
                            "strategy": "categorical",
                            "deterministic": True,
                            "namespace": "cbn",
                            "provider_config": {},
                        },
                    ],
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(cfg, table="accounts")
        assert exc.value.code == "chunked_strategy_conditions_unmet"
        assert "ca (faker" in exc.value.message
        assert "; cb (categorical" in exc.value.message


# ==========================================================================
# concat_masked_chunks -- coded schema-mismatch error carrying the offending
# column NAME and the conflicting TYPE names (data), not the sentence prose.
# ==========================================================================


class TestConcatMaskedChunks:
    def test_column_name_mismatch_names_the_columns(self) -> None:
        """Chunks disagreeing on column names raise chunked_schema_mismatch and
        the message names the disagreeing columns. Kills `message=None` and the
        dropped-message-kwarg mutation (the column names vanish)."""
        chunks = [
            pa.table({"val": pa.array(["a"], type=pa.string())}),
            pa.table({"other": pa.array(["b"], type=pa.string())}),
        ]
        with pytest.raises(ExecutionError) as exc:
            concat_masked_chunks(chunks, table="accounts")
        assert exc.value.code == "chunked_schema_mismatch"
        assert exc.value.message is not None
        assert "val" in exc.value.message
        assert "other" in exc.value.message

    def test_type_mismatch_lists_conflicting_types(self) -> None:
        """Chunks disagreeing on a column's non-null type raise
        chunked_schema_mismatch listing both TYPE names, comma-joined. Kills the
        `str(None)` per-type mutation (types become "None") and the `", "`
        separator (asserted as the exact joined substring)."""
        chunks = [
            pa.table({"val": pa.array(["a"], type=pa.string())}),
            pa.table({"val": pa.array(["b"], type=pa.large_string())}),
        ]
        with pytest.raises(ExecutionError) as exc:
            concat_masked_chunks(chunks, table="accounts")
        assert exc.value.code == "chunked_schema_mismatch"
        assert exc.value.message is not None
        assert "string, large_string" in exc.value.message


# ==========================================================================
# aggregate_chunk_timings -- elapsed SUMS, peak takes MAX, one record per
# (strategy, column). Driven with hand-built timing records.
# ==========================================================================


def _rec(strategy: str, column: str, elapsed: float, peak: int) -> StrategyTimingRecord:
    return StrategyTimingRecord(
        strategy_type=strategy, column=column, elapsed_ms=elapsed, peak_memory_delta_kb=peak
    )


def _fake_result(records: list[StrategyTimingRecord]) -> types.SimpleNamespace:
    return types.SimpleNamespace(timings=records)


class TestAggregateChunkTimings:
    def test_elapsed_sums_and_peak_maxes(self) -> None:
        """(hash, v) split across two chunks: elapsed sums (2.0 + 3.0 = 5.0),
        peak takes the max (max(5, 8) = 8). (redact, w) with peak 0 pins the
        zero-init. Kills: elapsed init 0.0->1.0, peak init 0->1, `+=`->`=`,
        `+=`->`-=`, `peak = None`, and elapsed/peak fields set to None in the
        emitted record."""
        results = [
            _fake_result([_rec("hash", "v", 2.0, 5), _rec("redact", "w", 1.0, 0)]),
            _fake_result([_rec("hash", "v", 3.0, 8)]),
        ]
        out = aggregate_chunk_timings(results)
        rolled = {(r.strategy_type, r.column): (r.elapsed_ms, r.peak_memory_delta_kb) for r in out}
        assert rolled == {("hash", "v"): (5.0, 8), ("redact", "w"): (1.0, 0)}

    def test_one_record_per_key_in_first_seen_order(self) -> None:
        """Order is first-emission; one record per (strategy, column)."""
        results = [
            _fake_result([_rec("hash", "v", 1.0, 2), _rec("fpe", "s", 1.0, 2)]),
            _fake_result([_rec("hash", "v", 1.0, 9)]),
        ]
        out = aggregate_chunk_timings(results)
        assert [(r.strategy_type, r.column) for r in out] == [("hash", "v"), ("fpe", "s")]
        assert out[0].elapsed_ms == 2.0
        assert out[0].peak_memory_delta_kb == 9


# ==========================================================================
# _warm_faker_pools -- builds each admitted faker column's pool ONCE into the
# pool cache. Observable via cache state (entries + the adapter hitting the
# warmed pool), which is the function's documented contract.
# ==========================================================================


class TestWarmFakerPools:
    _FAKER_COL = {
        "name": "c",
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": "n",
        "cardinality_mode": "reuse",
        "provider_config": {"pool_size": 8, "locale": "en_US"},
    }

    def _plan_and_env(self, tmp_path, columns, data):
        cfg = _config(tmp_path, columns)
        tbl = pa.Table.from_pandas(pd.DataFrame(data), preserve_index=False)
        profile = first_chunk_profile(tbl, table="accounts", engine_version=_EV)
        plan = compile_plan(cfg, profile, decoy_engine_version=_EV, no_profile=True)
        registry = get_default_registry()
        ns_registry = build_namespace_registry(cfg, profile)
        return plan, tbl, registry, ns_registry

    def test_warm_populates_cache(self, tmp_path) -> None:
        """Warming a single faker column builds exactly one pool into a fresh
        cache. Kills the mutations that build NOTHING: `table_seed = None`, the
        `name == table` -> `!=` and `is None` -> `is not None` inversions, and
        every strategy-skip flip (`!=`->`==`, `or`->`and`, literal rewrites,
        `is None`->`is not None`) that skips the faker column, plus the
        already-cached guard inverted so it always skips."""
        plan, _tbl, registry, _ns = self._plan_and_env(
            tmp_path, [self._FAKER_COL], {"c": [f"p{i}@x.com" for i in range(12)]}
        )
        cache = PoolCache()
        _warm_faker_pools(plan, table="accounts", registry=registry, pool_cache=cache)
        assert cache.stats().entries == 1

    def test_warm_skips_leading_nonfaker_column(self, tmp_path) -> None:
        """A non-faker column BEFORE the faker column must be skipped without
        aborting the loop. Kills the strategy-skip `continue`->`break` (which
        would stop at the hash column and never warm the faker)."""
        plan, _tbl, registry, _ns = self._plan_and_env(
            tmp_path,
            [{"name": "h", "strategy": "hash", "namespace": "hn"}, self._FAKER_COL],
            {"h": [f"h{i}" for i in range(6)], "c": [f"p{i}@x.com" for i in range(6)]},
        )
        cache = PoolCache()
        _warm_faker_pools(plan, table="accounts", registry=registry, pool_cache=cache)
        assert cache.stats().entries == 1

    def test_warm_missing_table_is_a_noop(self, tmp_path) -> None:
        """Warming a table absent from the plan returns without building or
        raising. Kills the `next(...)` no-default mutation (StopIteration)."""
        plan, _tbl, registry, _ns = self._plan_and_env(
            tmp_path, [self._FAKER_COL], {"c": [f"p{i}@x.com" for i in range(6)]}
        )
        cache = PoolCache()
        _warm_faker_pools(plan, table="ghost", registry=registry, pool_cache=cache)
        assert cache.stats().entries == 0

    def test_warmed_pool_is_hit_by_the_adapter(self, tmp_path) -> None:
        """The warmed pool must carry the SAME identity the per-chunk adapter
        looks up (right provider/size/locale/namespace), so the adapter HITS it
        (no rebuild, one cache entry). Kills the build-call param mutations that
        change the stored pool's identity: locale read nulled/renamed, locale
        kept in build_config, and namespace/locale set to None or dropped in the
        `build(...)` call."""
        plan, tbl, registry, ns_registry = self._plan_and_env(
            tmp_path, [self._FAKER_COL], {"c": [f"p{i}@x.com" for i in range(12)]}
        )
        cache = PoolCache()
        _warm_faker_pools(plan, table="accounts", registry=registry, pool_cache=cache)
        assert cache.stats().entries == 1
        adapter = PandasExecutionAdapter()
        adapter.run(
            plan,
            {"accounts": tbl},
            registry=registry,
            pool_cache=cache,
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=ns_registry,
        )
        assert cache.stats().hits >= 1
        assert cache.stats().entries == 1

    def test_warm_does_not_rebuild_an_already_cached_pool(self, tmp_path, monkeypatch) -> None:
        """With the pool already in the cache (built by a prior adapter run), the
        warmer's dedup guard must skip the build. Kills the identity_for param
        mutations (wrong dedup identity -> false miss -> rebuild), `identity =
        None`, and `pool_cache.get(None)` / the guard's `is not None`->`is None`
        flip, all of which force an unnecessary rebuild."""
        plan, tbl, registry, ns_registry = self._plan_and_env(
            tmp_path, [self._FAKER_COL], {"c": [f"p{i}@x.com" for i in range(12)]}
        )
        cache = PoolCache()
        # Populate the cache via a real adapter run (handler builds under the
        # canonical identity), then confirm the warmer finds it and does NOT
        # rebuild.
        adapter = PandasExecutionAdapter()
        adapter.run(
            plan,
            {"accounts": tbl},
            registry=registry,
            pool_cache=cache,
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=ns_registry,
        )
        assert cache.stats().entries == 1

        build_calls: list[int] = []
        original_build = poolmod.PoolBuilder.build

        def counting_build(self, *args, **kwargs):
            build_calls.append(1)
            return original_build(self, *args, **kwargs)

        monkeypatch.setattr(poolmod.PoolBuilder, "build", counting_build)
        _warm_faker_pools(plan, table="accounts", registry=registry, pool_cache=cache)
        assert build_calls == []

    # A faker column whose provider_config carries a key BEYOND pool_size/locale
    # (`domain`, a valid Faker email kwarg): build_config is NON-empty, so the
    # pool identity depends on it. The empty-build_config _FAKER_COL cannot expose
    # the identity_for config argument (config=None and config={} hash the same).
    _FAKER_COL_NONEMPTY_BUILD_CONFIG = {
        "name": "c",
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": "n",
        "cardinality_mode": "reuse",
        "provider_config": {"pool_size": 8, "locale": "en_US", "domain": "example.org"},
    }

    def test_warm_dedup_identity_includes_full_build_config(self, tmp_path, monkeypatch) -> None:
        """With a NON-empty build_config, the pre-warm dedup identity must be
        computed from the full build_config so an already-cached pool is found and
        not rebuilt. Kills the `identity_for(config=None)` and dropped-config
        mutations: they hash an empty config, so the dedup lookup misses the
        canonical pool and rebuilds it. (The empty-build_config warmer tests
        cannot catch this -- config None and {} produce the same config hash.)"""
        plan, tbl, registry, ns_registry = self._plan_and_env(
            tmp_path,
            [self._FAKER_COL_NONEMPTY_BUILD_CONFIG],
            {"c": [f"p{i}@x.com" for i in range(12)]},
        )
        cache = PoolCache()
        # Populate the cache via a real adapter run (handler builds under the
        # canonical identity, which includes the `domain` build_config).
        adapter = PandasExecutionAdapter()
        adapter.run(
            plan,
            {"accounts": tbl},
            registry=registry,
            pool_cache=cache,
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=ns_registry,
        )
        assert cache.stats().entries == 1

        build_calls: list[int] = []
        original_build = poolmod.PoolBuilder.build

        def counting_build(self, *args, **kwargs):
            build_calls.append(1)
            return original_build(self, *args, **kwargs)

        monkeypatch.setattr(poolmod.PoolBuilder, "build", counting_build)
        _warm_faker_pools(plan, table="accounts", registry=registry, pool_cache=cache)
        assert build_calls == []


# ==========================================================================
# run_mask_pipeline_chunked -- the mask-secret gate must be resolved and
# threaded to the per-chunk adapter, so a keyed job masks with the SECRET, not
# job_seed. Byte-parity against the full-frame keyed run is the oracle.
# ==========================================================================


class TestRunMaskKeyedGate:
    def test_keyed_job_masks_with_secret_matching_full_frame(self, tmp_path, monkeypatch) -> None:
        """A keyed config (mask_secret_ref -> a >=32-byte env secret) with a
        `hash` column: the chunked output must byte-match the full-frame keyed
        run. Kills the mask_secret_ref resolution mutations (the `key_provider is
        None` guard flip, `_ref=None`, the `or {}`->`and {}` and nulled/renamed
        config-key reads, `key_provider=None`, `key_provider_from_ref(None)`) and
        the per-chunk `adapter.run(key_provider=...)` being nulled/dropped -- all
        of which drop the secret so the chunk masks off job_seed (pre-GA
        fallback), diverging from the full-frame secret-keyed output."""
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        monkeypatch.setenv("DECOY_CHUNKED_MUTKILL_SECRET", "a" * 64)
        cfg = _validated(
            {
                "version": 1,
                "global_settings": {
                    "seed": 42,
                    "mask_secret_ref": "env:DECOY_CHUNKED_MUTKILL_SECRET",
                },
                "sources": {
                    "accounts": {"type": "file", "format": "csv", "path": str(tmp_path / "in.csv")}
                },
                "tables": [
                    {
                        "name": "accounts",
                        "columns": [{"name": "email", "strategy": "hash", "namespace": "e"}],
                    }
                ],
                "targets": {
                    "accounts": {
                        "type": "file",
                        "format": "csv",
                        "path": str(tmp_path / "out.csv"),
                    }
                },
            }
        )
        df = pd.DataFrame({"email": [f"user{i}@example.com" for i in range(24)]})
        df.to_csv(tmp_path / "in.csv", index=False)
        tbl = pa.Table.from_pandas(df, preserve_index=False)

        full = run_pipeline(cfg, sources={"accounts": tbl}, engine_version=_EV).outputs["accounts"]
        chunked = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(cfg, _chunks(df, 7), table="accounts", engine_version=_EV)
            )
        )
        assert chunked.to_pylist() == full.to_pylist()

    def test_keyed_output_actually_differs_from_seed_keyed(self, tmp_path, monkeypatch) -> None:
        """Guard for the oracle above: the secret must genuinely change the
        masked bytes versus a seed-only run, so the parity check is load-bearing
        (a dropped secret is observable)."""
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        monkeypatch.setenv("DECOY_CHUNKED_MUTKILL_SECRET", "b" * 64)
        base = {
            "version": 1,
            "sources": {
                "accounts": {"type": "file", "format": "csv", "path": str(tmp_path / "in.csv")}
            },
            "tables": [
                {
                    "name": "accounts",
                    "columns": [{"name": "email", "strategy": "hash", "namespace": "e"}],
                }
            ],
            "targets": {
                "accounts": {"type": "file", "format": "csv", "path": str(tmp_path / "out.csv")}
            },
        }
        keyed = _validated(
            {
                **base,
                "global_settings": {
                    "seed": 42,
                    "mask_secret_ref": "env:DECOY_CHUNKED_MUTKILL_SECRET",
                },
            }
        )
        seeded = _validated({**base, "global_settings": {"seed": 42}})
        df = pd.DataFrame({"email": [f"user{i}@example.com" for i in range(12)]})
        df.to_csv(tmp_path / "in.csv", index=False)
        tbl = pa.Table.from_pandas(df, preserve_index=False)
        keyed_out = run_pipeline(keyed, sources={"accounts": tbl}, engine_version=_EV).outputs[
            "accounts"
        ]
        seeded_out = run_pipeline(seeded, sources={"accounts": tbl}, engine_version=_EV).outputs[
            "accounts"
        ]
        assert keyed_out.to_pylist() != seeded_out.to_pylist()


# ==========================================================================
# run_mask_pipeline_chunked -- the per-chunk setup args (empty/first-chunk
# profile, the projection policy, and the FK-passthrough reject) must be
# threaded to the adapter.run() call and the guard with the right values.
# ==========================================================================


def _passthrough_fk_config() -> dict:
    """Minimal customers(id) -> orders(customer_id) passthrough-FK, REMAP orphan
    policy -- the shape MEDIUM #4 admits onto the chunked route (mirrors
    test_de10_chunked_fk_passthrough._passthrough_fk_config)."""
    return {
        "global_settings": {"seed": 7},
        "tables": [
            {
                "name": "customers",
                "columns": [{"name": "id", "strategy": "passthrough", "dtype": "int64"}],
            },
            {
                "name": "orders",
                "columns": [{"name": "customer_id", "strategy": "passthrough", "dtype": "int64"}],
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


class TestRunMaskChunkedCallSites:
    def test_lossy_passthrough_reject_names_the_table(self) -> None:
        """A lossy (null-bearing, > 2**53) passthrough FK column fails closed with
        the offending TABLE named in the message. Kills
        reject_lossy_chunked_fk_passthrough(table=None) (the message would read
        'Column None.customer_id ...'); the declared-dtype twin already asserts the
        table name, so this closes the same gap for the passthrough guard."""
        config = _passthrough_fk_config()
        chunk = pa.table({"customer_id": pa.array([1, None, 9007199254740993], type=pa.int64())})
        with pytest.raises(ExecutionError) as exc:
            list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_EV))
        assert exc.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE
        assert "orders" in exc.value.message

    def test_polars_nonnative_table_still_applies_fk_passthrough_guard(self) -> None:
        """On a POLARS adapter whose table carries a non-native strategy
        (`top_code`), the chunk falls back to the pandas oracle's unprotected
        ingestion, so the lossy-passthrough FK guard MUST still fire. Kills
        `chunked_adapter_touches_pandas_ingestion(adapter, config, None)` (mut_88):
        with `table=None` the polars native-check matches no table, sees an empty
        strategy set, wrongly reports the adapter never touches pandas, and the
        guard is skipped -- the null-bearing big-int passthrough FK then rounds
        silently instead of failing closed. `table` is load-bearing only on this
        polars branch (the pandas adapter returns True before reading it), which is
        why the pandas-route reject tests above cannot reach this mutant."""
        pytest.importorskip("polars")
        from decoy_engine.execution.polars._polars_adapter import PolarsExecutionAdapter

        config = _passthrough_fk_config()
        # A non-native (top_code) column on `orders` forces the polars adapter to
        # the pandas oracle for this table, so the pandas-ingestion guard applies.
        config["tables"][1]["columns"].append(
            {"name": "age", "strategy": "top_code", "provider_config": {"preset": "hipaa_age"}}
        )
        chunk = pa.table(
            {
                "customer_id": pa.array([1, None, 9007199254740993], type=pa.int64()),
                "age": pa.array([40, 55, 92], type=pa.int64()),
            }
        )
        with pytest.raises(ExecutionError) as exc:
            list(
                run_mask_pipeline_chunked(
                    config,
                    [chunk],
                    table="orders",
                    engine_version=_EV,
                    adapter=PolarsExecutionAdapter(),
                )
            )
        assert exc.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE

    def test_unconfigured_error_policy_threaded_to_each_chunk(self, tmp_path) -> None:
        """An explicit `error` policy plus a chunk column the config never declares:
        the resolved projection policy must reach the per-chunk adapter.run so the
        undeclared column fails closed. Kills projection_policy=None,
        resolve_unconfigured_column_policy(None), and the adapter.run
        unconfigured_column_policy nulled/dropped mutations -- each drops the error
        policy to the pre-GA warn default, so the raw column passes through
        silently instead of raising."""
        cfg = _validated(
            {
                "version": 1,
                "global_settings": {"seed": 42, "unconfigured_column_policy": "error"},
                "sources": {
                    "accounts": {"type": "file", "format": "csv", "path": str(tmp_path / "in.csv")}
                },
                "tables": [
                    {
                        "name": "accounts",
                        "columns": [{"name": "email", "strategy": "hash", "namespace": "e"}],
                    }
                ],
                "targets": {
                    "accounts": {
                        "type": "file",
                        "format": "csv",
                        "path": str(tmp_path / "out.csv"),
                    }
                },
            }
        )
        chunk = pa.table(
            {"email": pa.array(["a@x.com", "b@x.com"]), "note": pa.array(["n1", "n2"])}
        )
        with pytest.raises(ExecutionError) as exc:
            list(run_mask_pipeline_chunked(cfg, [chunk], table="accounts", engine_version=_EV))
        assert exc.value.code == "undeclared_output_columns"

    def test_empty_input_gate_profiles_the_real_table(self, tmp_path, monkeypatch) -> None:
        """The zero-chunk fail-closed gate must profile the REAL table so a keyed
        job's keyed strategy stays visible to require_mask_key. Kills
        empty_input_profile(table=None): a None-named profile table drops the
        config's keyed column from the seed envelope, so the GA gate no longer sees
        a keyed surface and silently returns an empty stream instead of demanding a
        secret. (Pre-GA the gate accepts job_seed, so GA is where the profile's
        table name is load-bearing.)"""
        monkeypatch.setattr(release, "RELEASE_PHASE", "ga")
        cfg = _config(tmp_path, [{"name": "email", "strategy": "hash", "namespace": "e"}])
        with pytest.raises(KeyedStrategyRequiresSecret):
            list(run_mask_pipeline_chunked(cfg, [], table="accounts", engine_version=_EV))
