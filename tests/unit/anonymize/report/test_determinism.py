"""The document depends on the rows as a multiset: not order, chunking, route or process."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pandas as pd
import pyarrow as pa
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine.anonymize.report import build_risk_utility_report
from decoy_engine.quality.anonymity import EntropyLDiversity, KAnonymity

from ._helpers import release, table

MODELS = (KAnonymity(2), EntropyLDiversity("dx", 2))


@st.composite
def frames(draw: Any) -> pd.DataFrame:
    n = draw(st.integers(1, 30))
    col = lambda values: draw(st.lists(st.sampled_from(values), min_size=n, max_size=n))
    return pd.DataFrame(
        {
            "age": col(list(range(20))),
            "zip": col(["120", "121", "130", "210"]),
            "dx": col(["a", "b", "c", None]),
        }
    )


def _chunked(frame: pd.DataFrame, pieces: int) -> pa.Table:
    whole = pa.Table.from_pandas(frame, preserve_index=False)
    size = max(1, -(-len(frame) // pieces))
    return pa.concat_tables([whole.slice(i, size) for i in range(0, len(frame), size)])


def _document(source: Any, population: Any, limit: float) -> str | None:
    lattice, result, released = release(source, MODELS, limit)
    if not result.found:
        return None
    report = build_risk_utility_report(
        released, lattice, result, risk_threshold=0.25, population=population
    )
    return json.dumps(report.to_payload(), sort_keys=True)


@settings(derandomize=True, max_examples=40, deadline=None)
@given(frame=frames(), seed=st.integers(0, 1000), pieces=st.integers(1, 4), limit=st.sampled_from([0, 0.3]))
def test_order_chunking_and_route_do_not_matter(
    frame: pd.DataFrame, seed: int, pieces: int, limit: float
) -> None:
    population = pd.concat([frame, frame.iloc[::2]], ignore_index=True)
    want = _document(frame, population, limit)
    shuffled = frame.sample(frac=1, random_state=seed).reset_index(drop=True)
    shuffled_population = population.sample(frac=1, random_state=seed + 1)
    assert _document(shuffled, shuffled_population, limit) == want
    assert _document(_chunked(frame, pieces), _chunked(population, pieces), limit) == want
    assert _document(_chunked(frame, pieces), population, limit) == want


_SCRIPT = """
import json
from tests.unit.anonymize.report._helpers import release, table
from decoy_engine.anonymize.report import build_risk_utility_report
from decoy_engine.quality.anonymity import KAnonymity
frame = table()
lattice, result, released = release(frame, (KAnonymity(3),), 0.25)
report = build_risk_utility_report(released, lattice, result, risk_threshold=0.2, population=frame)
print(json.dumps(report.to_payload(), sort_keys=True))
"""


def test_same_bytes_in_another_process() -> None:
    frame = table()
    lattice, result, released = release(frame, (KAnonymity(3),), 0.25)
    here = build_risk_utility_report(
        released, lattice, result, risk_threshold=0.2, population=frame
    )
    there = subprocess.run(  # noqa: S603 -- args are test literals
        [sys.executable, "-c", _SCRIPT], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert there == json.dumps(here.to_payload(), sort_keys=True)
