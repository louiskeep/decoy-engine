"""Snapshot bytes are decoded only by `parse_snapshot_json`.

A second JSON decode over snapshot bytes would bring back the raw `UnicodeDecodeError` that
carries the file. The surface is derived: every module under `src/decoy_engine` that mentions
"snapshot", owner included, so a new module or a second decoder inside the owner is caught
without anyone editing a file list. Spellings (`json.loads`, `from json import loads as x`,
`import json as js`, a re-binding) resolve through the shared call-resolution helper. Calls are
counted per (file, enclosing function) and compared with an exact map, so a second decode in an
already-allowed function still fails.
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _call_resolution import resolved_calls

SRC = Path(__file__).resolve().parents[3] / "src" / "decoy_engine"
OWNER = "generation/statistical/_snapshot_io.py"
DECODERS = ("loads", "load")

# (file, function) -> number of decode calls. The owner is the only entry that sees snapshot
# bytes. Every other entry decodes something else; the reason sits beside it.
ALLOWED = {
    (OWNER, "parse_snapshot_json"): 1,
    # config_json text carried on the shadow generation plan, not snapshot bytes
    ("execution/physical/_shadow_generation.py", "_decode_generation_config_shallow"): 1,
    # config_json of a generation plan column being compiled
    ("generation/_pinned_compile.py", "compile_pinned_plan_with_generation"): 1,
    # spec_json and config_json plan fields read back while checking a pinned plan
    ("generation/_pinned_execute.py", "_check_plan"): 4,
    # spec_json and config_json plan fields read back while running a pinned plan
    ("generation/_pinned_execute.py", "_run"): 2,
    # config_json and spec_json plan fields scanned for tokens
    ("generation/_pinned_execute.py", "_scan_plan_for_tokens"): 2,
    # config_json folded into the plan fingerprint
    ("generation/_pinned_plan.py", "fingerprint"): 1,
    # config_json of the generation entry being prepared
    ("generation/_plan_entry.py", "_prepare_generation"): 1,
    # user-supplied custom Faker provider .json value lists
    ("internal/faker_setup.py", "_load_json"): 1,
    # config_json of a serialized generation plan
    ("plan/_serialize.py", "_generation_plan_from_dict"): 1,
    # tomllib/tomli load of the dependency lock file (a TOML reader, not JSON)
    ("quality/dp_provenance.py", "assert_lock_matches_installed"): 1,
}


def _enclosing(tree: ast.AST) -> dict[int, str]:
    """Map id(node) of every node to the innermost function containing it."""
    out: dict[int, str] = {}

    def walk(node: ast.AST, fn: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            out[id(child)] = name
            walk(child, name)

    walk(tree, "<module>")
    return out


def decode_sites(rel_path: str, source_text: str) -> Counter[tuple[str, str]]:
    """Count JSON-decode calls in one module, keyed by (rel_path, enclosing function)."""
    tree = ast.parse(source_text)
    scope = _enclosing(tree)
    sites: Counter[tuple[str, str]] = Counter()
    for target in DECODERS:
        for call in resolved_calls(tree, target):
            sites[(rel_path, scope.get(id(call), "<module>"))] += 1
    return sites


def _surface() -> list[Path]:
    return [
        p for p in sorted(SRC.rglob("*.py")) if "snapshot" in p.read_text(encoding="utf-8").lower()
    ]


def _all_sites() -> Counter[tuple[str, str]]:
    total: Counter[tuple[str, str]] = Counter()
    for path in _surface():
        total += decode_sites(path.relative_to(SRC).as_posix(), path.read_text(encoding="utf-8"))
    return total


def test_surface_includes_the_owner() -> None:
    assert OWNER in {p.relative_to(SRC).as_posix() for p in _surface()}


def test_only_the_snapshot_io_owner_parses_snapshot_bytes() -> None:
    assert _all_sites() == ALLOWED


SEEDS = [
    "import json\ndef f(raw):\n    return json.loads(raw)\n",
    "import json\ndef f(fh):\n    return json.load(fh)\n",
    "import json as js\ndef f(raw):\n    return js.loads(raw)\n",
    "from json import loads\ndef f(raw):\n    return loads(raw)\n",
    "from json import loads as L\ndef f(raw):\n    return L(raw)\n",
    "from json import load as L\ndef f(fh):\n    return L(fh)\n",
    "import json\ndef f(raw):\n    g = json.loads\n    return g(raw)\n",
    "from json import loads\ndef f(raw):\n    g = loads\n    return g(raw)\n",
]


@pytest.mark.parametrize("source", SEEDS)
def test_every_decode_spelling_is_counted(source: str) -> None:
    assert decode_sites("m.py", source) == Counter({("m.py", "f"): 1})


def test_a_second_decode_in_the_owner_breaks_the_exact_map() -> None:
    owner_text = (SRC / OWNER).read_text(encoding="utf-8")
    extra = "\n\ndef _second(raw):\n    import json\n\n    return json.loads(raw)\n"
    seeded = (
        _all_sites() - decode_sites(OWNER, owner_text) + decode_sites(OWNER, owner_text + extra)
    )
    assert seeded != ALLOWED
