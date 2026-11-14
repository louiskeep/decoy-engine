"""One seed form: no raw-byte generator seed outside the registered composite builds.

A NumPy or `random` generator that is seeded from `int.from_bytes(...)` takes whatever width
it is handed: a 32-byte key sliced to its first 8 bytes made every `joint_mask` group of a run
walk one stream, and a missing seed fell back to `b"\\x00" * 8`. Two rules close that class.

1. A generator constructor (`default_rng`, `Random`, `RandomState` and the bit generators) must
   not take an argument built by `int.from_bytes` over raw bytes, directly or through a local
   name. A slice of a `derive(...)` output is fixed-width by construction and passes. The seed
   integer comes from `determinism.seed_bytes_to_int`, which refuses any width but 8. The one
   exception is the five composite `build_pool` functions: their `ProviderSpec` seed is 8 bytes
   or the 32-byte deterministic mask key, and the conversion keeps the full-width integer that
   keyed composite masking already uses. Each exception names `(path, function)` and holds only
   while that function runs its spec seed through `resolve_seed`.
2. An all-zero 8-byte seed literal (`b"\\x00" * 8`, `bytes(8)`) is a fixed seed that every
   caller without one shares. `resolve_seed` draws a fresh seed instead.

This file carries 24zf's rule only. The census of the other `default_rng` sites is 24ze's.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.sentry._call_resolution import local_names

SRC = Path(__file__).resolve().parents[3] / "src" / "decoy_engine"

_CONSTRUCTORS = ("default_rng", "Random", "RandomState", "PCG64", "MT19937", "Philox", "SFC64")

# The five composite builds whose seed passes through `resolve_seed`.
_COMPOSITE_BUILDS = frozenset(
    {
        ("generation/composite/_address.py", "CompositeAddress.build_pool"),
        ("generation/composite/_custom.py", "CompositeCustom.build_pool"),
        ("generation/composite/_name_email.py", "CompositeNameEmail.build_pool"),
        ("generation/composite/_person.py", "CompositePerson.build_pool"),
        ("generation/composite/_provider.py", "CompositeProvider.build_pool"),
    }
)

# Zero-seed literals that are not seeds (none today); a new entry needs a reason here.
_ZERO_LITERAL_ALLOWED: frozenset[tuple[str, str]] = frozenset()


def _functions(tree: ast.Module) -> Iterator[tuple[str, ast.AST]]:
    """Every function with its dotted qualified name, plus the module body as `<module>`."""
    yield "<module>", tree

    def walk(node: ast.AST, prefix: str) -> Iterator[tuple[str, ast.AST]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                yield from walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield f"{prefix}{child.name}", child
                yield from walk(child, f"{prefix}{child.name}.")
            else:
                yield from walk(child, prefix)

    yield from walk(tree, "")


def _own_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Nodes of `scope` that are not inside a nested function or class."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


_DERIVE_CALLS = frozenset({"derive", "derive_index", "derive_value"})


def _calls_derive(node: ast.AST) -> bool:
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            func = call.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in _DERIVE_CALLS:
                return True
    return False


def _is_raw_from_bytes(node: ast.AST) -> bool:
    """`int.from_bytes(x, ...)` where `x` is not the output of the keyed derivation.

    `derive` returns a fixed 32 bytes, so a slice of it is a width-safe integer by
    construction. Anything else (`job_seed[:8]`, a spec seed) takes whatever width it is given.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "from_bytes"
        and not any(_calls_derive(arg) for arg in node.args[:1])
    )


def _has_from_bytes(node: ast.AST) -> bool:
    return any(_is_raw_from_bytes(n) for n in ast.walk(node))


def _tainted_names(scope: ast.AST) -> set[str]:
    """Local names assigned from an expression holding `int.from_bytes(...)`."""
    tainted: set[str] = set()
    grew = True
    while grew:
        grew = False
        for node in _own_nodes(scope):
            value = None
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                value, targets = node.value, list(node.targets)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                value, targets = node.value, [node.target]
            if value is None:
                continue
            names_in_value = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
            if _has_from_bytes(value) or names_in_value & tainted:
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in tainted:
                        tainted.add(target.id)
                        grew = True
    return tainted


def _constructor_calls(tree: ast.Module) -> list[ast.Call]:
    spellings = {name for ctor in _CONSTRUCTORS for name in local_names(tree, ctor)}
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Name) and func.id in spellings) or (
            isinstance(func, ast.Attribute) and func.attr in _CONSTRUCTORS
        ):
            calls.append(node)
    return calls


def _raw_byte_seed_functions(tree: ast.Module) -> list[str]:
    """Qualified names of functions that seed a generator from `int.from_bytes`."""
    ctor_ids = {id(c) for c in _constructor_calls(tree)}
    found: list[str] = []
    for qual, scope in _functions(tree):
        tainted = _tainted_names(scope)
        for node in _own_nodes(scope):
            if id(node) not in ctor_ids or not isinstance(node, ast.Call):
                continue
            args = [*node.args, *(kw.value for kw in node.keywords)]
            if any(
                _has_from_bytes(a)
                or {n.id for n in ast.walk(a) if isinstance(n, ast.Name)} & tainted
                for a in args
            ):
                found.append(qual)
    return sorted(set(found))


def _is_zero_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        pair = (node.left, node.right)
        for raw, count in (pair, pair[::-1]):
            if (
                isinstance(raw, ast.Constant)
                and isinstance(raw.value, bytes)
                and raw.value
                and set(raw.value) == {0}
                and isinstance(count, ast.Constant)
                and isinstance(count.value, int)
                and len(raw.value) * count.value == 8
            ):
                return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in {"bytes", "bytearray"} and len(node.args) == 1:
            arg = node.args[0]
            return isinstance(arg, ast.Constant) and arg.value == 8
    return False


def _zero_seed_functions(tree: ast.Module) -> list[str]:
    found = [
        qual
        for qual, scope in _functions(tree)
        for node in _own_nodes(scope)
        if _is_zero_literal(node)
    ]
    return sorted(set(found))


def _calls_resolve_seed(tree: ast.Module, qual: str) -> bool:
    spellings = local_names(tree, "resolve_seed")
    for name, scope in _functions(tree):
        if name == qual:
            return any(
                isinstance(n, ast.Call)
                and (
                    (isinstance(n.func, ast.Name) and n.func.id in spellings)
                    or (isinstance(n.func, ast.Attribute) and n.func.attr == "resolve_seed")
                )
                for n in _own_nodes(scope)
            )
    return False


def _trees() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(SRC.rglob("*.py")):
        yield path.relative_to(SRC).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _real_sites() -> tuple[set[tuple[str, str]], set[tuple[str, str]], dict[str, ast.Module]]:
    raw: set[tuple[str, str]] = set()
    zero: set[tuple[str, str]] = set()
    trees: dict[str, ast.Module] = {}
    for rel, tree in _trees():
        trees[rel] = tree
        raw |= {(rel, q) for q in _raw_byte_seed_functions(tree)}
        zero |= {(rel, q) for q in _zero_seed_functions(tree)}
    return raw, zero, trees


# ----- the real tree ------------------------------------------------------------------------


def test_no_generator_is_seeded_from_raw_bytes_outside_the_composite_builds() -> None:
    raw, _, _ = _real_sites()
    stray = sorted(raw - _COMPOSITE_BUILDS)
    assert not stray, f"seed generators through determinism.seed_bytes_to_int: {stray}"


def test_every_composite_exception_is_a_real_site_that_resolves_its_seed() -> None:
    raw, _, trees = _real_sites()
    stale = sorted(_COMPOSITE_BUILDS - raw)
    assert not stale, f"allowlist entries that no longer match a raw-byte seed: {stale}"
    unresolved = sorted(
        (rel, qual) for rel, qual in _COMPOSITE_BUILDS if not _calls_resolve_seed(trees[rel], qual)
    )
    assert not unresolved, f"composite builds that skip resolve_seed: {unresolved}"


def test_no_all_zero_eight_byte_seed_literal_remains() -> None:
    _, zero, _ = _real_sites()
    found = sorted(zero - _ZERO_LITERAL_ALLOWED)
    assert not found, f"a shared fixed seed: use determinism.resolve_seed: {found}"


def test_the_zero_literal_allowlist_holds_only_real_sites() -> None:
    _, zero, _ = _real_sites()
    assert not sorted(_ZERO_LITERAL_ALLOWED - zero)


# ----- seeded spellings ---------------------------------------------------------------------

_RAW_SPELLINGS = {
    "direct": "def f(s):\n    return default_rng(int.from_bytes(s, 'big'))\n",
    "through a name": "def f(s):\n    n = int.from_bytes(s[:8], 'big')\n    return default_rng(n)\n",
    "through two names": (
        "def f(s):\n    n = int.from_bytes(s[:8], 'big')\n    m = n + 1\n    return default_rng(m)\n"
    ),
    "random.Random": "def f(s):\n    return random.Random(int.from_bytes(s, 'big'))\n",
    "bit generator": (
        "def f(s):\n    return np.random.Generator(PCG64(int.from_bytes(s, 'big')))\n"
    ),
    "numpy alias": "def f(s):\n    return npr.default_rng(int.from_bytes(s, 'big'))\n",
    "from-import alias": (
        "from numpy.random import default_rng as make\n"
        "def f(s):\n    return make(int.from_bytes(s, 'big'))\n"
    ),
    "plain re-binding": (
        "make = np.random.default_rng\ndef f(s):\n    return make(int.from_bytes(s, 'big'))\n"
    ),
    "keyword seed": "def f(s):\n    return default_rng(seed=int.from_bytes(s, 'big'))\n",
    "method": (
        "class C:\n    def build(self, s):\n        return default_rng(int.from_bytes(s, 'big'))\n"
    ),
}


@pytest.mark.parametrize("spelling", sorted(_RAW_SPELLINGS))
def test_each_raw_byte_spelling_is_a_hit(spelling: str) -> None:
    assert _raw_byte_seed_functions(ast.parse(_RAW_SPELLINGS[spelling]))


_CLEAN = {
    "derived slice": (
        "def f(k):\n    return default_rng(int.from_bytes(derive(k, 'ns', b'x')[:8], 'big'))\n"
    ),
    "derived slice through a name": (
        "def f(k):\n    n = int.from_bytes(derive(k, 'ns', b'x')[:8], 'big')\n"
        "    return default_rng(n)\n"
    ),
    "checked conversion": "def f(s):\n    return default_rng(seed_bytes_to_int(s))\n",
    "unseeded": "def f():\n    return default_rng()\n",
    "from_bytes not a seed": "def f(s):\n    n = int.from_bytes(s, 'big')\n    return n % 7\n",
    "faker seeding": "def f(s, fk):\n    fk.seed_instance(int.from_bytes(s, 'big'))\n",
}


@pytest.mark.parametrize("spelling", sorted(_CLEAN))
def test_each_clean_spelling_is_not_a_hit(spelling: str) -> None:
    assert not _raw_byte_seed_functions(ast.parse(_CLEAN[spelling]))


_ZERO_SPELLINGS = {
    "bytes times": 'def f():\n    return b"\\x00" * 8\n',
    "int first": 'def f():\n    return 8 * b"\\x00"\n',
    "bytes call": "def f():\n    return bytes(8)\n",
    "as a fallback": 'def f(spec):\n    return spec.seed if spec.seed else b"\\x00" * 8\n',
    "as an argument": 'def f(g):\n    return g(seed=b"\\x00" * 8)\n',
}


@pytest.mark.parametrize("spelling", sorted(_ZERO_SPELLINGS))
def test_each_zero_seed_spelling_is_a_hit(spelling: str) -> None:
    assert _zero_seed_functions(ast.parse(_ZERO_SPELLINGS[spelling]))


@pytest.mark.parametrize(
    "source",
    ['def f(b):\n    return b + b"\\x00" * 24\n', "def f():\n    return bytes(16)\n"],
)
def test_other_zero_padding_is_not_a_hit(source: str) -> None:
    assert not _zero_seed_functions(ast.parse(source))


def test_an_exception_that_skips_resolve_seed_is_found() -> None:
    skipping = ast.parse("class C:\n    def build_pool(self, spec):\n        return spec\n")
    resolving = ast.parse(
        "class C:\n    def build_pool(self, spec):\n"
        "        spec = replace(spec, seed=resolve_seed(spec.seed))\n"
    )
    assert not _calls_resolve_seed(skipping, "C.build_pool")
    assert _calls_resolve_seed(resolving, "C.build_pool")
