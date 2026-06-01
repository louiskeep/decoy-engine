"""MG-6 D1 (2026-05-31): distribution-behavior metadata regression cells.

Locks:
- Every shipped strategy has a stable distribution_behavior label.
- categorical resolves dynamically by config (preserves_all when
  weights/from_profile set, destroys_frequency otherwise).
- nested carries the sentinel `inherits`.
- Unknown strategy returns None.
- Plan-compile stamps the field on ColumnSeed.
- Plan serializer round-trips the field.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from decoy_engine.execution._distribution_behavior import (
    DistributionBehavior,
    distribution_behavior_for,
)
from decoy_engine.execution._strategies import SCALAR_HANDLERS


class TestPerStrategyLabel:
    @pytest.mark.parametrize(
        "strategy,expected",
        [
            ("passthrough", "preserves_all"),
            ("shuffle", "preserves_all"),
            ("hash", "preserves_cardinality_only"),
            ("fpe", "preserves_cardinality_only"),
            ("faker", "destroys_frequency"),
            ("bucketize", "coarsens"),
            ("truncate", "coarsens"),
            ("redact", "collapses"),
            ("text_redact", "collapses"),
            ("date_shift", "varies_shape"),
            ("formula", "mixed"),
            ("nested", "inherits"),
        ],
    )
    def test_static_strategy_labels(self, strategy, expected):
        assert distribution_behavior_for(strategy) == expected


class TestCategoricalDynamicResolution:
    def test_categorical_uniform_distribution_behavior_destroys_frequency(self):
        """No weights + no from_profile -> destroys_frequency."""
        assert distribution_behavior_for("categorical") == "destroys_frequency"
        assert distribution_behavior_for("categorical", ()) == "destroys_frequency"

    def test_categorical_source_weighted_distribution_behavior_preserves_all(self):
        """Manual weights -> preserves_all."""
        cfg = (("weights", [7, 2, 1]),)
        assert distribution_behavior_for("categorical", cfg) == "preserves_all"

    def test_categorical_from_profile_distribution_behavior_preserves_all(self):
        """from_profile: true -> preserves_all (overrides weights too)."""
        cfg = (("from_profile", True),)
        assert distribution_behavior_for("categorical", cfg) == "preserves_all"

    def test_categorical_empty_weights_falls_back_to_destroys_frequency(self):
        """Empty list of weights does NOT count as source-weighted."""
        cfg = (("weights", []),)
        assert distribution_behavior_for("categorical", cfg) == "destroys_frequency"


class TestUnknownStrategy:
    def test_unknown_strategy_returns_none(self):
        assert distribution_behavior_for("made_up") is None

    def test_none_strategy_returns_none(self):
        assert distribution_behavior_for(None) is None


class TestCoverage:
    """Pin coverage: every entry in SCALAR_HANDLERS must resolve to
    a non-None label (composite is handled by faker entry, nested via
    `inherits`). Without this, a new strategy can ship unclassified."""

    def test_every_shipped_strategy_has_a_label(self):
        unlabelled = sorted(
            name for name in SCALAR_HANDLERS
            if distribution_behavior_for(name) is None
        )
        assert unlabelled == [], (
            f"Strategies missing distribution_behavior: {unlabelled}. "
            "Add an entry to _STATIC_BEHAVIOR in "
            "_distribution_behavior.py or handle the dynamic case "
            "in distribution_behavior_for()."
        )


class TestPlanCompileStampsField:
    """End-to-end: plan-compile stamps distribution_behavior on
    every ColumnSeed."""

    def _profile_and_config(self, strategy: str, provider_config: dict | None = None):
        from decoy_engine.profile import ColumnProfile, Profile, TableProfile

        cfg_block: dict = {"strategy": strategy, "name": "c"}
        if provider_config:
            cfg_block["provider_config"] = provider_config
        # The faker strategy needs a provider; passthrough/redact etc
        # accept being provider-less. Strategy-specific overrides.
        if strategy == "faker":
            cfg_block["provider"] = "person_first_name"
        profile = Profile(
            schema_version=1,
            tables=(
                TableProfile(
                    name="t",
                    row_count=10,
                    columns=(
                        ColumnProfile(
                            name="c",
                            dtype="object",
                            row_count=10,
                            null_count=0,
                            distinct_count=10,
                            sampled=False,
                            is_candidate_key_sampled=False,
                            declared_pk=False,
                            is_fk=False,
                            fk_target=None,
                            pii_class=None,
                        ),
                    ),
                ),
            ),
            relationships=(),
            profiled_at=datetime(2026, 5, 31, 0, 0, 0),
            decoy_engine_version="0.1.0",
        )
        config = {
            "global_settings": {"seed": 42},
            "tables": [{"name": "t", "columns": [cfg_block]}],
            "relationships": [],
            "namespaces": {},
        }
        return profile, config

    @pytest.mark.parametrize(
        "strategy,expected",
        [
            ("passthrough", "preserves_all"),
            ("redact", "collapses"),
            ("truncate", "coarsens"),
        ],
    )
    def test_plan_compile_stamps_distribution_behavior(self, strategy, expected):
        from decoy_engine.plan import compile_plan

        profile, config = self._profile_and_config(strategy)
        plan = compile_plan(config, profile, decoy_engine_version="0.1.0")
        col_seed = plan.seed_envelope.per_table[0][1].per_column[0][1]
        assert col_seed.distribution_behavior == expected


class TestSerializerRoundTrip:
    def test_distribution_behavior_round_trips(self):
        from decoy_engine.plan._serialize import (
            _column_seed_from_dict,
            _column_seed_to_dict,
        )
        from decoy_engine.plan._types import ColumnSeed

        seed_in = ColumnSeed(
            namespace=None,
            strategy="redact",
            provider=None,
            backend_type="decoy_native",
            backend_version="1",
            cardinality_mode="reuse",
            deterministic=False,
            provider_config=(),
            distribution_behavior="collapses",
        )
        payload = _column_seed_to_dict(seed_in)
        assert payload["distribution_behavior"] == "collapses"
        seed_out = _column_seed_from_dict({**payload, "namespace": None})
        assert seed_out.distribution_behavior == "collapses"

    def test_none_distribution_behavior_omitted_from_payload(self):
        from decoy_engine.plan._serialize import _column_seed_to_dict
        from decoy_engine.plan._types import ColumnSeed

        seed = ColumnSeed(
            namespace=None,
            strategy="passthrough",
            provider=None,
            backend_type="decoy_native",
            backend_version="1",
            cardinality_mode="reuse",
            deterministic=False,
            provider_config=(),
            distribution_behavior=None,
        )
        payload = _column_seed_to_dict(seed)
        assert "distribution_behavior" not in payload
