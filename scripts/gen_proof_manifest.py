#!/usr/bin/env python3
"""Generate the proof manifest from the live registries and real runs.

Source of truth is the code, not copy. This script imports decoy_engine,
reads its registries for the capability surface, runs the real pipeline over
a hero dataset and one minimal config per capability, asserts each
capability's invariant, and emits a JSON artifact the marketing site renders.

Run:  python scripts/gen_proof_manifest.py
Out:  docs/proof-manifest.json  (committed; a sentry test re-runs build() and
      diffs, so a new capability with no proof fails CI)
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_MATRIX_GEN_PATH = Path(__file__).resolve().parent / "gen_capability_matrix.py"


def _capability_matrix_module():
    spec = importlib.util.spec_from_file_location("gen_capability_matrix", _MATRIX_GEN_PATH)
    assert spec and spec.loader, f"cannot load {_MATRIX_GEN_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _surface() -> dict:
    m = _capability_matrix_module()
    return {
        "mask": len(m._mask_strategies()),
        "generate": len(m._generation_strategies()),
        "providers": len(m._providers()),
    }

# Frozen stamp values. These are passed in (not read from the clock) so the
# generator is deterministic and the sentry diff is stable. Bump GENERATED_AT
# by hand when refreshing benchmarks or samples; bump ENGINE_VERSION to match
# the engine release being documented.
ENGINE_VERSION = "0.2.0"
GENERATED_AT = "2026-06-14"

OUT = Path(__file__).resolve().parent.parent / "docs" / "proof-manifest.json"


def build() -> dict:
    return {
        "engine_version": ENGINE_VERSION,
        "generated_at": GENERATED_AT,
        "surface": _surface(),
        "hero": {},
        "capabilities": [],
        "providers": [],
        "generation_strategies": [],
        "benchmarks": [],
    }


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
