"""DE-11 regressions: pool_size + scale resolve ONCE at compile.

`ColumnConfig.pool_size` / `ColumnConfig.scale` (config/_tables.py) are
validated at compile time (plan/_checks.py, generation/pool/_validate.py)
but, pre-fix, were never copied onto the compiled `ColumnSeed`
(plan/_types.py). Runtime consumers (execution/_strategies/_faker.py,
execution/_chunked.py) read `provider_config.pool_size` / the sampler's
hardcoded 2.0 default instead, so a config declaring a top-level
`pool_size` or `scale` compiled clean and then silently used a different
value at runtime. See docs/2026-07-13-de11-pool-size-precedence.md.

The fix resolves both fields ONCE at the envelope drop site
(`plan/_seed_envelope.py`) onto typed `ColumnSeed.pool_size` /
`ColumnSeed.scale` fields, fails compile if the two legal `pool_size`
locations (top-level + `provider_config.pool_size`) disagree, and points
every runtime consumer at the resolved field instead of re-parsing the
raw config.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import GenerationError, run_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.plan import PlanCompileError, compile_plan, plan_from_yaml, plan_to_yaml
from decoy_engine.profile import ColumnProfile, Profile, TableProfile

_ENGINE_VERSION = "de11-test"


def _config(tmp_path, columns: list[dict], table: str = "people") -> dict:
    """A real, pydantic-validated PipelineConfig dump (not a toy dict)."""
    cfg = {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {table: {"type": "file", "format": "csv", "path": str(tmp_path / "in.csv")}},
        "tables": [{"name": table, "columns": columns}],
        "targets": {table: {"type": "file", "format": "csv", "path": str(tmp_path / "out.csv")}},
    }
    return PipelineConfig.model_validate(cfg).model_dump()


def _write_and_run(tmp_path, df: pd.DataFrame, cfg: dict, table: str = "people"):
    df.to_csv(tmp_path / "in.csv", index=False)
    return run_pipeline(
        cfg,
        sources={table: pa.Table.from_pandas(df, preserve_index=False)},
        engine_version=_ENGINE_VERSION,
    )


class TestPoolSizeReachesRuntime:
    """A top-level `pool_size` must reach the runtime pool build.

    Pre-fix (plan/_seed_envelope.py dropping `pool_size` at the
    ColumnSeed boundary): the faker handler always built the 10_000-value
    default pool, so 2000 REUSE-mode draws would show close to 2000
    distinct values (collisions vanishingly rare with n << pool). Post-fix,
    a declared `pool_size: 5` bounds output to at most 5 distinct values --
    the pigeonhole makes the runtime pool size directly observable.
    """

    def test_resolved_onto_compiled_column_seed(self, tmp_path) -> None:
        cfg = _config(
            tmp_path,
            [
                {
                    "name": "email",
                    "strategy": "faker",
                    "provider": "person_email",
                    "cardinality_mode": "reuse",
                    "pool_size": 250_000,
                }
            ],
        )
        table_profile = TableProfile(
            name="people",
            row_count=1,
            columns=(
                ColumnProfile(
                    name="email",
                    dtype="object",
                    row_count=1,
                    null_count=0,
                    distinct_count=1,
                    sampled=False,
                    is_candidate_key_sampled=False,
                    declared_pk=False,
                    is_fk=False,
                    fk_target=None,
                    pii_class=None,
                ),
            ),
        )
        profile = Profile(
            schema_version=1,
            tables=(table_profile,),
            relationships=(),
            profiled_at=datetime(1970, 1, 1),
            decoy_engine_version=_ENGINE_VERSION,
        )
        plan = compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION)
        col_seed = dict(dict(plan.seed_envelope.per_table)["people"].per_column)["email"]
        assert col_seed.pool_size == 250_000

    def test_top_level_pool_size_builds_correct_pool(self, tmp_path) -> None:
        n = 2000
        cfg = _config(
            tmp_path,
            [
                {
                    "name": "email",
                    "strategy": "faker",
                    "provider": "person_email",
                    "cardinality_mode": "reuse",
                    "pool_size": 5,
                }
            ],
        )
        df = pd.DataFrame({"email": [f"user{i}@example.com" for i in range(n)]})
        result = _write_and_run(tmp_path, df, cfg)
        out = result.outputs["people"].column("email").to_pylist()
        assert len(out) == n
        # Would be close to `n` distinct pre-fix (10_000-default pool);
        # pigeonholed to <= 5 once pool_size actually reaches the builder.
        assert len(set(out)) <= 5


class TestScaleReachesRuntime:
    """A top-level `scale` must reach `PoolSampler.sample`.

    Pre-fix, `_faker.py` never passed `scale` at all, so the sampler
    always used its hardcoded 2.0 default regardless of the declared
    value. `pool_size=25` sits strictly between the two possible targets
    (2.0 * 10 = 20 fits; 5.0 * 10 = 50 doesn't), so a
    `cardinality_target_exceeds_pool` GenerationError is only possible if
    the declared `scale: 5.0` actually reached the sampler.
    """

    def test_top_level_scale_reaches_sampler(self, tmp_path) -> None:
        n = 500
        # 10 distinct source values, repeated.
        df = pd.DataFrame({"email": [f"user{i % 10}@example.com" for i in range(n)]})
        cfg = _config(
            tmp_path,
            [
                {
                    "name": "email",
                    "strategy": "faker",
                    "provider": "person_email",
                    "cardinality_mode": "scale_source_cardinality",
                    "scale": 5.0,
                    "pool_size": 25,
                }
            ],
        )
        with pytest.raises(GenerationError) as exc:
            _write_and_run(tmp_path, df, cfg)
        assert exc.value.code == "cardinality_target_exceeds_pool"


class TestPoolSizeLocationConflict:
    """Reject-on-contradiction: differing top-level vs. provider_config
    pool_size is ambiguous and must fail compile, not silently pick one.
    Equal values in both locations remain legal (not exercised as an
    error here, but covered by TestPoolSizeReachesRuntime's happy paths
    using a single location)."""

    def test_differing_locations_rejected_at_compile(self, tmp_path) -> None:
        cfg = _config(
            tmp_path,
            [
                {
                    "name": "email",
                    "strategy": "faker",
                    "provider": "person_email",
                    "cardinality_mode": "reuse",
                    "pool_size": 100,
                    "provider_config": {"pool_size": 200},
                }
            ],
        )
        table_profile = TableProfile(name="people", row_count=0, columns=())
        profile = Profile(
            schema_version=1,
            tables=(table_profile,),
            relationships=(),
            profiled_at=datetime(1970, 1, 1),
            decoy_engine_version=_ENGINE_VERSION,
        )
        with pytest.raises(PlanCompileError) as exc:
            compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION)
        assert exc.value.code == "pool_size_location_conflict"

    def test_equal_locations_are_legal(self, tmp_path) -> None:
        cfg = _config(
            tmp_path,
            [
                {
                    "name": "email",
                    "strategy": "faker",
                    "provider": "person_email",
                    "cardinality_mode": "reuse",
                    "pool_size": 150,
                    "provider_config": {"pool_size": 150},
                }
            ],
        )
        table_profile = TableProfile(name="people", row_count=0, columns=())
        profile = Profile(
            schema_version=1,
            tables=(table_profile,),
            relationships=(),
            profiled_at=datetime(1970, 1, 1),
            decoy_engine_version=_ENGINE_VERSION,
        )
        plan = compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION)
        col_seed = dict(dict(plan.seed_envelope.per_table)["people"].per_column)["email"]
        assert col_seed.pool_size == 150


class TestPlanRoundTrip:
    """serialize -> deserialize must preserve pool_size + scale."""

    def test_pool_size_and_scale_survive_yaml_round_trip(self, tmp_path) -> None:
        cfg = _config(
            tmp_path,
            [
                {
                    "name": "email",
                    "strategy": "faker",
                    "provider": "person_email",
                    "cardinality_mode": "scale_source_cardinality",
                    "pool_size": 4242,
                    "scale": 3.5,
                }
            ],
        )
        table_profile = TableProfile(name="people", row_count=0, columns=())
        profile = Profile(
            schema_version=1,
            tables=(table_profile,),
            relationships=(),
            profiled_at=datetime(1970, 1, 1),
            decoy_engine_version=_ENGINE_VERSION,
        )
        plan = compile_plan(cfg, profile, decoy_engine_version=_ENGINE_VERSION)
        col_seed = dict(dict(plan.seed_envelope.per_table)["people"].per_column)["email"]
        assert col_seed.pool_size == 4242
        assert col_seed.scale == 3.5

        recovered = plan_from_yaml(plan_to_yaml(plan))
        recovered_seed = dict(dict(recovered.seed_envelope.per_table)["people"].per_column)["email"]
        assert recovered_seed.pool_size == 4242
        assert recovered_seed.scale == 3.5
        assert recovered == plan
