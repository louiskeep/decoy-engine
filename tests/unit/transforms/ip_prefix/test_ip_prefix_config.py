"""ip_prefix config: the one parser, its compile and run codes, and malformed cells."""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine.execution._errors import StrategyError
from decoy_engine.plan import compile_plan, run_config_only_checks
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms.network import ip_prefix
from decoy_engine.transforms.network.ip_prefix import (
    FaultKind,
    IpPrefixConfig,
    IpPrefixConfigFault,
    parse_ip_prefix_config,
)
from tests.unit.transforms.ip_prefix import _support as s

_INVALID = [True, False, "24", 24.0, -1, None, [24]]
_TOO_MANY = {"keep_bits_v4": [31, 32, 33], "keep_bits_v6": [127, 128, 200]}


def test_defaults_and_bounds_parse() -> None:
    assert parse_ip_prefix_config({}) == IpPrefixConfig(24, 48)
    assert parse_ip_prefix_config(None) == IpPrefixConfig(24, 48)
    assert parse_ip_prefix_config({"keep_bits_v4": 0, "keep_bits_v6": 0}) == IpPrefixConfig(0, 0)
    assert parse_ip_prefix_config({"keep_bits_v4": 30, "keep_bits_v6": 126}) == IpPrefixConfig(
        30, 126
    )


def test_every_fault_kind_has_a_plan_code() -> None:
    assert set(ip_prefix._PLAN_CODE) == set(FaultKind)


def _compile_paths(cfg: dict[str, Any]) -> list[PlanCompileError]:
    """The refusal from both compile branches and the config-only checks."""
    errors = []
    prof = s.profile(**{t["name"]: [c["name"] for c in t["columns"]] for t in cfg["tables"]})
    for call in (
        lambda: compile_plan(cfg, prof, decoy_engine_version=s.ENGINE_VERSION),
        lambda: compile_plan(cfg, prof, decoy_engine_version=s.ENGINE_VERSION, no_profile=True),
        lambda: run_config_only_checks(cfg),
    ):
        with pytest.raises(PlanCompileError) as caught:
            call()
        errors.append(caught.value)
    return errors


def _assert_refused(cfg: dict[str, Any], code: str, path: str) -> None:
    for err in _compile_paths(cfg):
        assert err.code == code
        assert err.path == path
        assert err.__cause__ is None and err.__context__ is None
        assert s.SENTINEL_V4 not in str(err)


@pytest.mark.parametrize("option", ["keep_bits_v4", "keep_bits_v6"])
@pytest.mark.parametrize("value", _INVALID)
def test_invalid_keep_bits_is_refused(option: str, value: object) -> None:
    assert parse_ip_prefix_config({option: value}) == IpPrefixConfigFault(
        FaultKind.KEEP_BITS_INVALID, option
    )
    cfg = s.config(("t", [s.column(**{option: value})]))
    _assert_refused(
        cfg,
        reasons.plan.IP_PREFIX_KEEP_BITS_INVALID,
        f"tables.t.columns.ip.provider_config.{option}",
    )


@pytest.mark.parametrize(("option", "value"), [(o, v) for o, vs in _TOO_MANY.items() for v in vs])
def test_too_many_kept_bits_is_refused(option: str, value: int) -> None:
    cfg = s.config(("t", [s.column(**{option: value})]))
    _assert_refused(
        cfg,
        reasons.plan.IP_PREFIX_KEEPS_TOO_MANY_BITS,
        f"tables.t.columns.ip.provider_config.{option}",
    )


def test_unknown_option_is_refused() -> None:
    cfg = s.config(("t", [s.column(keep_bit_v4=8)]))
    _assert_refused(
        cfg,
        reasons.plan.IP_PREFIX_UNKNOWN_OPTION,
        "tables.t.columns.ip.provider_config.keep_bit_v4",
    )


def test_nested_child_is_refused() -> None:
    nested = {
        "name": "ip",
        "strategy": "nested",
        "namespace": "ip_ns",
        "provider_config": {"strategy": "ip_prefix", "paths": ["$.ip"]},
    }
    _assert_refused(
        s.config(("t", [nested])),
        reasons.plan.IP_PREFIX_UNSUPPORTED_IN_NESTED,
        "tables.t.columns.ip.provider_config.strategy",
    )


@pytest.mark.parametrize("option", ["keep_bits_v4", "keep_bits_v6"])
def test_one_namespace_with_two_keep_lengths_is_refused(option: str) -> None:
    cfg = s.config(
        ("a", [s.column(**{option: 16})]),
        ("b", [s.column()]),
    )
    _assert_refused(
        cfg,
        reasons.plan.IP_PREFIX_NAMESPACE_KEEP_BITS_MISMATCH,
        "tables.b.columns.ip.provider_config",
    )


def test_resolved_keep_bits_compare_so_an_explicit_default_matches() -> None:
    cfg = s.config(("a", [s.column(keep_bits_v4=24)]), ("b", [s.column()]))
    assert "fpe_join_groups" in run_config_only_checks(cfg)
    other_ns = s.config(("a", [s.column(keep_bits_v4=16)]), ("b", [s.column(namespace="b_ns")]))
    run_config_only_checks(other_ns)


def test_the_handler_refuses_a_raw_plan_with_the_runtime_code() -> None:
    for pc in ({"keep_bits_v4": True}, {"keep_bits_v6": 128}, {"keep_bit_v4": 8}):
        with pytest.raises(StrategyError) as caught:
            s.run_handler(["10.0.0.1"], pc)
        assert caught.value.code == reasons.runtime.IP_PREFIX_CONFIG_INVALID
        assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_the_handler_refuses_a_missing_namespace() -> None:
    import pandas as pd

    from decoy_engine.execution._strategies.network._ip_prefix import IpPrefixStrategyHandler

    df = pd.DataFrame({"ip": ["10.0.0.1"]})
    with pytest.raises(StrategyError) as caught:
        IpPrefixStrategyHandler().run(df, "ip", s.seed(namespace=None), s.FakeCtx())  # type: ignore[arg-type]
    assert caught.value.code == reasons.runtime.IP_PREFIX_REQUIRES_NAMESPACE


_MALFORMED: list[object] = [
    "",
    "1.2.3",
    "010.0.0.1",
    " 10.0.0.1",
    "10.0.0.1\n",
    "\u0661\u0660.0.0.1",  # Arabic-Indic digits
    "\uff110.0.0.1",  # a full-width digit
    "10.0.0.0/8",
    "fe80::1%eth0",
    "0x1.2.3.4",
    167772161,
    1.5,
    s.SENTINEL_BAD,
]


def test_each_malformed_cell_is_one_format_error_on_its_own_row() -> None:
    values = ["10.0.0.1", *_MALFORMED, None, "2001:db8::1"]
    out, ctx = s.run_handler(values)
    bad_rows = list(range(1, 1 + len(_MALFORMED)))
    assert [e.row_index for e in ctx.row_errors] == bad_rows
    assert {e.trigger for e in ctx.row_errors} == {"format_error"}
    assert {e.column for e in ctx.row_errors} == {"ip"}
    for err in ctx.row_errors:
        assert s.SENTINEL_BAD not in err.reason
    assert out[0] != "10.0.0.1" and out[0].startswith("10.0.0.")
    assert out[len(values) - 2] is None
    assert out[-1].startswith("2001:db8:")
