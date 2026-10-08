"""C8-iii-c acceptance test 1: a raw-dict `when` outside the closed grammar fails at compile.

Plan: docs/plans/2026-10-07-c8-iii-c-rawdict-when.md (rev 3), section 2b. Both compile
entrypoints (`compile_plan` and `run_config_only_checks`) reject the same inputs with the same
code and path, and the message never carries the predicate text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from decoy_engine.plan import compile_plan, run_config_only_checks
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile import profile_source
from tests.unit.execution import _c8_iii_c_support as sup

CODE = "when_outside_closed_grammar"
PATH = "tables.t.columns.s.when"

LONG = "x > 1" + " and x > 1" * 800  # 8005 characters

BAD_PREDICATES = [
    "x",
    "1 == 1",
    "x.notnull()",
    "s != b'zz'",
    "s == f'{x}'",
    "a != b",
    "`oops",
    "`unterminated",
    "index > 1",
    "amount > amount.mean()",
    "name.str.startswith('A')",
    LONG,
]
BAD_IDS = [
    "bare_column",
    "scalar_compare",
    "method_call",
    "bytes_literal",
    "fstring_literal",
    "ref_to_ref",
    "backtick_oops",
    "backtick_unterminated",
    "reserved_index",
    "aggregate_method",
    "str_accessor",
    "five_thousand_chars",
]
NON_STRING = [1, 0, True, False, ["region == 'US'"], [], {}]


def _compile(config: dict[str, Any], entry: str, profile: Any) -> Any:
    if entry == "compile_plan":
        return compile_plan(config, profile, decoy_engine_version="0.1.0")
    if entry == "compile_plan_no_profile":
        return compile_plan(config, profile, decoy_engine_version="0.1.0", no_profile=True)
    return run_config_only_checks(config)


ENTRIES = ["compile_plan", "compile_plan_no_profile", "config_only"]


@pytest.fixture
def job(tmp_path: Path) -> tuple[dict[str, Any], Any]:
    config, _sources = sup.single_table_config(tmp_path)
    return config, profile_source(config, seed=7)


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("predicate", BAD_PREDICATES, ids=BAD_IDS)
def test_out_of_grammar_predicate_is_rejected_with_code_and_path(
    job: tuple[dict[str, Any], Any], entry: str, predicate: str
) -> None:
    config, profile = job
    raw = sup.with_when(config, "t", "s", predicate)
    with pytest.raises(PlanCompileError) as info:
        _compile(raw, entry, profile)
    assert info.value.code == CODE
    assert info.value.path == PATH
    if len(predicate) > 6:
        assert predicate not in info.value.message
        assert predicate not in str(info.value)


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize(
    "value",
    NON_STRING,
    ids=["int", "zero", "true", "false", "list", "empty_list", "empty_dict"],
)
def test_non_string_when_is_rejected_not_silently_dropped(
    job: tuple[dict[str, Any], Any], entry: str, value: Any
) -> None:
    config, profile = job
    raw = sup.with_when(config, "t", "s", value)
    with pytest.raises(PlanCompileError) as info:
        _compile(raw, entry, profile)
    assert info.value.code == CODE
    assert info.value.path == PATH


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("blank", ["", " ", "   \t "], ids=["empty", "space", "mixed_blank"])
def test_blank_when_still_means_no_gate(
    job: tuple[dict[str, Any], Any], entry: str, blank: str
) -> None:
    config, profile = job
    raw = sup.with_when(config, "t", "s", blank)
    result = _compile(raw, entry, profile)
    if entry != "config_only":
        assert sup.column_seed(result, "t", "s").when is None


@pytest.mark.parametrize("entry", ENTRIES)
def test_padded_grammar_predicate_compiles_like_the_stripped_form(
    job: tuple[dict[str, Any], Any], entry: str
) -> None:
    config, profile = job
    padded = sup.with_when(config, "t", "s", "  region == 'US'  ")
    stripped = sup.with_when(config, "t", "s", "region == 'US'")
    got = _compile(padded, entry, profile)
    want = _compile(stripped, entry, profile)
    if entry != "config_only":
        assert sup.column_seed(got, "t", "s").when.strip() == sup.column_seed(want, "t", "s").when


def test_the_rejection_names_the_offending_column_not_a_neighbour(
    job: tuple[dict[str, Any], Any],
) -> None:
    config, _profile = job
    raw = sup.with_when(config, "t", "name", "name.str.startswith('A')")
    raw = sup.with_when(raw, "t", "s", "region == 'US'")
    with pytest.raises(PlanCompileError) as info:
        run_config_only_checks(raw)
    assert info.value.path == "tables.t.columns.name.when"


def test_a_grammar_predicate_is_not_newly_rejected(job: tuple[dict[str, Any], Any]) -> None:
    config, profile = job
    for predicate in ("region == 'US'", "x > 2 and name != 'n1'", "a in [1, 2, 3]"):
        raw = sup.with_when(config, "t", "s", predicate)
        assert run_config_only_checks(raw)
        assert sup.column_seed(_compile(raw, "compile_plan", profile), "t", "s").when == predicate


def test_the_check_skips_malformed_table_and_column_entries() -> None:
    """Shape errors belong to other checks; this one only reads dict entries."""
    from decoy_engine.plan._checks_when import check_when_grammar

    check_when_grammar({})
    check_when_grammar({"tables": None})
    check_when_grammar({"tables": ["junk", None, {"name": "t", "columns": None}]})
    check_when_grammar(
        {"tables": [{"name": "t", "columns": ["junk", None, {"name": "s", "when": "s == 'a'"}]}]}
    )
    with pytest.raises(PlanCompileError) as info:
        check_when_grammar(
            {"tables": ["junk", {"name": "t", "columns": [None, {"name": "s", "when": "s.x"}]}]}
        )
    assert info.value.path == "tables.t.columns.s.when"


def test_a_table_or_column_without_a_name_is_reported_with_a_placeholder() -> None:
    from decoy_engine.plan._checks_when import check_when_grammar

    with pytest.raises(PlanCompileError) as info:
        check_when_grammar({"tables": [{"columns": [{"when": "s.x"}]}]})
    assert info.value.path == "tables.?.columns.?.when"
