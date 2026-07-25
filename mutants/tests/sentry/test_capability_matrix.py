"""Capability-matrix drift sentry.

`docs/capability-matrix.md` is generated from the live registries by
`scripts/gen_capability_matrix.py`. This guard re-renders the matrix from the
current code and asserts it equals the committed file. If a registry changes
(a new mask strategy, generation strategy, synthetic provider, connector,
STORM detector, or disguise) without the matrix being regenerated, this fails.

That is the point: a new capability cannot ship without its doc entry being
refreshed. To fix a failure, run:

    python scripts/gen_capability_matrix.py

and commit the updated docs/capability-matrix.md.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "scripts" / "gen_capability_matrix.py"
MATRIX = REPO_ROOT / "docs" / "capability-matrix.md"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_capability_matrix", GENERATOR)
    assert spec and spec.loader, f"cannot load generator at {GENERATOR}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_capability_matrix_is_up_to_date():
    gen = _load_generator()
    expected = gen.render()
    assert MATRIX.exists(), f"{MATRIX} is missing. Run `python scripts/gen_capability_matrix.py`."
    actual = MATRIX.read_text(encoding="utf-8")
    assert actual == expected, (
        "docs/capability-matrix.md is stale: a registry changed but the matrix "
        "was not regenerated. Run `python scripts/gen_capability_matrix.py` and "
        "commit the result. (A new capability must update its docs.)"
    )
