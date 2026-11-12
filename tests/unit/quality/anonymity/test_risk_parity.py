"""One prosecutor formula: the k-anonymity report and the risk measure agree, and both call it."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

import decoy_engine.quality.anonymity as anonymity
from decoy_engine.quality.anonymity import measure_k_anonymity, measure_reidentification_risk


@st.composite
def frames(draw: Any) -> pd.DataFrame:
    n = draw(st.integers(0, 25))
    col = lambda values: draw(st.lists(st.sampled_from(values), min_size=n, max_size=n))
    return pd.DataFrame({"a": col([1, 2, 3, None]), "b": col(["x", "y"])})


@settings(derandomize=True, max_examples=60, deadline=None)
@given(frame=frames(), tau=st.sampled_from([0.05, 0.2, 0.5, 1.0]))
def test_prosecutor_figures_agree(frame: pd.DataFrame, tau: float) -> None:
    k = measure_k_anonymity(frame, ["a", "b"])
    risk = measure_reidentification_risk(frame, ["a", "b"], risk_threshold=tau)
    assert risk.highest_prosecutor_risk == k.highest_prosecutor_risk
    assert risk.average_prosecutor_risk == k.average_prosecutor_risk
    assert risk.class_count == k.class_count
    assert risk.k_min == k.k_min


def _called_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            names.add(func.id if isinstance(func, ast.Name) else getattr(func, "attr", ""))
    return names


def test_both_measures_call_the_shared_helpers() -> None:
    package = Path(anonymity.__file__).parent
    for module in ("_k_anonymity.py", "_risk.py"):
        called = _called_names(package / module)
        assert {"prosecutor_highest", "prosecutor_average"} <= called, module


def test_k_anonymity_does_not_divide_for_its_risk_figures() -> None:
    """The formulas live in ``_risk.py``; ``_k_anonymity.py`` has no division of its own."""
    tree = ast.parse((Path(anonymity.__file__).parent / "_k_anonymity.py").read_text("utf-8"))
    divisions = [
        n for n in ast.walk(tree) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)
    ]
    assert divisions == []
