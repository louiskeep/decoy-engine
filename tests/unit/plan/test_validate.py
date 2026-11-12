"""engine-v2 S10 slice 1: validate_plan compile consolidator.

validate_plan is a thin wrapper over compile_plan returning a structured
PlanValidationResult instead of raising. The load-bearing proof (Dennis): the
delegate test that validate_plan(...).plan == compile_plan(...), i.e. S10 added
no behavior. The rest assert the failure paths surface as ok=False without
raising, and that the check rollup matches compile_plan's.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from decoy_engine.plan import PlanCompileError, compile_plan
from decoy_engine.plan.validate import PlanCheckError, PlanValidationResult, validate_plan
from decoy_engine.profile import ColumnProfile, Profile, TableProfile

_VERSION = "0.1.0"


def _col_profile(name: str, *, distinct: int = 10) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype="object",
        row_count=10,
        null_count=0,
        distinct_count=distinct,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )


def _profile() -> Profile:
    return Profile(
        schema_version=1,
        tables=(TableProfile(name="t", row_count=10, columns=(_col_profile("c"),)),),
        relationships=(),
        profiled_at=datetime(2026, 5, 28),
        decoy_engine_version=_VERSION,
    )


def _valid_config(**col_extra: Any) -> dict[str, Any]:
    column = {
        "name": "c",
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": "ns_c",
    }
    column.update(col_extra)
    return {"global_settings": {"seed": 1}, "tables": [{"name": "t", "columns": [column]}]}


class TestValidatePlanDelegate:
    def test_plan_equals_compile_plan(self) -> None:
        # The load-bearing proof: validate_plan adds no behavior; its Plan is the
        # exact Plan compile_plan produces from the same inputs.
        config, profile = _valid_config(), _profile()
        result = validate_plan(config, profile, decoy_engine_version=_VERSION)
        assert result.ok is True
        assert result.plan == compile_plan(config, profile, decoy_engine_version=_VERSION)

    def test_checks_rollup_matches_compile_plan(self) -> None:
        config, profile = _valid_config(), _profile()
        result = validate_plan(config, profile, decoy_engine_version=_VERSION)
        plan = compile_plan(config, profile, decoy_engine_version=_VERSION)
        assert result.checks_passed == plan.plan_compile.checks_passed
        assert result.checks_skipped == plan.plan_compile.checks_skipped
        assert result.warnings == plan.plan_compile.warnings

    def test_no_profile_skips_profile_checks_like_compile_plan(self) -> None:
        config, profile = _valid_config(), _profile()
        result = validate_plan(config, profile, decoy_engine_version=_VERSION, no_profile=True)
        plan = compile_plan(config, profile, decoy_engine_version=_VERSION, no_profile=True)
        assert result.ok is True
        assert result.checks_skipped == plan.plan_compile.checks_skipped
        # --no-profile skips at least one profile-dependent check.
        assert result.checks_skipped


class TestValidatePlanFailurePaths:
    def test_unknown_provider_returns_ok_false_no_raise(self) -> None:
        config = _valid_config(provider="not_a_real_provider")
        result = validate_plan(config, _profile(), decoy_engine_version=_VERSION)
        assert result.ok is False
        assert isinstance(result.error, PlanCheckError)
        assert result.error.code == "unknown_provider"
        assert result.plan is None
        # compile_plan would have RAISED on the same input.
        with pytest.raises(PlanCompileError):
            compile_plan(config, _profile(), decoy_engine_version=_VERSION)

    def test_pool_capacity_error_caught(self) -> None:
        # A UNIQUE column under --no-profile cannot prove uniqueness (no distinct
        # counts) and raises PoolCapacityError; validate_plan catches it.
        config = _valid_config(cardinality_mode="unique")
        result = validate_plan(config, _profile(), decoy_engine_version=_VERSION, no_profile=True)
        assert result.ok is False
        assert isinstance(result.error, PlanCheckError)
        assert result.error.path is None  # PoolCapacityError carries no YAML path
        assert result.plan is None

    def test_result_is_frozen(self) -> None:
        result = validate_plan(_valid_config(), _profile(), decoy_engine_version=_VERSION)
        with pytest.raises((AttributeError, TypeError)):
            result.ok = False  # type: ignore[misc]
        assert isinstance(result, PlanValidationResult)


class TestSubsetBlockIsSplitOff:
    def test_subset_block_is_split_off_and_the_run_config_compiles(self) -> None:
        config = _valid_config()
        config["subset"] = {
            "seeds": [{"table": "t", "mode": "sample", "key_columns": ["id"], "count": 1}]
        }
        result = validate_plan(config, _profile(), decoy_engine_version=_VERSION)
        assert result.ok is True and result.plan is not None
        plain = _valid_config()
        plain["subset"] = None
        want = validate_plan(plain, _profile(), decoy_engine_version=_VERSION)
        assert want.plan is not None
        assert result.plan.pipeline_config_hash == want.plan.pipeline_config_hash


_BAD_SEEDS = [
    {"table": "t", "mode": "sample", "key_columns": [], "count": 1},
    {"table": "t", "mode": "sample", "key_columns": ["id"], "fraction": 3.0},
    {"table": "t", "mode": "sample", "key_columns": ["id"], "fraction": 0.5, "count": 2},
    {"table": "t", "mode": "sample", "key_columns": ["id"]},
    {"table": "t", "mode": "keys", "key_columns": ["id"], "keys": []},
]


@pytest.mark.parametrize("seed", _BAD_SEEDS)
def test_a_malformed_seed_is_refused_by_config_validation_before_the_split(
    seed: dict[str, object],
) -> None:
    """The split's `SeedSpec` raises `ValueError` on each of these, so the only way to reach it
    is past `PipelineConfig` validation, which refuses the same shapes."""
    from decoy_engine.config import PipelineConfig
    from decoy_engine.subset._types import SeedSpec

    raw = {
        "version": 2,
        "sources": {"t": {"type": "file", "format": "parquet", "path": "/x.parquet"}},
        "tables": [{"name": "t", "columns": [{"name": "id", "strategy": "passthrough"}]}],
        "targets": {"t": {"type": "file", "format": "parquet", "path": "/y.parquet"}},
        "subset": {"seeds": [seed]},
    }
    with pytest.raises(Exception) as info:
        PipelineConfig.model_validate(raw)
    assert "subset" in str(info.value).lower() or "seed" in str(info.value).lower()
    with pytest.raises(ValueError):
        SeedSpec(
            table="t",
            mode=seed["mode"],  # type: ignore[arg-type]
            key_columns=tuple(seed.get("key_columns", ())),  # type: ignore[call-overload]
            fraction=seed.get("fraction"),  # type: ignore[arg-type]
            count=seed.get("count"),  # type: ignore[arg-type]
            keys=tuple(seed.get("keys", ())),  # type: ignore[call-overload]
        )
