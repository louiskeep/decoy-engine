"""Item 2 (gap-closure): `allow_collisions` per-column knob.

`allow_collisions: true` is a documented operator-facing alias for Delphix
Secure Lookup's collision-allowed semantics: it compiles to
`cardinality_mode = reuse` + `deterministic = true`, producing a stable
many-to-one masked mapping (distinct sources may share a masked value, the
way natural data recurs). It requires a namespace (the deterministic derive
key) and conflicts with any non-`reuse` cardinality_mode.
"""

from __future__ import annotations

import copy

import pytest

from decoy_engine.config._tables import ColumnConfig
from decoy_engine.plan import PlanCompileError, compile_plan
from decoy_engine.profile import Profile


def _config_with_name_column(simple_config: dict, **col_overrides: object) -> dict:
    """Deep-copy simple_config and apply overrides to customers.name."""
    cfg = copy.deepcopy(simple_config)
    name_col = cfg["tables"][0]["columns"][0]
    assert name_col["name"] == "name"
    name_col.update(col_overrides)
    return cfg


def _name_seed(plan):
    per_table = dict(plan.seed_envelope.per_table)
    return dict(per_table["customers"].per_column)["name"]


class TestAllowCollisionsAlias:
    def test_alias_forces_reuse_and_deterministic(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        cfg = _config_with_name_column(
            simple_config,
            allow_collisions=True,
            namespace="names_ns",
        )
        # allow_collisions implies reuse; drop the explicit mode to prove the
        # alias supplies it.
        cfg["tables"][0]["columns"][0].pop("cardinality_mode", None)
        plan = compile_plan(cfg, simple_profile, decoy_engine_version="0.1.0")
        seed = _name_seed(plan)
        assert seed.cardinality_mode == "reuse"
        assert seed.deterministic is True

    def test_alias_with_explicit_reuse_is_allowed(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        cfg = _config_with_name_column(
            simple_config,
            allow_collisions=True,
            namespace="names_ns",
            cardinality_mode="reuse",
        )
        plan = compile_plan(cfg, simple_profile, decoy_engine_version="0.1.0")
        seed = _name_seed(plan)
        assert seed.cardinality_mode == "reuse"
        assert seed.deterministic is True

    def test_default_off_leaves_random_reuse(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        # simple_config's name column is plain reuse, no allow_collisions.
        plan = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")
        seed = _name_seed(plan)
        assert seed.deterministic is False


class TestAllowCollisionsRejections:
    def test_conflict_with_unique_mode(self, simple_config: dict, simple_profile: Profile) -> None:
        cfg = _config_with_name_column(
            simple_config,
            allow_collisions=True,
            namespace="names_ns",
            cardinality_mode="unique",
        )
        with pytest.raises(PlanCompileError) as exc:
            compile_plan(cfg, simple_profile, decoy_engine_version="0.1.0")
        assert exc.value.code == "allow_collisions_mode_conflict"

    def test_requires_namespace(self, simple_config: dict, simple_profile: Profile) -> None:
        cfg = _config_with_name_column(simple_config, allow_collisions=True)
        cfg["tables"][0]["columns"][0].pop("cardinality_mode", None)
        with pytest.raises(PlanCompileError) as exc:
            compile_plan(cfg, simple_profile, decoy_engine_version="0.1.0")
        # Reuses the deterministic-namespace gate (allow_collisions implies it).
        assert exc.value.code == "deterministic_namespace_missing"


class TestColumnConfigField:
    def test_field_accepted_and_defaults_false(self) -> None:
        assert ColumnConfig(name="x", strategy="hash").allow_collisions is False
        assert (
            ColumnConfig(name="x", strategy="hash", allow_collisions=True).allow_collisions is True
        )
