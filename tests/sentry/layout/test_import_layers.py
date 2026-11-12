"""Sentry: the engine's import layers hold, and the layer list is the reviewed one.

``.importlinter`` declares one import-linter ``layers`` contract over the top-level children
of ``decoy_engine``. It is exhaustive, so a new top-level package or module is red until it is
placed. ``LAYERS`` below is the reviewed snapshot of the tiers: config and snapshot must agree
exactly, so placing a package or moving one down a tier edits both in the same diff. The
ignore lines are measured back imports that exist today; they may only shrink (X0a baseline
``import-layers``, keyed ``layers::<importer> -> <imported>``).

This file runs only in the isolated ``layers`` gate step (``scripts/dev/gates.sh --steps
layers``), which syncs ``.venv-layers`` with the ``layers`` extra; the certified ``.venv`` has
no import-linter, and every other route ignores this path. import-linter is imported inside
the test functions, so collecting this file anywhere works, and running it without the
dependency fails with install advice instead of skipping.
"""

from __future__ import annotations

import ast
import configparser
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.sentry.layout import ignore_keys, map_paths

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / ".importlinter"
CONTRACT = "layers"
INSTALL = (
    "import-linter is not installed in this venv. Run `scripts/dev/gates.sh --steps layers`, "
    "which syncs .venv-layers with the `layers` extra; never add it to the certified .venv."
)

# Reviewed snapshot of the tiers in .importlinter, top to bottom; `:` siblings may import each
# other. Edit it in the same diff as the config.
LAYERS: tuple[str, ...] = (
    "unmask : subset : walks : identifiers : providers : records",
    "execution : kernel : quarantine : publication : validation : verification : vault",
    "plan : generation : generators : transforms : declared_casting : secret_providers"
    " : keyprovider : relationships : providers_v2 : reference_tables : quality",
    "profile : profiling : detection : internal : progress : context : config : determinism"
    " : contracts : connectors : expressions : validators : sdk : run_result",
    "io : data_discovery : disguises : phase : telemetry : safe_output : fileaccess",
    "errors : reason_adapter : validation_result : release : reasons : instrumentation"
    " : checksums : _opaque : _lexeme : _error_base : _progress_events : _reference_date"
    " : codesets : _execution_error : _field_names : _value_free",
)


def config_layers(text: str) -> tuple[str, ...]:
    """The tier lines of the contract, whitespace-normalised."""
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read_string(text)
    raw = parser.get(f"importlinter:contract:{CONTRACT}", "layers")
    return tuple(" ".join(line.split()) for line in raw.splitlines() if line.strip())


def _lint_api() -> Callable[..., bool]:
    try:
        from importlinter.application.use_cases import lint_imports
        from importlinter.configuration import configure
    except ImportError:
        pytest.fail(INSTALL)
    configure()
    return lint_imports


def test_the_layer_contract_is_kept() -> None:
    lint_imports = _lint_api()
    kept = lint_imports(config_filename=str(CONFIG), cache_dir=None, no_logo=True)
    assert kept, "the import-layer contract is broken; the import-linter report is above"


def test_config_layers_equal_the_reviewed_snapshot() -> None:
    assert config_layers(CONFIG.read_text(encoding="utf-8")) == LAYERS


def facade_ignores(text: str, src: Path) -> list[str]:
    """Ignore lines whose importer or imported side is a package rather than a leaf module.

    import-linter exempts a whole module edge, not the symbols that first needed it, so an
    ignore on a package facade lets any later import through that facade.
    """
    return sorted(
        key
        for key in ignore_keys(text)
        if any((src / Path(*side.split("."))).is_dir() for side in key.split(" -> "))
    )


def test_every_ignore_names_leaf_modules() -> None:
    assert facade_ignores(CONFIG.read_text(encoding="utf-8"), ROOT / "src") == []


# --- meta-tests: the guard fails closed and sees seeded violations -------------------------


def test_a_missing_import_linter_fails_with_install_advice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # None in sys.modules makes an import raise ImportError; cached submodules need it too.
    for name in ["importlinter", *(m for m in sys.modules if m.startswith("importlinter."))]:
        monkeypatch.setitem(sys.modules, name, None)
    with pytest.raises(pytest.fail.Exception, match="gates.sh --steps layers"):
        _lint_api()


def test_import_linter_is_imported_only_inside_functions() -> None:
    """Collection must work in a venv without the ``layers`` extra."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = [a.name for n in top for a in n.names] + [
        n.module or "" for n in top if isinstance(n, ast.ImportFrom)
    ]
    assert not [n for n in names if n.startswith("importlinter")]


_PROBE = """
import importlib.util, json, sys
from importlinter.configuration import configure
configure()
from importlinter.application.use_cases import lint_imports
origin = importlib.util.find_spec("decoy_engine").origin
kept = lint_imports(config_filename=sys.argv[1], cache_dir=None, no_logo=True)
print("PROBE " + json.dumps({"origin": origin, "kept": kept}))
"""


@pytest.fixture(scope="module")
def scratch(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A pristine copy of the package source and config; each case copies it again."""
    base = tmp_path_factory.mktemp("layers-base")
    shutil.copytree(
        ROOT / "src" / "decoy_engine",
        base / "src" / "decoy_engine",
        ignore=shutil.ignore_patterns("__pycache__", "*.so", "*.pyd"),
    )
    shutil.copy(CONFIG, base / ".importlinter")
    return base


def _tree(scratch: Path, tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    shutil.copytree(scratch, tree)
    return tree


def lint_scratch(tree: Path) -> bool:
    """Lint ``tree`` in a fresh interpreter that can only see the scratch package."""
    _lint_api()  # fail with install advice before spawning anything
    env = {**os.environ, "PYTHONPATH": str(tree / "src")}
    res = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", _PROBE, str(tree / ".importlinter")],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("PROBE ")]
    assert lines, f"probe printed no result (rc {res.returncode}): {res.stderr[-2000:]}"
    result = json.loads(lines[-1].removeprefix("PROBE "))
    assert Path(result["origin"]).resolve().is_relative_to(tree.resolve()), result["origin"]
    return bool(result["kept"])


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} is not unique in {path.name}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def _snapshot_with(text: str, layers: tuple[str, ...]) -> bool:
    return config_layers(text) == layers


def test_the_pristine_scratch_copy_is_kept(scratch: Path, tmp_path: Path) -> None:
    assert lint_scratch(_tree(scratch, tmp_path))


def test_removing_one_ignore_line_is_red(scratch: Path, tmp_path: Path) -> None:
    tree = _tree(scratch, tmp_path)
    _edit(
        tree / ".importlinter",
        "    decoy_engine.errors -> decoy_engine.execution._row_error_summary\n",
        "",
    )
    assert not lint_scratch(tree)


def test_a_tier_six_module_importing_execution_is_red(scratch: Path, tmp_path: Path) -> None:
    tree = _tree(scratch, tmp_path)
    leaf = tree / "src" / "decoy_engine" / "_opaque.py"
    leaf.write_text(
        leaf.read_text(encoding="utf-8") + "\nfrom decoy_engine.execution import _errors\n",
        encoding="utf-8",
    )
    assert not lint_scratch(tree)


def test_the_facade_check_sees_a_package_ignore() -> None:
    text = "[c]\nignore_imports =\n    decoy_engine.generation._bounds -> decoy_engine.execution\n"
    assert facade_ignores(text, ROOT / "src") == [
        "decoy_engine.generation._bounds -> decoy_engine.execution"
    ]


@pytest.mark.parametrize(
    ("module", "line"),
    [
        # Tier 3 reaching tier 2 through the execution facade.
        ("generation/_bounds.py", "from decoy_engine.execution import PandasExecutionAdapter"),
        # Tier 2 reaching tier 1 through the root facade.
        ("execution/_job_estimate.py", "from decoy_engine import DecodedRecord"),
    ],
)
def test_an_upward_import_through_a_facade_is_red(
    scratch: Path, tmp_path: Path, module: str, line: str
) -> None:
    """Both importers keep exempt leaf edges, and both edges once had a facade exemption."""
    tree = _tree(scratch, tmp_path)
    path = tree / "src" / "decoy_engine" / module
    path.write_text(path.read_text(encoding="utf-8") + f"\n{line}\n", encoding="utf-8")
    assert not lint_scratch(tree)


def test_an_unplaced_top_level_package_is_red(scratch: Path, tmp_path: Path) -> None:
    tree = _tree(scratch, tmp_path)
    (tree / "src" / "decoy_engine" / "newpkg").mkdir()
    (tree / "src" / "decoy_engine" / "newpkg" / "__init__.py").write_text("", encoding="utf-8")
    assert not lint_scratch(tree)


def test_an_ignore_line_matching_no_import_is_red(scratch: Path, tmp_path: Path) -> None:
    tree = _tree(scratch, tmp_path)
    _edit(
        tree / ".importlinter",
        "ignore_imports =\n",
        "ignore_imports =\n    decoy_engine.errors -> decoy_engine.unmask\n",
    )
    assert not lint_scratch(tree)


def test_a_reviewed_new_package_goes_green_only_with_its_snapshot(
    scratch: Path, tmp_path: Path
) -> None:
    """AT-5(e): a placed package plus its CODEMAP row is green and needs no new X0a key."""
    tree = _tree(scratch, tmp_path)
    pkg = tree / "src" / "decoy_engine" / "_probe_leaf"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('"""A reviewed foundation leaf."""\n', encoding="utf-8")
    cfg = tree / ".importlinter"
    before = ignore_keys(cfg.read_text(encoding="utf-8"))
    _edit(cfg, " : _value_free\n", " : _value_free : _probe_leaf\n")
    assert lint_scratch(tree)
    text = cfg.read_text(encoding="utf-8")
    assert ignore_keys(text) == before
    assert not _snapshot_with(text, LAYERS), "a config edit without its snapshot must be red"
    reviewed = (*LAYERS[:-1], LAYERS[-1] + " : _probe_leaf")
    assert _snapshot_with(text, reviewed)
    row = (
        "| Probe leaf | `src/decoy_engine/_probe_leaf/` | `src/decoy_engine/_probe_leaf/__init__.py`"
        " | owning surface tests | bottom layer in `./.importlinter` |\n"
    )
    assert map_paths(row, tree, (1, 2, 3, 4)) == []
    assert map_paths(row.replace("_probe_leaf", "_unbuilt_leaf"), tree, (1, 2, 3, 4))


def test_a_downward_tier_move_goes_green_only_with_its_snapshot(
    scratch: Path, tmp_path: Path
) -> None:
    """AT-5(e): ``vault`` moves from tier 2 to tier 3; no new X0a key."""
    tree = _tree(scratch, tmp_path)
    cfg = tree / ".importlinter"
    before = ignore_keys(cfg.read_text(encoding="utf-8"))
    _edit(cfg, " : verification : vault\n", " : verification\n")
    _edit(cfg, " : reference_tables : quality\n", " : reference_tables : quality : vault\n")
    assert lint_scratch(tree)
    text = cfg.read_text(encoding="utf-8")
    assert ignore_keys(text) == before
    assert not _snapshot_with(text, LAYERS), "a config edit without its snapshot must be red"
    moved = list(LAYERS)
    moved[1] = moved[1].removesuffix(" : vault")
    moved[2] += " : vault"
    assert _snapshot_with(text, tuple(moved))
