"""Table-kind choke point: `config.classify_table_kinds` is the one classifier of a config dict.

Part one: no function named `classify_table_kinds` or `_classify_table_kinds` is defined outside
the owner (`config/_tables.py`); the re-export in `execution/_pipeline_context.py` is an import,
not a definition. Part two: an AST ratchet over `.get("generate_columns")` calls used as a truth
test, the inline spelling of the same classification. Counts per module may only go down, and
`subset/_api.py` and `validators/_leak_check.py` stay at zero because they call the owner.

Named limit: only the call spelling `x.get("generate_columns")` in an `if`, a conditional
expression, a comprehension filter, a `not` or an `and`/`or` operand is counted. Subscript
access, a helper variable holding the value, and `.get` with a different key are not.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parents[2] / "src" / "decoy_engine"
OWNER = "config/_tables.py"
CLASSIFIER_NAMES = frozenset({"classify_table_kinds", "_classify_table_kinds"})
MUST_BE_ZERO = ("subset/_api.py", "validators/_leak_check.py")

# Inline truth tests of `.get("generate_columns")` per module on the slice that moved the
# subset and validator copies to the owner. Modules at zero are absent.
TRUTH_TEST_BASELINE: dict[str, int] = {
    "config/_generate_type_checks.py": 1,
    "config/_generate_unique.py": 1,
    "config/_tables.py": 1,
    "execution/_chunked.py": 1,
    "execution/_probe_scale.py": 2,
    "execution/_transforms_gate.py": 1,
    "execution/_transforms_table.py": 1,
    "execution/native/_plan.py": 1,
    "generation/_admission.py": 1,
    "generation/_bounds.py": 3,
    "generation/_numeric_typing.py": 1,
    "generation/_plan_entry.py": 2,
    "generation/_reproducibility.py": 2,
    "generation/_streamed.py": 1,
    "generation/_window_admission.py": 1,
    "generation/synthesize.py": 2,
    "plan/_checks.py": 3,
    "plan/_checks_declared_casting.py": 1,
    "plan/_checks_derived_aggregate.py": 2,
    "plan/_checks_dp.py": 2,
    "plan/_checks_group_key.py": 2,
    "plan/_checks_grouped_series.py": 2,
    "plan/_checks_windowed_date.py": 2,
    "plan/_generation.py": 3,
    "plan/_serialize.py": 1,
    "plan/checks/_generate_columns.py": 2,
    "secret_providers/_requirements.py": 1,
    "unmask.py": 1,
}


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _trees(root: Path) -> dict[str, ast.Module]:
    return {
        _rel(p, root): ast.parse(p.read_text(encoding="utf-8")) for p in sorted(root.rglob("*.py"))
    }


def duplicate_definitions(trees: dict[str, ast.Module]) -> list[str]:
    found = []
    for rel, tree in trees.items():
        if rel == OWNER:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in CLASSIFIER_NAMES:
                    found.append(f"{rel}:{node.lineno}: {node.name}")
    return found


def _is_generate_get(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "generate_columns"
    )


def _truth_positions(tree: ast.AST) -> list[ast.AST]:
    out: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp)):
            out.append(node.test)
        elif isinstance(node, ast.comprehension):
            out.extend(node.ifs)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            out.append(node.operand)
        elif isinstance(node, ast.BoolOp):
            out.extend(node.values)
    return out


def truth_test_counts(trees: dict[str, ast.Module]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rel, tree in trees.items():
        hits = {id(n) for n in _truth_positions(tree) if _is_generate_get(n)}
        if hits:
            counts[rel] = len(hits)
    return counts


def ratchet_findings(counts: dict[str, int], baseline: dict[str, int]) -> list[str]:
    found = []
    for rel, n in sorted(counts.items()):
        allowed = baseline.get(rel)
        if allowed is None:
            found.append(f"{rel}: {n} truth test(s) in a module with no baseline")
        elif n > allowed:
            found.append(f"{rel}: {n} truth tests, baseline {allowed}")
    return found


def test_one_classifier_definition() -> None:
    assert duplicate_definitions(_trees(SRC)) == []


def test_owner_defines_the_classifier() -> None:
    names = {n.name for n in ast.walk(_trees(SRC)[OWNER]) if isinstance(n, ast.FunctionDef)}
    assert "classify_table_kinds" in names


def test_inline_generate_truth_tests_only_go_down() -> None:
    counts = truth_test_counts(_trees(SRC))
    assert ratchet_findings(counts, TRUTH_TEST_BASELINE) == []
    assert [m for m in MUST_BE_ZERO if m in counts] == []
    assert [m for m in MUST_BE_ZERO if m in TRUTH_TEST_BASELINE] == []


def test_baseline_holds_no_stale_entries() -> None:
    counts = truth_test_counts(_trees(SRC))
    stale = {m: n for m, n in TRUTH_TEST_BASELINE.items() if counts.get(m, 0) < n}
    assert stale == {}, "lower the baseline to the live count"


def _parse(**files: str) -> dict[str, ast.Module]:
    return {name.replace("__", "/") + ".py": ast.parse(text) for name, text in files.items()}


def test_planted_second_definition_is_found() -> None:
    trees = _parse(
        validators__x="def _classify_table_kinds(c):\n    return {}\n",
        config___tables="def classify_table_kinds(c):\n    return {}\n",
    )
    assert duplicate_definitions(trees) == ["validators/x.py:1: _classify_table_kinds"]


def test_every_counted_spelling_is_seen_and_others_are_not() -> None:
    src = (
        "a = {t['n'] for t in ts if t.get('generate_columns')}\n"
        "if t.get('generate_columns'):\n    pass\n"
        "b = 1 if t.get('generate_columns') else 2\n"
        "c = not t.get('generate_columns')\n"
        "d = x and t.get('generate_columns')\n"
        "e = t.get('generate_columns')\n"
        "f = t['generate_columns']\n"
        "if t.get('columns'):\n    pass\n"
    )
    assert truth_test_counts({"m.py": ast.parse(src)}) == {"m.py": 5}


def test_a_new_module_or_higher_count_is_a_finding() -> None:
    assert ratchet_findings({"new.py": 1}, {}) == [
        "new.py: 1 truth test(s) in a module with no baseline"
    ]
    assert ratchet_findings({"a.py": 3}, {"a.py": 2}) == ["a.py: 3 truth tests, baseline 2"]
    assert ratchet_findings({"a.py": 1}, {"a.py": 2}) == []
