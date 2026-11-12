"""D9 engine sink sentry: every file-system sink in the engine is reached through a capability.

A sink call passes only as (1) an adapter module handing a library a capability's descriptor or
library path, (2) a capability implementation module using raw fd-relative primitives on a held
descriptor with a validated name, or (3) one reviewed registry entry for that exact call site.
Being inside `decoy_engine/fileaccess/` grants nothing. The scan resolves import aliases,
re-exports, assignment and bound-method aliases, `functools.partial`, `getattr`, `importlib`,
`__import__`, `Path` objects held in variables and sinks passed as values.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.sentry.fa import sinks
from tests.sentry.fa.classify import Roles, classify, registry_index
from tests.sentry.fa.rules import (
    creator_problems,
    load_modules_config,
    load_registry,
    registry_problems,
)
from tests.sentry.fa.scan import scan_tree

HERE = Path(__file__).parent
SRC = HERE.parents[1] / "src"
FIXTURES = HERE / "fa_fixtures"
MODULES = load_modules_config(HERE / "file_access_modules.toml")
REGISTRY_PATH = HERE / "file_access_registry.toml"

ROLES = Roles(
    adapters=frozenset(MODULES["adapter_modules"]),
    capabilities=frozenset(MODULES["capability_modules"]),
    local_constructors=frozenset(MODULES["local_constructors"]),
    creators=frozenset(MODULES["capability_creators"]),
)

# Fixture roles by directory: the fixture tree mirrors the real module lists.
FIXTURE_ROLES = Roles(
    adapters=frozenset(
        f"fx/adapters/{p.name}" for p in (FIXTURES / "fx" / "adapters").glob("*.py")
    ),
    capabilities=frozenset(f"fx/cap/{p.name}" for p in (FIXTURES / "fx" / "cap").glob("*.py")),
    local_constructors=frozenset(),
    creators=frozenset(
        {"fx/cap/good_creator.py::make_it", "fx/cap/bad_creator_unvalidated.py::make_unvalidated"}
    ),
)


@pytest.fixture(scope="module")
def engine_scan():  # type: ignore[no-untyped-def]
    return scan_tree(SRC)[0]


@pytest.fixture(scope="module")
def fixture_scan():  # type: ignore[no-untyped-def]
    return scan_tree(FIXTURES, package="fx")[0]


def _violations_by_file(sites, roles):  # type: ignore[no-untyped-def]
    violations, _ = classify(sites, roles, {})
    by_file: dict[str, list[str]] = {}
    for v in violations:
        by_file.setdefault(v.site.module, []).append(str(v))
    return by_file


# -- the engine itself ----------------------------------------------------------------------


def test_registry_entries_are_complete() -> None:
    assert registry_problems(load_registry(REGISTRY_PATH)) == []


def test_registry_count_is_a_ratchet() -> None:
    import re

    text = REGISTRY_PATH.read_text(encoding="utf-8")
    declared = int(re.search(r"^# Count: (\d+)", text, re.M).group(1))  # type: ignore[union-attr]
    assert declared == len(load_registry(REGISTRY_PATH)), (
        "the registry changed size: update the count in its header in the same reviewed change"
    )


def test_every_engine_sink_is_allowed(engine_scan) -> None:  # type: ignore[no-untyped-def]
    registry = registry_index(load_registry(REGISTRY_PATH))
    violations, _used = classify(engine_scan, ROLES, registry)
    assert not violations, "\n".join(str(v) for v in violations)


def test_registry_has_no_stale_entries(engine_scan) -> None:  # type: ignore[no-untyped-def]
    registry = registry_index(load_registry(REGISTRY_PATH))
    _violations, used = classify(engine_scan, ROLES, registry)
    stale = sorted(set(registry) - used)
    assert not stale, f"registry entries that match no call site (remove them): {stale}"


def test_every_engine_module_still_exists_for_the_module_lists() -> None:
    for key in (
        "adapter_modules",
        "capability_modules",
        "local_constructors",
        "ambient_only_modules",
    ):
        for rel in MODULES[key]:
            assert (SRC / rel).is_file(), rel


def test_operator_environment_exceptions_are_current() -> None:
    """Each documented exception names a live function that reads exactly the listed variables
    (as string literals), and carries an owner, a reason and a removal note."""
    from tests.sentry.fa.rules import toml_reader

    data = toml_reader.loads((HERE / "file_access_modules.toml").read_text(encoding="utf-8"))
    entries = data.get("operator_environment", [])
    assert entries, "positive control: the Vault TLS exception is documented"
    for entry in entries:
        for key in ("module", "function", "variables", "owner", "reason", "removal"):
            assert entry.get(key), f"{entry.get('module')}: missing `{key}`"
        tree = ast.parse((SRC / entry["module"]).read_text(encoding="utf-8"))
        funcs = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == entry["function"]
        ]
        assert len(funcs) == 1, f"{entry['module']}::{entry['function']} not found"
        read = {
            n.value
            for n in ast.walk(funcs[0])
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isupper()
        }
        path_vars = {v for v in read if v.endswith(("_CACERT", "_CERT", "_KEY", "_FILE", "_PATH"))}
        assert path_vars == set(entry["variables"]), (entry["function"], sorted(path_vars))


def test_ambient_only_entries_sit_behind_the_ambient_guard() -> None:
    entries = load_registry(REGISTRY_PATH)
    flagged = {e["module"] for e in entries if e.get("ambient_only")}
    assert flagged <= set(MODULES["ambient_only_modules"])
    for rel in MODULES["ambient_guards"]:
        tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and (
                getattr(n.func, "attr", None) == "require_ambient"
                or getattr(n.func, "id", None) == "require_ambient"
            )
        ]
        assert calls, f"{rel} must call fileaccess.require_ambient"


def test_no_registry_entry_for_a_caller_path_outside_the_ambient_modules() -> None:
    """Caller-supplied and config-supplied paths are never registrable, except the opens in
    `_local.py` and the ambient-guarded scratch route."""
    for e in load_registry(REGISTRY_PATH):
        text = f"{e['rule']}".lower()
        if "caller" in text and "ambient" not in text and "scratch" not in text:
            pytest.fail(f"{e['module']}:{e['function']} registers a caller path: {e['rule']}")


# -- the sentry bites -------------------------------------------------------------------------

NEGATIVE_PLAIN = [
    "direct_open",
    "import_as",
    "from_import_as",
    "via_reexport",
    "assignment_alias",
    "bound_method_alias",
    "partial_alias",
    "getattr_literal",
    "getattr_dynamic",
    "importlib_literal",
    "importlib_dynamic",
    "dunder_import",
    "path_variable_stat",
    "path_variable_iterdir",
    "module_constant_env",
    "lambda_call",
    "mapping_of_sinks",
    "execution_module_os_link",
    "duckdb_connection",
    "thread_pool_submit",
    "thread_target",
    "helpers_open_locator",
    "helpers_pq_read",
]


@pytest.mark.parametrize("name", NEGATIVE_PLAIN)
def test_sentry_bites_on_a_plain_module(fixture_scan, name: str) -> None:  # type: ignore[no-untyped-def]
    by_file = _violations_by_file(fixture_scan, FIXTURE_ROLES)
    assert by_file.get(f"fx/plain/{name}.py"), f"{name} was not flagged"


# Each fixture names the sink it exists for; the test fails unless THAT sink is the one flagged.
SPECIFIC_SINKS = {
    "sink_pa_input_stream": "pyarrow.input_stream",
    "sink_pa_output_stream": "pyarrow.output_stream",
    "sink_pa_create_memory_map": "pyarrow.create_memory_map",
    "sink_pa_orc": "pyarrow.orc.read_table",
    "sink_np_loadtxt": "numpy.loadtxt",
    "sink_np_fromfile": "numpy.fromfile",
    "sink_np_memmap": "numpy.memmap",
    "sink_pd_hdfstore": "pandas.HDFStore",
    "sink_pd_excelwriter": "pandas.ExcelWriter",
    "sink_codecs_open": "codecs.open",
    "sink_ctypes_cdll": "ctypes.CDLL",
    "sink_runpy": "runpy.run_path",
    "sink_spec_from_file": "importlib.util.spec_from_file_location",
    "chained_pool_submit": "executor.submit",
}
SPECIFIC_CAP_SINKS = {"bad_cdll_path": "ctypes.CDLL"}


@pytest.mark.parametrize("name,sink", sorted(SPECIFIC_CAP_SINKS.items()))
def test_sentry_flags_the_specific_capability_sink(fixture_scan, name: str, sink: str) -> None:  # type: ignore[no-untyped-def]
    violations, _ = classify(fixture_scan, FIXTURE_ROLES, {})
    flagged = {v.site.sink for v in violations if v.site.module == f"fx/cap/{name}.py"}
    assert sink in flagged, f"{name}: expected {sink}, flagged {sorted(flagged)}"


@pytest.mark.parametrize("name,sink", sorted(SPECIFIC_SINKS.items()))
def test_sentry_flags_the_specific_sink(fixture_scan, name: str, sink: str) -> None:  # type: ignore[no-untyped-def]
    violations, _ = classify(fixture_scan, FIXTURE_ROLES, {})
    flagged = {v.site.sink for v in violations if v.site.module == f"fx/plain/{name}.py"}
    assert sink in flagged, f"{name}: expected {sink}, flagged {sorted(flagged)}"


@pytest.mark.parametrize("name", ["bad_str_param", "bad_module_constant"])
def test_sentry_bites_on_a_bad_adapter(fixture_scan, name: str) -> None:  # type: ignore[no-untyped-def]
    by_file = _violations_by_file(fixture_scan, FIXTURE_ROLES)
    assert by_file.get(f"fx/adapters/{name}.py"), f"{name} was not flagged"


@pytest.mark.parametrize(
    "name",
    [
        "bad_third_party",
        "bad_no_dir_fd",
        "bad_caller_name",
        "bad_scandir_str",
        "bad_descriptor_caller_int",
        "bad_cdll_path",
        "bad_public_dir_fd",
        "bad_chmod_no_dir_fd",
    ],
)
def test_sentry_bites_on_a_bad_capability_module(fixture_scan, name: str) -> None:  # type: ignore[no-untyped-def]
    by_file = _violations_by_file(fixture_scan, FIXTURE_ROLES)
    assert by_file.get(f"fx/cap/{name}.py"), f"{name} was not flagged"


@pytest.mark.parametrize(
    "module", ["fx/adapters/good_arrow.py", "fx/cap/good_ops.py", "fx/cap/good_creator.py"]
)
def test_positive_fixtures_pass(fixture_scan, module: str) -> None:  # type: ignore[no-untyped-def]
    by_file = _violations_by_file(fixture_scan, FIXTURE_ROLES)
    assert not by_file.get(module), by_file.get(module)


def test_a_registry_entry_without_owner_or_rule_is_rejected() -> None:
    entry = {
        "module": "m.py",
        "function": "f",
        "sink": "open",
        "ordinal": 0,
        "rule": "",
        "owner": "",
        "reason": "x",
    }
    problems = registry_problems([entry])
    assert any("`rule`" in p for p in problems) and any("`owner`" in p for p in problems)


def test_a_registered_site_passes_and_an_unregistered_one_does_not(fixture_scan) -> None:  # type: ignore[no-untyped-def]
    sites = [s for s in fixture_scan if s.module == "fx/plain/direct_open.py"]
    assert sites
    entry = {
        "module": sites[0].module,
        "function": sites[0].function,
        "sink": sites[0].sink,
        "ordinal": sites[0].ordinal,
        "rule": "r",
        "owner": "o",
        "reason": "x",
    }
    ok, _ = classify(sites, FIXTURE_ROLES, registry_index([entry]))
    bad, _ = classify(sites, FIXTURE_ROLES, {})
    assert not ok and bad


def test_capability_creators_are_current() -> None:
    assert creator_problems(MODULES["capability_creators"], ROLES.capabilities, SRC) == []


def test_a_stale_capability_creator_is_flagged() -> None:
    caps = ROLES.capabilities
    missing = ["decoy_engine/fileaccess/_output.py::no_such_creator"]
    assert creator_problems(missing, caps, SRC)
    assert creator_problems(["decoy_engine/connectors/sftp.py::_open_sftp"], caps, SRC)
    assert creator_problems(["not-a-key"], caps, SRC)


def test_an_unlisted_public_dir_fd_function_is_still_flagged(fixture_scan) -> None:  # type: ignore[no-untyped-def]
    flagged = [
        v
        for v in classify(fixture_scan, FIXTURE_ROLES, {})[0]
        if v.site.module == "fx/cap/bad_public_dir_fd.py"
    ]
    assert flagged and "dir_fd" in flagged[0].reason


def test_chmod_without_dir_fd_is_still_flagged(fixture_scan) -> None:  # type: ignore[no-untyped-def]
    flagged = [
        v
        for v in classify(fixture_scan, FIXTURE_ROLES, {})[0]
        if v.site.module == "fx/cap/bad_chmod_no_dir_fd.py"
    ]
    assert flagged and "dir_fd" in flagged[0].reason


# -- F25 S1b: the scratch path release and native-boundary keys (plan 4.1) ------------------

RELEASE_OWNERS = frozenset({"decoy_engine/fileaccess/_scratch.py"})
NATIVE_OWNERS = RELEASE_OWNERS | {"decoy_engine/fileaccess/__init__.py"}
# A DuckDB connection or result call; `fetch*` is matched by prefix.
NATIVE_METHODS = sinks.DUCKDB_CONNECTION_METHODS | {"register", "unregister", "arrow", "df"}


def _qualified(tree: ast.Module) -> Iterator[tuple[str, ast.AST]]:
    def visit(body: list[ast.stmt], qual: str) -> Iterator[tuple[str, ast.AST]]:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                yield from visit(
                    stmt.body, stmt.name if qual == "<module>" else f"{qual}.{stmt.name}"
                )
            else:
                yield from ((qual, node) for node in ast.walk(stmt))

    return visit(tree.body, "<module>")


def _uses(tree: ast.Module, name: str) -> list[str]:
    """Where `name` is read as an attribute or name, imported, or looked up by string."""
    return [
        qual
        for qual, node in _qualified(tree)
        if (isinstance(node, ast.Attribute) and node.attr == name)
        or (isinstance(node, ast.Name) and node.id == name)
        or (isinstance(node, ast.alias) and name in (node.name, node.asname))
        or (isinstance(node, ast.Constant) and node.value == name)
    ]


def _bare_native_calls(tree: ast.Module) -> list[str]:
    """Connection and result methods reached other than as `scratch_native(conn.method, ...)`."""
    wrapped = {
        id(node.args[0])
        for _q, node in _qualified(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None)) == "scratch_native"
        and node.args
    }
    found = []
    for qual, node in _qualified(tree):
        method = node.attr if isinstance(node, ast.Attribute) else None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            method = node.value  # `getattr(conn, "execute")`
        if method is None or id(node) in wrapped:
            continue
        if method in NATIVE_METHODS or (method.startswith("fetch") and method.isidentifier()):
            found.append(qual)
    return found


def release_findings(trees: dict[str, ast.Module], keys: dict[str, list[str]]) -> list[str]:
    """Why the `scratch_path_release` and `scratch_native` keys do not match the code exactly."""
    return []  # neutered control (red commit only): no rule


def _source_trees(root: Path, package: str) -> dict[str, ast.Module]:
    return {
        p.relative_to(root).as_posix(): ast.parse(p.read_text(encoding="utf-8"))
        for p in sorted((root / package).rglob("*.py"))
    }


_EVASIONS = "fx/plain/scratch_release_evasions.py"
_RELEASE_CELLS = {
    # cell: (fixture keys, findings it must include)
    "unlisted_caller_evasions": (
        {"scratch_path_release": [], "scratch_native": [_EVASIONS]},
        {
            f"scratch_path_release: {_EVASIONS}::{f} is not listed"
            for f in ("release_by_alias", "release_by_getattr", "release_by_bound_method")
        },
    ),
    "listed_without_call": (
        {"scratch_path_release": ["fx/plain/direct_open.py"], "scratch_native": [_EVASIONS]},
        {"scratch_path_release: fx/plain/direct_open.py is listed but makes no such call"},
    ),
    "bare_native_call_evasions": (
        {"scratch_path_release": [_EVASIONS], "scratch_native": [_EVASIONS]},
        {
            f"scratch_native: {_EVASIONS}::{f} calls a connection bare"
            for f in ("native_bare", "native_by_getattr", "native_by_bound_method")
        },
    ),
    "unlisted_native_caller": (
        {"scratch_path_release": [_EVASIONS], "scratch_native": []},
        {f"scratch_native: {_EVASIONS}::{q} is not listed" for q in ("<module>", "native_wrapped")},
    ),
}


@pytest.mark.parametrize("cell", ["engine", *_RELEASE_CELLS])
def test_local_path_callers_are_allowlisted(cell: str) -> None:
    if cell == "engine":
        assert release_findings(_source_trees(SRC, "decoy_engine"), MODULES) == []
        return
    keys, expected = _RELEASE_CELLS[cell]
    findings = set(release_findings(_source_trees(FIXTURES, "fx"), keys))
    assert expected <= findings, sorted(expected - findings)
    assert not [f for f in findings if "native_wrapped calls" in f], "a wrapped call was flagged"


def test_a_listed_creator_that_does_not_validate_its_name_is_still_flagged(fixture_scan) -> None:  # type: ignore[no-untyped-def]
    flagged = [
        v
        for v in classify(fixture_scan, FIXTURE_ROLES, {})[0]
        if v.site.module == "fx/cap/bad_creator_unvalidated.py"
    ]
    assert flagged and "component validator" in flagged[0].reason
