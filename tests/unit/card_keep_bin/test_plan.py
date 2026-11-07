"""keep_bin at plan compile: accepted shapes, every refused shape with its code, the
join-group rules, and the three registry identities."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine.plan._checks_fpe import check_fpe_charset_config
from decoy_engine.plan._checks_fpe_join import check_fpe_join_groups
from decoy_engine.plan._compile import run_config_only_checks
from decoy_engine.plan._errors import PlanCompileError


def _col(name: str = "card", strategy: str = "fpe", **pc: Any) -> dict[str, Any]:
    return {"name": name, "strategy": strategy, "namespace": "ns", "provider_config": pc}


def _config(*columns: dict[str, Any]) -> dict[str, Any]:
    return {"tables": [{"name": "payments", "columns": list(columns)}]}


def _refusal(config: dict[str, Any]) -> PlanCompileError:
    with pytest.raises(PlanCompileError) as info:
        check_fpe_join_groups(config)
        check_fpe_charset_config(config)
    return info.value


@pytest.mark.parametrize("keep_bin", [6, 8])
@pytest.mark.parametrize("charset", [{}, {"charset": "digits"}, {"charset": "0123456789"}])
def test_accepted_shapes_compile(keep_bin: int, charset: dict[str, str]) -> None:
    config = _config(_col(checksum="luhn", keep_bin=keep_bin, **charset))
    check_fpe_charset_config(config)
    check_fpe_join_groups(config)


def test_null_keep_bin_is_off() -> None:
    check_fpe_charset_config(_config(_col(keep_bin=None)))
    check_fpe_charset_config(_config(_col(checksum="npi", keep_bin=None)))


@pytest.mark.parametrize(
    "value",
    [7, 0, -6, 16, "6", "", 6.0, True, False, [], [6], {"n": 6}],
    ids=repr,
)
def test_invalid_values_are_refused(value: Any) -> None:
    # The falsy cases fail a resolver that reads `cfg.get("keep_bin") or 0`.
    exc = _refusal(_config(_col(checksum="luhn", keep_bin=value)))
    assert exc.code == reasons.plan.FPE_KEEP_BIN_INVALID
    assert exc.path == "tables.payments.columns.card.provider_config.keep_bin"


@pytest.mark.parametrize(
    "pc",
    [
        {},
        {"checksum": "npi"},
        {"checksum": "ean13"},
        {"validate_luhn": True},
        {"checksum": "luhn", "charset": "alphanum"},
        {"checksum": "luhn", "charset": "9876543210"},
    ],
    ids=["no_checksum", "npi", "ean13", "validate_luhn_only", "alphanum", "reordered_digits"],
)
def test_keep_bin_needs_luhn_and_digits(pc: dict[str, Any]) -> None:
    exc = _refusal(_config(_col(keep_bin=6, **pc)))
    assert exc.code == reasons.plan.FPE_KEEP_BIN_REQUIRES_LUHN


@pytest.mark.parametrize("strategy", ["hash", "redact", "faker", "text_mask"])
def test_keep_bin_on_another_strategy_is_refused(strategy: str) -> None:
    exc = _refusal(_config(_col(strategy=strategy, keep_bin=6)))
    assert exc.code == reasons.plan.FPE_KEEP_BIN_NON_FPE_STRATEGY


def test_join_members_must_agree_on_keep_bin() -> None:
    a = _col("a", checksum="luhn", keep_bin=6, fpe_join_group="g")
    b = _col("b", checksum="luhn", keep_bin=8, fpe_join_group="g")
    with pytest.raises(PlanCompileError) as info:
        check_fpe_join_groups(_config(a, b))
    assert info.value.code == reasons.plan.FPE_JOIN_GROUP_CONFIG_MISMATCH
    assert "keep_bin" in info.value.message


def test_absent_and_null_keep_bin_join() -> None:
    a = _col("a", checksum="luhn", fpe_join_group="g")
    b = _col("b", checksum="luhn", keep_bin=None, fpe_join_group="g")
    check_fpe_join_groups(_config(a, b))


@pytest.mark.parametrize("value", [7, False, "6"], ids=repr)
def test_an_invalid_keep_bin_in_a_join_group_gets_its_own_code(value: Any) -> None:
    # The join check runs first at compile; it must not raise an uncoded error or a
    # mismatch for a value the keep_bin check refuses.
    a = _col("a", checksum="luhn", keep_bin=value, fpe_join_group="g")
    b = _col("b", checksum="luhn", fpe_join_group="g")
    with pytest.raises(PlanCompileError) as info:
        check_fpe_join_groups(_config(a, b))
    assert info.value.code == reasons.plan.FPE_KEEP_BIN_INVALID


def test_join_message_wording_is_unchanged_for_existing_keys() -> None:
    a = _col("a", charset="digits", fpe_join_group="g")
    b = _col("b", charset="alphanum", fpe_join_group="g")
    with pytest.raises(PlanCompileError) as info:
        check_fpe_join_groups(_config(a, b))
    assert "charset, preserve_separators, validate_luhn, and checksum" in info.value.message
    assert "['charset']" in info.value.message


def test_config_only_checks_refuse_too() -> None:
    config = {
        "version": 2,
        "global_settings": {"seed": 1},
        "sources": {"payments": {"type": "file", "format": "csv", "path": "in.csv"}},
        "tables": [{"name": "payments", "columns": [_col(checksum="luhn", keep_bin=7)]}],
        "targets": {"payments": {"type": "file", "format": "csv", "path": "out.csv"}},
    }
    with pytest.raises(PlanCompileError) as info:
        run_config_only_checks(config)
    assert info.value.code == reasons.plan.FPE_KEEP_BIN_INVALID


# --- registry ------------------------------------------------------------------------------

_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "f29"
    / "reason-manifest-v1.7-card-keep-bin.json"
)
_CODES = ("fpe_keep_bin_invalid", "fpe_keep_bin_non_fpe_strategy", "fpe_keep_bin_requires_luhn")


def test_the_three_codes_are_registered_and_pinned() -> None:
    live = reasons.manifest()
    entries = [
        e for e in live["entries"] if (e["namespace"], e["code"]) in {("plan", c) for c in _CODES}
    ]
    assert sorted(e["code"] for e in entries) == list(_CODES)
    assert all(e["since"] == "1.7.0" and e["owner"] == "PlanCompileError" for e in entries)
    pinned = json.loads(_FIXTURE.read_text())
    assert pinned["reason_schema_version"] == live["reason_schema_version"]
    assert pinned["entries"] == sorted(entries, key=lambda e: e["code"])
