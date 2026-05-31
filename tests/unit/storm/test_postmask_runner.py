"""Smoke tests for decoy_engine.storm.postmask (Reframe-A, 2026-05-31).

Covers the orchestrator + the 3 check categories at the seam level.
The detailed per-check assertions live in the per-check modules' own
tests once Reframe-A is gated; this file establishes the public API
contract + the no-op pass shape.
"""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.storm.postmask import (
    SCHEMA_VERSION,
    run_storm_post_mask,
)


class TestSchemaVersion:
    def test_schema_version_constant(self):
        assert SCHEMA_VERSION == "storm-post-mask/v1"


class TestRunStormPostMask:
    def test_empty_config_produces_empty_report(self):
        report = run_storm_post_mask(
            source_frames={},
            output_frames={},
            config={"version": 1, "tables": []},
        )
        assert report["schema_version"] == SCHEMA_VERSION
        assert report["residual_pii"] == []
        assert report["fk_preservation"] == []
        assert report["policy_validation"] == []
        assert report["pass_count"] == 0
        assert report["warning_count"] == 0
        assert report["fail_count"] == 0
        assert report["error_count"] == 0
        assert report["pass_failed_with"] is None

    def test_wrong_argument_types_raise_typeerror(self):
        with pytest.raises(TypeError):
            run_storm_post_mask("not a dict", {}, config={})  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            run_storm_post_mask({}, {}, config="not a dict")  # type: ignore[arg-type]

    def test_policy_validation_catches_noop_mask(self):
        """The configured strategy is 'hash' but the output is identical
        to the source. Should produce a fail-severity finding."""
        src = pd.DataFrame({"email": ["a@x.com", "b@y.com", "c@z.com"]})
        out = src.copy()  # mask "didn't fire"
        config = {
            "tables": [{
                "name": "users",
                "columns": [{"name": "email", "strategy": "hash"}],
            }],
        }
        report = run_storm_post_mask(
            source_frames={"users": src},
            output_frames={"users": out},
            config=config,
        )
        # One fail-severity policy_validation finding.
        fails = [f for f in report["policy_validation"] if f["severity"] == "fail"]
        assert len(fails) == 1, report["policy_validation"]
        assert fails[0]["table"] == "users"
        assert fails[0]["column"] == "email"
        assert fails[0]["strategy"] == "hash"
        assert report["fail_count"] >= 1

    def test_policy_validation_passthrough_is_info_not_fail(self):
        """passthrough is no-op-by-design; identical output is not a fail."""
        src = pd.DataFrame({"id": [1, 2, 3]})
        out = src.copy()
        config = {
            "tables": [{
                "name": "users",
                "columns": [{"name": "id", "strategy": "passthrough"}],
            }],
        }
        report = run_storm_post_mask(
            source_frames={"users": src},
            output_frames={"users": out},
            config=config,
        )
        passthroughs = [
            f for f in report["policy_validation"]
            if f["strategy"] == "passthrough"
        ]
        assert len(passthroughs) == 1
        assert passthroughs[0]["severity"] == "info"
        assert report["fail_count"] == 0

    def test_fk_preservation_no_relationships_means_empty(self):
        """No relationships configured -> no FK findings; no error."""
        src = pd.DataFrame({"id": [1, 2, 3]})
        out = src.copy()
        config = {"tables": [{"name": "users", "columns": []}]}
        report = run_storm_post_mask(
            source_frames={"users": src},
            output_frames={"users": out},
            config=config,
        )
        assert report["fk_preservation"] == []

    def test_fk_preservation_clean_resolves(self):
        """All child FKs resolve to parent PKs -> info severity."""
        users = pd.DataFrame({"id": [1, 2, 3]})
        orders = pd.DataFrame({"user_id": [1, 2, 2, 3]})
        config = {
            "tables": [
                {"name": "users", "columns": []},
                {"name": "orders", "columns": []},
            ],
            "relationships": [
                {
                    "parent": {"table": "users", "columns": ["id"]},
                    "children": [
                        {"table": "orders", "columns": ["user_id"]},
                    ],
                }
            ],
        }
        report = run_storm_post_mask(
            source_frames={"users": users, "orders": orders},
            output_frames={"users": users, "orders": orders},
            config=config,
        )
        assert len(report["fk_preservation"]) == 1
        finding = report["fk_preservation"][0]
        assert finding["severity"] == "info"
        assert finding["orphan_count"] == 0

    def test_fk_preservation_orphans_flagged(self):
        """A child FK that doesn't resolve to any parent PK is an orphan."""
        users = pd.DataFrame({"id": [1, 2, 3]})
        # 999 has no matching user.
        orders = pd.DataFrame({"user_id": [1, 2, 2, 999, 999]})
        config = {
            "tables": [
                {"name": "users", "columns": []},
                {"name": "orders", "columns": []},
            ],
            "relationships": [
                {
                    "parent": {"table": "users", "columns": ["id"]},
                    "children": [
                        {"table": "orders", "columns": ["user_id"]},
                    ],
                }
            ],
        }
        report = run_storm_post_mask(
            source_frames={"users": users, "orders": orders},
            output_frames={"users": users, "orders": orders},
            config=config,
        )
        finding = report["fk_preservation"][0]
        # 2 of 5 child rows orphaned = 40% orphan rate -> fail
        assert finding["orphan_count"] == 2
        assert finding["total_child_rows"] == 5
        assert finding["orphan_rate"] == 0.4
        assert finding["severity"] == "fail"
