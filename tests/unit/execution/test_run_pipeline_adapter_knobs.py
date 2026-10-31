"""`run_pipeline`'s adapter knob after the pre-GA removal of caller-chosen
substrates: pandas is the only adapter (the parity oracle), `fpe_chunk_count`
is the one live adapter knob, and the former `substrate` / `max_workers` /
`fallback_to_pandas` kwargs are gone (a caller passing them gets a TypeError,
never a silently ignored knob).
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import ExecutionError, run_pipeline, select_execution_adapter
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter

_ENGINE_VERSION = "adapter-knobs-test"


def _scalar_mask_config(tmp_path) -> dict:
    return PipelineConfig.model_validate(
        {
            "version": 2,
            "global_settings": {"seed": 42},
            "sources": {
                "customers": {"type": "file", "format": "csv", "path": str(tmp_path / "c.csv")},
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
    ).model_dump()


def _scalar_mask_sources(tmp_path) -> dict[str, pa.Table]:
    df = pd.DataFrame(
        {
            "email": ["a@x.com", "b@x.com", "c@x.com"],
            "zip": ["90210", "10001", "60601"],
            "secret": ["alpha", "beta", "gamma"],
        }
    )
    df.to_csv(tmp_path / "c.csv", index=False)
    return {"customers": pa.Table.from_pandas(df, preserve_index=False)}


# Captured from the original hardcoded `PandasExecutionAdapter()` path (seed 42).
_GOLDEN = {
    "email": [
        "fe41ab6a8df660b8689b220dedc9ed5bc4884e0b3aeeba7b37bbe7f75a82cefd",
        "6618c5f5c09e40858005383ff376e4964778587a870e875015989e0be0d31405",
        "e5aeefd8ad45bb1d8efbc1e70adf1512e89486daa5eb49579d52280521b8a9fc",
    ],
    "zip": ["902", "100", "606"],
    "secret": ["REDACTED", "REDACTED", "REDACTED"],
}


class TestDefaultRoute:
    def test_default_output_matches_golden_and_stamps_no_adapter_block(self, tmp_path):
        cfg = _scalar_mask_config(tmp_path)
        result = run_pipeline(
            cfg, sources=_scalar_mask_sources(tmp_path), engine_version=_ENGINE_VERSION
        )
        assert result.outputs["customers"].to_pydict() == _GOLDEN
        assert "execution_adapter" not in result.quality_metrics


class TestFpeChunkCountKnob:
    def test_non_default_count_stamps_the_adapter_block(self, tmp_path):
        cfg = _scalar_mask_config(tmp_path)
        result = run_pipeline(
            cfg,
            sources=_scalar_mask_sources(tmp_path),
            engine_version=_ENGINE_VERSION,
            fpe_chunk_count=9,
        )
        block = result.quality_metrics["execution_adapter"]
        assert block["resolved_substrate"] == "pandas"
        assert block["fpe_chunk_count"] == 9
        assert block["adapter_name"] == PandasExecutionAdapter().adapter_name
        assert "requested_substrate" not in block
        assert "max_workers" not in block

    @pytest.mark.parametrize("bad", [0, -1, True, "4"])
    def test_invalid_count_fails_early_with_typed_error(self, tmp_path, monkeypatch, bad):
        import decoy_engine.profile as profile_mod

        def bomb(*args, **kwargs):
            raise AssertionError("profile_source ran before knob validation")

        monkeypatch.setattr(profile_mod, "profile_source", bomb)
        cfg = _scalar_mask_config(tmp_path)
        with pytest.raises(ExecutionError) as exc:
            run_pipeline(cfg, sources={}, engine_version=_ENGINE_VERSION, fpe_chunk_count=bad)
        assert exc.value.code == "invalid_execution_knob"

    def test_select_execution_adapter_rejects_invalid_count(self):
        with pytest.raises(ExecutionError) as exc:
            select_execution_adapter(fpe_chunk_count=0)
        assert exc.value.code == "invalid_execution_knob"


class TestRemovedKnobsAreGone:
    @pytest.mark.parametrize(
        "kw", [{"substrate": "pandas"}, {"max_workers": 4}, {"fallback_to_pandas": True}]
    )
    def test_removed_kwarg_is_a_type_error_not_a_silent_no_op(self, tmp_path, kw):
        cfg = _scalar_mask_config(tmp_path)
        with pytest.raises(TypeError):
            run_pipeline(cfg, sources={}, engine_version=_ENGINE_VERSION, **kw)

    def test_substrate_env_var_is_not_read(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DECOY_SUBSTRATE", "polars")
        cfg = _scalar_mask_config(tmp_path)
        result = run_pipeline(
            cfg, sources=_scalar_mask_sources(tmp_path), engine_version=_ENGINE_VERSION
        )
        assert result.outputs["customers"].to_pydict() == _GOLDEN
