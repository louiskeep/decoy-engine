"""Phase F FR0: the executed-backend witness over the unified core's persistent streams.

`_f0_witness` was written against the whole-table generator. Eligible plans no longer run it, so
the witness hooks the core's streams too, and this file proves it still answers its question for
a plan that never touches the legacy path: which backend produced each column's values, and were
those the values that reached the column, across SEVERAL batches. The mutants (a discarded
result, a recomputed selection, a wrong-column attribution) must each trip it.

FR3 moved the core's uniform and weighted selections onto the index kernel, so those columns now
report the compiled label (or the Python reference kernel without the companion). A column whose
weights the kernel mapping declines keeps the Python RNG and still reports `python_rng_selection`;
`cat_py` and `ref_py` below pin that.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._index_ext import load_compiled_index_kernel
from decoy_engine.generation import _column_streams, _faker_pool, _kernel_select
from tests.parity import _f0_fixtures as fx
from tests.parity import _f0_witness as w
from tests.parity._f0_canonical import table_fingerprint
from tests.parity._fr0_support import fingerprints, partition_from_cuts, run_resident

_THRESHOLD = 90
_ROWS = 100


def _has_companion() -> bool:
    try:
        load_compiled_index_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


# Which label the kernel lookup yields on this host. The values are identical either way; only the
# executed backend differs.
_KERNEL_LABEL = w.COMPILED_INDEX if _has_companion() else w.PYTHON_INDEX_REFERENCE
# A weight share below 2**-32 cannot own a slot of the kernel's draw space, so the mapping declines.
_TINY = 1e-12


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
        {
            "name": "cat_py",
            "type": "categorical",
            "categories": ["x", "y", "z"],
            "weights": [1, _TINY, 2],
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
        {
            "name": "ref_py",
            "type": "reference",
            "reference_table": "par",
            "reference_column": "pk",
            "distribution": "weighted",
            "weights": [1, _TINY, 2],
        },
    ]
    # `pk` is a sequence so the parent holds exactly three distinct keys and `ref_py`'s weights line up.
    parent = [
        {"name": "k", "type": "categorical", "categories": ["K1", "K2", "K3"]},
        {"name": "pk", "type": "sequence", "start": 1, "prefix": "K"},
    ]
    return fx.config(
        [fx.table("w", _ROWS, cols), fx.table("child", 40, child), fx.table("par", 3, parent)]
    )


def _run() -> dict[str, pa.Table]:
    """Several irregular batches per table, so per-batch events must aggregate."""
    cuts = {"w": [13, 13, 60], "child": [7, 31], "par": [1]}
    return run_resident(_core_config(), partition_from_cuts(cuts))


_NONE = frozenset({w.NO_SELECTION})
_PYRNG = frozenset({w.PYTHON_RNG})
_KERNEL = frozenset({_KERNEL_LABEL})
_LIVE = frozenset({w.LIVE_PROVIDER})
_NO_SUB: frozenset[str] = frozenset()
CORE_BASELINE = {
    ("w", "seq"): (_NONE, _NO_SUB),
    ("w", "cat"): (_KERNEL, _NO_SUB),
    ("w", "cat_null"): (_KERNEL, frozenset({w.NULL_MASK})),
    ("w", "cat_py"): (_PYRNG, _NO_SUB),
    ("w", "pooled"): (_KERNEL, frozenset({w.POOL_CONSTRUCTION})),
    ("w", "pooled_null"): (_KERNEL, frozenset({w.POOL_CONSTRUCTION, w.NULL_MASK})),
    ("w", "live"): (_LIVE, _NO_SUB),
    ("w", "live_optout"): (_LIVE, _NO_SUB),
    ("child", "rid"): (_NONE, _NO_SUB),
    ("child", "ref_rand"): (_KERNEL, _NO_SUB),
    ("child", "ref_weighted"): (_KERNEL, _NO_SUB),
    ("child", "ref_py"): (_PYRNG, _NO_SUB),
    ("par", "k"): (_KERNEL, _NO_SUB),
    ("par", "pk"): (_NONE, _NO_SUB),
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


def test_real_threshold_pooled_fixture_is_kernel_selection_on_the_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = w.classify_columns(monkeypatch, lambda: fx.run_generate_tables(fx.cfg_pooled_faker()))
    assert _summary(reports) == {
        ("pooled", c): (_KERNEL, frozenset({w.POOL_CONSTRUCTION}))
        for c in ("city", "last", "street")
    }


def test_stand_in_compiled_kernel_is_labelled_compiled_with_stand_in_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runs on a host without the companion: the assertion logic is exercised, never claimed real."""
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", _THRESHOLD)
    reports = w.classify_columns(
        monkeypatch, _run, compiled=w.StandInCompiledKernel(), compiled_evidence=w.STAND_IN
    )
    for key in (("w", "cat"), ("w", "pooled"), ("child", "ref_rand"), ("par", "k")):
        assert reports[key].selection == frozenset({w.COMPILED_INDEX})
        assert reports[key].evidence == w.STAND_IN
    assert reports[("w", "cat_py")].evidence == w.REAL


def test_the_reference_kernel_without_a_companion_is_labelled_python_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", _THRESHOLD)
    reports = w.classify_columns(monkeypatch, _run, compiled=None)
    for key in (("w", "cat"), ("w", "pooled"), ("child", "ref_rand"), ("par", "k")):
        assert reports[key].selection == frozenset({w.PYTHON_INDEX_REFERENCE})


def test_the_kernel_result_not_just_the_call_supplied_the_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A skewed kernel result must be exactly what the columns hold (shift 7 mod pool size)."""
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", _THRESHOLD)
    plain = _run()
    with w.end_to_end(monkeypatch, marking=True, kernel_shift=7) as witness:
        skewed = _run()
    assert _summary(witness.classify(skewed)) == CORE_BASELINE
    assert fingerprints(skewed) != fingerprints(plain)


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


def test_core_kernel_pooled_discarded_result_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    def discard(inner: Any) -> Any:
        def select(self: Any, offset: int, size: int) -> list[Any]:
            return [w.strip_mark(v) for v in inner(self, offset, size)]

        return select

    _trips(
        monkeypatch,
        lambda mp: _outer(mp, _faker_pool.PooledSelector, "select", discard),
        match="kernel-selected|marked",
        marking=True,
    )


def test_core_kernel_pooled_recompute_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    def recompute(inner: Any) -> Any:
        def select(self: Any, offset: int, size: int) -> list[Any]:
            picked = inner(self, offset, size)
            return [self._values[(i * 7) % len(self._values)] for i in range(len(picked))]

        return select

    _trips(
        monkeypatch,
        lambda mp: _outer(mp, _faker_pool.PooledSelector, "select", recompute),
        match="kernel-selected",
        marking=True,
    )


def test_core_kernel_called_but_result_ignored_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    """The indexer calls the kernel (so a call counter would pass), then returns its own draw."""

    def ignore(inner: Any) -> Any:
        def draw(self: Any, offset: int, size: int) -> Any:
            real = inner(self, offset, size)
            return (real + self.pool_size // 3 + 1) % self.pool_size

        return draw

    _trips(
        monkeypatch,
        lambda mp: _outer(mp, _kernel_select.PositionalIndexer, "draw", ignore),
        match="kernel-selected",
    )


def test_core_kernel_never_called_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    def bypass(inner: Any) -> Any:
        def draw(self: Any, offset: int, size: int) -> Any:
            import numpy as np

            return np.arange(offset, offset + size, dtype=np.uint64) % self.pool_size

        return draw

    _trips(
        monkeypatch,
        lambda mp: _outer(mp, _kernel_select.PositionalIndexer, "draw", bypass),
        match="no observable backend event",
    )


def test_core_kernel_categorical_discarded_result_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    def discard(inner: Any) -> Any:
        def next_batch(self: Any, offset: int, size: int) -> list[Any]:
            return list(reversed(inner(self, offset, size)))

        return next_batch

    _trips(
        monkeypatch,
        lambda mp: _outer(mp, _column_streams.CategoricalStream, "next_batch", discard),
        match="kernel-selected|python RNG",
    )


def test_core_python_rng_discarded_result_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    """`cat_py` keeps the Python RNG (its weights are declined), and the witness still sees it."""

    def discard(inner: Any) -> Any:
        def next_batch(self: Any, offset: int, size: int) -> list[Any]:
            out = inner(self, offset, size)
            return list(reversed(out)) if self._rng is not None else out  # drawn, then reordered

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
        match="kernel-selected|python RNG selections",
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
            "kernel-selected|pool|selected",
        ),
        (
            {("w", "cat"): ("w", "cat_null"), ("w", "cat_null"): ("w", "cat")},
            "kernel-selected",
        ),
        (
            {("w", "cat_py"): ("child", "ref_py"), ("child", "ref_py"): ("w", "cat_py")},
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
