"""Sentry test: every strict-mypy override module must resolve to real code.

consultant-2026-07-09 F8: `pyproject.toml`'s `[[tool.mypy.overrides]]` blocks
had accumulated ~13 `decoy_engine.graph.*` entries for a module tree that no
longer exists (`src/decoy_engine/graph` was removed). A dangling override
resolves silently under mypy, so the allowlist looked like live strict
coverage when it was dead config nobody would notice drifting further. See
docs/engine-consultant-findings-2026-07-09.md.

This walks every `[[tool.mypy.overrides]] module = [...]` entry and asserts
each dotted module path maps to a real file under `src/`, so a future removed
module trips CI instead of silently rotting the allowlist again.
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

REPO = Path(__file__).resolve().parents[2]
PYPROJECT = REPO / "pyproject.toml"
SRC = REPO / "src"


def _load() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _module_paths(dotted: str) -> list[Path]:
    """Candidate filesystem paths for a dotted module path: a package dir
    (`__init__.py`), a plain module file (`.py`), or a stub-only module
    (`.pyi`). No `src/decoy_engine` override currently targets a `.pyi`-only
    or PEP 420 namespace-package module, but checking `.pyi` here is cheap
    and avoids a false "dangling" flag if one is ever added."""
    rel = Path(*dotted.split("."))
    return [
        SRC / rel / "__init__.py",
        SRC / rel.with_suffix(".py"),
        SRC / rel.with_suffix(".pyi"),
    ]


def test_mypy_override_modules_resolve_to_real_files() -> None:
    data = _load()
    overrides = data.get("tool", {}).get("mypy", {}).get("overrides", [])
    assert overrides, "expected at least one [[tool.mypy.overrides]] block"

    dangling: list[str] = []
    for block in overrides:
        for dotted in block.get("module", []):
            if not dotted.startswith("decoy_engine."):
                continue
            if not any(p.exists() for p in _module_paths(dotted)):
                dangling.append(dotted)

    assert not dangling, (
        "mypy override(s) target modules that no longer exist on disk; "
        f"remove or update these entries in pyproject.toml: {sorted(dangling)}"
    )
