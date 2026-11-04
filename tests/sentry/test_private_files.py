"""Sentry: private files go through `fileaccess.write_private`; no late or swallowed chmod.

A file that holds credentials, key material, or an engine-internal copy of real data must be
0600 from the moment it exists. Writing it at the umask and narrowing it afterwards leaves a
window in which others can read it; a `chmod` whose failure an `except: pass` hides leaves it
open with no signal. The fix is the create-0600, fsync, replace recipe behind
`fileaccess.write_private` (plan "Private files").

Every registry row (`file_access_registry.toml`) whose sink can create or write a file carries
a `disposition`:
- `private`: the file holds credentials, key material, or an engine-internal copy of real data
  (payloads, staged sources, spill and scratch files, vault and key files);
- `public`: a declared job output at a caller-chosen target, a log, or a shipped artifact;
- `read`: the call never creates or writes a file (the `open`, `os.open` and `pyarrow.OSFile`
  rows mix reads and writes under one sink name). A third value is plan drift, kept so a
  mislabeled writer is still caught: a `read` row whose call passes a write mode, or `os.open`
  flags that are not plain `os.O_*` read flags, is a finding.

Rules (findings carry path, line, function qualname and rule id only):
- P1: a creating-sink row without a valid disposition, or a `read` row whose call writes.
- P2: a `private` row is a finding unless its registry key is in the shrink-only baseline
  `baselines/private-file-sinks.json`; a stale baseline key fails.
- P3: a `chmod`, `fchmod` or `Path.chmod` inside a `try` body whose `OSError`,
  `PermissionError`, `Exception`, `BaseException` or bare handler is only `pass`, `...` or
  `continue`. No baseline.
- P4: a `chmod` (not `fchmod`) whose mode is a file mode (no execute bit) or not a constant,
  outside `decoy_engine/fileaccess/`. Directory modes such as `0o700` pass.

Named limits: SQL passed to `duckdb.connection.execute` is not read, so those rows'
dispositions are the hand review's decision. P3 and P4 see the calls the file-access scan
records (`os.chmod`, `os.fchmod`, and `.chmod()` on a value the scan knows is a `Path`);
`os.lchmod` is not in the scan's sink set and does not exist on Linux.

The scan, resolver and registry loader are the file-access sentry's (`tests/sentry/fa/`).
"""

from __future__ import annotations

import ast
import json
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pytest

from tests.sentry.fa.rules import load_registry
from tests.sentry.fa.scan import Site, scan_tree

HERE = Path(__file__).parent
SRC = HERE.parents[1] / "src"
REGISTRY = HERE / "file_access_registry.toml"
SINKS_JSON = HERE / "baselines" / "private-file-sinks.json"
SEEDED = HERE / "fixtures" / "private_files"
EXEMPT_PREFIX = "decoy_engine/fileaccess/"

CREATING_SINKS = frozenset(
    {
        "open",
        "builtins.open",
        "io.open",
        "os.open",
        "Path.open",
        "Path.write_text",
        "Path.write_bytes",
        "tempfile.mkstemp",
        "tempfile.NamedTemporaryFile",
        "pyarrow.parquet.write_table",
        "pyarrow.parquet.ParquetWriter",
        "pyarrow.OSFile",
        "pyarrow.ipc.new_stream",
        "pyarrow.ipc.new_file",
        "joblib.dump",
        "logging.handlers.RotatingFileHandler",
        "duckdb.connection.execute",
    }
)
DISPOSITIONS = frozenset({"public", "private", "read"})
READABLE_SINKS = frozenset(
    {"open", "builtins.open", "io.open", "os.open", "Path.open", "pyarrow.OSFile"}
)
MODE_SINKS = frozenset({"os.chmod", "os.lchmod", "os.fchmod", "Path.chmod", "Path.lchmod"})
SWALLOWED = frozenset({"OSError", "PermissionError", "Exception", "BaseException"})
WRITE_FLAGS = frozenset({"O_CREAT", "O_WRONLY", "O_RDWR", "O_TRUNC", "O_APPEND"})


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    function: str
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} {self.function} {self.rule}"


Key = tuple[str, str, str, int]


def registry_key(key: Key) -> str:
    return "entry::" + "::".join(str(p) for p in key)


# -- P1 and P2: registry dispositions ---------------------------------------------------------


def _mode_arg(node: ast.Call, index: int, name: str = "mode") -> ast.AST | None:
    for kw in node.keywords:
        if kw.arg == name:
            return kw.value
    return node.args[index] if len(node.args) > index else None


def _writes(site: Site) -> bool:
    """True when a readable sink's call passes a write mode or write flags (fail closed)."""
    node = site.node
    if not node.args and not node.keywords:
        return False  # the sink named as a value; vetted where it is called
    if site.sink == "os.open":
        flags = _mode_arg(node, 1, "flags")
        if flags is None:
            return True
        for sub in ast.walk(flags):
            if isinstance(sub, ast.Attribute) and sub.attr in WRITE_FLAGS:
                return True
            if isinstance(sub, ast.Name) and sub.id != "os":
                return True  # a named flag set the scan cannot read
        return False
    index = 0 if site.sink == "Path.open" else 1
    mode = _mode_arg(node, index)
    if mode is None:
        return False
    if not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
        return True
    return any(c in mode.value for c in "wax+")


def disposition_findings(
    entries: list[dict[str, object]], sites: dict[Key, Site], kept: frozenset[str]
) -> list[Finding]:
    out: list[Finding] = []
    for entry in entries:
        sink = str(entry["sink"])
        if sink not in CREATING_SINKS:
            continue
        key: Key = (str(entry["module"]), str(entry["function"]), sink, int(entry["ordinal"]))  # type: ignore[call-overload]
        site = sites.get(key)
        line = site.line if site is not None else 0
        disposition = entry.get("disposition")
        if disposition not in DISPOSITIONS or (
            disposition == "read"
            and (
                (sink not in READABLE_SINKS and sink != "duckdb.connection.execute")
                or (site is not None and sink in READABLE_SINKS and _writes(site))
            )
        ):
            out.append(Finding(key[0], line, key[1], "P1"))
        elif disposition == "private" and registry_key(key) not in kept:
            out.append(Finding(key[0], line, key[1], "P2"))
    return out


# -- P3 and P4: mode changes ------------------------------------------------------------------


def _swallows(handler: ast.ExceptHandler) -> bool:
    quiet = all(
        isinstance(s, (ast.Pass, ast.Continue))
        or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and s.value.value is ...)
        for s in handler.body
    )
    if not quiet:
        return False
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    names = {getattr(t, "id", None) or getattr(t, "attr", None) for t in types}
    return bool(names & SWALLOWED)


def _in_swallowing_try(site: Site) -> bool:
    """The call sits in the `try` body (not a handler) of a try with a swallowing handler."""
    scope = site.scope_info.node if site.scope_info is not None else None
    if scope is None:
        return False
    for node in ast.walk(scope):
        if isinstance(node, ast.Try) and any(_swallows(h) for h in node.handlers):
            if any(site.node is sub for stmt in node.body for sub in ast.walk(stmt)):
                return True
    return False


def _is_file_mode(node: ast.AST | None) -> bool:
    if node is None:
        return True
    try:
        value = ast.literal_eval(node)
    except ValueError:
        return True
    return not isinstance(value, int) or value & 0o111 == 0


def mode_findings(sites: list[Site], exempt_prefix: str) -> list[Finding]:
    out: list[Finding] = []
    for site in sites:
        if site.sink not in MODE_SINKS:
            continue
        if _in_swallowing_try(site):
            out.append(Finding(site.module, site.line, site.function, "P3"))
        if site.sink == "os.fchmod" or site.module.startswith(exempt_prefix):
            continue
        index = 0 if site.sink.startswith("Path.") else 1
        if _is_file_mode(_mode_arg(site.node, index)):
            out.append(Finding(site.module, site.line, site.function, "P4"))
    return out


# -- the engine -------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _engine_sites() -> tuple[Site, ...]:
    return tuple(scan_tree(SRC)[0])


def _kept() -> frozenset[str]:
    if not SINKS_JSON.is_file():
        return frozenset()  # test_private_baseline_has_no_stale_keys fails on the absence
    doc = json.loads(SINKS_JSON.read_text(encoding="utf-8"))
    assert doc["sentry_id"] == "private-file-sinks"
    return frozenset(doc["entries"])


def _engine_dispositions() -> list[Finding]:
    sites = {s.key: s for s in _engine_sites()}
    return disposition_findings(load_registry(REGISTRY), sites, _kept())


def test_creating_rows_have_an_honest_disposition() -> None:
    found = [str(f) for f in _engine_dispositions() if f.rule == "P1"]
    assert not found, "add disposition = public | private | read:\n" + "\n".join(found)


def test_private_rows_go_through_write_private() -> None:
    found = [str(f) for f in _engine_dispositions() if f.rule == "P2"]
    assert not found, "write private files with fileaccess.write_private:\n" + "\n".join(found)


def test_private_baseline_has_no_stale_keys() -> None:
    assert SINKS_JSON.is_file(), "the private-file-sinks baseline is missing"
    private = {
        registry_key((str(e["module"]), str(e["function"]), str(e["sink"]), int(e["ordinal"])))  # type: ignore[call-overload]
        for e in load_registry(REGISTRY)
        if e.get("disposition") == "private"
    }
    stale = sorted(_kept() - private)
    assert not stale, "baseline keys that are no longer private rows (remove them):\n" + (
        "\n".join(stale)
    )


def test_no_swallowed_mode_change() -> None:
    found = [str(f) for f in mode_findings(list(_engine_sites()), EXEMPT_PREFIX) if f.rule == "P3"]
    assert not found, "a chmod failure must not be swallowed:\n" + "\n".join(found)


def test_no_late_file_mode() -> None:
    found = [str(f) for f in mode_findings(list(_engine_sites()), EXEMPT_PREFIX) if f.rule == "P4"]
    assert not found, "create the file 0600 with write_private, not chmod:\n" + "\n".join(found)


def test_walk_is_not_vacuous() -> None:
    creating = [e for e in load_registry(REGISTRY) if e["sink"] in CREATING_SINKS]
    assert len(creating) >= 30
    seen: dict[tuple[str, str], int] = {}
    for site in _engine_sites():
        if site.sink in MODE_SINKS:
            seen[(site.module, site.sink)] = seen.get((site.module, site.sink), 0) + 1
    known = {
        ("decoy_engine/execution/out_of_core/_scratch.py", "os.chmod"): 3,
        ("decoy_engine/execution/out_of_core/_scratch.py", "os.fchmod"): 1,
        ("decoy_engine/execution/out_of_core/_duckdb.py", "os.chmod"): 1,
        ("decoy_engine/connectors/postgres_snapshot.py", "os.chmod"): 1,
        ("decoy_engine/declared_casting/_quarantine_local.py", "os.chmod"): 1,
        ("decoy_engine/declared_casting/_quarantine_local.py", "os.fchmod"): 1,
    }
    short = {k: (n, seen.get(k, 0)) for k, n in known.items() if seen.get(k, 0) < n}
    assert not short, f"the scan missed known mode-change sites: {short}"


# -- seeded corpus ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> dict[str, set[str]]:
    root = tmp_path_factory.mktemp("private_files")
    package = root / "pf"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for module in SEEDED.glob("*.py.txt"):
        shutil.copyfile(module, package / module.name.removesuffix(".txt"))
    sites = scan_tree(root, package="pf")[0]
    entries = load_registry(SEEDED / "seeded_registry.toml.txt")
    findings = disposition_findings(entries, {s.key: s for s in sites}, frozenset())
    findings += mode_findings(sites, "pf/fileaccess/")
    out: dict[str, set[str]] = {}
    for f in findings:
        out.setdefault(f"{f.path}::{f.function}", set()).add(f.rule)
    return out


@pytest.mark.parametrize(
    ("case", "rules"),
    [
        ("pf/seeded_p1_p2.py::master_key", {"P1"}),
        ("pf/seeded_p1_p2.py::key_dir", {"P2"}),
        ("pf/seeded_read_row.py::save", {"P1"}),
        ("pf/seeded_private_row.py::save", {"P2"}),
        ("pf/seeded_modes.py::swallowed_dir_chmod", {"P3"}),
        ("pf/seeded_modes.py::swallowed_fchmod", {"P3"}),
        ("pf/seeded_modes.py::from_import_alias", {"P4"}),
        ("pf/seeded_modes.py::module_alias_variable_mode", {"P4"}),
    ],
)
def test_seeded_case_is_caught_by_its_rule(
    seeded: dict[str, set[str]], case: str, rules: set[str]
) -> None:
    assert seeded.get(case) == rules


def test_seeded_controls_pass(seeded: dict[str, set[str]]) -> None:
    assert "pf/seeded_modes.py::directory_chmod_and_fchmod" not in seeded
    assert "pf/seeded_p1_p2.py::public_report" not in seeded
