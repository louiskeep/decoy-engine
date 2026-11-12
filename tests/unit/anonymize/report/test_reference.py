"""The report's risk section equals a plain-Python ``Fraction`` reference."""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Any

import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine.anonymize.report import build_risk_utility_report
from decoy_engine.quality.anonymity import EntropyLDiversity, KAnonymity

from ._helpers import release_if_found
from ._reference import risk_section


@st.composite
def frames(draw: Any, min_size: int = 1) -> pd.DataFrame:
    n = draw(st.integers(min_size, 30))
    col = lambda values: draw(st.lists(st.sampled_from(values), min_size=n, max_size=n))
    return pd.DataFrame(
        {
            "age": col(list(range(20, 40))),
            "zip": col(["120", "121", "130", "210"]),
            "dx": col(["a", "b", "c"]),
        }
    )


def _close(got: Any, want: Any) -> None:
    if want is None:
        assert got is None
        return
    for name, value in want.items():
        if isinstance(value, Fraction):
            assert math.isclose(got[name], float(value), rel_tol=1e-12, abs_tol=0), name
        else:
            assert got[name] == value, name


@settings(derandomize=True, max_examples=60, deadline=None)
@given(
    frame=frames(),
    extra=frames(min_size=0),
    with_population=st.booleans(),
    limit=st.sampled_from([0, 0.2, 0.5]),
    k=st.integers(1, 4),
    entropy=st.booleans(),
    tau=st.sampled_from([0.05, 0.2, 1 / 3, 0.5, 1.0]),
)
def test_risk_section_matches_reference(
    frame: pd.DataFrame,
    extra: pd.DataFrame,
    with_population: bool,
    limit: float,
    k: int,
    entropy: bool,
    tau: float,
) -> None:
    models = (KAnonymity(k), EntropyLDiversity("dx", 2)) if entropy else (KAnonymity(k),)
    found = release_if_found(frame, models, limit)
    if found is None:
        return
    lattice, result, released = found
    population = pd.concat([frame, extra], ignore_index=True) if with_population else None
    report = build_risk_utility_report(
        released, lattice, result, risk_threshold=tau, population=population
    )
    generalized_population = (
        None if population is None else lattice.apply(population, result.node or ())
    )
    want = risk_section(released.table, lattice.columns, tau, generalized_population)
    risk = report.to_payload()["risk"]
    for section in ("prosecutor", "journalist", "marketer"):
        _close(risk[section], want[section])
    assert risk["population"] == ("supplied" if with_population else "not_supplied")
