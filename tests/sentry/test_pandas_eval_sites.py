"""Sentry (C8-i test 9): `DataFrame.eval` / `.query` and simpleeval's `.eval` stay at audited sites.

The public `when:` field validates a closed grammar, but the oracle still evaluates a
validated predicate with `DataFrame.eval` (numexpr, empty scopes), and the native route
reaches that call only through the oracle's `_eval_predicate`. This sentry keeps the set of
eval-like method calls exact: a new pandas eval or query call anywhere else, a dynamic
`getattr(obj, "eval")` and a removed audited call all fail it. The existing
`test_expression_safety` and `test_source_hygiene` sentries cover bare eval/exec/compile.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).parents[2] / "src" / "decoy_engine"

_EVAL_METHODS = frozenset({"eval", "query"})

# Every audited eval-like method call in the engine, by file and the exact receiver.
AUDITED_SITES: dict[str, frozenset[str]] = {
    # The oracle's predicate gate: engine="numexpr", local_dict={} and global_dict={}.
    "execution/_when_gate.py": frozenset({"pdf.eval"}),
    # The `filter_rows` transform: the same numexpr pin and empty scopes.
    "execution/_transforms.py": frozenset({"df.eval"}),
    # The simpleeval sandbox call of the formula strategy (`safe_eval`), not pandas.
    "expressions/_safe_eval.py": frozenset(
        {"EvalWithCompoundTypes(names=scope, functions=functions).eval"}
    ),
}


def eval_sites(source: str) -> list[str]:
    """The receivers of every eval-like call in `source`: `x.eval(...)`, `x.query(...)` and
    `getattr(x, "eval")`."""
    sites: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _EVAL_METHODS:
            sites.append(ast.unparse(func))
        elif (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _EVAL_METHODS
        ):
            sites.append(f"getattr(..., {node.args[1].value!r})")
    return sorted(sites)


def violations(relative: str, source: str) -> list[str]:
    found = eval_sites(source)
    allowed = sorted(AUDITED_SITES.get(relative, frozenset()))
    return [] if found == allowed else [f"{relative}: found {found}, audited {allowed}"]


def test_the_audited_sites_are_exactly_the_eval_like_calls_in_the_source_tree() -> None:
    bad: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        bad.extend(violations(relative, path.read_text(encoding="utf-8")))
    assert not bad, "\n".join(bad)


def test_every_audited_file_exists() -> None:
    for relative in AUDITED_SITES:
        assert (SRC / relative).exists(), relative


@pytest.mark.parametrize(
    "snippet",
    [
        "def f(df):\n    return df.eval('a > 1')\n",
        "def f(df):\n    return df.query('a > 1')\n",
        "import pandas as pd\nx = pd.eval('1 + 1')\n",
        "def f(df):\n    return getattr(df, 'eval')('a')\n",
        "class C:\n    def m(self, frame):\n        return frame.loc[frame.eval('a')]\n",
    ],
    ids=["eval", "query", "pd_eval", "getattr", "nested"],
)
def test_a_new_eval_like_call_in_an_unaudited_file_fails(snippet: str) -> None:
    assert violations("execution/native/_new_module.py", snippet)


def test_a_second_call_or_a_new_receiver_in_an_audited_file_fails() -> None:
    original = (SRC / "execution/_when_gate.py").read_text(encoding="utf-8")
    assert violations("execution/_when_gate.py", original) == []
    assert violations(
        "execution/_when_gate.py", original + "\n\ndef g(d):\n    return d.eval('x')\n"
    )
    assert violations("execution/_when_gate.py", original.replace("pdf.eval(", "other.eval("))


def test_a_removed_audited_call_also_fails_so_the_list_cannot_go_stale() -> None:
    assert violations("execution/_when_gate.py", "x = 1\n")


@pytest.mark.parametrize(
    "relative", ["execution/native/_when_mask.py", "expressions/_when_parser.py"]
)
def test_the_when_modules_make_no_eval_exec_or_compile_calls_of_their_own(relative: str) -> None:
    tree = ast.parse((SRC / relative).read_text(encoding="utf-8"))
    banned_names = {"eval", "exec", "compile", "__import__"}
    banned_methods = _EVAL_METHODS | {"exec", "compile"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            assert not (isinstance(func, ast.Name) and func.id in banned_names), relative
            assert not (isinstance(func, ast.Attribute) and func.attr in banned_methods), relative
    assert eval_sites((SRC / relative).read_text(encoding="utf-8")) == []


def test_the_native_mask_reaches_pandas_eval_only_through_the_oracles_predicate_function() -> None:
    text = (SRC / "execution/native/_when_mask.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    imported = [
        (n.module, a.name) for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names
    ]
    assert ("decoy_engine.execution._when_gate", "_eval_predicate") in imported
    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_eval_predicate" in called
