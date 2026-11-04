"""Acceptance tests for rules 7 and 8 of ``scripts/check_pure_move.py`` (H-1 plan 1.3).

Each case moves ``moved`` from ``src/pkg/old.py`` to ``src/pkg/dest.py``; the kept module
imports it back at its old position, so rules 1 to 4 pass and only the targeted rule fires.
Missing decorator and default names are also read at import time, which rule 1c reports
too, so those cases are isolated with ``only={"7", "8"}``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from scripts.check_pure_move import check_move
from scripts.check_pure_move_scope import shape

OLD, DEST = "src/pkg/old.py", "src/pkg/dest.py"
LINK = "from pkg.dest import moved"
RULES = frozenset({"1a", "1b", "1c", "import-position", "2", "3", "4", "7", "8"})
SCOPE = frozenset({"7", "8"})
FUTURE = "from __future__ import annotations"


def src(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def run(base: str, kept: str, dest: str, only=RULES, also: tuple[str, ...] = ()) -> set[str]:
    owners = {name: DEST for name in ("moved", *also)}
    manifest = {"old": OLD, "new": [OLD, DEST], "map": owners, "importers": []}
    manifest |= {"docstring_prose": False, "tests": []}
    head = {OLD: kept, DEST: dest}
    problems = check_move(manifest, {OLD: base}.get, head.get, [], only=only)
    return {problem.split(":", 1)[0] for problem in problems}


def moved_only(prefix: tuple[str, ...], moved: tuple[str, ...], only=RULES) -> set[str]:
    """``prefix`` stays in the kept module; ``moved`` goes to the destination unchanged."""
    return run(src(*prefix, *moved), src(*prefix, LINK), src(*moved), only)


HELPER = ("def helper():", "    return 1")


def test_scope_dropped_runtime_import():
    assert moved_only(("import re",), ("def moved():", '    return re.compile("x")')) == {"7"}


def test_scope_default_arg_name():
    moved = ("def moved(x=DEFAULT_LIMIT):", "    return x")
    assert moved_only(("DEFAULT_LIMIT = 3",), moved, SCOPE) == {"7"}
    assert moved_only(("DEFAULT_LIMIT = 3",), moved) == {"1c", "7"}


def test_scope_decorator_name():
    prefix = ("def cache_it(f):", "    return f")
    moved = ("@cache_it", "def moved():", "    return 1")
    assert moved_only(prefix, moved, SCOPE) == {"7"}
    assert moved_only(prefix, moved) == {"1c", "7"}


def test_scope_local_shadowing_needs_nothing():
    moved = ("def moved(item):", "    value = item", "    return value")
    assert moved_only(("value = 1", "item = 2"), moved) == set()


def test_scope_global_declaration():
    reads = ("def moved():", "    global counter", "    return counter")
    assert moved_only(("counter = 0",), reads) == {"7"}
    writes = ("LIMIT = 1", "def moved():", "    global LIMIT", "    LIMIT = 2")
    kept = src("from pkg.dest import LIMIT, moved")
    assert run(src(*writes), kept, src(*writes), also=("LIMIT",)) == {"7"}


def test_scope_nested_closure():
    inner = ("def moved():", "    def inner():", "        return helper()", "    return inner")
    assert moved_only(HELPER, inner) == {"7"}
    free = ("def moved():", "    x = 1", "    def inner():", "        return x", "    return inner")
    assert moved_only((), free) == set()


def test_scope_comprehension():
    moved = ("def moved():", "    return [table[k] for k in range(3)]")
    assert moved_only(("table = {}",), moved) == {"7"}


def test_scope_type_checking_only_binding():
    guard = ("if TYPE_CHECKING:", "    import type_stub as dep")
    moved = ("def moved():", "    return dep")
    head = ("from typing import TYPE_CHECKING",)
    base = src(*head, "import runtime_dep as dep", *guard, *moved)
    kept = src(*head, "import runtime_dep as dep", LINK)
    assert run(base, kept, src(*head, *guard, *moved)) == {"7"}


def test_scope_conditional_binding():
    guarded = ("try:", "    import yaml as dep", "except ImportError:", "    dep = None")
    moved = (*guarded, "def moved():", "    return dep")
    assert run(src(*moved), src(LINK), src(*moved), also=("dep",)) == {"7"}


def test_scope_star_import():
    moved = ("def moved():", "    return thing")
    star = "from somewhere import *"
    assert run(src(star, *moved), src(star, LINK), src(star, *moved)) == {"7"}
    assert run(src(star, *moved), src(star, LINK), src(*moved)) == {"7"}


def test_scope_exec_or_globals():
    assert moved_only((), ("def moved():", "    return globals()")) == {"7"}


def test_scope_builtin_needs_nothing():
    moved = ("def moved(x):", "    if not len(x):", "        raise ValueError(x)", "    return x")
    assert moved_only((), moved) == set()


def test_scope_annotation_only_binding():
    moved = ("dep: object", "def moved():", "    return dep")
    assert run(src(*moved), src(LINK), src(*moved)) == {"7"}


def test_scope_lambda_in_default():
    moved = ("def moved(x=(lambda: helper)()):", "    return x")
    assert moved_only(HELPER, moved, SCOPE) == {"7"}


def test_scope_lambda_in_decorator_arg():
    register = ("def register(value):", "    return lambda f: f")
    moved = (*register, "@register((lambda: helper)())", "def moved():", "    return 1")
    kept = src(*HELPER, "from pkg.dest import register, moved")
    base = src(*HELPER, *moved)
    assert run(base, kept, src(*moved), SCOPE, also=("register",)) == {"7"}


TOP = ("def moved(x: int) -> str:", "    return str(x)")
NESTED = (
    "def moved():",
    "    def inner(x: int) -> None:",
    "        return None",
    "    return inner",
)
CLASS_BODY = ("def moved():", "    class Box:", "        size: int = 0", "    return Box")


def future_case(moved: tuple[str, ...], keep_future: bool) -> set[str]:
    dest = src(FUTURE, *moved) if keep_future else src(*moved)
    return run(src(FUTURE, *moved), src(FUTURE, LINK), dest)


def test_future_dropped_top_level():
    assert future_case(TOP, keep_future=False) == {"8"}


def test_future_dropped_nested():
    assert future_case(NESTED, keep_future=False) == {"8"}
    assert future_case(CLASS_BODY, keep_future=False) == {"8"}


def test_future_kept():
    for moved in (TOP, NESTED, CLASS_BODY):
        assert future_case(moved, keep_future=True) == set()


def test_scope_retained_def_loses_runtime_import():
    stay = ("def stay():", '    return re.compile("x")')
    base = src("import re", *stay, "def moved():", "    return 1")
    assert run(base, src(LINK, *stay), src("def moved():", "    return 1")) == {"7"}
    kept = src("import re", LINK, *stay)
    assert run(base, kept, src("def moved():", "    return 1")) == set()


def test_scope_retained_def_keeps_conditional_binding():
    guarded = ("try:", "    import yaml as dep", "except ImportError:", "    dep = None")
    stay = ("def stay():", "    return dep")
    base = src(*guarded, *stay, "def moved():", "    return 1")
    assert run(base, src(*guarded, LINK, *stay), src("def moved():", "    return 1")) == {"7"}


def retained_dep_case(guard: tuple[str, ...], base_import: bool, head_import: bool) -> set[str]:
    stay = ("def stay():", "    return dep")
    runtime = ("import runtime_dep as dep",)
    tail = ("def moved():", "    return 1")
    base = src("from typing import TYPE_CHECKING", *runtime * base_import, *guard, *stay, *tail)
    kept = src("from typing import TYPE_CHECKING", *runtime * head_import, *guard, LINK, *stay)
    return run(base, kept, src(*tail))


def test_scope_retained_def_loses_unconditional_binding_behind_type_checking():
    guard = ("if TYPE_CHECKING:", "    import type_stub as dep")
    assert retained_dep_case(guard, True, False) == {"7"}
    assert retained_dep_case(guard, True, True) == set()


def test_scope_retained_def_loses_unconditional_binding_behind_if_false():
    guard = ("if False:", "    import type_stub as dep")
    assert retained_dep_case(guard, True, False) == {"7"}


def test_scope_retained_def_conditional_at_base_stays_pure():
    guard = ("if TYPE_CHECKING:", "    import type_stub as dep")
    assert retained_dep_case(guard, False, False) == {"7"}


CALLER = "src/pkg/caller.py"


def module_attribute_case(caller: str) -> set[str]:
    manifest = {"old": OLD, "new": [OLD, DEST], "map": {"moved": DEST}, "importers": []}
    manifest |= {"docstring_prose": False, "tests": []}
    base = {OLD: src("def stay():", "    return 0", "def moved():", "    return 1"), CALLER: caller}
    head = {OLD: src("import pkg.dest", "def stay():", "    return 0"), CALLER: caller}
    head[DEST] = src("def moved():", "    return 1")
    problems = check_move(manifest, base.get, head.get, [CALLER], only=None)
    return {problem.split(":", 1)[0] for problem in problems}


def test_importer_reads_moved_name_through_old_module_object():
    for binding, use in (
        ("import pkg.old as old", "old"),
        ("import pkg.old", "pkg.old"),
        ("from pkg import old", "old"),
        ("from . import old as kept", "kept"),
    ):
        assert module_attribute_case(src(binding, f"VALUE = {use}.moved")) == {"5"}, binding
    assert module_attribute_case(src("import pkg.old as old", "VALUE = old.stay")) == set()


def global_state_case(base: tuple[str, ...], kept: tuple[str, ...], dest: tuple[str, ...], *moved):
    """Scope rules only; ``moved`` names are the ones the map sends to the destination."""
    return run(src(*base), src(*kept), src(*dest), SCOPE, also=moved)


def test_scope_retained_def_writes_moved_global():
    cache = ("_CACHE = {}", "def lookup():", "    return _CACHE")
    reset = ("def reset():", "    global _CACHE", "    _CACHE = {}")
    link = ("from pkg.dest import _CACHE, lookup",)
    assert global_state_case((*cache, *reset), (*link, *reset), cache, "_CACHE", "lookup") == {"7"}
    assert global_state_case(cache, link, cache, "_CACHE", "lookup") == set()
    bare = ("_CACHE = {}",)
    assert global_state_case((*bare, *reset), (*link, *reset), bare, "_CACHE") == {"7"}


def test_scope_global_only_binding_read_by_moved_def():
    init = ("def init():", "    global _engine", "    _engine = 1")
    use = ("def use():", "    return _engine")
    assert global_state_case((*init, *use), (*init, "from pkg.dest import use"), use, "use") == {
        "7"
    }


def test_scope_retained_def_reaches_moved_name_dynamically():
    poke = ("def poke():", '    globals()["X"] = 2')
    base, kept = ("X = 1", *poke), ("from pkg.dest import X", *poke)
    assert global_state_case(base, kept, ("X = 1",), "X") == {"7"}


def test_scope_class_body_rebinds_module_name():
    case = ("class C:", "    helper = helper")
    kept = (*HELPER, "from pkg.dest import C")
    assert global_state_case((*HELPER, *case), kept, case, "C") == {"7"}
    local = ("class C:", "    size = 1", "    twice = size * 2")
    assert global_state_case((*HELPER, *local), kept, local, "C") == set()


def test_importer_star_import_of_old_path_reaches_moved_name():
    star = "from pkg.old import *"
    assert module_attribute_case(src(star, "VALUE = moved")) == {"5"}
    assert module_attribute_case(src(star, "VALUE = stay")) == set()


def test_importer_package_alias_chain_reaches_moved_name():
    assert module_attribute_case(src("import pkg as p", "VALUE = p.old.moved")) == {"5"}
    assert module_attribute_case(src("import pkg as p", "VALUE = p.dest.moved")) == set()


def retained_binding_case(base_bind: str, kept_bind: tuple[str, ...]) -> set[str]:
    stay, tail = ("def stay():", "    return dep"), ("def moved():", "    return 1")
    kept = src("FLAG = 0", *kept_bind, LINK, *stay)
    return run(src("FLAG = 0", base_bind, *stay, *tail), kept, src(*tail), SCOPE)


def test_scope_retained_binding_inside_match_is_conditional():
    block = ("match FLAG:", "    case 1:", "        import runtime_dep as dep")
    assert retained_binding_case("import runtime_dep as dep", block) == {"7"}


def test_scope_retained_binding_in_short_circuit_walrus_is_conditional():
    assert retained_binding_case("dep = 1", ("FLAG and (dep := 1)",)) == {"7"}
    assert retained_binding_case("dep = 1", ("(dep := 1) if FLAG else 0",)) == {"7"}
    assert retained_binding_case("dep = 1", ("[0 for _ in (1,) if (dep := 1)]",)) == {"7"}


def test_scope_retained_binding_top_level_walrus_is_refused():
    assert retained_binding_case("dep = 1", ("(dep := 1)",)) == {"7"}
    assert retained_binding_case("dep = 1", ("1 > 2 < (dep := 1)",)) == {"7"}


def test_scope_retained_def_loses_conditional_binding_when_block_changes():
    guarded = ("try:", "    import yaml as dep", "except ImportError:", "    dep = None")
    edited = ("try:", "    import yaml as dep", "except ImportError:", "    dep = 0")
    stay, tail = ("def stay():", "    return dep"), ("def moved():", "    return 1")
    base = src(*guarded, *stay, *tail)
    assert run(base, src(*edited, LINK, *stay), src(*tail), SCOPE) == {"7"}


MOVED = ("def moved():", "    return 1")


def test_scope_module_level_conditionals_are_refused_and_bind_nothing():
    for guard in (
        ('if __name__ == "pkg.old":', "    dep = 1"),
        ("match FLAG:", "    case _:", "        dep = 1"),
        ("try:", "    dep = 1", "except ImportError:", "    pass"),
        ("for _ in (1,):", "    dep = 1"),
    ):
        assert moved_only(("FLAG = 0", *guard, "VALUE = dep"), MOVED) == {"1c", "7"}, guard


def test_scope_type_checking_import_block_is_allowed_but_binds_nothing():
    head = ("from typing import TYPE_CHECKING",)
    block = ("if TYPE_CHECKING:", "    from collections.abc import Callable")
    assert moved_only((*head, *block), MOVED, SCOPE) == set()
    assert moved_only((*head, *block, "VALUE = Callable"), MOVED) == {"1c"}
    loose = ("if TYPE_CHECKING:", "    from collections.abc import Callable", "    X = 1")
    assert moved_only((*head, *loose), MOVED, SCOPE) == {"7"}


def test_module_shape_accepts_the_four_planned_split_files():
    root = Path(__file__).resolve().parents[2] / "src" / "decoy_engine"
    for rel in (
        "detection/detectors.py",
        "plan/_checks.py",
        "profiling/eval/corpus.py",
        "generation/synthesize.py",
    ):
        tree = ast.parse((root / rel).read_text())
        assert shape(tree, rel) == [], rel
