"""Phase D / D1b-ii step 1: pin the pre-D1b-ii OOC categorical output (test-only, no source change).

Every expected value is a hardcoded literal in `_d1b2_categorical_data.py`, captured from the
pure-Python `categorical_array` before the native route existed. The same suite stays green
when D1b-ii routes admitted columns through the compiled index kernel: native output must
reproduce these bytes for shared-Unicode-version input, so a drift in either route fails here.
It runs the payload columns through both drivers and both output paths, uniform and weighted,
plus the sources the native route must decline (they keep their Python selection).
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import StrategyError
from decoy_engine.keyprovider import SecretKeyProvider
from tests.parity._d1a_hash_data import SECRET, run_chain
from tests.parity._d1b2_categorical_data import (
    CATEGORIES,
    EXPECTED,
    EXPECTED_NON_STRING,
    WEIGHTS,
    categorical_seed,
    non_string_sources,
    source_column,
)
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs

_SEEDS = (
    ("c_uniform", categorical_seed()),
    ("c_weighted", categorical_seed(weights=WEIGHTS)),
    ("c_other_ns", categorical_seed(namespace="ns_other")),
)
_MODES = ["resident", "sink-batch", "sink-reorder"]


def _payload() -> pa.Table:
    col = source_column()
    return pa.table({name: col for name, _ in _SEEDS})


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("batch_rows", [None, 7, 1])
def test_ooc_categorical_matches_the_pinned_baseline(
    mode: str, batch_rows: int | None, tmp_path: Path
) -> None:
    run = run_chain(_payload(), _SEEDS, batch_rows=batch_rows, **mode_kwargs(mode, tmp_path))
    for name, _ in _SEEDS:
        got = run.tables["t"].column(name)
        assert got.type == pa.string(), name
        assert got.to_pylist() == EXPECTED[name], name


def test_secret_backed_mask_key_differs_from_job_seed_and_matches_the_pin() -> None:
    seeds = (("c_secret", categorical_seed(weights=WEIGHTS)),)
    run = run_chain(
        pa.table({"c_secret": source_column()}), seeds, key_provider=SecretKeyProvider(SECRET)
    )
    got = run.tables["t"].column("c_secret").to_pylist()
    assert got == EXPECTED["c_secret"]
    assert got != EXPECTED["c_weighted"]


def test_composed_and_decomposed_text_select_one_category() -> None:
    assert EXPECTED["c_uniform"][4] == EXPECTED["c_uniform"][5]  # "e" + U+0301 vs U+00E9
    assert EXPECTED["c_weighted"][4] == EXPECTED["c_weighted"][5]


def test_namespace_binds_the_selection() -> None:
    assert EXPECTED["c_other_ns"] != EXPECTED["c_uniform"]


def test_every_pool_member_is_reachable_and_nulls_stay_null() -> None:
    assert {v for v in EXPECTED["c_uniform"] if v is not None} == set(CATEGORIES)
    assert EXPECTED["c_uniform"][6] is None
    assert EXPECTED["c_weighted"][6] is None


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("name", sorted(EXPECTED_NON_STRING))
def test_non_string_sources_keep_their_python_selection(
    name: str, mode: str, tmp_path: Path
) -> None:
    column = non_string_sources()[name]
    seeds = ((name, categorical_seed(categories=CATEGORIES[:3])),)
    run = run_chain(pa.table({name: column}), seeds, batch_rows=2, **mode_kwargs(mode, tmp_path))
    assert run.tables["t"].column(name).to_pylist() == EXPECTED_NON_STRING[name]


def test_an_unbuildable_weight_config_raises_the_oracle_error_not_a_native_one() -> None:
    seeds = (("c", categorical_seed(categories=("x", "y"), weights=(1e-9, 1))),)
    with pytest.raises(StrategyError) as caught:
        run_chain(pa.table({"c": pa.array(["a", "b"], pa.string())}), seeds)
    assert caught.value.code == "categorical_weight_below_resolution"


@pytest.mark.parametrize("weights", [(float("nan"), 1), (float("inf"), 1)], ids=["nan", "inf"])
def test_non_finite_weights_raise_the_oracle_value_error(weights: tuple[float, float]) -> None:
    seeds = (("c", categorical_seed(categories=("x", "y"), weights=weights)),)
    with pytest.raises(ValueError, match="cannot convert float NaN to integer"):
        run_chain(pa.table({"c": pa.array(["a", "b"], pa.string())}), seeds)
