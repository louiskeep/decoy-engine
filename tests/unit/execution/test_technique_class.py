"""MG-1 S1 (2026-06-01): regression cells for the GDPR technique class
registry. The map is the single source of truth for the FE strategy-
picker badge + the plan manifest's per-column class. Pin the
classification of every shipped strategy so a future rename / move
doesn't drift the operator's mental model.
"""

from __future__ import annotations

import pytest

from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.execution._technique_class import (
    TECHNIQUE_CLASS_BY_STRATEGY,
    technique_class_for,
)


class TestTechniqueClassRegistry:
    # Wrappers (MG-3 / M2): strategies whose GDPR posture is the CHILD
    # strategy's, not their own. Intentionally left out of the
    # TECHNIQUE_CLASS_BY_STRATEGY map so the FE renders the child's
    # badge for the column. Surfacing the child's class on the wrapper
    # row is the MG-1.5-FE-pickers follow-up.
    _WRAPPER_STRATEGIES = frozenset({"nested"})

    def test_every_shipped_strategy_is_classified(self):
        """Every entry in SCALAR_HANDLERS that is NOT a wrapper has a
        TECHNIQUE_CLASS_BY_STRATEGY entry. Without this, a new
        strategy ships unclassified + the FE renders the needs-review
        badge in production."""
        unclassified = sorted(
            name
            for name in SCALAR_HANDLERS
            if name not in TECHNIQUE_CLASS_BY_STRATEGY and name not in self._WRAPPER_STRATEGIES
        )
        assert unclassified == [], (
            f"Strategies missing a technique class: {unclassified}. "
            "Add an entry to TECHNIQUE_CLASS_BY_STRATEGY before merge, "
            "or add the strategy to _WRAPPER_STRATEGIES if it is a wrapper."
        )

    @pytest.mark.parametrize(
        "strategy,expected",
        [
            ("passthrough", "passthrough"),
            ("redact", "anonymisation"),
            ("truncate", "anonymisation"),
            ("bucketize", "anonymisation"),
            ("shuffle", "anonymisation"),
            ("hash", "pseudonymisation"),
            ("fpe", "pseudonymisation"),
            ("date_shift", "pseudonymisation"),
            ("formula", "pseudonymisation"),
            ("faker", "synthetic"),
            ("categorical", "synthetic"),
        ],
    )
    def test_classification_matches_industry_taxonomy(self, strategy, expected):
        """Pin the per-strategy classification rationale documented in
        _technique_class.py. A change here is a contract-level change
        that should be reviewed by the PO + Dennis."""
        assert technique_class_for(strategy) == expected

    def test_unknown_strategy_returns_none(self):
        """Forwards compat: a strategy name we don't know about returns
        None so the FE can render the needs-review badge instead of
        crashing the picker."""
        assert technique_class_for("not_a_real_strategy") is None

    def test_none_strategy_returns_none(self):
        """Defensive: technique_class_for(None) is the call site shape
        when the column's strategy field is unset."""
        assert technique_class_for(None) is None

    def test_empty_string_returns_none(self):
        """Defensive: empty strategy string treated the same as missing."""
        assert technique_class_for("") is None

    def test_four_user_visible_classes_only(self):
        """The classification union is closed at four values. If a new
        class lands, this test forces an explicit update."""
        values = set(TECHNIQUE_CLASS_BY_STRATEGY.values())
        expected = {"pseudonymisation", "anonymisation", "synthetic", "passthrough"}
        extra = values - expected
        assert extra == set(), (
            f"Unexpected technique class values in registry: {extra}. "
            "Update _technique_class.TechniqueClass Literal + this test."
        )


class TestColumnSeedCarriesTechniqueClass:
    """plan-compile reads TECHNIQUE_CLASS_BY_STRATEGY and writes the
    looked-up class onto ColumnSeed.technique_class. The manifest
    serializer round-trips it. Without these pins the field could
    silently regress to None on every column."""

    def test_columnseed_default_technique_class_is_none(self):
        """Backward-compat: a ColumnSeed built without technique_class
        defaults to None (legacy code paths that don't pass it)."""
        from decoy_engine.plan._types import ColumnSeed

        seed = ColumnSeed(
            namespace="ns",
            strategy="hash",
            provider=None,
            backend_type="decoy_native",
            backend_version="1",
            cardinality_mode="bijective",
            deterministic=True,
        )
        assert seed.technique_class is None

    def test_columnseed_accepts_explicit_technique_class(self):
        from decoy_engine.plan._types import ColumnSeed

        seed = ColumnSeed(
            namespace="ns",
            strategy="hash",
            provider=None,
            backend_type="decoy_native",
            backend_version="1",
            cardinality_mode="bijective",
            deterministic=True,
            technique_class="pseudonymisation",
        )
        assert seed.technique_class == "pseudonymisation"

    def test_serializer_emits_technique_class(self):
        from decoy_engine.plan._serialize import _column_seed_to_dict
        from decoy_engine.plan._types import ColumnSeed

        seed = ColumnSeed(
            namespace="ns",
            strategy="hash",
            provider=None,
            backend_type="decoy_native",
            backend_version="1",
            cardinality_mode="bijective",
            deterministic=True,
            technique_class="pseudonymisation",
        )
        out = _column_seed_to_dict(seed)
        assert out["technique_class"] == "pseudonymisation"

    def test_serializer_omits_field_when_unset(self):
        """Legacy plans without technique_class deserialize cleanly."""
        from decoy_engine.plan._serialize import _column_seed_to_dict
        from decoy_engine.plan._types import ColumnSeed

        seed = ColumnSeed(
            namespace="ns",
            strategy="hash",
            provider=None,
            backend_type="decoy_native",
            backend_version="1",
            cardinality_mode="bijective",
            deterministic=True,
        )
        out = _column_seed_to_dict(seed)
        assert "technique_class" not in out

    def test_deserializer_reads_technique_class(self):
        from decoy_engine.plan._serialize import _column_seed_from_dict

        data = {
            "namespace": "ns",
            "strategy": "hash",
            "provider": None,
            "backend_type": "decoy_native",
            "backend_version": "1",
            "cardinality_mode": "bijective",
            "deterministic": True,
            "technique_class": "pseudonymisation",
        }
        seed = _column_seed_from_dict(data)
        assert seed.technique_class == "pseudonymisation"

    def test_deserializer_defaults_missing_field_to_none(self):
        from decoy_engine.plan._serialize import _column_seed_from_dict

        data = {
            "namespace": "ns",
            "strategy": "hash",
            "provider": None,
            "backend_type": "decoy_native",
            "backend_version": "1",
            "cardinality_mode": "bijective",
            "deterministic": True,
        }
        seed = _column_seed_from_dict(data)
        assert seed.technique_class is None
