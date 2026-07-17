"""HC-7: plan-compile check for the clinical free-text advisory.

Two layers, mirroring the sibling per-strategy check test files:
`TestCheckFreetextAdvisory*` exercises `check_freetext_advisory` directly
against local config/profile fixtures (self-contained, like
`test_checks_top_code.py`); `TestCompileIntegration` exercises the full
`compile_plan` chokepoint using the shared `simple_config` / `simple_profile`
fixtures from `conftest.py`, confirming the advisory compiles successfully
(warn, never raise) and does not perturb `pipeline_config_hash`.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from decoy_engine.plan._checks_freetext_advisory import check_freetext_advisory
from decoy_engine.plan._compile import compile_plan
from decoy_engine.profile import ColumnProfile, Profile, TableProfile


def _col(
    name: str,
    *,
    dtype: str = "object",
    row_count: int = 1000,
    null_count: int = 0,
    distinct_count: int | None = 900,
    avg_length: float | None = None,
    max_length: int | None = None,
) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype=dtype,
        row_count=row_count,
        null_count=null_count,
        distinct_count=distinct_count,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
        avg_length=avg_length,
        max_length=max_length,
    )


def _profile(*tables: TableProfile) -> Profile:
    return Profile(
        schema_version=1,
        tables=tables,
        relationships=(),
        profiled_at=datetime(2026, 7, 17, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


def _config(columns: list[dict[str, Any]], *, table_name: str = "t") -> dict[str, Any]:
    return {"tables": [{"name": table_name, "columns": columns}]}


class TestCheckFreetextAdvisoryColumnSelection:
    def test_unmasked_clinical_notes_with_profile_stats_warns(self) -> None:
        profile = _profile(
            TableProfile(
                name="t",
                row_count=1000,
                columns=(_col("clinical_notes", avg_length=210.0, distinct_count=980),),
            )
        )
        config = _config([{"name": "clinical_notes", "strategy": "passthrough"}])
        warnings = check_freetext_advisory(config, profile)
        assert len(warnings) == 1
        assert "clinical_notes" in warnings[0]

    def test_masked_column_never_warns(self) -> None:
        profile = _profile(
            TableProfile(
                name="t",
                row_count=1000,
                columns=(_col("clinical_notes", avg_length=210.0, distinct_count=980),),
            )
        )
        config = _config([{"name": "clinical_notes", "strategy": "text_mask"}])
        assert check_freetext_advisory(config, profile) == ()

    def test_column_missing_from_profile_degrades_to_name_hint_only(self) -> None:
        # No matching ColumnProfile at all (e.g. a stale profile). Name
        # hint must still fire; a non-hinted oddly-named column must not.
        profile = _profile(TableProfile(name="t", row_count=1000, columns=()))
        config = _config([{"name": "clinical_notes", "strategy": "passthrough"}])
        warnings = check_freetext_advisory(config, profile)
        assert len(warnings) == 1

        config_no_hint = _config([{"name": "field_7", "strategy": "passthrough"}])
        assert check_freetext_advisory(config_no_hint, profile) == ()

    def test_non_string_profile_column_never_warns(self) -> None:
        profile = _profile(
            TableProfile(
                name="t",
                row_count=1000,
                columns=(_col("clinical_notes", dtype="int64", avg_length=None),),
            )
        )
        config = _config([{"name": "clinical_notes", "strategy": "passthrough"}])
        assert check_freetext_advisory(config, profile) == ()

    def test_icd10_style_column_does_not_warn(self) -> None:
        profile = _profile(
            TableProfile(
                name="t",
                row_count=1000,
                columns=(_col("diagnosis_code", avg_length=6.1, distinct_count=970),),
            )
        )
        config = _config([{"name": "diagnosis_code", "strategy": "passthrough"}])
        assert check_freetext_advisory(config, profile) == ()

    def test_multi_table_lookup_uses_table_and_column_name(self) -> None:
        # Two tables can each have a "notes" column; the (table, column)
        # index must not cross-contaminate.
        profile = _profile(
            TableProfile(
                name="t1",
                row_count=1000,
                columns=(_col("notes", avg_length=200.0, distinct_count=900),),
            ),
            TableProfile(
                name="t2",
                row_count=1000,
                columns=(_col("notes", dtype="int64", avg_length=None),),
            ),
        )
        config = {
            "tables": [
                {"name": "t1", "columns": [{"name": "notes", "strategy": "passthrough"}]},
                {"name": "t2", "columns": [{"name": "notes", "strategy": "passthrough"}]},
            ]
        }
        warnings = check_freetext_advisory(config, profile)
        # t1.notes (string, long) warns; t2.notes (int64) never does, even
        # though the column name is identical.
        assert len(warnings) == 1

    def test_does_not_mutate_config_or_profile(self) -> None:
        profile = _profile(
            TableProfile(
                name="t",
                row_count=1000,
                columns=(_col("clinical_notes", avg_length=210.0, distinct_count=980),),
            )
        )
        config = _config([{"name": "clinical_notes", "strategy": "passthrough"}])
        config_before = copy.deepcopy(config)
        profile_before = copy.deepcopy(profile)
        check_freetext_advisory(config, profile)
        assert config == config_before
        assert profile == profile_before

    def test_disabled_via_sentinel_threshold(self) -> None:
        profile = _profile(
            TableProfile(
                name="t",
                row_count=1000,
                columns=(_col("clinical_notes", avg_length=210.0, distinct_count=980),),
            )
        )
        config = _config([{"name": "clinical_notes", "strategy": "passthrough"}])
        config["global_settings"] = {"freetext_advisory_min_avg_length": 0.0}
        assert check_freetext_advisory(config, profile) == ()


class TestCompileIntegration:
    def test_unmasked_clinical_notes_compiles_and_warns(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        # simple_profile's "customers" table gets an extra unmasked
        # clinical_notes column; simple_config leaves it undeclared as a
        # real strategy (explicit passthrough), which must compile
        # successfully (warn, not error) and surface the advisory.
        customers = simple_profile.tables[0]
        extra_col = _col("clinical_notes", avg_length=220.0, distinct_count=9)
        new_customers = TableProfile(
            name=customers.name,
            row_count=customers.row_count,
            columns=(*customers.columns, extra_col),
        )
        profile = Profile(
            schema_version=simple_profile.schema_version,
            tables=(new_customers, *simple_profile.tables[1:]),
            relationships=simple_profile.relationships,
            profiled_at=simple_profile.profiled_at,
            decoy_engine_version=simple_profile.decoy_engine_version,
        )
        config = copy.deepcopy(simple_config)
        config["tables"][0]["columns"].append({"name": "clinical_notes", "strategy": "passthrough"})
        plan = compile_plan(config, profile, decoy_engine_version="0.1.0")
        assert "freetext_advisory" in plan.plan_compile.checks_passed
        assert any(
            "freetext_advisory" in w and "clinical_notes" in w for w in plan.plan_compile.warnings
        )

    def test_default_knob_does_not_perturb_pipeline_config_hash(
        self, simple_config: dict, simple_profile: Profile
    ) -> None:
        p1 = compile_plan(simple_config, simple_profile, decoy_engine_version="0.1.0")

        with_knob = copy.deepcopy(simple_config)
        with_knob["global_settings"]["freetext_advisory_min_avg_length"] = 999.0
        with_knob["global_settings"]["freetext_advisory_min_distinctness"] = 0.99
        p2 = compile_plan(with_knob, simple_profile, decoy_engine_version="0.1.0")

        assert p1.pipeline_config_hash == p2.pipeline_config_hash
