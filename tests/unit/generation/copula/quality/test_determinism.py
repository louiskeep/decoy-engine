"""A8: the report is a pure function of its inputs: across processes and under row order."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from ._helpers import fit, report, sample

_SRC = Path(__file__).resolve().parents[5] / "src"

_BUILD = """
import numpy as np, pandas as pd
from decoy_engine.generation.copula import (
    compute_copula_quality_report, fit_gaussian_copula, sample_gaussian_copula,
)
from decoy_engine.quality.snapshot import compute_distribution_snapshot

def build():
    rng = np.random.default_rng(50)
    x = rng.normal(size=300)
    real = pd.DataFrame({
        "x": x,
        "y": x + rng.normal(size=300),
        "c": np.where(x > 0.3, "hi", "lo").astype(object),
    })
    real.loc[rng.random(300) < 0.1, "y"] = np.nan
    model = fit_gaussian_copula(real, compute_distribution_snapshot(real), seed=bytes(range(16)))
    synth = sample_gaussian_copula(model, 300, seed=bytes(range(1, 17)))
    holdout = real.iloc[::2]
    return real, model, synth, holdout
"""

_CHILD = (
    _BUILD
    + """
import json
real, model, synth, holdout = build()
print(json.dumps(compute_copula_quality_report(real, synth, model, holdout=holdout).to_dict(),
                 sort_keys=True))
"""
)


def _child() -> str:
    result = subprocess.run(  # noqa: S603 -- fixed interpreter and script
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(_SRC)!r})\n{_CHILD}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_same_bytes_across_processes() -> None:
    namespace: dict[str, object] = {}
    exec(_BUILD, namespace)  # noqa: S102 -- the same fixed builder the children run
    real, model, synth, holdout = namespace["build"]()  # type: ignore[operator]
    from decoy_engine.generation.copula import compute_copula_quality_report

    here = json.dumps(
        compute_copula_quality_report(real, synth, model, holdout=holdout).to_dict(),
        sort_keys=True,
    )
    assert _child() == _child() == here


_RNG = np.random.default_rng(51)
_X = _RNG.normal(size=250)
_REAL = pd.DataFrame(
    {
        "x": _X,
        "y": _X * 0.5 + _RNG.normal(size=250),
        "c": np.where(_X > 0, "p", "q").astype(object),
    }
)
_MODEL = fit(_REAL)
_SYNTH = sample(_MODEL)
_BASE = report(_REAL, _MODEL, _SYNTH).to_dict()


@settings(max_examples=15, derandomize=True, deadline=None)
@given(st.integers(min_value=0, max_value=2**32 - 1), st.integers(min_value=0, max_value=2**32 - 1))
def test_row_order_does_not_matter(real_seed: int, synth_seed: int) -> None:
    real = _REAL.iloc[np.random.default_rng(real_seed).permutation(250)].reset_index(drop=True)
    synth = _SYNTH.iloc[np.random.default_rng(synth_seed).permutation(250)].reset_index(drop=True)
    # Every row is under dcr_max_rows, so the whole report is unchanged.
    assert report(real, _MODEL, synth).to_dict() == _BASE
