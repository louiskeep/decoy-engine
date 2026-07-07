"""P1/P4 (job-performance sprints): `run_pipeline` substrate routing + knobs.

P1 routes `run_pipeline`'s mask-kind execution through
`select_execution_adapter()` instead of a hardcoded
`PandasExecutionAdapter()`; P4 exposes the selection knobs
(`substrate`, `fpe_chunk_count`, `max_workers`, `fallback_to_pandas`)
as `run_pipeline` keyword parameters.

The load-bearing contract pinned here: the DEFAULT call is byte-identical
to the pre-P1 hardcoded pandas path. `resolve_substrate`'s S13 default
flip to polars must NOT leak into `run_pipeline`'s default -- the
`substrate` parameter defaults to `"pandas"`, and `substrate=None` is the
explicit opt-in to env-resolved (`DECOY_SUBSTRATE`) selection. Every test
pins or clears `DECOY_SUBSTRATE` so the suite is hermetic under the CI
substrate matrix (which exports `DECOY_SUBSTRATE=polars`).
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import ExecutionError, run_pipeline
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution.polars._polars_adapter import PolarsExecutionAdapter

_ENGINE_VERSION = "p1p4-substrate-test"


def _validated_dump(cfg: dict) -> dict:
    return PipelineConfig.model_validate(cfg).model_dump()


def _scalar_mask_config(tmp_path) -> dict:
    """Scalar no-FK mask config over version-stable strategies (hash /
    truncate / redact), so golden values do not move with Faker releases."""
    return _validated_dump(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                "customers": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "customers.csv"),
                },
            },
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {"name": "email", "strategy": "hash", "namespace": "email_ns"},
                        {"name": "zip", "strategy": "truncate", "provider_config": {"length": 3}},
                        {"name": "secret", "strategy": "redact"},
                    ],
                },
            ],
            "targets": {
                "customers": {"type": "file", "format": "csv", "path": str(tmp_path / "out.csv")},
            },
        }
    )


def _scalar_mask_sources(tmp_path) -> dict[str, pa.Table]:
    df = pd.DataFrame(
        {
            "email": ["a@x.com", "b@x.com", "c@x.com"],
            "zip": ["90210", "10001", "60601"],
            "secret": ["alpha", "beta", "gamma"],
        }
    )
    df.to_csv(tmp_path / "customers.csv", index=False)
    return {"customers": pa.Table.from_pandas(df, preserve_index=False)}


def _fk_mask_config(tmp_path) -> dict:
    """Parent/child FK mask config: the polars adapter cannot run FK
    resolution natively and must route through the pandas oracle."""
    return _validated_dump(
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
                    "columns": [{"name": "customer_id", "strategy": "hash", "namespace": "id_ns"}],
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
                "customers": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "customers_out.csv"),
                },
                "orders": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "orders_out.csv"),
                },
            },
        }
    )


def _fk_mask_sources(tmp_path) -> dict[str, pa.Table]:
    customers = pd.DataFrame({"id": ["C1", "C2", "C3"]})
    orders = pd.DataFrame({"customer_id": ["C1", "C1", "C2"]})
    customers.to_csv(tmp_path / "customers.csv", index=False)
    orders.to_csv(tmp_path / "orders.csv", index=False)
    return {
        "customers": pa.Table.from_pandas(customers, preserve_index=False),
        "orders": pa.Table.from_pandas(orders, preserve_index=False),
    }


@pytest.fixture
def select_spy(monkeypatch):
    """Wrap `select_execution_adapter` so tests can assert the forwarded
    knobs and the concrete adapter `run_pipeline` selected."""
    from decoy_engine.execution import _substrate

    calls: list[dict[str, Any]] = []
    real = _substrate.select_execution_adapter

    def spy(**kwargs):
        adapter = real(**kwargs)
        calls.append({**kwargs, "adapter": adapter})
        return adapter

    monkeypatch.setattr(_substrate, "select_execution_adapter", spy)
    return calls


@pytest.fixture
def forbid_profiling(monkeypatch):
    """Fail the test if run_pipeline reaches profile_source: knob errors
    must surface before any profiling/compile work (P4 fail-early)."""
    import decoy_engine.profile as profile_mod

    def bomb(*args, **kwargs):
        raise AssertionError("profile_source ran before knob validation")

    monkeypatch.setattr(profile_mod, "profile_source", bomb)


# Captured from the pre-P1 hardcoded `PandasExecutionAdapter()` path
# (seed 42): the default route must keep producing exactly these bytes.
_GOLDEN_PRE_P1 = {
    "email": [
        "d86d99770dbd6506ff8dea0d67bd501d63cdfec4dd5b02aa518233c23228f69e",
        "783fb9d532f88b4a580d7cd350e1cdc3e0e2bcfe74478a1a5cf1fb7f07b46406",
        "56ea87a5865feff0d4c79e98043d3055867894527710ab7f130afe633592bece",
    ],
    "zip": ["902", "100", "606"],
    "secret": ["REDACTED", "REDACTED", "REDACTED"],
}
# Arrow's string-width label (`string` vs `large_string`) is pandas-major-version
# dependent (pandas 2 emits `string`, pandas 3 `large_string`) for wide columns and
# carries no semantic meaning — the same drift the polars-parity test below accepts.
# The suite runs across both (CI Python 3.10 -> pandas 2; local 3.11+ -> pandas 3), so
# normalize the width before comparing while still catching real type changes.
_GOLDEN_PRE_P1_SCHEMA_TYPES = ["string", "string", "string"]


def _schema_type_names(table):
    return [str(field.type).replace("large_string", "string") for field in table.schema]


# --------------------------------------------------------------------------
# P1: the default route stays pandas and byte-identical
# --------------------------------------------------------------------------


class TestDefaultPandasRoute:
    def test_default_selects_pandas_adapter_with_default_knobs(
        self, tmp_path, monkeypatch, select_spy
    ):
        monkeypatch.setenv("DECOY_SUBSTRATE", "polars")  # must be ignored by default
        cfg = _scalar_mask_config(tmp_path)
        sources = _scalar_mask_sources(tmp_path)
        run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
        assert len(select_spy) == 1
        call = select_spy[0]
        assert isinstance(call["adapter"], PandasExecutionAdapter)
        assert call["substrate"] == "pandas"
        assert call["fpe_chunk_count"] == 4
        assert call["max_workers"] == 4
        assert call["fallback_to_pandas"] is True

    def test_default_output_matches_pre_change_golden(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg = _scalar_mask_config(tmp_path)
        sources = _scalar_mask_sources(tmp_path)
        result = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
        out = result.outputs["customers"]
        assert out.to_pydict() == _GOLDEN_PRE_P1
        assert _schema_type_names(out) == _GOLDEN_PRE_P1_SCHEMA_TYPES

    def test_default_output_unchanged_when_env_requests_polars(self, tmp_path, monkeypatch):
        """The S13 DECOY_SUBSTRATE default flip must not leak into the
        default `run_pipeline` route (pre-P1 it hardcoded pandas)."""
        monkeypatch.setenv("DECOY_SUBSTRATE", "polars")
        cfg = _scalar_mask_config(tmp_path)
        sources = _scalar_mask_sources(tmp_path)
        result = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
        out = result.outputs["customers"]
        assert out.to_pydict() == _GOLDEN_PRE_P1
        assert _schema_type_names(out) == _GOLDEN_PRE_P1_SCHEMA_TYPES

    def test_default_quality_metrics_carry_no_adapter_block(self, tmp_path, monkeypatch):
        """Byte-identity extends to metadata: default knobs stamp no
        `execution_adapter` block. S2 (landed on the integration branch
        ahead of this P1 test) unconditionally stamps a
        `quality_metrics["execution"]` honesty-telemetry block on every
        full_frame run, so the original P1 golden (`quality_metrics == {}`)
        no longer holds verbatim; the load-bearing assertion here -- that
        all-default knobs add no *adapter-selection* metadata -- still
        does.
        """
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg = _scalar_mask_config(tmp_path)
        sources = _scalar_mask_sources(tmp_path)
        result = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
        assert set(result.quality_metrics) == {"execution"}
        assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"


class TestEnvResolvedSubstrate:
    def test_substrate_none_honors_env_pandas(self, tmp_path, monkeypatch, select_spy):
        monkeypatch.setenv("DECOY_SUBSTRATE", "pandas")
        cfg = _scalar_mask_config(tmp_path)
        sources = _scalar_mask_sources(tmp_path)
        run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION, substrate=None)
        assert isinstance(select_spy[0]["adapter"], PandasExecutionAdapter)

    def test_substrate_none_honors_env_polars(self, tmp_path, monkeypatch, select_spy):
        monkeypatch.setenv("DECOY_SUBSTRATE", "polars")
        cfg = _scalar_mask_config(tmp_path)
        sources = _scalar_mask_sources(tmp_path)
        run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION, substrate=None)
        assert isinstance(select_spy[0]["adapter"], PolarsExecutionAdapter)


# --------------------------------------------------------------------------
# P1: explicit polars route -- native scalar parity + FK fallback
# --------------------------------------------------------------------------


class TestPolarsRoute:
    def test_polars_scalar_no_fk_runs_native_and_matches_pandas_values(self, tmp_path, monkeypatch):
        """Value parity per the v2 substrate contract: `to_pydict()`
        equality (schema-level string-width drift is the accepted
        difference, per SEMANTIC_DIFFERENCES.md)."""
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg = _scalar_mask_config(tmp_path)
        sources = _scalar_mask_sources(tmp_path)
        pandas_result = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, substrate="pandas"
        )
        polars_result = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, substrate="polars"
        )
        assert (
            polars_result.outputs["customers"].to_pydict()
            == pandas_result.outputs["customers"].to_pydict()
        )
        executed = polars_result.quality_metrics["executed_substrate"]
        assert executed == {"hash": "polars", "truncate": "polars", "redact": "polars"}

    def test_polars_fk_job_falls_back_to_pandas_oracle(self, tmp_path, monkeypatch):
        """FK resolution is not polars-native: the executed substrate of
        record must be pandas and the values must match the pandas run."""
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg = _fk_mask_config(tmp_path)
        sources = _fk_mask_sources(tmp_path)
        pandas_result = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, substrate="pandas"
        )
        polars_result = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, substrate="polars"
        )
        for table in ("customers", "orders"):
            assert (
                polars_result.outputs[table].to_pydict() == pandas_result.outputs[table].to_pydict()
            ), f"{table} diverged from the pandas oracle"
        executed = polars_result.quality_metrics["executed_substrate"]
        assert set(executed.values()) == {"pandas"}
        # FK integrity survives the fallback: child keys resolve through
        # the parent map, so masked child values appear in the parent.
        masked_parent = set(polars_result.outputs["customers"].column("id").to_pylist())
        masked_child = set(polars_result.outputs["orders"].column("customer_id").to_pylist())
        assert masked_child <= masked_parent

    def test_polars_fallback_disabled_fk_job_raises_typed(self, tmp_path, monkeypatch):
        """`fallback_to_pandas=False` keeps its existing hard-error
        semantics when routed via run_pipeline (no silent downgrade)."""
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg = _fk_mask_config(tmp_path)
        sources = _fk_mask_sources(tmp_path)
        with pytest.raises(ExecutionError) as exc:
            run_pipeline(
                cfg,
                sources=sources,
                engine_version=_ENGINE_VERSION,
                substrate="polars",
                fallback_to_pandas=False,
            )
        assert exc.value.code == "polars_substrate_strategy_unmigrated"


# --------------------------------------------------------------------------
# P4: knob forwarding, fail-early validation, and metadata stamping
# --------------------------------------------------------------------------


class TestKnobForwarding:
    def test_knobs_forward_to_adapter_selection(self, tmp_path, monkeypatch, select_spy):
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg = _scalar_mask_config(tmp_path)
        sources = _scalar_mask_sources(tmp_path)
        run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            substrate="pandas",
            fpe_chunk_count=8,
            max_workers=2,
            fallback_to_pandas=False,
        )
        call = select_spy[0]
        assert call["substrate"] == "pandas"
        assert call["fpe_chunk_count"] == 8
        assert call["max_workers"] == 2
        assert call["fallback_to_pandas"] is False


class TestKnobValidation:
    def test_invalid_substrate_fails_early_with_typed_error(
        self, tmp_path, monkeypatch, forbid_profiling
    ):
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg = _scalar_mask_config(tmp_path)
        sources = _scalar_mask_sources(tmp_path)
        with pytest.raises(ExecutionError) as exc:
            run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION, substrate="duckdb")
        assert exc.value.code == "invalid_substrate"

    @pytest.mark.parametrize(
        ("knob", "value"),
        [
            ("fpe_chunk_count", 0),
            ("fpe_chunk_count", -1),
            ("fpe_chunk_count", True),
            ("max_workers", 0),
            ("max_workers", False),
        ],
    )
    def test_invalid_int_knob_fails_early_with_typed_error(
        self, tmp_path, monkeypatch, forbid_profiling, knob, value
    ):
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg = _scalar_mask_config(tmp_path)
        sources = _scalar_mask_sources(tmp_path)
        with pytest.raises(ExecutionError) as exc:
            run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION, **{knob: value})
        assert exc.value.code == "invalid_execution_knob"
        assert knob in str(exc.value)

    def test_select_execution_adapter_rejects_invalid_int_knobs(self):
        from decoy_engine.execution._substrate import select_execution_adapter

        with pytest.raises(ExecutionError) as exc:
            select_execution_adapter(substrate="pandas", fpe_chunk_count=0)
        assert exc.value.code == "invalid_execution_knob"

    @pytest.mark.parametrize("bad", ["false", "true", 0, 1, None])
    def test_non_bool_fallback_to_pandas_fails_typed(self, bad):
        # A str like "false" is truthy, so an untyped job-payload value would
        # silently invert fail-closed intent; reject anything non-bool.
        from decoy_engine.execution._substrate import select_execution_adapter

        with pytest.raises(ExecutionError) as exc:
            select_execution_adapter(substrate="polars", fallback_to_pandas=bad)
        assert exc.value.code == "invalid_execution_knob"

    @pytest.mark.parametrize("bad", [123, True, ["polars"]])
    def test_non_str_substrate_fails_typed(self, bad):
        # substrate is now a public run_pipeline kwarg, so a non-str must raise
        # the coded error rather than an opaque AttributeError from .strip().
        from decoy_engine.execution._substrate import resolve_substrate

        with pytest.raises(ExecutionError) as exc:
            resolve_substrate(bad)
        assert exc.value.code == "invalid_substrate"


class TestNonDefaultKnobMetadata:
    def test_non_default_substrate_stamps_execution_adapter_block(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg = _scalar_mask_config(tmp_path)
        sources = _scalar_mask_sources(tmp_path)
        result = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, substrate="polars"
        )
        block = result.quality_metrics["execution_adapter"]
        assert block["adapter_name"] == "polars"
        assert isinstance(block["adapter_version"], str)
        assert block["requested_substrate"] == "polars"
        assert block["resolved_substrate"] == "polars"
        assert block["fpe_chunk_count"] == 4
        assert block["max_workers"] == 4
        assert block["fallback_to_pandas"] is True

    def test_non_default_int_knob_stamps_block_on_pandas(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
        cfg = _scalar_mask_config(tmp_path)
        sources = _scalar_mask_sources(tmp_path)
        result = run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, fpe_chunk_count=8
        )
        block = result.quality_metrics["execution_adapter"]
        assert block["adapter_name"] == "pandas"
        assert block["requested_substrate"] == "pandas"
        assert block["resolved_substrate"] == "pandas"
        assert block["fpe_chunk_count"] == 8

    def test_substrate_none_env_resolved_stamps_resolution(self, tmp_path, monkeypatch):
        """Operators must be able to reproduce the performance mode from
        metadata: env-resolved selection records requested None + the
        resolved substrate."""
        monkeypatch.setenv("DECOY_SUBSTRATE", "pandas")
        cfg = _scalar_mask_config(tmp_path)
        sources = _scalar_mask_sources(tmp_path)
        result = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION, substrate=None)
        block = result.quality_metrics["execution_adapter"]
        assert block["requested_substrate"] is None
        assert block["resolved_substrate"] == "pandas"
        assert block["adapter_name"] == "pandas"
