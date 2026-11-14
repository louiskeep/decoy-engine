"""One canonical JSON owner (D3): no second sorted-key dump that is hashed, signed or seeded.

Every digest, HMAC and seed fingerprint is built by `decoy_engine._canonical`. A `json.dumps`
(or `dump`, or a `JSONEncoder`) with `sort_keys` outside it is either a stored or executed
document, which is on the exact allowlist below with its kind and reason, or a second digest
rule, which fails here. An allowlist entry that no longer matches a real site also fails, so the
list cannot outlive the code it excuses.

The seed form is frozen under `SEED_PROTOCOL_VERSION`: exactly two functions may use it, and the
goldens in `tests/unit/canonical/test_seed_form_pins.py` pin both byte streams.

The scan resolves import aliases (`import json as j`, `from json import dumps as d`) through the
shared call-resolution helper. Deliberate evasion (`getattr(json, "dumps")`, `exec`) is not
modelled.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.sentry._call_resolution import local_names
from tests.sentry.defects import _defect_scan as scan

OWNER = "_canonical/_json.py"
_DUMPERS = ("dumps", "dump")

# (src-relative path, qualified function) -> (kind, why it stays). Keyed on exact site identity.
ALLOWLIST: dict[tuple[str, str], tuple[str, str]] = {
    ("vault.py", "VaultWriter.write"): (
        "stored file header",
        "frozen vault file format, not a digest input",
    ),
    ("declared_casting/_quarantine_local.py", "LocalProtectedQuarantineSink._write_header"): (
        "stored file header",
        "the DQA1 header is written, not hashed",
    ),
    ("declared_casting/_quarantine_local.py", "LocalProtectedQuarantineSink.write_row"): (
        "stored encrypted row",
        "a persisted format line, not a digest constructor",
    ),
    ("declared_casting/_quarantine_local.py", "LocalProtectedQuarantineSink.seal"): (
        "stored trailer",
        "carries an already-computed digest; the trailer itself is not hashed",
    ),
    ("declared_casting/_catalog.py", "_resolve"): (
        "in-process cache key",
        "an lru_cache key that `_resolved_json` parses back; it never leaves the process",
    ),
    ("declared_casting/_catalog.py", "_key"): (
        "in-process cache key",
        "parsed back inside the same process; not a digest or seed input",
    ),
    ("plan/_generation.py", "build_generation_plan"): (
        "executed payload",
        "`config_json` is `json.loads`-ed back; its bytes are hashed only against themselves",
    ),
    ("generation/_pinned_compile.py", "compile_pinned_plan_with_generation"): (
        "executed payload",
        "`spec_json` and `config_json` are `json.loads`-ed back when the plan executes",
    ),
    ("profile/_serialize.py", "profile_to_json"): (
        "stored document",
        "compact profile JSON read by `profile_from_json`; its document bytes are not hashed",
    ),
    ("profiling/model_pack/_packio.py", "write_json"): (
        "indented document for people",
        "no allowlisted indented document is hashed by a reader",
    ),
    ("profiling/model_pack/provenance.py", "sign_pack"): (
        "indented document for people",
        "the signature covers the parsed manifest dictionary, not these bytes",
    ),
    ("profiling/model_pack/trainer.py", "train_and_evaluate"): (
        "indented document for people",
        "the loader hashes model bytes only",
    ),
    ("subset/_api.py", "run_subset"): (
        "indented document for people",
        "the manifest file; no reader hashes its bytes",
    ),
}

# The only functions that may name `seed_fingerprint_bytes`.
SEED_FORM_CALLERS = frozenset(
    {
        ("generators/derivation.py", "strategy_config_fingerprint"),
        ("generation/pool/_builder.py", "_config_hash"),
    }
)
# The modules that may import it (the two callers' files).
SEED_FORM_IMPORTERS = frozenset({"generators/derivation.py", "generation/pool/_builder.py"})


def _sorts_keys(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "sort_keys":
            return not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
    return False


def _json_names(tree: ast.AST) -> tuple[set[str], set[str], set[str]]:
    """Local spellings of ``json`` itself, of its dumpers, and of ``JSONEncoder``."""
    modules: set[str] = set()
    dumpers: set[str] = set()
    encoders: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update((a.asname or a.name) for a in node.names if a.name == "json")
        elif isinstance(node, ast.ImportFrom) and node.module == "json":
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name in _DUMPERS:
                    dumpers.add(local)
                elif alias.name == "JSONEncoder":
                    encoders.add(local)
    return modules, dumpers, encoders


def sorted_dump_sites(tree: ast.Module) -> list[str]:
    """The qualified function of every sorted-key JSON dump or encoder in one module."""
    modules, dumpers, encoders = _json_names(tree)
    owner = scan._enclosing(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _sorts_keys(node):
            continue
        func = node.func
        hit = False
        if isinstance(func, ast.Name):
            hit = func.id in dumpers or func.id in encoders
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            hit = func.value.id in modules and func.attr in (*_DUMPERS, "JSONEncoder")
        if hit:
            found.append(owner.get(id(node), "") or "<module>")
    return found


def seed_form_uses(tree: ast.Module) -> tuple[bool, list[str]]:
    """``(imports it, qualified functions that name it)`` for one module."""
    imported = any(
        isinstance(n, ast.ImportFrom) and any(a.name == "seed_fingerprint_bytes" for a in n.names)
        for n in ast.walk(tree)
    )
    names = local_names(tree, "seed_fingerprint_bytes")
    owner = scan._enclosing(tree)
    used = [
        owner.get(id(n), "") or "<module>"
        for n in ast.walk(tree)
        if (isinstance(n, ast.Name) and n.id in names and isinstance(n.ctx, ast.Load))
        or (isinstance(n, ast.Attribute) and n.attr == "seed_fingerprint_bytes")
    ]
    return imported, used


def _real_sites() -> dict[tuple[str, str], int]:
    sites: dict[tuple[str, str], int] = {}
    for path, tree in scan.ModuleIndex().trees.items():
        rel = path.relative_to(scan.SRC).as_posix()
        if rel == OWNER:
            continue
        for qual in sorted_dump_sites(tree):
            sites[(rel, qual)] = sites.get((rel, qual), 0) + 1
    return sites


# ----- the real tree ------------------------------------------------------------------------


def test_every_sorted_key_dump_outside_the_owner_is_an_allowlisted_document() -> None:
    new = sorted(set(_real_sites()) - set(ALLOWLIST))
    assert not new, (
        "a sorted-key JSON dump outside decoy_engine._canonical; a digest, signature or seed "
        f"input goes through canonical_json_bytes, a document goes on the allowlist: {new}"
    )


def test_every_allowlist_entry_still_matches_a_real_site() -> None:
    stale = sorted(set(ALLOWLIST) - set(_real_sites()))
    assert not stale, f"the site was removed or moved; delete these allowlist entries: {stale}"


def test_every_allowlist_entry_names_a_kind_and_a_reason() -> None:
    for site, (kind, why) in ALLOWLIST.items():
        assert kind and why, site


def test_the_seed_form_is_named_by_exactly_its_two_callers() -> None:
    used: set[tuple[str, str]] = set()
    importers: set[str] = set()
    for path, tree in scan.ModuleIndex().trees.items():
        rel = path.relative_to(scan.SRC).as_posix()
        if rel.startswith("_canonical/"):
            continue
        imported, functions = seed_form_uses(tree)
        if imported:
            importers.add(rel)
        used.update((rel, fn) for fn in functions)
    assert used == SEED_FORM_CALLERS, f"seed_fingerprint_bytes callers moved: {sorted(used)}"
    assert importers == SEED_FORM_IMPORTERS, f"seed form importers moved: {sorted(importers)}"


# ----- seeded spellings: the scanner must hit each ------------------------------------------

_SEEDED = [
    "import json\ndef f(x):\n    return json.dumps(x, sort_keys=True)\n",
    "import json as j\ndef f(x):\n    return j.dumps(x, sort_keys=True)\n",
    "from json import dumps as d\ndef f(x):\n    return d(x, sort_keys=True, default=str)\n",
    "import json\ndef f(x):\n    return json.JSONEncoder(sort_keys=True).encode(x)\n",
    "from json import JSONEncoder as E\ndef f(x):\n    return E(sort_keys=True).encode(x)\n",
    "import json\ndef f(x, fh):\n    json.dump(x, fh, sort_keys=True)\n",
    "import json\nclass C:\n    def m(self, x):\n        return json.dumps(x, sort_keys=flag)\n",
]


@pytest.mark.parametrize("source", _SEEDED)
def test_the_scanner_hits_each_seeded_spelling(source: str) -> None:
    assert sorted_dump_sites(ast.parse(source)), source


@pytest.mark.parametrize(
    "source",
    [
        "import json\ndef f(x):\n    return json.dumps(x)\n",
        "import json\ndef f(x):\n    return json.dumps(x, sort_keys=False)\n",
        "import yaml\ndef f(x):\n    return yaml.safe_dump(x, sort_keys=True)\n",
        "import pyarrow.compute as pc\ndef f(t):\n    return pc.sort_indices(t, sort_keys=[('a', 'x')])\n",
    ],
)
def test_the_scanner_ignores_a_plain_dump_and_foreign_sort_keys(source: str) -> None:
    assert sorted_dump_sites(ast.parse(source)) == []


def test_the_scanner_reports_the_enclosing_qualified_function() -> None:
    src = (
        "import json\nclass C:\n    def m(self, x):\n        return json.dumps(x, sort_keys=True)\n"
    )
    assert sorted_dump_sites(ast.parse(src)) == ["C.m"]


@pytest.mark.parametrize(
    "source",
    [
        "from decoy_engine._canonical import seed_fingerprint_bytes\n",
        "from decoy_engine._canonical import seed_fingerprint_bytes as s\n"
        "def f(x):\n    return s(x)\n",
        "from decoy_engine import _canonical\n"
        "def f(x):\n    return _canonical.seed_fingerprint_bytes(x)\n",
    ],
)
def test_the_seed_form_scanner_sees_an_import_or_a_call(source: str) -> None:
    imported, used = seed_form_uses(ast.parse(source))
    assert imported or used


def test_the_owner_file_is_the_only_one_exempt() -> None:
    assert (Path(scan.SRC) / OWNER).is_file()
