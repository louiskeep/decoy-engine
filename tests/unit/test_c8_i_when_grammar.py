"""C8-i acceptance tests 1, 1b, 1c, 1d: the closed `when:` grammar.

The grammar is the security boundary of the public `ColumnConfig.when` field, so the
tests pin both directions: every allowed form parses, and every excluded form is refused
with `when_outside_closed_grammar`. The generated check proves the claim the plan makes
about the language: an accepted predicate over compatible column types evaluates under the
oracle's own `DataFrame.eval` call without error.
"""

from __future__ import annotations

import keyword

import numpy as np
import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from decoy_engine.errors import ValidationError
from decoy_engine.expressions._when_parser import (
    RESERVED_NAMES,
    WHEN_OUTSIDE_GRAMMAR_CODE,
    parse_when,
    when_column_refs,
)
from tests.native._c8_i_support import identifiers, predicates

ACCEPTED = [
    "s == 'a'",
    's == "a"',
    "'a' == s",
    "s != 'a'",
    "s < 'b'",
    "s <= 'b'",
    "s > 'b'",
    "s >= 'b'",
    "n == 1",
    "n == -1",
    "n == 1.5",
    "n == -2.5e3",
    "n == 1e3",
    "n == 9223372036854775807",
    "n == -9223372036854775808",
    "1 == n",
    "flag == True",
    "flag != False",
    "s in ['a']",
    "s in ['a', 'b', \"c\"]",
    "n in [1, 2.5, -3]",
    "s not in ['a', 'b']",
    "n not in [1]",
    "s == 'a' and n > 1",
    "s == 'a' or n > 1",
    "not s == 'a'",
    "not (s == 'a')",
    "(s == 'a')",
    "((s == 'a' or s == 'b') and not (n < 1))",
    "s == 'a' and n > 1 or t != 'z'",
    "s == ''",
    "s == 'with spaces and é日'",
    "  s == 'a'  ",
    "android == 'a'",
    "index_x == 1",
    "_private == 1",
    "a1_b2 == 1",
]

REJECTED = [
    # arithmetic, calls, attributes, subscripts, scope references
    "n + 1 == 2",
    "n * 2 > 1",
    "-n == 1",
    "len(s) == 1",
    "f() == 1",
    "s() > 1",
    "len(s, t) == 1",
    "s.str.len() == 1",
    "s.notnull()",
    "s.isna() == True",
    "s[0] == 'a'",
    "@s == 'a'",
    "n == @x",
    "s == `t`",
    "s.upper() == 'A'",
    "x if y else z",
    "lambda: 1",
    "__import__('os')",
    # references to references, chained comparisons
    "s == t",
    "s < t",
    "1 < n < 5",
    "a < b < c",
    "1 == 1",
    "'a' == 'a'",
    "s in t",
    # None, empty and malformed lists
    "s == None",
    "s is None",
    "s != None",
    "s in []",
    "s in ['a',]",
    "s in [None]",
    "s in 'abc'",
    "s in ('a', 'b')",
    "s in {'a'}",
    "s in ['a', t]",
    # a bare reference or literal is not a predicate
    "s",
    "True",
    "not s",
    "s and t",
    "",
    "   ",
    "()",
    # string literal restrictions
    "s == 'a\\'b'",
    "s == 'a\\n'",
    "s == 'back\\\\slash'",
    "s == \"say 'hi'\"",
    "s == 'say \"hi\"'",
    "s == 'tab\there'",
    "s == 'new\nline'",
    "s == 'nul\x00'",
    "s == 'c1\x85'",
    f"s == 'sep{chr(0x2028)}'",
    "s == f'a'",
    "s == b'a'",
    "s == 'a' 'b'",
    "s == 'unterminated",
    # numeric literal restrictions
    "n == 9223372036854775808",
    "n == -9223372036854775809",
    "n == 1e999",
    "n == -1e999",
    "n == nan",
    "n == inf",
    "n == 1_000",
    "n == 0x10",
    "n == 1j",
    "n == 1.",
    "n == .5",
    "n == +1",
    "n == - 1",
    # identifiers
    "__x == 1",
    "x__ == 1",
    "__class__ == 1",
    f"caf{chr(0xE9)} == 1",
    f"{chr(0xFF58)} == 1",
    "my col == 1",
    # reserved identifiers (numexpr functions, pandas globals, keywords)
    "sin == 1",
    "abs == 1",
    "where == 1",
    "nan == 1",
    "NaN == 1",
    "inf == 1",
    "Inf == 1",
    "index == 1",
    "columns == 1",
    "list == 1",
    "Timestamp == 1",
    "datetime == 1",
    "None == 1",
    # syntax
    "s == 'a' or",
    "(s == 'a'",
    "s == 'a')",
    "s == 'a' and and n == 1",
    "s = 'a'",
    "s === 'a'",
    "s <> 'a'",
    "s == 'a'; import os",
    "s == 'a' # comment",
]


@pytest.mark.parametrize("expr", ACCEPTED)
def test_every_allowed_form_parses(expr: str) -> None:
    ast = parse_when(expr)
    assert when_column_refs(ast), expr


@pytest.mark.parametrize("expr", REJECTED)
def test_every_excluded_form_is_refused_with_the_grammar_code(expr: str) -> None:
    with pytest.raises(ValidationError) as info:
        parse_when(expr)
    assert info.value.code == WHEN_OUTSIDE_GRAMMAR_CODE


def test_a_very_deeply_nested_predicate_is_refused_not_a_recursion_error() -> None:
    deep = "not " * 1000 + "s == 'a'"
    with pytest.raises(ValidationError) as info:
        parse_when(deep)
    assert info.value.code == WHEN_OUTSIDE_GRAMMAR_CODE
    with pytest.raises(ValidationError):
        parse_when("(" * 60 + "s == 'a'" + ")" * 60)
    with pytest.raises(ValidationError):
        parse_when("s == '" + "a" * 5000 + "'")


def test_the_error_message_names_the_construct_and_never_echoes_literal_text() -> None:
    secret = "VERY-SECRET-123-45-6789"
    with pytest.raises(ValidationError) as info:
        parse_when(f"s == '{secret}' + 1")
    assert secret not in str(info.value)


def test_refs_are_sorted_unique_and_cover_every_branch() -> None:
    ast = parse_when("(b == 1 or a == 2) and not (c in [1] and a != 3) or 'x' == d")
    assert when_column_refs(ast) == ("a", "b", "c", "d")


# ---------------------------------------------------------------------------
# 1b. Every accepted string evaluates under the oracle's eval without error.
# ---------------------------------------------------------------------------

_N = 6


@st.composite
def _case(draw: st.DrawFn) -> tuple[str, pd.DataFrame]:
    names = draw(st.lists(identifiers(), min_size=1, max_size=4, unique=True))
    kinds = {n: draw(st.sampled_from(["string", "number", "bool"])) for n in names}
    expr = draw(predicates(kinds))
    data: dict[str, object] = {}
    for name, kind in kinds.items():
        if kind == "string":
            data[name] = pd.Series(
                draw(st.lists(st.one_of(st.none(), st.text(max_size=4)), min_size=_N, max_size=_N)),
                dtype=object,
            )
        elif kind == "bool":
            data[name] = np.array(draw(st.lists(st.booleans(), min_size=_N, max_size=_N)))
        else:
            data[name] = np.array(
                draw(
                    st.lists(
                        st.floats(allow_nan=True, allow_infinity=False, width=32),
                        min_size=_N,
                        max_size=_N,
                    )
                )
            )
    return expr, pd.DataFrame(data)


@settings(
    max_examples=150,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(_case())
def test_an_accepted_predicate_over_compatible_types_evaluates_without_error(
    case: tuple[str, pd.DataFrame],
) -> None:
    expr, frame = case
    parse_when(expr)
    mask = frame.eval(expr, engine="numexpr", local_dict={}, global_dict={})
    assert isinstance(mask, pd.Series) and pd.api.types.is_bool_dtype(mask.dtype), expr
    assert len(mask) == _N


# ---------------------------------------------------------------------------
# 1c. The reserved list covers the live numexpr and pandas names.
# ---------------------------------------------------------------------------


def test_the_reserved_list_contains_numexpr_pandas_and_python_names() -> None:
    from numexpr.expressions import functions
    from pandas.core.computation.scope import DEFAULT_GLOBALS

    live = set(functions) | set(DEFAULT_GLOBALS) | set(keyword.kwlist) | set(keyword.softkwlist)
    assert live <= RESERVED_NAMES, sorted(live - RESERVED_NAMES)
    assert len(set(functions)) == 48


@pytest.mark.parametrize("name", sorted(RESERVED_NAMES))
def test_every_reserved_name_is_refused_as_a_column_reference(name: str) -> None:
    with pytest.raises(ValidationError) as info:
        parse_when(f"{name} == 1")
    assert info.value.code == WHEN_OUTSIDE_GRAMMAR_CODE


# ---------------------------------------------------------------------------
# 1d. Characterization: why the restriction exists. Not every reserved name collides.
# ---------------------------------------------------------------------------


def test_some_reserved_names_collide_in_the_oracle_and_some_do_not() -> None:
    frame = pd.DataFrame({"zz": ["a", "b"]})
    collides: set[str] = set()
    for name in sorted(RESERVED_NAMES):
        if not name.isidentifier():
            continue
        try:
            got = frame.eval(f"{name} == 1", engine="numexpr", local_dict={}, global_dict={})
        except Exception:
            continue
        if not isinstance(got, pd.Series):
            collides.add(name)  # resolved to a scalar: the mask would not be a Series
    # `inf` and `Inf` are pandas eval globals: the comparison is a scalar, not a column mask.
    assert {"inf", "Inf"} <= collides
    # Several reserved names still yield valid masks over a string column (the restriction is
    # a deliberate language rule, not a proof that every name breaks the oracle).
    assert collides != {n for n in RESERVED_NAMES if n.isidentifier()}


def test_more_than_the_comparison_cap_is_rejected() -> None:

    from decoy_engine.errors import ValidationError
    from decoy_engine.expressions._when_parser import parse_when

    too_many = " or ".join(f"c == 's{i}'" for i in range(33))
    with pytest.raises(ValidationError):
        parse_when(too_many)


def test_nesting_beyond_the_depth_cap_is_rejected() -> None:

    from decoy_engine.errors import ValidationError
    from decoy_engine.expressions._when_parser import parse_when

    with pytest.raises(ValidationError):
        parse_when("not " * 50 + "c == 'a'")


def test_the_largest_accepted_predicates_evaluate_under_the_oracle() -> None:
    import pandas as pd

    from decoy_engine.execution._when_gate import _eval_predicate
    from decoy_engine.expressions._when_parser import parse_when

    frame = pd.DataFrame({"c": ["s1", "x", None], "n": [1, 5, None]})
    largest = [
        " or ".join(f"c == 's{i}'" for i in range(32)),
        " or ".join(f"n == {i}" for i in range(32)),
        "not " * 49 + "c == 'x'",
    ]
    for predicate in largest:
        parse_when(predicate)
        mask = _eval_predicate(frame, predicate, "hash")
        assert len(mask) == 3


@pytest.mark.parametrize(
    "predicate",
    ["n == 01", "n == -01", "n == 007", "s == 'x'\nand n == 1", "s == 'x'\r\nand n == 1"],
    ids=["leading_zero", "negative_leading_zero", "padded", "newline", "crlf"],
)
def test_spellings_pandas_eval_cannot_run_are_rejected(predicate: str) -> None:
    from decoy_engine.errors import ValidationError
    from decoy_engine.expressions._when_parser import parse_when

    with pytest.raises(ValidationError):
        parse_when(predicate)


@pytest.mark.parametrize("predicate", ["n == 0", "n == -0", "n == 0.5", "n == 10", "n == 1e05"])
def test_ordinary_numbers_still_parse_and_evaluate(predicate: str) -> None:
    import pandas as pd

    from decoy_engine.execution._when_gate import _eval_predicate
    from decoy_engine.expressions._when_parser import parse_when

    parse_when(predicate)
    assert len(_eval_predicate(pd.DataFrame({"n": [0, 10, None]}), predicate, "hash")) == 3


@pytest.mark.parametrize(
    "predicate",
    [
        "x == 1 ory == 2",
        "x == 1 andy == 2",
        "x notin [1]",
        "x == Trueand y == False",
        "x == 1\fand x == 2",
        "x == 1\vand x == 2",
    ],
    ids=["or_prefix", "and_prefix", "notin", "true_suffix", "form_feed", "vertical_tab"],
)
def test_keywords_need_their_own_token_and_only_spaces_separate_tokens(predicate: str) -> None:
    from decoy_engine.errors import ValidationError
    from decoy_engine.expressions._when_parser import parse_when

    with pytest.raises(ValidationError):
        parse_when(predicate)


@pytest.mark.parametrize(
    "predicate",
    ["x == 1 or y == 2", "x == 1\tand y == 2", "not x in [1]", "x not in [1, 2]", "x == True"],
)
def test_spaced_keywords_still_parse(predicate: str) -> None:
    from decoy_engine.expressions._when_parser import parse_when

    parse_when(predicate)
