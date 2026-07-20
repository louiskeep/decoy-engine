"""DPS-3: compile-time DP generate contract (Task 5).

`global_settings.dp` hard-rejects the anti-DP generate-column knobs
(`allow_real_categories: true`, `high_cardinality: true`) at compile time.

Real-API reconciliation against the plan's representative sketch:
- No top-level `mode: generate` / `dp:` config key exists (FC-1 dropped
  the `mode` discriminator; per-table kind is inferred from `columns`
  vs `generate_columns`). The dp declaration lives at
  `global_settings.dp` (`config.DpGenerateSettings`), alongside the
  engine's other opt-in pipeline-wide knobs.
- The real compile error type is `PlanCompileError` (code, path,
  message), not a `ConfigError`.
- `compile_plan(config, profile, *, decoy_engine_version)` requires a
  `Profile`, not a bare dict; `run_config_only_checks(config)` is the
  existing profile-free entry point (mirrors
  `tests/unit/generation/test_statistical.py::TestCompileCheck`) and is
  used here instead, since the DP contract check is config-only.

Task 6 (consume-only contract lock) is added to this file separately.
"""

from __future__ import annotations

import pytest

from decoy_engine import run_config_only_checks
from decoy_engine.plan import PlanCompileError
from decoy_engine.plan._checks_dp import check_dp_generate_contract


def _dp_cfg(*, table_columns: list[dict]) -> dict:
    return {
        "global_settings": {"seed": 1, "dp": {"epsilon": 1.0, "delta": 1e-6}},
        "tables": [{"name": "t", "row_count": 5, "generate_columns": table_columns}],
    }


class TestCheckDpGenerateContract:
    """Direct unit coverage of check_dp_generate_contract (config-only)."""

    def test_dp_unset_never_raises_even_with_anti_dp_knobs(self):
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "t",
                    "generate_columns": [
                        {
                            "name": "dx",
                            "type": "statistical",
                            "snapshot_file": "whatever.json",
                            "allow_real_categories": True,
                            "high_cardinality": True,
                        }
                    ],
                }
            ],
        }
        check_dp_generate_contract(cfg)  # no raise

    def test_dp_set_without_anti_dp_knobs_passes(self):
        cfg = _dp_cfg(
            table_columns=[{"name": "dx", "type": "statistical", "snapshot_file": "whatever.json"}]
        )
        check_dp_generate_contract(cfg)  # no raise

    def test_dp_rejects_high_cardinality(self):
        cfg = _dp_cfg(
            table_columns=[
                {
                    "name": "dx",
                    "type": "statistical",
                    "snapshot_file": "whatever.json",
                    "allow_real_categories": True,
                    "high_cardinality": True,
                }
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            check_dp_generate_contract(cfg)
        assert exc.value.code == "dp_generate_high_cardinality_unsupported"
        assert "dx" in exc.value.message

    def test_dp_rejects_allow_real_categories(self):
        cfg = _dp_cfg(
            table_columns=[
                {
                    "name": "dx",
                    "type": "statistical",
                    "snapshot_file": "whatever.json",
                    "allow_real_categories": True,
                }
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            check_dp_generate_contract(cfg)
        assert exc.value.code == "dp_generate_allow_real_categories_unsupported"
        assert "dx" in exc.value.message

    def test_non_statistical_generate_column_ignored(self):
        cfg = _dp_cfg(table_columns=[{"name": "id", "type": "sequence"}])
        check_dp_generate_contract(cfg)  # no raise: gate only applies to type: statistical


class TestCompileIntegration:
    """Wired into the real compile chokepoint (config-only branch)."""

    def test_dp_rejects_high_cardinality_via_run_config_only_checks(self):
        cfg = _dp_cfg(
            table_columns=[
                {
                    "name": "dx",
                    "type": "statistical",
                    "snapshot_file": "whatever.json",
                    "allow_real_categories": True,
                    "high_cardinality": True,
                }
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "dp_generate_high_cardinality_unsupported"

    def test_dp_rejects_allow_real_categories_via_run_config_only_checks(self):
        cfg = _dp_cfg(
            table_columns=[
                {
                    "name": "dx",
                    "type": "statistical",
                    "snapshot_file": "whatever.json",
                    "allow_real_categories": True,
                }
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "dp_generate_allow_real_categories_unsupported"

    def test_dp_contract_check_surfaces_before_snapshot_artifact_check(self):
        # The dp-contract violation is a config-shape verdict independent
        # of whether the referenced snapshot artifact even exists; it
        # must surface on its OWN code, not fall through to
        # statistical_snapshot_unreadable from the missing file.
        cfg = _dp_cfg(
            table_columns=[
                {
                    "name": "dx",
                    "type": "statistical",
                    "snapshot_file": "/nonexistent/path.json",
                    "allow_real_categories": True,
                    "high_cardinality": True,
                }
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "dp_generate_high_cardinality_unsupported"
