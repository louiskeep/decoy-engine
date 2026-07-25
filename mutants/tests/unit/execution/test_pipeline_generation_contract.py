"""DPS Scope B (guide section 5, step 9): `run_pipeline` passes the
COMPILED `Plan` to `generate_tables`, never the raw config, and
`run_config_only_checks` cannot produce anything a caller could hand to
`generate_tables` instead of a real compile.

`generate_tables` is Plan-only (guide section 4.8): a runtime TypeError
guard rejects anything that isn't a `decoy_engine.plan.Plan`. These tests
pin the two guide-named assertions at the pipeline boundary: the
production call site actually passes a `Plan` object (not a dict), and
the config-only check path -- which deliberately does LESS work than a
real compile (no profile, no relationship graph) -- has no way to
manufacture a generation-capable object a caller could route around
`compile_plan` with.
"""

from __future__ import annotations

import pytest

from decoy_engine import run_config_only_checks
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.generation.synthesize import generate_tables
from decoy_engine.plan import Plan

_ENGINE_VERSION = "dps-pipeline-test"


def _pure_generate_config() -> dict:
    return PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {},
            "tables": [
                {
                    "name": "employees",
                    "row_count": 5,
                    "generate_columns": [
                        {"name": "employee_id", "type": "sequence", "start": 1000, "step": 1},
                    ],
                },
            ],
            "targets": {"employees": {"type": "file", "format": "csv", "path": "out.csv"}},
        }
    ).model_dump()


def test_run_pipeline_passes_compiled_plan_to_generation(monkeypatch):
    """Capture the actual object `_pipeline.py` hands to `generate_tables`
    for a pure-generate config: it must be the compiled `Plan`, not the
    raw config dict `run_pipeline` was called with."""
    import decoy_engine.generation.synthesize as synthesize_mod

    # `_pipeline.py` imports `generate_tables` lazily, inside the
    # function body, so it re-fetches this module's attribute on every
    # call -- patching it here is what the local `from ... import`
    # actually observes at call time.
    captured: dict[str, object] = {}
    real_generate_tables = synthesize_mod.generate_tables

    def _spy(plan, **kwargs):
        captured["plan"] = plan
        return real_generate_tables(plan, **kwargs)

    monkeypatch.setattr(synthesize_mod, "generate_tables", _spy)

    cfg = _pure_generate_config()
    result = run_pipeline(cfg, engine_version=_ENGINE_VERSION)

    assert "plan" in captured
    assert isinstance(captured["plan"], Plan)
    assert not isinstance(captured["plan"], dict)
    assert result.outputs["employees"].num_rows == 5


def test_pipeline_cannot_generate_after_config_only_checks():
    """`run_config_only_checks` is the profile-free, no-compile check
    subset (guide section 5 reorder note): it returns only the names of
    the checks that ran, never a `Plan` or anything else a caller could
    pass to `generate_tables`. There is exactly one way to reach a
    generation-capable object: a real `compile_plan` (or validated Plan
    deserialization) -- config-only checks cannot shortcut it."""
    cfg = _pure_generate_config()

    checks_ran = run_config_only_checks(cfg)

    assert isinstance(checks_ran, tuple)
    assert all(isinstance(name, str) for name in checks_ran)
    assert not isinstance(checks_ran, Plan)
    assert not hasattr(checks_ran, "generation")

    # The only object this path returns is unusable as a `generate_tables`
    # argument -- confirms there's no accidental generation-capable value
    # smuggled through config-only checks.
    with pytest.raises(TypeError):
        generate_tables(checks_ran)  # type: ignore[arg-type]
