"""
Tests for the public API surface declared in decoy_engine.__init__.

These tests guard the contract that CLI and platform code depend on. If
a name disappears from __all__ or its import path changes, that is a
breaking change and these tests should fail.
"""

import logging

import pytest

import decoy_engine
from decoy_engine import (
    ConfigError,
    ConnectorAuthError,
    ConnectorError,
    DecoyError,
    ExecutionContext,
    LicenseError,
    LicenseExpiredError,
    LicenseVerifier,
    Logger,
    PipelineValidationError,
    SchemaInspector,
)


def test_all_lists_every_public_name():
    expected = {
        # engine-v2 S8 composite generators
        "BundlePool",
        "CompositeAdapter",
        "CompositeError",
        "CompositeGenerator",
        "composite_city_state_zip",
        "composite_name_email",
        # engine-v2 S1 (Pipeline config schema, the validation choke-point).
        "PipelineConfig",
        # S9: Masker + DataGenerator removed from the public surface. The
        # platform mask + generate paths run through the v2 ExecutionAdapter +
        # generation.synthesize directly; graph-mode keeps the run_graph /
        # preview_graph / validate_graph_full / normalize_config exports.
        "ExecutionContext",
        "Logger",
        "StructuredEvents",
        "TelemetryClient",
        "emit_step",
        "emit_lineage",
        "emit_fidelity",
        "emit_quarantine",
        "emit_throughput_sample",
        "make_key_resolver",
        "SchemaInspector",
        "LicenseVerifier",
        "validate_config",
        "validate_graph",
        "validate_graph_full",
        "normalize_config",
        "run_graph",
        "preview_graph",
        "RunResult",
        "PreviewResult",
        "VALIDATION_CODES",
        "ValidationMessage",
        "ValidationResult",
        "run_storm",
        "StormProfile",
        "FieldStats",
        "DetectorMatch",
        "SentinelFlag",
        "run_discovery_sql",
        "DiscoveryResult",
        "DiscoverySqlError",
        "recommend",
        "ForecastReport",
        "DisguiseRecommendation",
        "FieldRecommendation",
        "RiskFlag",
        "DecoyError",
        "ConfigError",
        "PipelineValidationError",
        "ValidationError",
        "ConnectorError",
        "ConnectorAuthError",
        "LicenseError",
        "LicenseExpiredError",
        "FlagPauseSignal",
        "register_faker_provider",
        "register_faker_list_provider",
        "unregister_faker_provider",
        "load_custom_providers",
        # Connector SDK (Sprint G Week 1).
        "SDK_VERSION",
        "CAP_STREAMING",
        "CAP_RESUMABLE",
        "CAP_SIGNED_URL",
        "CAP_MULTIPART",
        "CAP_INTROSPECTION",
        "CAP_DRY_RUN",
        "ConnectorConfig",
        "FileMeta",
        "CheckResult",
        "WriteResult",
        "FileSource",
        "FileSink",
        "TransientError",
        "PermanentError",
        # engine-v2 S2 (Relationship Coordinator + Namespace).
        "NamespaceBinding",
        "NamespaceConfigError",
        "NamespaceRegistry",
        "OrphanPolicy",
        "RelationshipEdge",
        "RelationshipGraph",
        "build_namespace_registry",
        "build_relationship_graph",
        "check_orphan_fk_policy_completeness",
        # engine-v2 S3 (Determinism Layer).
        "SEED_PROTOCOL_VERSION",
        "DeterminismError",
        "Domain",
        "IdentityDomain",
        "derive",
        "derive_index",
        "derive_value",
        # engine-v2 S4 (Provider Registry + Faker Adapter).
        "AdapterError",
        "BackendAdapter",
        "CapabilityMatrix",
        "ProviderError",
        "ProviderRegistry",
        "ProviderSpec",
        "get_default_registry",
        "register_faker_provider_v2",
        # engine-v2 S5 (Pool Manager).
        "CardinalityMode",
        "GenerationError",
        "PoolAdapter",
        "PoolBuilder",
        "PoolCache",
        "PoolCapacityError",
        "PoolSampler",
        "QualityWarning",
        "ValuePool",
        "get_default_pool_cache",
        # engine-v2 S6 (Custom Identifier Generators).
        "EinAdapter",
        "EinDomain",
        "EinValidator",
        "IdentifierError",
        "IdentifierFormatError",
        "MrnAdapter",
        "MrnDomain",
        "MrnValidator",
        "NdcAdapter",
        "NdcDomain",
        "NdcValidator",
        "NpiAdapter",
        "NpiDomain",
        "NpiValidator",
        "SsnAdapter",
        "SsnDomain",
        "SsnValidator",
        # engine-v2 S9 (Execution Adapter, pandas).
        "ExecutionAdapter",
        "ExecutionError",
        "ExecutionEvent",
        "ExecutionResult",
        "PandasExecutionAdapter",
        "StrategyError",
        "get_default_executor",
        # engine-v2 S10 (Validator: compile consolidator + post-validation).
        "PlanCheckError",
        "PlanValidationResult",
        "validate_plan",
        "PostValidationRunner",
        "QualitySummary",
        "DistinctCount",
        "NullCount",
        "FkValidityReport",
        "CompositeCoherenceReport",
    }
    assert set(decoy_engine.__all__) == expected


def test_version_attribute_exists():
    assert isinstance(decoy_engine.__version__, str)


class TestExceptions:
    def test_config_error_subclasses_decoy_error(self):
        assert issubclass(ConfigError, DecoyError)

    def test_pipeline_validation_error_subclasses_config_error(self):
        assert issubclass(PipelineValidationError, ConfigError)

    def test_connector_error_subclasses_decoy_error(self):
        assert issubclass(ConnectorError, DecoyError)

    def test_connector_auth_error_subclasses_connector_error(self):
        assert issubclass(ConnectorAuthError, ConnectorError)

    def test_license_expired_error_subclasses_license_error(self):
        assert issubclass(LicenseExpiredError, LicenseError)


class TestLoggerProtocol:
    def test_stdlib_logger_satisfies_protocol(self):
        log = logging.getLogger("decoy_engine.test")
        assert isinstance(log, Logger)

    def test_object_missing_method_does_not_satisfy_protocol(self):
        class Incomplete:
            def info(self, msg):
                pass

            def warning(self, msg):
                pass

            def error(self, msg):
                pass

            # missing debug

        assert not isinstance(Incomplete(), Logger)


class TestExecutionContext:
    def test_default_construction(self):
        ctx = ExecutionContext()
        assert ctx.logger is None
        assert ctx.telemetry is None

    def test_logger_injection(self):
        log = logging.getLogger("decoy_engine.test")
        ctx = ExecutionContext(logger=log)
        assert ctx.logger is log


class TestStubs:
    def test_license_verifier_returns_free_tier(self):
        result = LicenseVerifier.verify()
        assert result["tier"] == "free"
        assert result["expires_at"] is None

    def test_schema_inspector_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            SchemaInspector()


# S11-CL-DEADCODE: V2-behavior regression pins for the four QA findings the
# rev 2 plan classified as "verified-fixed on V2" (Q8, Q9, Q13, Q14). Each
# pin asserts the V2 strategy's behavior, not the absence of legacy code --
# the V1 `transforms/` package stays on disk (3 V2 strategies REUSE it per
# best-practices §6.2) and the pins survive any future cleanup decision.
# See `docs/audit/dennis-s11-rescope-2026-05-30.md` for the triage rationale.
class TestV2BehaviorRegressionPinsS11:
    def _ctx(self):
        from decoy_engine.execution._adapter import StrategyContext
        from decoy_engine.generation.pool._cache import PoolCache
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships._graph import RelationshipGraph
        from decoy_engine.relationships._namespace import NamespaceRegistry

        return StrategyContext(
            registry=get_default_registry(),
            pool_cache=PoolCache(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=NamespaceRegistry(bindings=()),
            job_seed=(0xC0FFEE).to_bytes(8, "big"),
        )

    def _column_seed(self, *, namespace, strategy, provider=None, deterministic=True,
                     cardinality_mode="reuse"):
        from decoy_engine.plan._types import ColumnSeed

        return ColumnSeed(
            namespace=namespace,
            strategy=strategy,
            provider=provider,
            backend_type="faker",
            backend_version="v",
            cardinality_mode=cardinality_mode,
            deterministic=deterministic,
            provider_config=(),
            coherent_with=(),
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Q13 carries forward on V2 _shuffle.py despite Dennis rev 2 plan's "
            "'verified-fixed on V2' triage. `_shuffle.py:55` does "
            "`df[column] = out` where out is a list[object] of ints + None; "
            "pandas re-infers float64 on assignment when nulls mix with ints, "
            "reproducing the V1 anti-pattern (QA report Q13). Fix is a one-line "
            "wrap: `df[column] = pd.Series(out, dtype=object, index=df.index)` "
            "or equivalent. Escalated to Dennis for triage on 2026-05-30; "
            "carrying to S21-QA-FIXES (or earlier if Dennis re-scopes). When "
            "the fix lands the xfail flips to expected-pass."
        ),
    )
    def test_v2_shuffle_preserves_or_uses_object_dtype(self):
        """Q13 regression pin: V2 shuffle output dtype is object (or the input
        nullable dtype) -- never silent float64 promotion. The V1 carrier was
        `transforms/shuffle.py` which set `shuffled[na_mask] = None` against
        `dtype=column.dtype`, promoting int64 to float64. V2
        `execution/_strategies/_shuffle.py` was supposed to fix this via
        `list[object]` output, but `df[column] = out` re-infers dtype on
        assignment, so the V1 bug carries forward in V2 today.
        """
        import pandas as pd

        from decoy_engine.execution._strategies._shuffle import ShuffleStrategyHandler

        df = pd.DataFrame({"x": pd.array([1, None, 3, None, 5], dtype="Int64")})
        plan = self._column_seed(namespace="x_ns", strategy="shuffle")
        out_df, _ = ShuffleStrategyHandler().run(df, "x", plan, self._ctx())
        # The V2 contract claim: output is object dtype (list[object]) OR
        # the input nullable extension type. Never raw float64 (the V1 bug).
        assert out_df["x"].dtype == object or str(out_df["x"].dtype) == "Int64"
        # Spot-check: nulls in the input are preserved at the same positions.
        for pos in (1, 3):
            assert pd.isna(out_df["x"].iloc[pos]) or out_df["x"].iloc[pos] is None

    def test_v2_faker_uses_pool_sampler_not_per_row_apply(self, monkeypatch):
        """Q8 regression pin: V2 faker calls PoolSampler.sample once per
        column (vectorized), not per row. The V1 carrier was
        `transforms/faker_based.py`'s `column.apply(fake_for)`. V2
        `execution/_strategies/_faker.py:57` calls `PoolSampler().sample(...)`
        once, then assigns the materialized list to the column.
        """
        import pandas as pd

        from decoy_engine.execution._strategies._faker import FakerStrategyHandler
        from decoy_engine.generation.pool import PoolSampler

        original = PoolSampler.sample
        calls = []

        def counting_sample(self, *args, **kwargs):
            calls.append(1)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(PoolSampler, "sample", counting_sample)

        df = pd.DataFrame({"name": [f"user_{i}" for i in range(100)]})
        plan = self._column_seed(
            namespace="name_ns", strategy="faker", provider="person_name",
        )
        FakerStrategyHandler().run(df, "name", plan, self._ctx())
        assert len(calls) == 1, (
            f"V2 faker called PoolSampler.sample {len(calls)} times for a "
            "single 100-row column run; expected exactly 1 (per-pool, not "
            "per-row). Q8 regression."
        )

    def test_v2_strategy_context_requires_explicit_job_seed(self):
        """Q14 regression pin: the V2 strategy substrate requires an explicit
        `job_seed` on `StrategyContext`. There is no default-42 (or any
        default) fallback. The V1 carrier was `masker/masker.py`:
        `seed = self.config.get("global_settings", {}).get("seed", 42)`. V2
        `StrategyContext` is a frozen dataclass with `job_seed: bytes` and
        no default, so constructing it without that field raises TypeError.
        """
        from decoy_engine.execution._adapter import StrategyContext
        from decoy_engine.generation.pool._cache import PoolCache
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships._graph import RelationshipGraph
        from decoy_engine.relationships._namespace import NamespaceRegistry

        with pytest.raises(TypeError):
            StrategyContext(
                registry=get_default_registry(),
                pool_cache=PoolCache(),
                relationship_graph=RelationshipGraph(edges=(), ordering=()),
                namespace_registry=NamespaceRegistry(bindings=()),
                # job_seed deliberately omitted; the V2 contract requires it.
            )

    def test_v2_strategies_derive_per_strategy_namespace(self, monkeypatch):
        """Q9 regression pin: each V2 strategy binds the column's namespace
        into its `derive(job_seed, namespace, source)` call. The V1 carrier
        was `transforms/{hash, date_shift}.py` both calling
        `derive_key("mask")` with the same literal label, sharing the
        derived subkey across strategies. V2 strategies (`_hash.py:55`,
        `_date_shift.py:63`) pass `plan.namespace` -- different per column
        and per strategy by construction.
        """
        import pandas as pd

        from decoy_engine.execution._strategies import _date_shift, _hash

        recorded = []
        original_hash_derive = _hash.derive
        original_ds_derive = _date_shift.derive

        def recording_hash_derive(job_seed, namespace, source_bytes):
            recorded.append(("hash", namespace))
            return original_hash_derive(job_seed, namespace, source_bytes)

        def recording_ds_derive(job_seed, namespace, source_bytes):
            recorded.append(("date_shift", namespace))
            return original_ds_derive(job_seed, namespace, source_bytes)

        monkeypatch.setattr(_hash, "derive", recording_hash_derive)
        monkeypatch.setattr(_date_shift, "derive", recording_ds_derive)

        df_a = pd.DataFrame({"a": ["alice@example.com", "bob@example.com"]})
        plan_a = self._column_seed(namespace="A_ns", strategy="hash")
        _hash.HashStrategyHandler().run(df_a, "a", plan_a, self._ctx())

        df_b = pd.DataFrame({"b": ["2020-01-01", "2021-06-15"]})
        plan_b = self._column_seed(namespace="B_ns", strategy="date_shift",
                                    cardinality_mode="reuse")
        _date_shift.DateShiftStrategyHandler().run(df_b, "b", plan_b, self._ctx())

        hash_namespaces = {ns for kind, ns in recorded if kind == "hash"}
        ds_namespaces = {ns for kind, ns in recorded if kind == "date_shift"}
        assert hash_namespaces == {"A_ns"}, (
            f"V2 hash strategy must derive with its column's namespace; got {hash_namespaces}"
        )
        assert ds_namespaces == {"B_ns"}, (
            f"V2 date_shift strategy must derive with its column's namespace; got {ds_namespaces}"
        )
        # Critical Q9 assertion: no shared "mask" literal across strategies.
        assert "mask" not in hash_namespaces and "mask" not in ds_namespaces, (
            "V2 strategies must NOT derive with a shared 'mask' label; Q9 regression."
        )
