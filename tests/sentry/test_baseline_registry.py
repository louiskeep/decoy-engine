"""Every committed baseline or allowlist a sentry reads is registered and mirrored.

The ratchet only protects what ``sentry_registry.toml`` names. This test ties each live source
to its registered baseline (same key set) and fails when a sentry reads a committed list that is
not registered, so a new allowlist cannot slip in beside the ratchet.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline_sources as bs

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

LIST_NAME = re.compile(r"ALLOW|EXEMPT|WAIVER|SEAMS|DELIBERATELY|E0_TEST_FILES|BASELINE")
DATA_LITERAL = re.compile(r"[\w.\-]+\.(?:json|toml)$")
IGNORED_DATA = {"pyproject.toml", "Cargo.toml", "package.json", "e1-1.json"}


def registry() -> dict[str, str]:
    text = (bs.ROOT / "scripts" / "dev" / "sentry_registry.toml").read_text(encoding="utf-8")
    return {r["id"]: r["baseline"] for r in tomllib.loads(text).get("sentry", [])}


@pytest.mark.parametrize("src", bs.SOURCES, ids=lambda s: s.sentry_id)
def test_source_matches_its_registered_baseline(src: bs.Source) -> None:
    rel = registry().get(src.sentry_id)
    assert rel == f"tests/sentry/baselines/{src.sentry_id}.json", "sentry not registered"
    doc = json.loads((bs.ROOT / rel).read_text(encoding="utf-8"))
    assert doc["sentry_id"] == src.sentry_id
    live, recorded = src.keys(), set(doc["entries"])
    assert live - recorded == set(), "live entries missing from the baseline"
    assert recorded - live == set(), "stale baseline entries (shrink the baseline)"
    if src.maxes is not None:
        assert {k: doc["entries"][k].get("max") for k in recorded} == src.maxes(), (
            "a numeric entry's recorded max differs from its live value"
        )


def test_every_numeric_source_declares_its_max() -> None:
    """A source with integer values must emit them as ``max`` so the ratchet can see them."""
    numeric = {s.sentry_id for s in bs.SOURCES if s.maxes is not None}
    assert "package-fanout" in numeric


def test_registry_has_no_source_less_sentry() -> None:
    assert set(registry()) == {s.sentry_id for s in bs.SOURCES}


def unregistered_readers(
    sources: dict[str, str],
    data_files: set[str],
    covered_consts: set[tuple[str, str]],
    covered_files: set[str],
) -> list[str]:
    """Allowlist-shaped module constants and committed data files read but not registered."""
    found = []
    for name, text in sources.items():
        tree = ast.parse(text)
        for node in tree.body:
            targets = (
                node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
            )
            for t in targets:
                is_list = isinstance(t, ast.Name) and LIST_NAME.search(t.id)
                is_coll = isinstance(
                    getattr(node, "value", None), (ast.Dict, ast.Tuple, ast.List, ast.Set)
                )
                if is_list and is_coll and (name, t.id) not in covered_consts:
                    found.append(f"{name}: constant {t.id}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                base = node.value.rsplit("/", 1)[-1]
                if (
                    DATA_LITERAL.fullmatch(base)
                    and base in data_files
                    and base not in covered_files
                ):
                    found.append(f"{name}: data file {base}")
    return sorted(set(found))


def _covered() -> tuple[set[tuple[str, str]], set[str]]:
    files = set(bs.NOT_A_LIST) | IGNORED_DATA | {Path(s.where).name for s in bs.SOURCES}
    consts = {
        (s.keys.test_file, s.keys.constant) for s in bs.SOURCES if isinstance(s.keys, bs.InCode)
    }
    return consts, files


def _data_files() -> set[str]:
    return {
        p.name
        for root in ("tests", "docs")
        for p in (bs.ROOT / root).rglob("*")
        if p.suffix in {".json", ".toml"}
    }


def test_no_sentry_reads_an_unregistered_baseline() -> None:
    consts, files = _covered()
    sources = {p.name: p.read_text(encoding="utf-8") for p in bs.SENTRY.glob("test_*.py")}
    sources.pop("test_baseline_registry.py")  # holds the seeded fixtures below
    assert unregistered_readers(sources, _data_files(), consts, files) == []


def test_seeded_unregistered_readers_are_caught() -> None:
    consts, files = _covered()
    seeded_const = {"test_new.py": 'NEW_ALLOWLIST = {"a::b": "why"}\n'}
    assert unregistered_readers(seeded_const, set(), consts, files) == [
        "test_new.py: constant NEW_ALLOWLIST"
    ]
    seeded_file = {"test_new.py": 'P = ROOT / "tests" / "fixtures" / "fresh-list.json"\n'}
    assert unregistered_readers(seeded_file, {"fresh-list.json"}, consts, files) == [
        "test_new.py: data file fresh-list.json"
    ]
