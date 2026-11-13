"""A formula that uses a number reference as text is refused at compile (validate and run)."""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine import run_config_only_checks
from decoy_engine.plan import PlanCompileError, compile_plan
from tests.unit._dps_helpers import empty_profile

COMPARE = (
    "A formula uses a number reference as text. At table 't', column 'f', option 'formula', "
    "reference 'a'. Hint: compare with a number."
)
OTHER = (
    "A formula uses a number reference as text. At table 't', column 'f', option 'formula', "
    "reference 'a'. Hint: wrap the reference in str()."
)


def _cfg(expr: str, a: dict[str, Any] | None = None, refs: list[str] | None = None) -> dict:
    cols = [
        a or {"name": "a", "type": "sequence", "start": 1},
        {"name": "b", "type": "sequence", "start": 1},
        {"name": "code", "type": "categorical", "categories": ["x", "y"]},
        {"name": "r", "type": "reference", "reference_table": "p", "reference_column": "id"},
        {"name": "f", "type": "formula", "formula": expr, "references": refs or ["a", "b"]},
    ]
    parent = {
        "name": "p",
        "row_count": 3,
        "generate_columns": [{"name": "id", "type": "sequence", "start": 1}],
    }
    return {
        "global_settings": {"seed": 1},
        "tables": [parent, {"name": "t", "row_count": 3, "generate_columns": cols}],
    }


def _compile(config: dict) -> Any:
    return compile_plan(
        config, empty_profile(), decoy_engine_version="test", instance_default_locale=None
    )


def _refused(config: dict) -> None:
    for site in (run_config_only_checks, _compile):
        with pytest.raises(PlanCompileError) as info:
            site(config)
        assert info.value.code == "generate_formula_number_used_as_text"
        assert info.value.path == "tables.t.generate_columns.f.formula"


def _message(expr: str, refs: list[str]) -> str:
    with pytest.raises(PlanCompileError) as info:
        _compile(_cfg(expr, refs=refs))
    return info.value.message


_COMPARE = [
    ("a == '1'", ["a"]),
    ("'1' != a", ["a"]),
    ("a in ('1', '2')", ["a"]),
    ("a not in ['x']", ["a"]),
    ("a == code", ["a", "code"]),
    ("a in code", ["a", "code"]),
    ("a not in code", ["a", "code"]),
]
_OTHER = [
    ("'ORD-' + a", ["a"]),
    ("a + 'x'", ["a"]),
    ("f'x' + a", ["a"]),
    ("a.zfill(5)", ["a"]),
    ("a.upper()", ["a"]),
    ("a[0]", ["a"]),
    ("a[-4:]", ["a"]),
    ("len(a)", ["a"]),
    ("'-'.join([a, b])", ["a", "b"]),
]


@pytest.mark.parametrize(("expr", "refs"), _COMPARE + _OTHER)
def test_text_use_is_refused_at_both_sites(expr: str, refs: list[str]) -> None:
    _refused(_cfg(expr, refs=refs))


@pytest.mark.parametrize(("expr", "refs"), _COMPARE)
def test_compare_message_is_exact(expr: str, refs: list[str]) -> None:
    assert _message(expr, refs) == COMPARE


@pytest.mark.parametrize(("expr", "refs"), _OTHER)
def test_other_message_is_exact(expr: str, refs: list[str]) -> None:
    assert _message(expr, refs) == OTHER


def test_alias_to_a_sequence_parent_is_refused() -> None:
    config = _cfg("'ORD-' + r", refs=["r"])
    for site in (run_config_only_checks, _compile):
        with pytest.raises(PlanCompileError) as info:
            site(config)
        assert info.value.code == "generate_formula_number_used_as_text"
        assert "reference 'r'" in info.value.message


@pytest.mark.parametrize(
    "expr",
    [
        "'ORD-' + str(a)",
        "f'ORD-{a}'",
        "a == 1",
        "a in (1, 2)",
        "a == b",
        "str(a).zfill(5)",
        "str(a)[-4:]",
        "len(str(a))",
        "len([a for a in ['x']])",
    ],
)
def test_number_safe_spellings_are_accepted(expr: str) -> None:
    config = _cfg(expr)
    run_config_only_checks(config)
    _compile(config)


@pytest.mark.parametrize("expr", ["'ORD-' + a", "a.zfill(5)", "a[0]", "a == '1'", "len(a)"])
def test_same_spellings_pass_on_a_prefixed_sequence(expr: str) -> None:
    config = _cfg(expr, a={"name": "a", "type": "sequence", "start": 1, "prefix": "P-"})
    run_config_only_checks(config)
    _compile(config)


def test_message_holds_no_formula_text() -> None:
    assert "ORD-" not in _message("'ORD-' + a", ["a"])
