"""Every manifest validator lives in scripts/release/reason_manifest_policy.py.

Discovery walks every tracked .py under src/, scripts/ and tests/ and parses it. A non-test
function is a validator candidate when its body uses a registry key (string constant, attribute
or name) in a comparison, a comprehension filter, or an `if` that appends or returns, or when any
branch tests a release transition's pending sets (a second transition decision even when it only
picks a version). Function names play no part. A candidate outside the policy module fails unless it is an adapter (one
`return` of a call to a name imported from the policy module) or is in EXEMPT with a reason.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICY = "scripts/release/reason_manifest_policy.py"
POLICY_MODULE = "scripts.release.reason_manifest_policy"
KEYS = frozenset(
    {
        "registry_version",
        "since",
        "since_counts",
        "reason_schema_version",
        "event_schema_version",
        "manifest_sha256",
        "deprecated_in",
        "replaced_by",
        "REGISTRY_VERSION",
        "REASON_SCHEMA_VERSION",
        "EVENT_SCHEMA_VERSION",
        *(TRANSITION_KEYS := ("pending_identities", "pending_deprecations", "pending_summaries")),
    }
)
# (path, qualified name): reason. A method is keyed as Class.method, so a same-named method on
# another class is still checked. The table only shrinks; a stale row fails the guard.
EXEMPT: dict[tuple[str, str], str] = {
    ("scripts/release/sbom_checks.py", "pin_violations"): "requirements-file pins, unrelated",
    (
        "src/decoy_engine/reasons/_registry.py",
        "check_integrity",
    ): "the registry's own structural check over specs, not a manifest comparison",
    # Deviation (orchestrator, 2026-11-06): the plan's candidate rule also selects these three.
    # They compare one record or event against the live registry, not two manifests.
    (
        "src/decoy_engine/_progress_events.py",
        "_field_value",
    ): "emits the live registry version into one event field",
    (
        "src/decoy_engine/reasons/_record.py",
        "ReasonRecord.validate",
    ): "ReasonRecord.validate: one record's version against the live registry",
    # RESULT E1 (#397) moved the check from from_payload into parse_payload; from_payload now
    # only delegates, so its exemption moved with the check.
    (
        "src/decoy_engine/reasons/_record.py",
        "ReasonRecord.parse_payload",
    ): "ReasonRecord.parse_payload: checks one record's version, not a manifest comparison",
    (
        "tests/unit/run_result/test_model.py",
        "_compat_literals",
    ): "an AST scanner that finds version literals for the one-parser test, not a validator",
}


def _is_key(node: ast.AST, names: frozenset[str] = frozenset()) -> bool:
    if isinstance(node, ast.Constant):
        return node.value in KEYS
    if isinstance(node, ast.Attribute):
        return node.attr in KEYS or ast.unparse(node) in names
    return isinstance(node, ast.Name) and (node.id in KEYS or node.id in names)


def _has_key(node: ast.AST, names: frozenset[str] = frozenset()) -> bool:
    return any(_is_key(n, names) for n in ast.walk(node))


def _key_constants(tree: ast.Module) -> frozenset[str]:
    """Module-level names bound to a tuple, list or set made only of registry key strings."""
    found = set()
    for n in tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.value, (ast.Tuple, ast.List, ast.Set)):
            elts = n.value.elts
            if elts and all(isinstance(e, ast.Constant) and e.value in KEYS for e in elts):
                found |= {t.id for t in n.targets if isinstance(t, ast.Name)}
    return frozenset(found)


ACCESSORS = frozenset({"version_of", "entries_since", "contract_versions"})


def _policy_imports(tree: ast.Module, policy_consts: frozenset[str]) -> frozenset[str]:
    """Names that carry registry keys because they come from the policy module: its key-tuple
    constants (such as SCHEMA_ROOTS) and key accessor functions, bound by ``from`` import, or
    reached through a module alias (``import m as p``, ``import a.b.m``, ``from a.b import m``)
    as the dotted form ``p.SCHEMA_ROOTS``."""
    carry = policy_consts | ACCESSORS
    found: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == POLICY_MODULE:
            found |= {a.asname or a.name for a in n.names if a.name in carry}
        elif isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names:
                if f"{n.module}.{a.name}" == POLICY_MODULE:
                    found |= {f"{a.asname or a.name}.{c}" for c in carry}
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name == POLICY_MODULE:
                    found |= {f"{a.asname or a.name}.{c}" for c in carry}
    return frozenset(found)


def _bound_names(target: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


def _aliases(func: ast.AST, consts: frozenset[str]) -> frozenset[str]:
    """Local names assigned or iterated from a registry-key expression, to a fixed point."""
    names: set[str] = set()
    while True:
        before = len(names)
        seen = frozenset(names | consts)
        for n in ast.walk(func):
            if isinstance(n, ast.Assign) and _has_key(n.value, seen):
                for t in n.targets:
                    names |= _bound_names(t)
            elif isinstance(n, (ast.AnnAssign, ast.NamedExpr)) and n.value is not None:
                if _has_key(n.value, seen):
                    names |= _bound_names(n.target)
            elif isinstance(n, (ast.For, ast.comprehension)) and _has_key(n.iter, seen):
                names |= _bound_names(n.target)
        if len(names) == before:
            return frozenset(names | consts)


def _collects(stmts: list[ast.stmt]) -> bool:
    for s in stmts:
        for n in ast.walk(s):
            if isinstance(n, ast.Return):
                return True
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                if n.func.attr in {"append", "extend", "add"}:
                    return True
            if isinstance(n, ast.AugAssign):
                return True
    return False


def _uses_key_as_check(func: ast.AST, consts: frozenset[str] = frozenset()) -> bool:
    names = _aliases(func, consts)
    for n in ast.walk(func):
        if isinstance(n, ast.Compare) and _has_key(n, names):
            return True
        if isinstance(n, ast.comprehension) and any(_has_key(i, names) for i in n.ifs):
            return True
        if isinstance(n, ast.If) and _has_key(n.test, names) and _collects(n.body + n.orelse):
            return True
        if isinstance(n, (ast.If, ast.IfExp, ast.While)) and _branches_on_transition(n.test):
            return True
    return False


def _branches_on_transition(test: ast.AST) -> bool:
    return any(_is_key(n, frozenset()) for n in ast.walk(test)) and any(
        (isinstance(n, ast.Attribute) and n.attr in TRANSITION_KEYS)
        or (isinstance(n, ast.Name) and n.id in TRANSITION_KEYS)
        or (isinstance(n, ast.Constant) and n.value in TRANSITION_KEYS)
        for n in ast.walk(test)
    )


def _imports_engine(n: ast.AST) -> bool:
    if isinstance(n, ast.Import):
        return any(a.name.split(".")[0] == "decoy_engine" for a in n.names)
    return isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "decoy_engine"


def _policy_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == POLICY_MODULE:
            names |= {a.asname or a.name for a in n.names}
    return names


def _is_adapter(func: ast.FunctionDef, imported: set[str]) -> bool:
    body = list(func.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False
    call = body[0].value
    return (
        isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in imported
    )


def tracked_py(root: Path) -> list[str]:
    git = shutil.which("git")
    if git and (root / ".git").exists():
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [git, "ls-files", "--", "src", "scripts", "tests"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode == 0:
            return sorted(p for p in out.stdout.splitlines() if p.endswith(".py"))
    return sorted(
        p.relative_to(root).as_posix()
        for top in ("src", "scripts", "tests")
        for p in (root / top).rglob("*.py")
    )


def _functions(
    tree: ast.AST, prefix: str = ""
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Every function with its qualified name: ``Class.method`` inside classes, else the name."""
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for child in ast.iter_child_nodes(tree):
        if isinstance(child, ast.ClassDef):
            found += _functions(child, f"{prefix}{child.name}.")
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append((f"{prefix}{child.name}", child))
            found += _functions(child, prefix)
        else:
            found += _functions(child, prefix)
    return found


def _policy_key_constants(root: Path) -> frozenset[str]:
    path = root / POLICY
    return (
        _key_constants(ast.parse(path.read_text(encoding="utf-8")))
        if path.exists()
        else frozenset()
    )


def guard_violations(root: Path, files: list[str], exempt: dict[tuple[str, str], str]) -> list[str]:
    problems: list[str] = []
    policy_consts = _policy_key_constants(root)
    defined: set[tuple[str, str]] = set()
    for rel in files:
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        if rel == POLICY:
            imports = [n for n in ast.walk(tree) if _imports_engine(n)]
            if imports:
                problems.append(f"{rel}: imports decoy_engine")
            continue
        imported = _policy_names(tree)
        consts = _key_constants(tree) | _policy_imports(tree, policy_consts)
        for qual, node in _functions(tree):
            defined.add((rel, qual))
            if node.name.startswith("test_") or not _uses_key_as_check(node, consts):
                continue
            ident = (rel, qual)
            if ident in exempt:
                continue
            if isinstance(node, ast.FunctionDef) and _is_adapter(node, imported):
                continue
            problems.append(f"{rel}::{qual}")
    problems += [f"stale exemption {p}::{f}" for (p, f) in sorted(exempt) if (p, f) not in defined]
    return problems


def test_no_manifest_validator_lives_outside_the_policy_module() -> None:
    assert guard_violations(ROOT, tracked_py(ROOT), EXEMPT) == []


def _tree(tmp_path: Path, files: dict[str, str]) -> list[str]:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return sorted(files)


_STRAY = (
    "def stray_violations(a, b):\n"
    "    return [e for e in b['entries'] if e['since'] > a['registry_version']]\n"
)
_RENAMED = (
    "def ensure_catalog(a, b):\n"
    "    out = []\n"
    "    if a['registry_version'] != b['registry_version']:\n"
    "        out.append('x')\n"
    "    return out\n"
)
_ADAPTER = (
    f"from {POLICY_MODULE} import since_window_violations\n"
    "def _since_violations(specs, p, l):\n"
    '    """doc"""\n'
    "    return since_window_violations(((s.n, s.since) for s in specs), p, l)\n"
)


def test_guard_flags_seeded_validators_outside_any_list(tmp_path: Path) -> None:
    files = _tree(tmp_path, {"src/a.py": _STRAY, "scripts/b.py": _RENAMED, POLICY: "x = 1\n"})
    assert guard_violations(tmp_path, files, {}) == [
        "scripts/b.py::ensure_catalog",
        "src/a.py::stray_violations",
    ]


def test_guard_accepts_adapters_and_exemptions(tmp_path: Path) -> None:
    files = _tree(tmp_path, {"tests/c.py": _ADAPTER, "scripts/b.py": _RENAMED})
    assert guard_violations(tmp_path, files, {("scripts/b.py", "ensure_catalog"): "r"}) == []


def test_guard_ignores_test_functions(tmp_path: Path) -> None:
    files = _tree(tmp_path, {"tests/t.py": _STRAY.replace("stray_violations", "test_x")})
    assert guard_violations(tmp_path, files, {}) == []


def test_guard_flags_a_policy_module_that_imports_the_engine(tmp_path: Path) -> None:
    files = _tree(tmp_path, {POLICY: "from decoy_engine import reasons\n"})
    assert guard_violations(tmp_path, files, {}) == [f"{POLICY}: imports decoy_engine"]
    files = _tree(tmp_path, {POLICY: "import decoy_engine.reasons\n"})
    assert guard_violations(tmp_path, files, {}) == [f"{POLICY}: imports decoy_engine"]


def test_guard_flags_a_stale_exemption(tmp_path: Path) -> None:
    files = _tree(tmp_path, {"src/a.py": "x = 1\n"})
    assert guard_violations(tmp_path, files, {("src/a.py", "gone"): "r"}) == [
        "stale exemption src/a.py::gone"
    ]


_TWO_CLASSES = (
    "class A:\n"
    "    def validate(self, x):\n"
    "        return x['since'] > 1\n"
    "class B:\n"
    "    def validate(self, x):\n"
    "        return x['since'] > 1\n"
)


def test_a_qualified_exemption_does_not_cover_a_same_named_method(tmp_path: Path) -> None:
    files = _tree(tmp_path, {"src/a.py": _TWO_CLASSES})
    assert guard_violations(tmp_path, files, {("src/a.py", "A.validate"): "r"}) == [
        "src/a.py::B.validate"
    ]
    assert guard_violations(tmp_path, files, {("src/a.py", "validate"): "r"}) == [
        "src/a.py::A.validate",
        "src/a.py::B.validate",
        "stale exemption src/a.py::validate",
    ]


_ALIAS = (
    "def drift(a, b):\n"
    "    x = a['registry_version']\n"
    "    y = b['registry_version']\n"
    "    if x != y:\n"
    "        return ['drift']\n"
    "    return []\n"
)
_ASSIGN_THEN_COMPARE = (
    "def late(entries, floor):\n"
    "    out = []\n"
    "    for e in entries:\n"
    "        s = e['since']\n"
    "        if s <= floor:\n"
    "            out.append(e)\n"
    "    return out\n"
)
_CONST_TUPLE_COPY = (
    "_ROOTS = ('reason_schema_version', 'event_schema_version')\n"
    "def roots_differ(a, b):\n"
    "    return [f'{r}' for r in _ROOTS if a[r] != b[r]]\n"
)


def test_guard_flags_aliases_and_keys_held_in_a_constant_tuple(tmp_path: Path) -> None:
    files = _tree(
        tmp_path,
        {"src/a.py": _ALIAS, "src/b.py": _ASSIGN_THEN_COMPARE, "src/c.py": _CONST_TUPLE_COPY},
    )
    assert guard_violations(tmp_path, files, {}) == [
        "src/a.py::drift",
        "src/b.py::late",
        "src/c.py::roots_differ",
    ]


_POLICY_WITH_ROOTS = "SCHEMA_ROOTS = ('reason_schema_version', 'event_schema_version')\n"
_IMPORTED_TUPLE = (
    f"from {POLICY_MODULE} import SCHEMA_ROOTS\n"
    "def roots_differ(a, b):\n"
    "    return [r for r in SCHEMA_ROOTS if a[r] != b[r]]\n"
)
_ACCESSOR_COMPARE = (
    f"from {POLICY_MODULE} import version_of\n"
    "def moved(m, h):\n"
    "    if version_of(m) != version_of(h):\n"
    "        return ['moved']\n"
    "    return []\n"
)


def test_guard_flags_policy_constants_and_accessors_used_outside_the_policy(tmp_path: Path) -> None:
    files = _tree(
        tmp_path,
        {POLICY: _POLICY_WITH_ROOTS, "src/a.py": _IMPORTED_TUPLE, "src/b.py": _ACCESSOR_COMPARE},
    )
    assert guard_violations(tmp_path, files, {}) == [
        "src/a.py::roots_differ",
        "src/b.py::moved",
    ]


_QUALIFIED_TUPLE = (
    f"import {POLICY_MODULE} as policy\n"
    "def roots_differ(a, b):\n"
    "    return [r for r in policy.SCHEMA_ROOTS if a[r] != b[r]]\n"
)
_QUALIFIED_ACCESSOR = (
    f"import {POLICY_MODULE}\n"
    "def moved(m, h):\n"
    f"    if {POLICY_MODULE}.version_of(m) != {POLICY_MODULE}.version_of(h):\n"
    "        return ['moved']\n"
    "    return []\n"
)
_PACKAGE_ALIAS = (
    "from scripts.release import reason_manifest_policy as pol\n"
    "def moved(m, h):\n"
    "    if pol.version_of(m) != pol.version_of(h):\n"
    "        return ['moved']\n"
    "    return []\n"
)


def test_guard_flags_qualified_uses_through_module_aliases(tmp_path: Path) -> None:
    files = _tree(
        tmp_path,
        {
            POLICY: _POLICY_WITH_ROOTS,
            "src/a.py": _QUALIFIED_TUPLE,
            "src/b.py": _QUALIFIED_ACCESSOR,
            "src/c.py": _PACKAGE_ALIAS,
        },
    )
    assert guard_violations(tmp_path, files, {}) == [
        "src/a.py::roots_differ",
        "src/b.py::moved",
        "src/c.py::moved",
    ]


# The four registry transitions and the pending-state decisions are made only by
# the policy module. A branch on a transition's pending sets outside it is a second decision point
# even when it collects nothing (it may only pick a version).
_DEPRECATION_BRANCH = (
    "def target(t, allowed):\n"
    "    version = allowed\n"
    "    if t.pending_deprecations and not t.pending_identities:\n"
    "        version = allowed + '.1'\n"
    "    return version\n"
)
_PENDING_SUMMARY_FILTER = (
    "def marked(t, live):\n    return [e for e in live if (e[0], e[1]) in t.pending_summaries]\n"
)


def test_guard_flags_a_local_transition_branch(tmp_path: Path) -> None:
    files = _tree(
        tmp_path,
        {"scripts/release/s.py": _DEPRECATION_BRANCH, "scripts/dev/r.py": _PENDING_SUMMARY_FILTER},
    )
    assert guard_violations(tmp_path, files, {}) == [
        "scripts/dev/r.py::marked",
        "scripts/release/s.py::target",
    ]


def private_registry_imports(root: Path, files: list[str]) -> list[str]:
    """Scripts that import the registry's private module instead of `decoy_engine.reasons`."""
    found = []
    for rel in files:
        if not rel.startswith("scripts/"):
            continue
        for n in ast.walk(ast.parse((root / rel).read_text(encoding="utf-8"))):
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else []
            if isinstance(n, ast.ImportFrom) and n.module:
                names = [n.module] + [f"{n.module}.{a.name}" for a in n.names]
            if any(x.startswith("decoy_engine.reasons._registry") for x in names):
                found.append(rel)
    return found


def test_no_script_imports_the_private_registry_module() -> None:
    assert private_registry_imports(ROOT, tracked_py(ROOT)) == []


def test_private_registry_import_guard_flags_a_seeded_import(tmp_path: Path) -> None:
    files = _tree(
        tmp_path,
        {
            "scripts/a.py": "from decoy_engine.reasons._registry import PENDING_SUMMARY_EDITS\n",
            "scripts/b.py": "from decoy_engine.reasons import _registry\n",
            "scripts/c.py": "from decoy_engine.reasons import pending_summary_edits\n",
        },
    )
    assert private_registry_imports(tmp_path, files) == ["scripts/a.py", "scripts/b.py"]


RELEASE_CHECKS = ROOT / "scripts" / "release" / "release_checks.sh"
# Registry state words a shell check would need to parse the catalog itself.
_SHELL_STATE = ("REGISTRY_VERSION", "PENDING_SUMMARY_EDITS", "UNRELEASED", "unreleased", "since=")


def test_release_checks_asks_the_policy_and_parses_no_registry_state() -> None:
    text = RELEASE_CHECKS.read_text(encoding="utf-8")
    assert '"$py" scripts/release/reason_release_pin.py --release-ready' in text
    assert [w for w in _SHELL_STATE if w in text] == []
