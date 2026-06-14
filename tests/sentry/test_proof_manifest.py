"""Proof-manifest drift sentry.

`docs/proof-manifest.json` is generated from the live registries and real
pipeline runs by `scripts/gen_proof_manifest.py`. This guard re-renders the
manifest from current code and asserts it equals the committed file. A new
capability (or a changed sample) that is not regenerated fails CI.

To fix a failure, run:

    python scripts/gen_proof_manifest.py

and commit the updated docs/proof-manifest.json.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "scripts" / "gen_proof_manifest.py"
MANIFEST = REPO_ROOT / "docs" / "proof-manifest.json"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_proof_manifest", GENERATOR)
    assert spec and spec.loader, f"cannot load generator at {GENERATOR}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generator_build_has_required_top_level_keys():
    gen = _load_generator()
    manifest = gen.build()
    for key in (
        "engine_version",
        "generated_at",
        "surface",
        "hero",
        "capabilities",
        "providers",
        "generation_strategies",
        "benchmarks",
    ):
        assert key in manifest, f"manifest missing top-level key {key!r}"


def test_surface_counts_match_capability_matrix():
    gen = _load_generator()
    surface = gen.build()["surface"]
    # Mask count excludes the internal `nested` wrapper, matching the matrix.
    assert surface["mask"] == 12
    assert surface["generate"] == 7
    assert surface["providers"] == 34


def test_hero_has_input_output_and_audit_log():
    gen = _load_generator()
    hero = gen.build()["hero"]
    assert hero["disguise"] == "hipaa"
    table_names = {t["name"] for t in hero["tables"]}
    assert {"members", "claims", "providers"} <= table_names
    members = next(t for t in hero["tables"] if t["name"] == "members")
    assert len(members["input"]) == len(members["output"]) >= 1
    # ssn is masked: at least one row's ssn changes.
    assert any(
        i["ssn"] != o["ssn"] for i, o in zip(members["input"], members["output"], strict=True)
    )
    assert len(hero["audit_log"]) >= 1
    assert isinstance(hero["invariants"], list) and hero["invariants"]


def test_capabilities_include_fpe_and_redact_with_invariants():
    gen = _load_generator()
    caps = {c["id"]: c for c in gen.build()["capabilities"]}
    assert "mask.fpe" in caps and "mask.redact" in caps
    fpe = caps["mask.fpe"]
    assert fpe["kind"] == "mask"
    assert fpe["invariant"]
    assert fpe["config_yaml"].strip()
    assert len(fpe["input"]) == len(fpe["output"]) >= 1
    # fpe preserves length on the masked column.
    col = fpe["column"]
    assert all(len(i[col]) == len(o[col]) for i, o in zip(fpe["input"], fpe["output"], strict=True))
    # redact replaces the value (output differs from input).
    red = caps["mask.redact"]
    rcol = red["column"]
    assert all(i[rcol] != o[rcol] for i, o in zip(red["input"], red["output"], strict=True))
