"""A5: same model and seed give the same rows, in any process and any window."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine.generation.copula import (
    GaussianCopulaModel,
    fit_gaussian_copula,
    sample_gaussian_copula,
)

from ._fixtures import numeric_frame, seed_bytes, snapshot

_SRC = Path(__file__).resolve().parents[4] / "src"
_FRAME = numeric_frame(3, n=600)
_MODEL = fit_gaussian_copula(_FRAME, snapshot(_FRAME), seed=seed_bytes(11))


def _digest(frame: pd.DataFrame) -> str:
    h = hashlib.sha256()
    for name in frame.columns:
        h.update(name.encode())
        h.update(np.ascontiguousarray(frame[name].to_numpy()).tobytes())
    return h.hexdigest()


_CHILD = """
import hashlib, json, sys
import numpy as np
from decoy_engine.generation.copula import GaussianCopulaModel, sample_gaussian_copula
model = GaussianCopulaModel.from_dict(json.loads(sys.stdin.read()))
frame = sample_gaussian_copula(model, 400, seed=bytes(range(16)), start=5)
h = hashlib.sha256()
for name in frame.columns:
    h.update(name.encode())
    h.update(np.ascontiguousarray(frame[name].to_numpy()).tobytes())
print(h.hexdigest())
"""


def _child_digest() -> str:
    result = subprocess.run(  # noqa: S603 -- fixed interpreter and script
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(_SRC)!r})\n{_CHILD}"],
        input=json.dumps(_MODEL.to_dict()),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_identical_bytes_across_processes() -> None:
    first, second = _child_digest(), _child_digest()
    here = _digest(sample_gaussian_copula(_MODEL, 400, seed=bytes(range(16)), start=5))
    assert first == second == here


def test_different_seeds_differ() -> None:
    a = sample_gaussian_copula(_MODEL, 50, seed=seed_bytes(1))
    b = sample_gaussian_copula(_MODEL, 50, seed=seed_bytes(2))
    assert not a.equals(b)
    assert (a["x"] != b["x"]).mean() > 0.9


def test_fit_is_deterministic_for_the_same_seed() -> None:
    again = fit_gaussian_copula(_FRAME, snapshot(_FRAME), seed=seed_bytes(11))
    assert again == _MODEL


def test_json_round_trip_keeps_the_model_and_its_samples() -> None:
    text = json.dumps(_MODEL.to_dict(), allow_nan=False)
    back = GaussianCopulaModel.from_dict(json.loads(text))
    assert back == _MODEL
    pd.testing.assert_frame_equal(
        sample_gaussian_copula(back, 30, seed=seed_bytes(4)),
        sample_gaussian_copula(_MODEL, 30, seed=seed_bytes(4)),
    )


@settings(max_examples=60, deadline=None, derandomize=True)
@given(
    n=st.integers(min_value=0, max_value=300),
    start=st.integers(min_value=0, max_value=10**6),
    split=st.floats(min_value=0.0, max_value=1.0),
)
def test_windows_concatenate_to_the_full_draw(n: int, start: int, split: float) -> None:
    k = int(n * split)
    seed = seed_bytes(9)
    whole = sample_gaussian_copula(_MODEL, n, seed=seed, start=start)
    left = sample_gaussian_copula(_MODEL, k, seed=seed, start=start)
    right = sample_gaussian_copula(_MODEL, n - k, seed=seed, start=start + k)
    joined = pd.concat([left, right], ignore_index=True)
    pd.testing.assert_frame_equal(joined, whole, check_exact=True)


def test_empty_draw_has_the_model_columns_and_dtypes() -> None:
    empty = sample_gaussian_copula(_MODEL, 0, seed=seed_bytes(1))
    full = sample_gaussian_copula(_MODEL, 3, seed=seed_bytes(1))
    assert list(empty.columns) == [c.name for c in _MODEL.columns]
    assert empty.dtypes.to_dict() == full.dtypes.to_dict()
