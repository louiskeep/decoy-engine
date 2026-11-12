"""GEN-REL: every reference route goes through one pool check, one policy parser, one emitter.

`check_reference_pool` (`generation/relational/_cardinality.py`) holds the feasibility, weights
and empty-parent rules. A route that draws parent keys without it would enforce a different rule,
so this sentry inventories the helper's callers by exact path and function and fails on any
other shape:

1. The helper is defined once and called from exactly two places: the whole-table draw
   `_synthesize_derived._reference` and the streamed `_column_streams.ReferenceStream.__init__`
   (once per column stream, never per batch).
2. In `_reference` nothing is drawn or returned before the call, and the only condition around it
   is the named `fitted` exemption: an empty fitted pool keeps its own refusal.
3. Pinned execution reaches the same draw: `_pinned_execute._run` calls `_generate_column`
   with the compiled policy, and `_generate_column` dispatches to `_reference`.
4. `parent_keys` is read only by the listed draw sites, so a new draw cannot skip the helper.
5. Relationship policies are parsed only in `relationships/_graph.py`: generation never reads an
   `orphan_policy` key itself.
6. The relational package warns only through `emit_generation_warning`, keys every counter by
   `value_key`, and holds no `assert` (a postcondition must survive `python -O`).

The meta-tests seed one realistic bypass of each rule and require the scan to see it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src" / "decoy_engine"
HELPER = "check_reference_pool"
HELPER_FILE = "generation/relational/_cardinality.py"
DRAW_FILE = "generation/_synthesize_derived.py"
STREAM_FILE = "generation/_column_streams.py"
CALLERS = frozenset({(DRAW_FILE, "_reference"), (STREAM_FILE, "ReferenceStream.__init__")})
# The one branch that may skip the helper: fitted fan-out has its own frozen empty-pool refusal.
EXEMPT_DISTRIBUTION = "fitted"
# Every function that reads a parent key list. The pair draw is GEN-UNIQUE's own refusal path.
KEY_READERS = frozenset({(DRAW_FILE, "_reference"), ("generation/_unique.py", "unique_pair")})
POLICY_KEY = "orphan_policy"
POLICY_PARSER_FILE = "relationships/_graph.py"
GENERATION_DIRS = ("generation", "generators")
LOG_METHODS = frozenset({"warning", "warn", "error", "info", "exception", "critical"})
CONTAINERS = frozenset({"Counter", "set", "dict", "defaultdict", "frozenset"})
# The names the repair and the pool check give a list of raw parent or child values.
RAW_VALUE_NAMES = frozenset({"values", "ref_values", "ref_vals", "keys"})


def _callee(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _functions(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    """(qualified name, node) of each function, methods as `Class.method`."""
    found: list[tuple[str, ast.AST]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((f"{prefix}{child.name}", child))
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


def calls_of(source: str, name: str) -> list[str]:
    """The innermost enclosing function of each call to `name` (`<module>` at top level)."""
    tree = ast.parse(source)
    owner: dict[int, str] = {}
    for qualname, fn in _functions(tree):
        for node in ast.walk(fn):
            owner[id(node)] = qualname
    return [
        owner.get(id(node), "<module>")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _callee(node) == name
    ]


def _function(source: str, qualname: str) -> ast.AST:
    for name, node in _functions(ast.parse(source)):
        if name == qualname:
            return node
    raise AssertionError(f"{qualname} not found: the scan must not go empty")


def draw_order_problems(source: str) -> list[str]:
    """Problems with where `_reference` calls the helper relative to its draws and returns."""
    fn = _function(source, "_reference")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and _callee(n) == HELPER]
    if len(calls) != 1:
        return [f"_reference calls {HELPER} {len(calls)} times, expected once"]
    call = calls[0]
    problems: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and node.lineno < call.lineno:
            problems.append(f"line {node.lineno}: returns before the pool check")
        if isinstance(node, ast.Call) and node.lineno < call.lineno:
            if _callee(node) in {"choice", "choices", "unique_reference", "Random"}:
                problems.append(f"line {node.lineno}: draws before the pool check")
    for parent in ast.walk(fn):
        if not isinstance(parent, (ast.If, ast.While, ast.For, ast.Try, ast.With)):
            continue
        if parent is fn or not any(sub is call for sub in ast.walk(parent)):
            continue
        constants = (
            {c.value for c in ast.walk(parent.test) if isinstance(c, ast.Constant)}
            if isinstance(parent, ast.If)
            else set()
        )
        if constants != {EXEMPT_DISTRIBUTION}:
            problems.append(f"line {parent.lineno}: the pool check sits under an unnamed condition")
    return problems


def stream_problems(source: str) -> list[str]:
    """Problems with how `ReferenceStream` uses the helper."""
    problems: list[str] = []
    if calls_of(source, HELPER) != ["ReferenceStream.__init__"]:
        problems.append("ReferenceStream.__init__ must be the one streamed caller of the helper")
    cls = next(
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.ClassDef) and n.name == "ReferenceStream"
    )
    for node in ast.walk(cls):
        if (
            isinstance(node, ast.Compare)
            and sum(isinstance(sub, ast.Call) and _callee(sub) == "len" for sub in ast.walk(node))
            >= 2
        ):
            problems.append(f"line {node.lineno}: a weights length comparison outside the helper")
    return problems


def policy_key_reads(source: str) -> list[int]:
    """Lines that name the relationship policy key as a string literal."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and node.value == POLICY_KEY
    ]


def relational_problems(source: str) -> list[str]:
    """Rules for a module of the relational package itself."""
    tree = ast.parse(source)
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            problems.append(f"line {node.lineno}: assert is removed by python -O")
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in LOG_METHODS:
            problems.append(f"line {node.lineno}: logs directly instead of the one emitter")
        if _callee(node) in CONTAINERS and node.args:
            names = {n.id for a in node.args for n in ast.walk(a) if isinstance(n, ast.Name)}
            if names & RAW_VALUE_NAMES and "value_key" not in names:
                problems.append(f"line {node.lineno}: a container built from raw parent values")
    return problems


def _sources(*dirs: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for directory in dirs:
        for path in sorted((SRC / directory).rglob("*.py")):
            out.append((path.relative_to(SRC).as_posix(), path.read_text(encoding="utf-8")))
    return out


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


# ---- the real tree -----------------------------------------------------------------------------


def test_the_helper_is_defined_once_and_called_only_by_the_listed_routes() -> None:
    definitions: list[str] = []
    callers: list[tuple[str, str]] = []
    for rel, text in _sources(""):
        definitions += [rel for name, _ in _functions(ast.parse(text)) if name == HELPER]
        callers += [(rel, fn) for fn in calls_of(text, HELPER)]
    assert definitions == [HELPER_FILE]
    assert sorted(callers) == sorted(CALLERS)


def test_the_whole_table_draw_checks_the_pool_before_any_value() -> None:
    assert draw_order_problems(_read(DRAW_FILE)) == []


def test_the_streamed_draw_checks_once_at_construction() -> None:
    assert stream_problems(_read(STREAM_FILE)) == []


def test_pinned_execution_reaches_the_same_draw_with_its_compiled_policy() -> None:
    run = _function(_read("generation/_pinned_execute.py"), "_run")
    calls = [
        n for n in ast.walk(run) if isinstance(n, ast.Call) and _callee(n) == "_generate_column"
    ]
    assert len(calls) == 1
    assert "reference_policy" in {kw.arg for kw in calls[0].keywords}
    assert calls_of(_read("generation/synthesize.py"), "_reference") == ["_generate_column"]
    assert calls_of(_read("generation/_pinned_execute.py"), "parent_keys") == []


def test_parent_keys_are_read_only_by_the_listed_draws() -> None:
    readers = {(rel, fn) for rel, text in _sources("") for fn in calls_of(text, "parent_keys")}
    assert readers == KEY_READERS


def test_generation_never_parses_a_relationship_policy_itself() -> None:
    bad = [
        f"{rel}:{line}"
        for rel, text in _sources(*GENERATION_DIRS)
        for line in policy_key_reads(text)
    ]
    assert bad == []
    assert policy_key_reads(_read(POLICY_PARSER_FILE)), "the scan must see the one parser"
    assert calls_of(_read(HELPER_FILE), "normalize_orphan_policy_lookup") == ["reference_policies"]


def test_the_relational_package_has_one_emitter_keyed_counters_and_no_assert() -> None:
    sources = _sources("generation/relational")
    assert [rel for rel, _ in sources if rel == HELPER_FILE], "the scan must not go empty"
    assert {
        rel: relational_problems(text) for rel, text in sources if relational_problems(text)
    } == {}
    assert calls_of(_read(HELPER_FILE), "emit_generation_warning") == [HELPER]


# ---- seeded bypasses: each rule must see its own violation -------------------------------------


def _swap(text: str, old: str, new: str) -> str:
    assert old in text, f"seed anchor is gone: {old!r}"
    return text.replace(old, new, 1)


def test_seeded_a_second_caller_is_seen() -> None:
    extra = (
        _read(STREAM_FILE)
        + "\n\ndef another_route(col, keys):\n    check_reference_pool(col, keys, 1, None, 't', None)\n"
    )
    assert stream_problems(extra)


def test_seeded_a_per_batch_check_is_seen() -> None:
    moved = "class ReferenceStream:\n    def __init__(self):\n        pass\n\n    def next_batch(self, offset, size):\n        check_reference_pool(1, 2, 3, None, 't', None)\n"
    assert stream_problems(moved)


def test_seeded_a_restored_uniform_fallback_is_seen() -> None:
    fallback = (
        "class ReferenceStream:\n    def __init__(self, col, ref_vals):\n"
        "        check_reference_pool(col, ref_vals, 1, None, 't', None)\n"
        "        weights = col.get('weights')\n"
        "        self._weights = weights if weights and len(weights) == len(ref_vals) else None\n"
    )
    assert stream_problems(fallback)


@pytest.mark.parametrize(
    "body",
    [
        # the helper dropped
        "    return [rng.choice(ref_vals) for _ in range(n)]\n",
        # a return ahead of it (generic empty handling first)
        "    if not ref_vals:\n        return [None] * n\n    check_reference_pool(col, ref_vals, n, policy, t, cb)\n",
        # a draw ahead of it
        "    first = rng.choice(ref_vals)\n    check_reference_pool(col, ref_vals, n, policy, t, cb)\n",
        # hidden behind a condition that is not the fitted exemption
        "    if policy == 'fail':\n        check_reference_pool(col, ref_vals, n, policy, t, cb)\n",
        "    if n > 100:\n        check_reference_pool(col, ref_vals, n, policy, t, cb)\n",
    ],
)
def test_seeded_draw_bypasses_are_seen(body: str) -> None:
    assert draw_order_problems("def _reference(col, n, ref_vals, policy, t, cb, rng):\n" + body)


def test_the_fitted_exemption_is_the_only_accepted_condition() -> None:
    ok = (
        "def _reference(col, n, ref_vals, policy, t, cb, distribution):\n"
        "    if distribution != 'fitted':\n"
        "        check_reference_pool(col, ref_vals, n, policy, t, cb)\n"
        "    return [None] * n\n"
    )
    assert draw_order_problems(ok) == []


def test_seeded_a_second_policy_parser_is_seen() -> None:
    reparse = (
        "def policies(config):\n    return [r['orphan_policy'] for r in config['relationships']]\n"
    )
    assert policy_key_reads(reparse) == [2]
    assert policy_key_reads("def f(entry):\n    return entry.get('orphan_policy')\n") == [2]


@pytest.mark.parametrize(
    "snippet",
    [
        "def f(log, table):\n    log.warning('empty parent for %s', table)\n",
        "import logging\ndef f():\n    logging.getLogger(__name__).warning('x')\n",
        "def f(values):\n    assert max(values) <= 3\n",
        "from collections import Counter\ndef f(values):\n    return Counter(values)\n",
        "def f(values):\n    return set(values)\n",
    ],
)
def test_seeded_relational_bypasses_are_seen(snippet: str) -> None:
    assert relational_problems(snippet)


def test_keyed_containers_are_accepted() -> None:
    ok = (
        "from collections import Counter\n"
        "def f(values, counts):\n"
        "    tally = Counter(counts)\n"
        "    return Counter(value_key(v) for v in values), tally\n"
    )
    assert relational_problems(ok) == []
