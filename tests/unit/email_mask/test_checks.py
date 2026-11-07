"""Compile-time refusal of bad email_mask configs (AT-4) and the dark guarantee (AT-7)."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.plan import PlanCompileError, compile_plan, plan_to_yaml, run_config_only_checks
from decoy_engine.profile import profile_source
from tests.unit.email_mask import _support as s
from tests.unit.execution import _auto_chunk_support as support


def _bad_columns() -> list[tuple[str, dict[str, Any], str, str]]:
    """(id, column entry, expected code, path suffix)."""
    rows: list[tuple[str, dict[str, Any], str, str]] = []
    for i, keep in enumerate([True, 1, "Domain", "domain ", None, "domain_class\n", 1.5]):
        rows.append((f"keep{i}", s.column(keep), "email_mask_keep_invalid", "provider_config.keep"))
    missing = s.column()
    missing["provider_config"] = {}
    rows.append(("keep_missing", missing, "email_mask_keep_invalid", "provider_config.keep"))
    unknown = s.column()
    unknown["provider_config"]["keep_domain"] = True
    rows.append(("unknown", unknown, "email_mask_option_unknown", "provider_config.keep_domain"))
    no_ns = s.column()
    del no_ns["namespace"]
    rows.append(("no_namespace", no_ns, "email_mask_requires_namespace", "namespace"))
    for j, child in enumerate([{"keep": "domain"}, {"keep": "nope"}]):
        nested = {
            "name": "email",
            "strategy": "nested",
            "namespace": s.NAMESPACE,
            "provider_config": {
                "target": "$.e",
                "strategy": "email_mask",
                "strategy_config": child,
            },
        }
        rows.append(
            (f"nested{j}", nested, "email_mask_unsupported_in_nested", "provider_config.strategy")
        )
    return rows


BAD = _bad_columns()


def _profiled(tmp_path: Path, column: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    source = pa.table({"email": pa.array(["a@acme.com", "b@gmail.com"])})
    cfg = s.raw_config([column], path=s.write(source, tmp_path / "s.parquet"))
    return cfg, profile_source(cfg)


def _check_error(err: PlanCompileError, code: str, suffix: str, column: dict[str, Any]) -> None:
    assert err.code == code
    assert err.path == f"tables.t.columns.email.{suffix}"
    keep = (column.get("provider_config") or {}).get("keep")
    if isinstance(keep, str) and keep:
        assert repr(keep) not in err.message
    assert err.__cause__ is None and err.__context__ is None


@pytest.mark.parametrize(("case", "column", "code", "suffix"), BAD, ids=[b[0] for b in BAD])
def test_config_only_refuses(case: str, column: dict[str, Any], code: str, suffix: str) -> None:
    with pytest.raises(PlanCompileError) as caught:
        run_config_only_checks(s.raw_config([column]))
    _check_error(caught.value, code, suffix, column)


@pytest.mark.parametrize("no_profile", [False, True])
@pytest.mark.parametrize(("case", "column", "code", "suffix"), BAD, ids=[b[0] for b in BAD])
def test_compile_refuses(
    case: str, column: dict[str, Any], code: str, suffix: str, no_profile: bool, tmp_path: Path
) -> None:
    cfg, profile = _profiled(tmp_path, column)
    with pytest.raises(PlanCompileError) as caught:
        compile_plan(cfg, profile, decoy_engine_version=s.ENGINE_VERSION, no_profile=no_profile)
    _check_error(caught.value, code, suffix, column)


@pytest.mark.parametrize("keep", ["domain", "domain_class"])
def test_a_valid_config_passes_and_reports_once(keep: str, tmp_path: Path) -> None:
    names = run_config_only_checks(s.raw_config([s.column(keep)]))
    assert names.count("email_mask_config") == 1
    cfg, profile = _profiled(tmp_path, s.column(keep))
    for no_profile in (False, True):
        plan = compile_plan(cfg, profile, decoy_engine_version="v", no_profile=no_profile)
        assert plan.plan_compile.checks_passed.count("email_mask_config") == 1


# --- AT-7: a config without email_mask is unchanged from main dd31ea91 ---
# Captured on main before the change, from the fixture below.

MAIN_CONFIG_ONLY = (
    "unknown_provider",
    "when_with_coherent_with",
    "deterministic_namespace_completeness",
    "non_poolable_provider_with_pool_backend",
    "dp_generate_contract",
    "statistical_columns",
    "text_redact_ner_available",
    "text_mask_ner_available",
    "vault_columns",
    "fpe_checksum_scheme",
    "derived_column_refs",
    "derived_aggregate_refs",
    "grouped_series_refs",
    "windowed_date_refs",
    "group_key_refs",
    "date_shift_group_by_refs",
    "fpe_join_groups",
    "truncate_config",
    "bucketize_config",
    "top_code_config",
    "bucket_perturb_config",
    "categorical_categories",
    "fpe_charset_config",
    "faker_requires_provider",
)
_PROFILED_HEAD = (
    "namespace_ambiguity",
    "unknown_provider",
    "fk_plan_ordering",
    "basic_uniqueness_pre_flight",
    "composite_columns_length_match",
    "orphan_fk_policy_completeness",
    "pool_capacity_pre_flight",
    "composite_wiring_consistent",
    "deterministic_namespace_completeness",
    "null_bearing_int_unsupported",
    "when_string_output_on_numeric_unsupported",
)
_NO_PROFILE_HEAD = (
    "namespace_ambiguity",
    "unknown_provider",
    "fk_plan_ordering",
    "composite_columns_length_match",
    "orphan_fk_policy_completeness",
    "composite_wiring_consistent",
    "deterministic_namespace_completeness",
)
MAIN_PROFILED = _PROFILED_HEAD + MAIN_CONFIG_ONLY[3:] + ("freetext_advisory",)
MAIN_NO_PROFILE = _NO_PROFILE_HEAD + MAIN_CONFIG_ONLY[3:] + ("freetext_advisory",)
MAIN_YAML_SHA256 = {
    False: "77fce76eec6197d5254d06b1e948d05a569c71e43cb41bb3df52a2eacdd6c092",
    True: "eebd0b0735d1d9a78cf347c23da5fa0304c88f457f4df4d75d99e990906d708f",
}


def _dark_config(tmp_path: Path) -> tuple[dict[str, Any], Any]:
    src = pa.table(
        {
            "val": pa.array([f"user{i}@example.com" for i in range(20)]),
            "n": pa.array(list(range(20))),
        }
    )
    path = support.write_source(src, str(tmp_path / "s.parquet"))
    cfg = support.make_cfg(
        [
            {"name": "val", "strategy": "hash", "namespace": "ns_val"},
            {"name": "n", "strategy": "redact"},
        ],
        path=path,
    )
    return cfg, profile_source(cfg)


def test_a_config_without_email_mask_reports_mains_checks(tmp_path: Path) -> None:
    cfg, profile = _dark_config(tmp_path)
    before = copy.deepcopy(cfg)
    assert run_config_only_checks(cfg) == MAIN_CONFIG_ONLY
    profiled = compile_plan(cfg, profile, decoy_engine_version="dark-pin")
    bare = compile_plan(cfg, profile, decoy_engine_version="dark-pin", no_profile=True)
    assert profiled.plan_compile.checks_passed == MAIN_PROFILED
    assert bare.plan_compile.checks_passed == MAIN_NO_PROFILE
    assert cfg == before


@pytest.mark.parametrize("no_profile", [False, True])
def test_a_config_without_email_mask_serialises_as_on_main(
    no_profile: bool, tmp_path: Path
) -> None:
    cfg, profile = _dark_config(tmp_path)
    plan = compile_plan(cfg, profile, decoy_engine_version="dark-pin", no_profile=no_profile)
    digest = hashlib.sha256(plan_to_yaml(plan).encode()).hexdigest()
    assert digest == MAIN_YAML_SHA256[no_profile]
