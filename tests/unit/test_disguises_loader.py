"""Smoke tests for the Disguise schema + loader.

These tests fail loud if a contributor edits a YAML with a typo or a missing
field — that's the whole point of the Pydantic schema.
"""

import pytest
import yaml

from decoy_engine.disguises import Disguise, load_disguises
from decoy_engine.disguises.schema import FieldRule, TriggerSpec

# Full 8-bundle launch set from DISGUISES_GUIDE.md.
_EXPECTED_DISGUISE_IDS = {"default", "hipaa", "pci", "gdpr", "glba", "ccpa", "ferpa", "sox"}


class TestLoaderShipsBundles:
    def test_loads_full_launch_set(self):
        # All 8 bundles must be present; adding a new one without updating
        # this set is fine, but removing any existing id breaks CI.
        loaded_ids = {d.id for d in load_disguises()}
        missing = _EXPECTED_DISGUISE_IDS - loaded_ids
        assert not missing, f"Missing Disguise bundles: {sorted(missing)}"

    def test_loads_at_least_default_and_hipaa(self):
        # Kept for historical context — the bones-only PR guarantee.
        ds = {d.id for d in load_disguises()}
        assert "default" in ds
        assert "hipaa" in ds

    def test_every_disguise_has_at_least_one_field_rule(self):
        for d in load_disguises():
            assert d.field_rules, f"{d.id} has no field_rules"

    def test_every_disguise_has_a_trigger(self):
        for d in load_disguises():
            t = d.triggers
            # Either required, any, or co_occurrence — at least one nonempty.
            assert t.required_detectors or t.any_detectors or t.co_occurrence, \
                f"{d.id} has no triggers"

    def test_every_disguise_field_rule_references_known_detectors(self):
        """Guard: all detector IDs in field_rules must be known to detectors.py."""
        from decoy_engine.storm.detectors import REGISTERED_DETECTORS
        # Extract the id from each detector function's closure.
        known_ids: set[str] = set()
        import re
        for fn in REGISTERED_DETECTORS:
            # Convention: every registered function calls _evaluate("<id>", ...)
            # as its first arg; extract via the function's source name.
            # Simpler: the detector id is fn.__name__ without the "detect_" prefix.
            known_ids.add(fn.__name__.replace("detect_", "", 1))
        for d in load_disguises():
            for rule in d.field_rules:
                for det_id in rule.detectors:
                    assert det_id in known_ids, (
                        f"Disguise '{d.id}' references unknown detector '{det_id}'"
                    )


class TestSchemaValidatesShape:
    def test_minimum_valid_disguise(self):
        d = Disguise(
            id="x", name="X", summary="X",
            triggers=TriggerSpec(any_detectors=["email"]),
            field_rules=[FieldRule(detectors=["email"], mask="faker", params={"faker_type": "email"})],
        )
        assert d.id == "x"

    def test_missing_id_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            Disguise(
                name="X", summary="X",
                triggers=TriggerSpec(any_detectors=["email"]),
                field_rules=[],
            )

    def test_min_score_default(self):
        t = TriggerSpec()
        assert t.min_score == 0.3


class TestLoaderHandlesTempDirectory:
    def test_loads_only_from_specified_dir(self, tmp_path):
        # An empty temp dir loads zero disguises.
        assert load_disguises(tmp_path) == []

    def test_loads_a_disguise_written_to_tmp(self, tmp_path):
        yaml_text = yaml.safe_dump({
            "id": "tmp",
            "name": "Tmp",
            "summary": "test",
            "triggers": {"any_detectors": ["email"], "min_score": 0.3},
            "field_rules": [{"detectors": ["email"], "mask": "faker", "params": {"faker_type": "email"}}],
        })
        (tmp_path / "tmp.yaml").write_text(yaml_text, encoding="utf-8")
        ds = load_disguises(tmp_path)
        assert len(ds) == 1 and ds[0].id == "tmp"

    def test_malformed_yaml_raises(self, tmp_path):
        (tmp_path / "bad.yaml").write_text("id: bad\nname: B\n", encoding="utf-8")  # missing required fields
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            load_disguises(tmp_path)
