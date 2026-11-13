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
    NATIVE_OWN_MAPPING,
    NATIVE_OWNERS,
    SCRATCH_NATIVE_PENDING,
    SCRATCH_READ_SCOPE,
    bare_native,
    creator_problems,
    load_modules_config,
    load_registry,
    native_findings,
    registry_problems,
    release_findings,
    source_modules,
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


# -- F25 plan 4.1: the scratch path release key and the scratch-native rule ------------------


def _source_trees(root: Path, package: str) -> dict[str, ast.Module]:
    return {
        p.relative_to(root).as_posix(): ast.parse(p.read_text(encoding="utf-8"))
        for p in sorted((root / package).rglob("*.py"))
    }


_EVASIONS = "fx/plain/scratch_release_evasions.py"
_RELEASE_CELLS = {
    # cell: (listed modules, findings it must include)
    "unlisted_caller_evasions": (
        set(),
        {
            f"scratch_path_release: {_EVASIONS}::{f} is not listed"
            for f in ("release_by_alias", "release_by_getattr", "release_by_bound_method")
        },
    ),
    "listed_without_call": (
        {_EVASIONS, "fx/plain/direct_open.py"},
        {"scratch_path_release: fx/plain/direct_open.py is listed but makes no such call"},
    ),
}


@pytest.mark.parametrize("cell", ["engine", *_RELEASE_CELLS])
def test_local_path_callers_are_allowlisted(cell: str) -> None:
    if cell == "engine":
        listed = set(MODULES["scratch_path_release"])
        assert release_findings(_source_trees(SRC, "decoy_engine"), listed) == []
        return
    listed, expected = _RELEASE_CELLS[cell]
    findings = set(release_findings(_source_trees(FIXTURES, "fx"), listed))
    assert expected <= findings, sorted(expected - findings)


_DERIVED = "fx/plain/scratch_native_derived.py"
_READS = "fx/plain/scratch_read_evasions.py"
_ISOLATED_RUN = "decoy_engine/execution/_isolated_run.py"


@pytest.fixture(scope="module")
def engine_bare() -> dict[str, list[str]]:
    return bare_native(SRC, "decoy_engine", release=frozenset(MODULES["scratch_path_release"]))


@pytest.fixture(scope="module")
def fixture_bare() -> dict[str, list[str]]:
    return bare_native(FIXTURES, "fx")


def _flagged(bare: dict[str, list[str]], module: str, function: str, what: str = "") -> bool:
    return any(f.startswith(f"{function}:") and what in f for f in bare.get(module, []))


_DERIVED_CELLS = {
    # cell: (fixture, function, text of the finding); `_READS` never names the wrapper
    "connect_site_unlisted": (_READS, "connect_site_unlisted", "conn.execute"),
    "helper_receives_connection": (_READS, "_load", "conn.execute"),
    "class_attribute_connection": (_DERIVED, "ClassAttributeConnection.run", ".execute"),
    "method_returns_connection": (_DERIVED, "MethodReturnsConnection.query", "conn.execute"),
    "attr_from_local_connection": (_DERIVED, "AttrFromLocalConnection.drop", ".unregister"),
    "annotated_assignment_connection": (_DERIVED, "annotated_assignment_connection", ".execute"),
    "wrapper_resolved_not_by_name": (_DERIVED, "wrapper_resolved_not_by_name", ".execute"),
}


@pytest.mark.parametrize(
    "cell",
    [
        "engine",
        *_DERIVED_CELLS,
        "pending_bare_ok",
        "pending_entry_stale",
        "pending_set_exact",
        "own_mapping_fixed",
    ],
)
def test_native_key_membership_is_derived(cell: str, engine_bare, fixture_bare) -> None:  # type: ignore[no-untyped-def]
    """Membership follows the scanner's connection flow, never a module naming the wrapper."""
    fixture_modules = set(source_modules(FIXTURES, "fx").values())
    if cell == "engine":
        modules = set(source_modules(SRC, "decoy_engine").values())
        assert native_findings(engine_bare, SCRATCH_NATIVE_PENDING, modules) == []
        stream = "decoy_engine/execution/out_of_core/_stream_join.py"
        assert _flagged(engine_bare, stream, "StreamFkJoiner.total_orphans", "_ensure_conn")
        assert any("self._conn.unregister" in f for f in engine_bare[stream])
    elif cell in _DERIVED_CELLS:
        module, function, what = _DERIVED_CELLS[cell]
        assert _flagged(fixture_bare, module, function, what), fixture_bare.get(module)
        if cell == "method_returns_connection":
            assert _flagged(fixture_bare, _DERIVED, "_returned_helper", ".fetchall")
            assert _flagged(fixture_bare, _DERIVED, "MethodReturnsConnection.query", "bare through")
    elif cell == "pending_bare_ok":
        findings = native_findings(fixture_bare, frozenset(fixture_bare), fixture_modules)
        assert findings == [], findings
    elif cell == "pending_entry_stale":
        pending = frozenset(fixture_bare) | {_EVASIONS}
        findings = native_findings(fixture_bare, pending, fixture_modules)
        assert findings == [f"pending: {_EVASIONS} has no bare call or read (stale)"], findings
        assert _flagged(fixture_bare, _DERIVED, "by_getattr_and_bound_method", "getattr")
        assert _flagged(fixture_bare, _DERIVED, "by_getattr_and_bound_method", ".fetchall")
    elif cell == "pending_set_exact":
        pending = frozenset(fixture_bare) - {_DERIVED}
        findings = native_findings(fixture_bare, pending, fixture_modules)
        assert findings and all(f.startswith(f"{_DERIVED}::") for f in findings), findings
    else:
        assert {
            "decoy_engine/fileaccess/_scratch.py",
            "decoy_engine/fileaccess/__init__.py",
        } == NATIVE_OWNERS
        assert {
            "decoy_engine/fileaccess/adapters/duckdb.py",
            "decoy_engine/connectors/postgres_snapshot.py",
            "decoy_engine/data_discovery.py",
        } == NATIVE_OWN_MAPPING


def _pending_literal(text: str) -> frozenset[str] | None:
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "SCRATCH_NATIVE_PENDING" for t in node.targets
        ):
            assert isinstance(node.value, ast.Call)
            return frozenset(ast.literal_eval(node.value.args[0]))
    return None


def test_pending_set_only_shrinks() -> None:
    """Against the nearest ancestor literal that differs from this one: no name added or swapped
    (exact keys, `_ratchet.growth`). The first literal has no ancestor and is allowed."""
    import subprocess

    from tests.sentry._ratchet import _git_show, growth

    rules = "tests/sentry/fa/rules.py"
    repo = HERE.parents[1]
    current = _pending_literal((repo / rules).read_text(encoding="utf-8"))
    assert current is not None
    log = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(repo), "log", "--format=%H", "HEAD", "--", rules],  # noqa: S607
        capture_output=True,
        text=True,
    )
    for sha in log.stdout.split():
        text = _git_show(repo, sha, rules)
        ancestor = _pending_literal(text) if text is not None else None
        if ancestor is None:
            break
        if ancestor != current:
            grown, _ = growth(current, ancestor, per_site=False)
            assert not grown, f"SCRATCH_NATIVE_PENDING gained {sorted(grown)} over {sha[:12]}"
            break


_LAZY_CELLS = {
    "to_arrow_reader_bare": ("to_arrow_reader_bare", ".to_arrow_reader"),
    "reader_for_loop": ("reader_for_loop", "pull"),
    "yield_from_reader": ("yield_from_reader", "pull"),
    "next_on_reader": ("next_on_reader", "pull"),
    "read_next_batch": ("read_next_batch", ".read_next_batch"),
    "bare_connect_duckdb": ("bare_connect_duckdb", "bare through connect_duckdb"),
}


@pytest.mark.parametrize("cell", [*_LAZY_CELLS, "iter_wrapped_passes"])
def test_lazy_native_reads_are_wrapped(cell: str, fixture_bare) -> None:  # type: ignore[no-untyped-def]
    """`scratch_native` for each call and `scratch_native_iter` for each pull (plan 4.1)."""
    if cell == "iter_wrapped_passes":
        assert not _flagged(fixture_bare, _DERIVED, "iter_wrapped_passes")
        return
    assert _flagged(fixture_bare, _DERIVED, *_LAZY_CELLS[cell]), fixture_bare.get(_DERIVED)


_READ_CELLS = {
    "read_metadata_bare": "read_metadata_bare",
    "read_table_bare": "read_table_bare",
    "ipc_open_stream_bare": "ipc_open_stream_bare",
    "osfile_bare": "osfile_bare",
    "get_batch_bare": "get_batch_bare",
}
_READ_PASSES = ["wrapped_passes", "reader_access_passes", "read_parquet_in_sql_passes"]


@pytest.mark.parametrize(
    "cell",
    [
        *_READ_CELLS,
        *_READ_PASSES,
        "pending_entry_stale",
        "isolated_run_in_scope",
        "out_of_scope_passes",
    ],
)
def test_scratch_file_reads_are_wrapped(cell: str, fixture_bare) -> None:  # type: ignore[no-untyped-def]
    """A pyarrow read of a scratch file by path is a native call too (plan 4.1)."""
    bare = fixture_bare
    if cell in _READ_CELLS:
        assert _flagged(bare, _READS, _READ_CELLS[cell]), bare.get(_READS)
        if cell == "get_batch_bare":
            assert _flagged(bare, _READS, "get_batch_bare", ".get_batch")
    elif cell in _READ_PASSES:
        assert not _flagged(bare, _DERIVED, cell), bare.get(_DERIVED)
    elif cell == "pending_entry_stale":
        pending = frozenset(bare) | {"fx/plain/helpers_pq_read.py"}
        findings = native_findings(bare, pending, set(source_modules(FIXTURES, "fx").values()))
        assert findings == ["pending: fx/plain/helpers_pq_read.py has no bare call or read (stale)"]
    elif cell == "isolated_run_in_scope":
        stand_in = "fx/plain/helpers_pq_read.py"
        scope = frozenset(stand_in if e == _ISOLATED_RUN else e for e in SCRATCH_READ_SCOPE)
        assert _flagged(bare_native(FIXTURES, "fx", scope=scope), stand_in, "read_it")
    else:
        assert "fx/plain/helpers_pq_read.py" not in fixture_bare


def test_a_listed_creator_that_does_not_validate_its_name_is_still_flagged(fixture_scan) -> None:  # type: ignore[no-untyped-def]
    flagged = [
        v
        for v in classify(fixture_scan, FIXTURE_ROLES, {})[0]
        if v.site.module == "fx/cap/bad_creator_unvalidated.py"
    ]
    assert flagged and "component validator" in flagged[0].reason
