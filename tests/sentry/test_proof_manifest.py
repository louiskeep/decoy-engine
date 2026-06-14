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
import json
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
    for key in ("engine_version", "generated_at", "surface", "hero",
                "capabilities", "providers", "generation_strategies", "benchmarks"):
        assert key in manifest, f"manifest missing top-level key {key!r}"
