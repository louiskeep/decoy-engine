"""`detection/` is the base layer: `profiling/` and `verification/` import it, never the reverse."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src" / "decoy_engine"
UPWARD = ("decoy_engine.profiling", "decoy_engine.verification")


def _upward_imports(path: Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):  # lazy imports included
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        else:
            continue
        found += [f"{path.name}:{node.lineno} {n}" for n in names if n.startswith(UPWARD)]
        if isinstance(node, ast.ImportFrom) and node.module == "decoy_engine":
            found += [
                f"{path.name}:{node.lineno} decoy_engine.{a.name}"
                for a in node.names
                if a.name in ("profiling", "verification")
            ]
    return found


def test_detection_never_imports_profiling_or_verification() -> None:
    offenders = [
        hit for p in sorted((SRC / "detection").rglob("*.py")) for hit in _upward_imports(p)
    ]
    assert offenders == []


def test_the_scan_catches_a_planted_upward_import(tmp_path: Path) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text("def f():\n    from decoy_engine.profiling.model_pack import classify\n")
    assert _upward_imports(planted) != []
    planted.write_text("from decoy_engine import profiling\n")
    assert _upward_imports(planted) != []


def test_detector_order_is_defined_once() -> None:
    from typing import get_args

    from decoy_engine.config import _pre_scan
    from decoy_engine.detection.types import DETECTOR_ORDER, DetectorSource

    assert DETECTOR_ORDER == get_args(DetectorSource) == ("regex", "classifier", "ner")
    assert _pre_scan.DETECTOR_ORDER is DETECTOR_ORDER
    assert not hasattr(_pre_scan, "CANONICAL_ORDER")
