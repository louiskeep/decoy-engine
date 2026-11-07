"""Draw synthetic rows from a fitted Gaussian copula.

Row `start + i` is a pure function of (model, seed, start + i): each row consumes exactly `d` raw
outputs of the sample stream (`_rng`), and a window advances the stream by `start * d` first.
Correlated normals are `W L^T` with `L` the Cholesky factor of the released matrix, accumulated one
latent column at a time with elementwise operations rather than a BLAS product, so a row's value
does not depend on the batch size or on BLAS threading. Each column then inverts its marginal at
`ndtr(Z)` (`_marginals`). Bit-identity holds for one numpy build on one CPU feature set; across
machines the last bits of `exp` and `log` may differ.
"""

from __future__ import annotations

import hashlib

import numpy as np
import numpy.typing as npt
import pandas as pd

from decoy_engine.generation.copula import _rng
from decoy_engine.generation.copula._marginals import (
    STATE_NULL,
    decode_states,
    integer_inverse,
    intervals_for,
    is_integer_dtype,
    numeric_inverse,
)
from decoy_engine.generation.copula._model import (
    KIND_CATEGORICAL,
    KIND_NUMERIC,
    ROLE_NULL,
    ROLE_STATE,
    ROLE_VALUE,
    ColumnMarginal,
    GaussianCopulaModel,
)
from decoy_engine.generation.copula._normal import ndtr, ndtri

FloatArray = npt.NDArray[np.float64]


def _check_index(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"copula: {name} must be an integer.")
    if value < 0:
        raise ValueError(f"copula: {name} must not be negative.")
    return value


def correlated_uniforms(
    model: GaussianCopulaModel, n: int, bitgen: _rng.BitGenerator, start: int
) -> FloatArray:
    d = len(model.latent)
    if d == 0 or n == 0:
        return np.empty((n, d))
    bitgen.advance(start * d)
    w = ndtri(_rng.uniforms(bitgen, n * d).reshape(n, d))
    factor = np.linalg.cholesky(model.correlation_matrix())
    z = np.zeros((n, d))
    for j in range(d):
        z += w[:, j : j + 1] * factor[:, j][None, :]
    return ndtr(z)


def sample_gaussian_copula(
    model: GaussianCopulaModel, n: int, *, seed: bytes, start: int = 0
) -> pd.DataFrame:
    """`n` synthetic rows, equal to rows `[start, start + n)` of any longer draw with this seed."""
    try:
        if not isinstance(model, GaussianCopulaModel):
            raise TypeError("copula: model must be a GaussianCopulaModel.")
        _rng.check_seed(seed)
        n = _check_index(n, "n")
        start = _check_index(start, "start")
        bitgen = np.random.PCG64(
            np.random.SeedSequence(
                int.from_bytes(
                    hashlib.sha256(
                        _rng.framed(_rng.SAMPLE_LABEL) + len(seed).to_bytes(4, "big") + seed
                    ).digest(),
                    "big",
                )
            )
        )
        u = correlated_uniforms(model, n, bitgen, start)
        position = {(v.column, v.role): j for j, v in enumerate(model.latent)}
        data: dict[str, pd.Series] = {}
        for index, column in enumerate(model.columns):
            if column.kind == KIND_NUMERIC:
                null_u = (
                    u[:, position[(index, ROLE_NULL)]] if (index, ROLE_NULL) in position else None
                )
                data[column.name] = _numeric_series(
                    column, u[:, position[(index, ROLE_VALUE)]], null_u
                )
            elif column.kind == KIND_CATEGORICAL:
                data[column.name] = _categorical_series(column, u[:, position[(index, ROLE_STATE)]])
            else:
                data[column.name] = pd.Series([None] * n, dtype=object)
        return pd.DataFrame(data, index=pd.RangeIndex(n))
    except BaseException:
        del seed
        raise


def _numeric_series(column: ColumnMarginal, u: FloatArray, null_u: FloatArray | None) -> pd.Series:
    null = np.zeros(len(u), dtype=bool)
    if null_u is not None:
        intervals = intervals_for(column, ROLE_NULL)
        kinds = np.array([s.kind for s in intervals.states])
        null = kinds[decode_states(intervals, null_u)] == STATE_NULL
    if is_integer_dtype(column.dtype):
        ints = integer_inverse(column, u)
        if null_u is None:
            return pd.Series(ints, dtype=np.int64)
        return pd.Series(pd.array(ints, dtype="Int64").copy()).mask(null)
    return pd.Series(np.where(null, np.nan, numeric_inverse(column, u)), dtype=np.float64)


def _categorical_series(column: ColumnMarginal, u: FloatArray) -> pd.Series:
    intervals = intervals_for(column, ROLE_STATE)
    labels = np.array(
        [None if s.kind == STATE_NULL else s.label for s in intervals.states[: intervals.kept]],
        dtype=object,
    )
    return pd.Series(labels[decode_states(intervals, u)], dtype=object)
