"""A7: every refusal is raised before any statistic, names no value, and carries no context."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation.copula import compute_copula_quality_report

from ._helpers import fit, sample

_SENTINEL = "SENTINEL_9137"


def _setup() -> tuple[pd.DataFrame, Any, pd.DataFrame]:
    rng = np.random.default_rng(40)
    real = pd.DataFrame(
        {
            "x": rng.normal(size=200),
            "c": rng.choice(np.array([_SENTINEL, "b", "c"], dtype=object), 200),
        }
    )
    model = fit(real)
    return real, model, sample(model)


def _tampered_label(real: pd.DataFrame) -> pd.DataFrame:
    out = real.copy()
    out.loc[out.index[out["c"] == "b"][0], "c"] = _SENTINEL + "X"
    return out


def _cases() -> list[tuple[str, type[Exception], Callable[..., Any]]]:
    def call(real: Any, synth: Any, model: Any, **kw: Any) -> Any:
        return compute_copula_quality_report(real, synth, model, **kw)

    return [
        ("model", TypeError, lambda r, m, s: call(r, s, {"model": _SENTINEL})),
        ("real type", TypeError, lambda r, m, s: call(r.to_dict(), s, m)),
        ("synthetic type", TypeError, lambda r, m, s: call(r, [_SENTINEL], m)),
        ("holdout type", TypeError, lambda r, m, s: call(r, s, m, holdout=_SENTINEL)),
        ("names collide", ValueError, lambda r, m, s: call(r, s.set_axis([1, "1"], axis=1), m)),
        ("columns", ValueError, lambda r, m, s: call(r, s.rename(columns={"c": "d"}), m)),
        ("row count", ValueError, lambda r, m, s: call(r.iloc[1:], s, m)),
        ("marginals", ValueError, lambda r, m, s: call(_tampered_label(r), s, m)),
        ("kind", ValueError, lambda r, m, s: call(r, s.assign(x=_SENTINEL), m)),
        ("infinite", ValueError, lambda r, m, s: call(r, s.assign(x=np.inf), m)),
        ("empty synthetic", ValueError, lambda r, m, s: call(r, s.iloc[:0], m)),
        ("holdout rows", ValueError, lambda r, m, s: call(r, s, m, holdout=r.iloc[:99])),
        ("holdout columns", ValueError, lambda r, m, s: call(r, s, m, holdout=r[["x"]])),
        ("max rows bool", TypeError, lambda r, m, s: call(r, s, m, dcr_max_rows=True)),
        ("max rows low", ValueError, lambda r, m, s: call(r, s, m, dcr_max_rows=99)),
        ("max rows high", ValueError, lambda r, m, s: call(r, s, m, dcr_max_rows=20001)),
        ("extras type", TypeError, lambda r, m, s: call(r, s, m, attack_extras=5)),
        ("extras name", ValueError, lambda r, m, s: call(r, s, m, attack_extras=f"a;{_SENTINEL}")),
        ("extras newline", ValueError, lambda r, m, s: call(r, s, m, attack_extras="mod\n")),
    ]


@pytest.mark.parametrize(("name", "error", "action"), _cases(), ids=[c[0] for c in _cases()])
def test_refusal(name: str, error: type[Exception], action: Callable[..., Any]) -> None:
    real, model, synth = _setup()
    with pytest.raises(error) as info:
        action(real, model, synth)
    assert _SENTINEL not in str(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None
