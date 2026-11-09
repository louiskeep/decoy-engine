"""G-ONE-PROTECTED-DIR: every directory creator is the allocator or a categorized registry row.

Built on the D9 file-access registry and scan (no second walker). The creating sinks come from
`tests/sentry/fa/sinks.py`; a creator in a capability module is fd-relative and judged by D9 rule 2.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.sentry.fa import sinks
from tests.sentry.fa.rules import load_modules_config, load_registry
from tests.sentry.fa.scan import Site, scan_tree

HERE = Path(__file__).parent
SENTRY = HERE.parent
ROOT = HERE.parents[2]
SRC = ROOT / "src"
REGISTRY = SENTRY / "file_access_registry.toml"
FD_RELATIVE = frozenset(
    load_modules_config(SENTRY / "file_access_modules.toml")["capability_modules"]
)
ALLOCATOR = "decoy_engine/fileaccess/_protected_dir.py"
CATEGORIES = frozenset(
    {"protected_root", "protected_child", "output_parent", "published_output", "non_row"}
)
# Protected-root rows allowed outside the allocator. Every engine row directory goes through it,
# so this is empty and only the allocator may own a protected_root row.
PENDING_L0_2: tuple[str, ...] = ()
_CREATOR_NAMES = ("mkdir", "makedirs", "mkdtemp", "TemporaryDirectory")


def _creating_sinks() -> frozenset[str]:
    found = {s for s in sinks.EXACT if s.rsplit(".", 1)[-1] in _CREATOR_NAMES}
    found |= {f"Path.{m}" for m in sinks.PATH_METHODS if m in _CREATOR_NAMES}
    return frozenset(found)


CREATING = _creating_sinks()


def _key(e: dict[str, object]) -> str:
    return f"{e['module']}::{e['function']}::{e['sink']}::{e['ordinal']}"


def _node_resolves(node_id: str) -> bool:
    path, _, name = node_id.partition("::")
    file = ROOT / path
    if not file.is_file():
        return False
    tree = ast.parse(file.read_text(encoding="utf-8"))
    parts = name.split("::")
    scope: list[ast.stmt] = tree.body
    for part in parts[:-1]:
        cls = [n for n in scope if isinstance(n, ast.ClassDef) and n.name == part]
        if not cls:
            return False
        scope = cls[0].body
    return any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == parts[-1]
        for n in scope
    )


def category_findings(
    found: list[Site], entries: list[dict[str, object]], fd_relative: frozenset[str]
) -> list[str]:
    """Why the creators in `found` are not all categorized; empty when they are."""
    by_key = {_key(e): e for e in entries}
    out: list[str] = []
    for site in found:
        if site.sink not in CREATING or site.module in fd_relative:
            continue
        key = "::".join(map(str, site.key))
        entry = by_key.get(key)
        if entry is None:
            out.append(f"{key}: no registry row")
            continue
        category = entry.get("dir_category")
        if category not in CATEGORIES:
            out.append(f"{key}: dir_category {category!r} is missing or unknown")
        elif category != "protected_root" and not _node_resolves(str(entry.get("dir_test", ""))):
            out.append(f"{key}: dir_test is missing or names no test")
    return out


def owner_findings(entries: list[dict[str, object]], pending: tuple[str, ...]) -> list[str]:
    return [
        f"{_key(e)}: protected_root outside the allocator"
        for e in entries
        if e.get("dir_category") == "protected_root"
        and e["module"] != ALLOCATOR
        and _key(e) not in pending
    ]


@pytest.fixture(scope="module")
def engine_sites() -> list[Site]:
    return scan_tree(SRC)[0]


def test_every_directory_creator_has_a_registry_category(engine_sites: list[Site]) -> None:
    assert category_findings(engine_sites, load_registry(REGISTRY), FD_RELATIVE) == []


def test_only_the_allocator_owns_protected_roots() -> None:
    assert owner_findings(load_registry(REGISTRY), PENDING_L0_2) == []


def test_the_walk_is_not_vacuous(engine_sites: list[Site]) -> None:
    for name in _CREATOR_NAMES:  # a rename in sinks.py must not empty the sink set
        assert any(s.endswith(name) for s in CREATING), name
    entries = load_registry(REGISTRY)
    creators = [s for s in engine_sites if s.sink in CREATING and s.module not in FD_RELATIVE]
    assert len(creators) >= 15
    assert CATEGORIES - {"non_row"} <= {e.get("dir_category") for e in entries}
    assert any(
        e["module"] == ALLOCATOR and e.get("dir_category") == "protected_root" for e in entries
    )


def test_no_protected_root_row_is_pending() -> None:
    assert PENDING_L0_2 == ()
    roots = {_key(e) for e in load_registry(REGISTRY) if e.get("dir_category") == "protected_root"}
    assert roots and {k.partition("::")[0] for k in roots} == {ALLOCATOR}


def test_the_other_rows_have_their_expected_categories() -> None:
    expect = {
        "decoy_engine/execution/_isolated_run.py::_write_payload::Path.mkdir::0": "protected_child",
        "decoy_engine/execution/_isolated_worker.py::_stage_outputs::os.makedirs::0": "protected_child",
        "decoy_engine/execution/_isolated_commit.py::stage_tables::Path.mkdir::0": "output_parent",
        "decoy_engine/execution/_isolated_commit.py::atomic_commit::Path.mkdir::0": "output_parent",
        "decoy_engine/connectors/postgres_snapshot.py::snapshot_tables_to_parquet::Path.mkdir::0": "output_parent",
        "decoy_engine/connectors/postgres_snapshot.py::snapshot_tables_to_parquet::Path.mkdir::1": "published_output",
        "decoy_engine/fileaccess/_local.py::_LocalStageDir.create_dir::Path.mkdir::0": "published_output",
        "decoy_engine/fileaccess/_local.py::LocalOutputTxn.__init__::tempfile.mkdtemp::0": "published_output",
        "decoy_engine/fileaccess/_local.py::LocalOutputDir.ensure_dir::Path.mkdir::1": "published_output",
        "decoy_engine/fileaccess/_local.py::LocalOutputTxn.__init__::Path.mkdir::0": "output_parent",
        "decoy_engine/fileaccess/_local.py::LocalOutputTxn.create_file::Path.mkdir::0": "output_parent",
        "decoy_engine/fileaccess/_local.py::LocalOutputTxn.publish::Path.mkdir::0": "output_parent",
        "decoy_engine/fileaccess/_local.py::LocalOutputDir.ensure_dir::Path.mkdir::0": "output_parent",
        "decoy_engine/fileaccess/_local.py::LocalFileAccess.log_handler::Path.mkdir::0": "output_parent",
        f"{ALLOCATOR}::_mkdir::os.mkdir::0": "protected_root",
    }
    live = {_key(e): e.get("dir_category") for e in load_registry(REGISTRY)}
    assert {k: live.get(k) for k in expect} == expect


# -- seeded bypasses: each must be seen ----------------------------------------------------------


def _seed(tmp_path: Path, body: str) -> list[Site]:
    pkg = tmp_path / "decoy_engine"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "worker.py").write_text(body)
    return scan_tree(tmp_path)[0]


@pytest.mark.parametrize(
    "body",
    [
        "import os\n\ndef stage(p):\n    os.makedirs(p)\n",
        "from pathlib import Path\n\ndef stage():\n    Path('x').mkdir(mode=0o700)\n",
        "import tempfile\n\ndef stage():\n    return tempfile.TemporaryDirectory()\n",
    ],
)
def test_a_seeded_inline_creator_is_flagged(tmp_path: Path, body: str) -> None:
    found = _seed(tmp_path, body)
    assert category_findings(found, [], frozenset()), "the walk missed a creator"


def test_a_row_without_a_category_is_flagged(tmp_path: Path) -> None:
    found = _seed(tmp_path, "import os\n\ndef stage(p):\n    os.makedirs(p)\n")
    site = next(s for s in found if s.sink == "os.makedirs")
    row = {"module": site.module, "function": site.function, "sink": site.sink, "ordinal": 0}
    assert category_findings(found, [row], frozenset())
    assert category_findings(found, [{**row, "dir_category": "protected_root"}], frozenset()) == []
    bad = {**row, "dir_category": "output_parent", "dir_test": "tests/nope.py::test_x"}
    assert category_findings(found, [bad], frozenset())


def test_a_protected_root_outside_the_allocator_is_flagged() -> None:
    row = {"module": "decoy_engine/x.py", "function": "f", "sink": "os.mkdir", "ordinal": 0}
    row["dir_category"] = "protected_root"
    assert owner_findings([row], ())
    assert owner_findings([row], ("decoy_engine/x.py::f::os.mkdir::0",)) == []
