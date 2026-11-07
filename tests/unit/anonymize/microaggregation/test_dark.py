"""The package is reachable by import only; the engine's root surface does not name it."""

from __future__ import annotations

import subprocess
import sys

_PROBE = """
import sys
import decoy_engine
assert "decoy_engine.anonymize" not in sys.modules, "root import pulls in anonymize"
import decoy_engine.anonymize.microaggregation
print("ok")
"""


def _ours(name: str) -> bool:
    lowered = name.lower()
    return "anonymize" in lowered or "microaggregat" in lowered


def test_root_import_does_not_load_the_package() -> None:
    out = subprocess.run(  # noqa: S603 -- args are test literals
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "ok"


def test_root_surface_does_not_name_the_package() -> None:
    import decoy_engine.anonymize.microaggregation

    assert not [n for n in decoy_engine.__all__ if _ours(n)]
    assert not [i.name for i in decoy_engine.strategy_catalog() if _ours(i.name)]
