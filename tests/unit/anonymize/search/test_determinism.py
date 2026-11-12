"""The result depends on the rows as a multiset: not order, chunking, route or process."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pandas as pd
import pyarrow as pa
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine.anonymize.search import (
    Discernibility,
    LossMetric,
    NonUniformEntropy,
    SearchResult,
    search_lattice,
)
from decoy_engine.quality.anonymity import EntropyLDiversity, KAnonymity, OrderedTCloseness

from ._fixtures import NON_MONOTONE_ENTROPY, two_level_lattice

MODELS = (KAnonymity(2), EntropyLDiversity("dx", 2))


@st.composite
def tables(draw: Any) -> pd.DataFrame:
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


@settings(derandomize=True, max_examples=40, deadline=None)
@given(
    frame=tables(),
    seed=st.integers(0, 1000),
    pieces=st.integers(1, 4),
    limit=st.sampled_from([0, 0.2]),
    metric=st.sampled_from([Discernibility(), LossMetric(), NonUniformEntropy()]),
)
def test_order_chunking_and_route_do_not_matter(
    frame: pd.DataFrame, seed: int, pieces: int, limit: float, metric: Any
) -> None:
    lattice = two_level_lattice()
    run = lambda t: search_lattice(t, lattice, MODELS, suppression_limit=limit, metric=metric)
    expected = run(frame)
    shuffled = frame.sample(frac=1.0, random_state=seed).reset_index(drop=True)
    assert run(shuffled) == expected
    assert run(_chunked(shuffled, pieces)) == expected
    assert run(frame) == expected


def _summary(result: SearchResult) -> list[Any]:
    return [
        result.node,
        result.rows_suppressed,
        result.discernibility,
        result.loss_metric,
        result.non_uniform_entropy,
    ]


def test_same_result_in_another_process() -> None:
    frame = pd.DataFrame(NON_MONOTONE_ENTROPY)
    frame["s"] = list(range(len(frame)))
    models = (EntropyLDiversity("dx", 2), OrderedTCloseness("s", 0.4))
    result = search_lattice(
        frame, two_level_lattice(), models, suppression_limit=0.3, metric=NonUniformEntropy()
    )
    code = (
        "import json, pandas as pd\n"
        "from decoy_engine.anonymize.search import search_lattice, NonUniformEntropy\n"
        "from decoy_engine.quality.anonymity import EntropyLDiversity, OrderedTCloseness\n"
        "from tests.unit.anonymize.search._fixtures import NON_MONOTONE_ENTROPY, two_level_lattice\n"
        "f = pd.DataFrame(NON_MONOTONE_ENTROPY); f['s'] = list(range(len(f)))\n"
        "r = search_lattice(f, two_level_lattice(), (EntropyLDiversity('dx', 2), OrderedTCloseness('s', 0.4)),"
        " suppression_limit=0.3, metric=NonUniformEntropy())\n"
        "print(json.dumps([r.node, r.rows_suppressed, r.discernibility, r.loss_metric, r.non_uniform_entropy]))\n"
    )
    out = subprocess.run(  # noqa: S603 -- args are test literals
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    other = json.loads(out.stdout)
    other[0] = tuple(other[0])
    assert other == _summary(result)
