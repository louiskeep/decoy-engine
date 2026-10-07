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
from pathlib import Path

import pytest

from tests.sentry.fa.classify import Roles, classify, registry_index
from tests.sentry.fa.rules import (
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
)

# Fixture roles by directory: the fixture tree mirrors the real module lists.
FIXTURE_ROLES = Roles(
    adapters=frozenset(
        f"fx/adapters/{p.name}" for p in (FIXTURES / "fx" / "adapters").glob("*.py")
    ),
    capabilities=frozenset(f"fx/cap/{p.name}" for p in (FIXTURES / "fx" / "cap").glob("*.py")),
    local_constructors=frozenset(),
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
    ],
)
def test_sentry_bites_on_a_bad_capability_module(fixture_scan, name: str) -> None:  # type: ignore[no-untyped-def]
    by_file = _violations_by_file(fixture_scan, FIXTURE_ROLES)
    assert by_file.get(f"fx/cap/{name}.py"), f"{name} was not flagged"


@pytest.mark.parametrize("module", ["fx/adapters/good_arrow.py", "fx/cap/good_ops.py"])
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
