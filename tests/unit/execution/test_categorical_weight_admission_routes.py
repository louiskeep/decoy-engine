"""Categorical weight admission through the production entry points of every route.

`_build_cdf` admits weights by exact type (`int`, numpy integers, `float`, numpy
float16/32/64). Each route used to coerce with `float(w)` before calling it, so a
`bool`, a numeric string, a `Decimal` or a `Fraction` was converted and accepted. These
tests call the seams production runs, not `_build_cdf`: the resident handler, the
out-of-core FK route, the native prepared artifact and the public chunked entry.
"""

from __future__ import annotations

from decimal import Decimal
from enum import IntEnum
from fractions import Fraction
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._categorical import (
    CategoricalStrategyHandler,
    _build_cdf,
)
from decoy_engine.execution.native._categorical_prepared import (
    prepare_categorical,
    prepare_positional_categorical,
)
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._mask_group_b import categorical_array
from decoy_engine.relationships._graph import OrphanPolicy
from tests.native._b8_support import ENGINE_VERSION, TABLE, key_provider
from tests.native._chunked_categorical_support import cat_col, make_config, source
from tests.parity.test_out_of_core_group_b_parity import _REG, _payload_edge_job
from tests.parity.test_out_of_core_group_b_parity import _seed as _ooc_seed
from tests.unit.execution.test_categorical_weighted import _Ctx, _seed

_NONPOSITIVE = "categorical_weights_nonpositive"


class _Weight(IntEnum):
    ONE = 1


class _Float(float):
    pass


# Each refused vector is two weights over the categories ("A", "B"); the bad weight is
# first so the refusal is at index 0 on every route.
_REFUSED: dict[str, list[Any]] = {
    "bool": [True, 1],
    "str": ["1", "1"],
    "decimal": [Decimal(1), Decimal(1)],
    "fraction": [Fraction(1), Fraction(1)],
    "int_subclass": [_Weight.ONE, 1],
    "float_subclass": [_Float(1.0), 1.0],
}
_ACCEPTED: dict[str, tuple[list[Any], list[Any]]] = {
    # weights, and the plain-Python equivalent whose CDF they must build
    "numpy_int64": ([np.int64(1), np.int64(3)], [1, 3]),
    "numpy_uint8": ([np.uint8(1), np.uint8(3)], [1, 3]),
    "numpy_float32": ([np.float32(0.25), np.float32(0.75)], [0.25, 0.75]),
    "numpy_float64": ([np.float64(0.25), np.float64(0.75)], [0.25, 0.75]),
    "int_and_float": ([1, 3.0], [1, 3]),
}
_CATS = ["A", "B"]


def _resident(weights: list[Any]) -> pd.DataFrame:
    df = pd.DataFrame({"c": ["x", "y", None, "z"]})
    plan = _seed({"categories": list(_CATS), "weights": list(weights)})
    out, _ = CategoricalStrategyHandler().run(df, "c", plan, _Ctx())
    return out


def _ooc(weights: list[Any]) -> list[Any]:
    arr = categorical_array(
        pa.array(["x", "y", None, "z"]),
        job_seed=_Ctx.job_seed,
        namespace="ns",
        deterministic=True,
        cfg={"categories": list(_CATS), "weights": list(weights)},
    )
    return arr.to_pylist()


def _ooc_fk_job(weights: list[Any]) -> Any:
    payload = _ooc_seed(
        "categorical",
        namespace="catw",
        provider_config=(("categories", tuple(_CATS)), ("weights", tuple(weights))),
    )
    plan, sources, graph = _payload_edge_job(
        payload, ["one", "two", None, "three"], policy=OrphanPolicy.PRESERVE
    )
    return run_fk_out_of_core(plan, sources, config={}, registry=_REG, relationship_graph=graph)


def _chunked(weights: list[Any]) -> list[pa.Table]:
    col = cat_col(categories=list(_CATS))
    col["provider_config"]["weights"] = list(weights)
    return list(
        run_mask_chunked(
            make_config([col]),
            [source(["a", "b", None, "a"])],
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    )


@pytest.mark.parametrize("kind", list(_REFUSED))
def test_resident_handler_refuses(kind: str) -> None:
    with pytest.raises(StrategyError) as info:
        _resident(_REFUSED[kind])
    assert info.value.code == _NONPOSITIVE
    assert "index 0" in str(info.value)


@pytest.mark.parametrize("kind", list(_REFUSED))
def test_out_of_core_categorical_refuses(kind: str) -> None:
    with pytest.raises(StrategyError) as info:
        _ooc(_REFUSED[kind])
    assert info.value.code == _NONPOSITIVE


@pytest.mark.parametrize("kind", list(_REFUSED))
def test_out_of_core_fk_route_refuses(kind: str) -> None:
    with pytest.raises(StrategyError) as info:
        _ooc_fk_job(_REFUSED[kind])
    assert info.value.code == _NONPOSITIVE


@pytest.mark.parametrize("kind", list(_REFUSED))
def test_native_prepared_declines(kind: str) -> None:
    for prepare in (prepare_categorical, prepare_positional_categorical):
        kwargs: dict[str, Any] = {"deterministic": True} if prepare is prepare_categorical else {}
        artifact, reason = prepare(
            "c",
            namespace="ns",
            provider_config={"categories": list(_CATS), "weights": list(_REFUSED[kind])},
            **kwargs,
        )
        assert artifact is None, prepare.__name__
        assert reason == "categorical_weights_not_numeric:c", prepare.__name__


# Decimal and Fraction never reach this route: plan compilation hashes the config as
# JSON first and fails on them.
@pytest.mark.parametrize("kind", ["bool", "str", "int_subclass", "float_subclass"])
def test_public_chunked_entry_refuses(kind: str) -> None:
    with pytest.raises(StrategyError) as info:
        _chunked(_REFUSED[kind])
    assert info.value.code == _NONPOSITIVE


@pytest.mark.parametrize("kind", list(_ACCEPTED))
def test_accepted_types_build_the_plain_cdf_on_every_route(kind: str) -> None:
    weights, plain = _ACCEPTED[kind]
    assert _resident(weights).equals(_resident(plain))
    assert _ooc(weights) == _ooc(plain)
    artifact, reason = prepare_categorical(
        "c",
        deterministic=True,
        namespace="ns",
        provider_config={"categories": list(_CATS), "weights": list(weights)},
    )
    assert reason is None
    assert artifact is not None
    assert artifact.cdf == tuple(_build_cdf(plain))


def test_accepted_mixed_weights_run_on_the_public_chunked_entry() -> None:
    weights, plain = _ACCEPTED["int_and_float"]
    assert [t.to_pydict() for t in _chunked(weights)] == [t.to_pydict() for t in _chunked(plain)]
