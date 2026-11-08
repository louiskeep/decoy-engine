"""Compile refusals for date_shift entity_key, on every compile entry point."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import reasons
from decoy_engine.plan import compile_plan, run_config_only_checks
from decoy_engine.plan._checks_date_shift import (
    _ENTITY_FAULT_CODE,
    check_date_preserve_values,
)
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile import profile_source
from decoy_engine.transforms.dates.entity_offset import FaultKind
from tests.unit.transforms.date_shift_entity._support import (
    config,
    date_col,
    passthrough,
    write_sources,
)

INVALID = reasons.plan.DATE_SHIFT_ENTITY_KEY_INVALID
COMBO = reasons.plan.DATE_SHIFT_ENTITY_KEY_UNSUPPORTED_COMBINATION
INCONSISTENT = reasons.plan.DATE_SHIFT_ENTITY_KEY_INCONSISTENT
PC = "tables.{t}.columns.{c}.provider_config"

ENTRIES = ["compile_plan", "compile_plan_no_profile", "config_only"]


def _job(tmp: Path, tables: dict[str, list[dict[str, Any]]], **kw: Any) -> dict[str, Any]:
    cfg = config(tmp, tables, **kw)
    sources = {}
    for name, cols in tables.items():
        sources[name] = pa.table(
            {c["name"]: ["2020-01-01", "2020-02-01"] for c in cols} | {"pid": ["p1", "p2"]}
        )
    write_sources(tmp, sources)
    return cfg


def _compile(cfg: dict[str, Any], entry: str) -> Any:
    if entry == "config_only":
        return run_config_only_checks(cfg)
    profile = profile_source(cfg, seed=7)
    return compile_plan(
        cfg, profile, decoy_engine_version="0.1.0", no_profile=entry.endswith("no_profile")
    )


def _refused(cfg: dict[str, Any], entry: str, code: str, path: str) -> None:
    with pytest.raises(PlanCompileError) as info:
        _compile(cfg, entry)
    err = info.value
    assert (err.code, err.path) == (code, path)
    assert err.__cause__ is None and err.__context__ is None
    assert "SENTINEL" not in str(err)


_ONE_TABLE_CASES: list[tuple[str, dict[str, Any], dict[str, Any], str, str]] = [
    ("not_a_string", {"entity_key": 123}, {}, INVALID, PC + ".entity_key"),
    ("explicit_null", {"entity_key": None}, {}, INVALID, PC + ".entity_key"),
    ("blank", {"entity_key": "  "}, {}, INVALID, PC + ".entity_key"),
    ("missing_column", {"entity_key": "nope"}, {}, INVALID, PC + ".entity_key"),
    ("self", {"entity_key": "d"}, {}, INVALID, PC + ".entity_key"),
    ("bool_bound", {"entity_key": "pid", "min_days": True}, {}, INVALID, PC + ".min_days"),
    ("str_bound", {"entity_key": "pid", "max_days": "5"}, {}, INVALID, PC + ".max_days"),
    ("float_bound", {"entity_key": "pid", "max_days": 5.0}, {}, INVALID, PC + ".max_days"),
    ("big_bound", {"entity_key": "pid", "max_days": 106_752}, {}, INVALID, PC + ".max_days"),
    ("typo_dash", {"entity-key": "pid"}, {}, INVALID, PC + ".entity-key"),
    ("typo_camel", {"EntityKey": "pid"}, {}, INVALID, PC + ".EntityKey"),
    ("typo_swap", {"entity_kye": "pid"}, {}, INVALID, PC + ".entity_kye"),
    ("typo_drop", {"enity_key": "pid"}, {}, INVALID, PC + ".enity_key"),
    ("with_group_by", {"entity_key": "pid", "group_by": "pid"}, {}, COMBO, PC + ".group_by"),
    (
        "with_preserve",
        {"entity_key": "pid", "preserve_values": ["9999-12-31"]},
        {},
        COMBO,
        PC + ".preserve_values",
    ),
    (
        "with_when",
        {"entity_key": "pid"},
        {"when": "pid == 'p1'"},
        COMBO,
        "tables.{t}.columns.{c}.when",
    ),
]


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize(
    ("label", "pc", "extra", "code", "path"), _ONE_TABLE_CASES, ids=[c[0] for c in _ONE_TABLE_CASES]
)
def test_single_column_refusals(
    tmp_path: Path,
    entry: str,
    label: str,
    pc: dict[str, Any],
    extra: dict[str, Any],
    code: str,
    path: str,
) -> None:
    col = date_col("d", **pc) | extra
    cfg = _job(tmp_path, {"t": [passthrough("pid"), col]})
    _refused(cfg, entry, code, path.format(t="t", c="d"))


@pytest.mark.parametrize("entry", ENTRIES)
def test_valid_configs_and_ignored_keys_pass(tmp_path: Path, entry: str) -> None:
    cfg = _job(
        tmp_path,
        {
            "t": [
                passthrough("pid"),
                date_col(
                    "d", entity_key="pid", min_days=-9, max_days=9, entity_keys_note="x", other=1
                ),
                date_col("e", entity_key="pid", max_days=-9, min_days=9),  # swapped: same range
            ]
        },
    )
    _compile(cfg, entry)


@pytest.mark.parametrize("entry", ENTRIES)
def test_nested_child_is_refused(tmp_path: Path, entry: str) -> None:
    nested = {
        "name": "d",
        "strategy": "nested",
        "provider_config": {
            "format": "json",
            "path": "$.when",
            "strategy": "date_shift",
            "strategy_config": {"entity_key": "pid"},
        },
    }
    cfg = _job(tmp_path, {"t": [passthrough("pid"), nested]})
    _refused(cfg, entry, COMBO, "tables.t.columns.d.provider_config.strategy_config.entity_key")


@pytest.mark.parametrize("entry", ENTRIES)
def test_fk_date_column_is_refused(tmp_path: Path, entry: str) -> None:
    rel = [
        {
            "parent": {"table": "a", "columns": ["d"]},
            "children": [{"table": "b", "columns": ["d"]}],
            "orphan_policy": "warn",
        }
    ]
    cfg = _job(
        tmp_path,
        {
            "a": [passthrough("pid"), date_col("d", entity_key="pid")],
            "b": [passthrough("pid"), date_col("d")],
        },
        relationships=rel,
    )
    _refused(cfg, entry, COMBO, "tables.a.columns.d.provider_config.entity_key")


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("which", ["min", "max"])
def test_one_namespace_one_range(tmp_path: Path, entry: str, which: str) -> None:
    second = {"min_days": -10} if which == "min" else {"max_days": 10}
    cfg = _job(
        tmp_path,
        {
            "a": [passthrough("pid"), date_col("d", entity_key="pid")],
            "b": [passthrough("pid"), date_col("d", entity_key="pid", **second)],
        },
    )
    _refused(cfg, entry, INCONSISTENT, "tables.b.columns.d.provider_config")


@pytest.mark.parametrize("entry", ENTRIES)
def test_implicit_and_explicit_default_range_agree(tmp_path: Path, entry: str) -> None:
    cfg = _job(
        tmp_path,
        {
            "a": [passthrough("pid"), date_col("d", entity_key="pid")],
            "b": [passthrough("pid"), date_col("d", entity_key="pid", min_days=-365, max_days=365)],
        },
    )
    _compile(cfg, entry)


@pytest.mark.parametrize("entry", ENTRIES)
def test_one_entity_column_one_namespace_per_table(tmp_path: Path, entry: str) -> None:
    cfg = _job(
        tmp_path,
        {
            "t": [
                passthrough("pid"),
                date_col("d", entity_key="pid"),
                date_col("e", entity_key="pid", namespace="other_ns"),
            ]
        },
    )
    _refused(cfg, entry, INCONSISTENT, "tables.t.columns.e.namespace")


def test_preserve_check_alone_raises_the_entity_code() -> None:
    cfg = {
        "tables": [
            {
                "name": "t",
                "columns": [
                    {"name": "pid", "strategy": "passthrough"},
                    {
                        "name": "d",
                        "strategy": "date_shift",
                        "namespace": "n",
                        "provider_config": {
                            "entity_key": "pid",
                            "date_format": "%Y-%m-%d",
                            "preserve_values": ["9999-12-31"],
                        },
                    },
                ],
            }
        ]
    }
    with pytest.raises(PlanCompileError) as info:
        check_date_preserve_values(copy.deepcopy(cfg))
    assert info.value.code == COMBO
    assert info.value.path == "tables.t.columns.d.provider_config.entity_key"


def test_every_fault_kind_has_a_plan_code() -> None:
    assert set(_ENTITY_FAULT_CODE) == set(FaultKind)
