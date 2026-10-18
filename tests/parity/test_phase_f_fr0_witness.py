"""Phase F FR0: the executed-backend witness over the unified core's persistent streams.

`_f0_witness` was written against the whole-table generator. Eligible plans no longer run it, so
the witness hooks the core's streams too, and this file proves it still answers its question for
a plan that never touches the legacy path: which backend produced each column's values, and were
those the values that reached the column, across SEVERAL batches. The mutants (a discarded
result, a recomputed selection, a wrong-column attribution) must each trip it.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.generation import _column_streams, _faker_pool
from tests.parity import _f0_fixtures as fx
from tests.parity import _f0_witness as w
from tests.parity._f0_canonical import table_fingerprint
from tests.parity._fr0_support import fingerprints, partition_from_cuts, run_resident

_THRESHOLD = 90
_ROWS = 100


def _core_config() -> dict[str, Any]:
    cols: list[dict[str, Any]] = [
        {"name": "seq", "type": "sequence", "start": 1},
        {"name": "cat", "type": "categorical", "categories": ["a", "b", "c"], "weights": [3, 2, 1]},
        {
            "name": "cat_null",
            "type": "categorical",
            "categories": ["u", "v", "w"],
            "null_probability": 0.3,
        },
        {"name": "pooled", "type": "faker", "faker_type": "city"},
        {
            "name": "pooled_null",
            "type": "faker",
            "faker_type": "last_name",
            "null_probability": 0.2,
        },
        {"name": "live", "type": "faker", "faker_type": "email"},
        {"name": "live_optout", "type": "faker", "faker_type": "city", "pooled": False},
    ]
    child: list[dict[str, Any]] = [
        {"name": "rid", "type": "sequence", "start": 1},
        {
            "name": "ref_rand",
            "type": "reference",
            "reference_table": "par",
            "reference_column": "k",
            "distribution": "random",
        },
        {
            "name": "ref_weighted",
            "type": "reference",
            "reference_table": "par",
            "reference_column": "k",
            "distribution": "weighted",
            "weights": [1, 2, 3],
        },
    ]
    parent = [{"name": "k", "type": "categorical", "categories": ["K1", "K2", "K3"]}]
    return fx.config(
        [fx.table("w", _ROWS, cols), fx.table("child", 40, child), fx.table("par", 3, parent)]
    )


def _run() -> dict[str, pa.Table]:
    """Several irregular batches per table, so per-batch events must aggregate."""
    cuts = {"w": [13, 13, 60], "child": [7, 31], "par": [1]}
    return run_resident(_core_config(), partition_from_cuts(cuts))


_NONE = frozenset({w.NO_SELECTION})
_PYRNG = frozenset({w.PYTHON_RNG})
_NUMPY = frozenset({w.NUMPY_SELECTION})
_LIVE = frozenset({w.LIVE_PROVIDER})
_NO_SUB: frozenset[str] = frozenset()
CORE_BASELINE = {
    ("w", "seq"): (_NONE, _NO_SUB),
    ("w", "cat"): (_PYRNG, _NO_SUB),
    ("w", "cat_null"): (_PYRNG, frozenset({w.NULL_MASK})),
    ("w", "pooled"): (_NUMPY, frozenset({w.POOL_CONSTRUCTION})),
    ("w", "pooled_null"): (_NUMPY, frozenset({w.POOL_CONSTRUCTION, w.NULL_MASK})),
    ("w", "live"): (_LIVE, _NO_SUB),
    ("w", "live_optout"): (_LIVE, _NO_SUB),
    ("child", "rid"): (_NONE, _NO_SUB),
    ("child", "ref_rand"): (_PYRNG, _NO_SUB),
    ("child", "ref_weighted"): (_PYRNG, _NO_SUB),
    ("par", "k"): (_PYRNG, _NO_SUB),
}


def _summary(reports: dict[w.ColumnKey, w.ColumnReport]) -> dict[Any, Any]:
    return {k: (r.selection, r.subpaths) for k, r in reports.items()}


def test_the_witness_config_really_runs_on_the_core() -> None:
    from decoy_engine.generation._admission import classify_plan
    from tests.parity._fr0_support import prepared_config

    assert classify_plan(prepared_config(_core_config())).eligible


def test_core_baseline_is_observed_per_column(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", _THRESHOLD)
    reports = w.classify_columns(monkeypatch, _run)
    assert _summary(reports) == CORE_BASELINE
    assert all(r.evidence == w.REAL for r in reports.values())


def test_core_baseline_with_marked_pools_reads_indices_back_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", _THRESHOLD)
    reports = w.classify_columns(monkeypatch, _run, marking=True)
    assert _summary(reports) == CORE_BASELINE


def test_core_observer_is_transparent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", _THRESHOLD)
    plain = _run()
    with w.end_to_end(monkeypatch) as witness:
        observed = _run()
    assert fingerprints(observed) == fingerprints(plain)
    assert {k: table_fingerprint(t) for k, t in observed.items()} == {
        k: table_fingerprint(t) for k, t in plain.items()
    }
    witness.classify(observed)


def test_real_threshold_pooled_fixture_is_numpy_selection_on_the_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = w.classify_columns(monkeypatch, lambda: fx.run_generate_tables(fx.cfg_pooled_faker()))
    assert _summary(reports) == {
        ("pooled", c): (_NUMPY, frozenset({w.POOL_CONSTRUCTION}))
        for c in ("city", "last", "street")
    }


# -- mutants on the core: each must trip the witness ------------------------------------------


def _outer(monkeypatch: pytest.MonkeyPatch, target: Any, name: str, replace: Any) -> None:
    monkeypatch.setattr(target, name, replace(getattr(target, name)))


def _trips(
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
    *,
    match: str,
    marking: bool = False,
    attribution: Any = None,
) -> None:
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", _THRESHOLD)
    with w.end_to_end(monkeypatch, marking=marking, attribution=attribution) as witness:
        mutate(monkeypatch)
        tables = _run()
    with pytest.raises(w.WitnessError, match=match):
        witness.classify(tables)


def test_core_numpy_discarded_result_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    def discard(inner: Any) -> Any:
        def select(self: Any, size: int) -> list[Any]:
            return [w.strip_mark(v) for v in inner(self, size)]

        return select

    _trips(
        monkeypatch,
        lambda mp: _outer(mp, _faker_pool.PooledSelector, "select", discard),
        match="numpy-selected",
        marking=True,
    )


def test_core_numpy_recompute_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    def recompute(inner: Any) -> Any:
        def select(self: Any, size: int) -> list[Any]:
            picked = inner(self, size)
            return [self._values[(i * 7) % len(self._values)] for i in range(len(picked))]

        return select

    _trips(
        monkeypatch,
        lambda mp: _outer(mp, _faker_pool.PooledSelector, "select", recompute),
        match="numpy-selected",
        marking=True,
    )


def test_core_python_rng_discarded_result_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    def discard(inner: Any) -> Any:
        def next_batch(self: Any, offset: int, size: int) -> list[Any]:
            return list(reversed(inner(self, offset, size)))  # drawn, then reordered

        return next_batch

    _trips(
        monkeypatch,
        lambda mp: _outer(mp, _column_streams.CategoricalStream, "next_batch", discard),
        match="python RNG selections",
    )


def test_core_reference_discarded_result_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    def discard(inner: Any) -> Any:
        def next_batch(self: Any, offset: int, size: int) -> list[Any]:
            out = inner(self, offset, size)
            return ["K1"] * len(out)

        return next_batch

    _trips(
        monkeypatch,
        lambda mp: _outer(mp, _column_streams.ReferenceStream, "next_batch", discard),
        match="python RNG selections",
    )


def test_core_live_provider_discarded_result_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    def discard(inner: Any) -> Any:
        def next_batch(self: Any, offset: int, size: int) -> list[Any]:
            out = inner(self, offset, size)
            return ["constant"] * len(out)

        return next_batch

    _trips(
        monkeypatch,
        lambda mp: _outer(mp, _column_streams.PerRowFakerStream, "next_batch", discard),
        match="live provider results",
    )


@pytest.mark.parametrize(
    ("swap", "match"),
    [
        (
            {("w", "pooled"): ("w", "pooled_null"), ("w", "pooled_null"): ("w", "pooled")},
            "numpy|pool|selected",
        ),
        (
            {("w", "cat"): ("w", "cat_null"), ("w", "cat_null"): ("w", "cat")},
            "python RNG selections",
        ),
        (
            {("w", "live"): ("w", "live_optout"), ("w", "live_optout"): ("w", "live")},
            "live provider results",
        ),
    ],
)
def test_core_wrong_attribution_trips(
    monkeypatch: pytest.MonkeyPatch, swap: dict[Any, Any], match: str
) -> None:
    _trips(monkeypatch, lambda mp: None, match=match, attribution=lambda k: swap.get(k, k))


def test_a_selection_in_the_core_no_selection_column_is_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", _THRESHOLD)
    with w.end_to_end(monkeypatch) as witness:

        def drawing(self: Any, offset: int, size: int) -> list[str]:
            return [str(v) for v in _column_streams.random.Random(3).choices(["x", "y"], k=size)]

        monkeypatch.setattr(_column_streams.SequenceStream, "next_batch", drawing)
        tables = _run()
    with pytest.raises(w.WitnessError, match="sequence must not select"):
        witness.classify(tables)
