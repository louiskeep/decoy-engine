"""Phase F F0, deliverable 3.3: the executed-backend witness harness and its controls.

What F3 does with this. F3 routes some generation selection through the compiled kernel. It gates
itself by (1) keeping `assert_matches_oracle` green (values unchanged) and (2) calling
`_f0_witness.classify_columns` on the same fixture and asserting that the routed column moved from
its `TODAY_BASELINE` label to the compiled label, with the marked indices in the final column.
The baseline pinned below is the LEGACY whole-table path (this config holds formula columns, so the
plan is ineligible for the unified core): pooled faker is `numpy_selection`, categorical and
reference are `python_rng_selection`, unpooled faker is `live_provider_generation`, sequence /
derived / derived_aggregate are `no_selection`. It is the proof that the Python reference stays
reachable for non-admitted cells. FR3 moved the unified core's uniform and weighted selections onto
the index kernel; `test_phase_f_fr0_witness.py` pins that baseline.

Two levels, kept apart on purpose:

- end-to-end (`generate_tables`): numpy, python-RNG, live-provider, formula and no-selection are
  all observable here. The compiled / python-reference INDEX kernels are not reachable from
  `generate_tables` today, because the pooled-faker route calls `PoolSampler.sample`
  non-deterministically (a `default_rng` draw), never the deterministic index derivation.
- sampler-level (`PoolSampler.sample(deterministic=True)`) and composite-level
  (`generate_bundle`): where the compiled / python-reference index kernels and the composite's own
  `derive` + modulo are observed.

The route that connects them is `_faker_pool._build_and_sample_returning_pool`, which calls
`PoolSampler.sample(..., deterministic=False)`. An F3 slice that wants compiled selection for
pooled faker re-points exactly that call, and the end-to-end witness is what proves it.

Evidence is labelled: `real` (production code or the real compiled kernel ran) versus `stand_in`
(a pure-Python kernel standing in so the assertion logic is tested where no companion exists).
The stand-in tests run in the no-companion CI mirror; the real-compiled tests skip there.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._index_ext import (
    load_compiled_index_kernel,
    reference_index_derivation,
)
from decoy_engine.generation import _faker_pool, synthesize
from decoy_engine.generation.composite import _name_email, _person
from decoy_engine.generation.pool import _sampler
from decoy_engine.generation.pool._cardinality import CardinalityMode
from decoy_engine.generation.pool._sampler import PoolSampler
from tests.parity import _f0_fixtures as fx
from tests.parity import _f0_witness as w
from tests.parity._f0_canonical import table_fingerprint


def _companion() -> Any | None:
    try:
        return load_compiled_index_kernel()
    except CryptoExtensionUnavailableError:
        return None


needs_companion = pytest.mark.skipif(_companion() is None, reason="needs the native companion")

# --------------------------------------------------------------------------------------------
# end-to-end witness over generate_tables
# --------------------------------------------------------------------------------------------

_THRESHOLD = 150  # lowered so the pooled path runs on a 160-row table; the real one is below.
_ROWS = 160


def _witness_config() -> dict[str, Any]:
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
        {"name": "fm", "type": "formula", "formula": "randint(1, 1000)"},
        {"name": "der", "type": "derived", "expression": "fm + 1"},
        {"name": "agg", "type": "derived_aggregate", "op": "sum", "column": "fm"},
        {
            "name": "rfm",
            "type": "formula",
            "formula": "f'{cat}-{seq}'",
            "references": ["cat", "seq"],
        },
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


_NONE = frozenset({w.NO_SELECTION})
_PYRNG = frozenset({w.PYTHON_RNG})
_NUMPY = frozenset({w.NUMPY_SELECTION})
_LIVE = frozenset({w.LIVE_PROVIDER})
_FORMULA = frozenset({w.FORMULA_EXPRESSION})
_NO_SUB: frozenset[str] = frozenset()

# TODAY_BASELINE: column -> (selection backends, sub-paths). No column is compiled today.
TODAY_BASELINE: dict[tuple[str, str], tuple[frozenset[str], frozenset[str]]] = {
    ("w", "seq"): (_NONE, _NO_SUB),
    ("w", "cat"): (_PYRNG, _NO_SUB),
    ("w", "cat_null"): (_PYRNG, frozenset({w.NULL_MASK})),
    ("w", "pooled"): (_NUMPY, frozenset({w.POOL_CONSTRUCTION})),
    ("w", "pooled_null"): (_NUMPY, frozenset({w.POOL_CONSTRUCTION, w.NULL_MASK})),
    ("w", "live"): (_LIVE, _NO_SUB),
    ("w", "live_optout"): (_LIVE, _NO_SUB),
    ("w", "fm"): (_FORMULA, _NO_SUB),
    ("w", "der"): (_NONE, _NO_SUB),
    ("w", "agg"): (_NONE, _NO_SUB),
    ("w", "rfm"): (_FORMULA, _NO_SUB),
    ("child", "rid"): (_NONE, _NO_SUB),
    ("child", "ref_rand"): (_PYRNG, _NO_SUB),
    ("child", "ref_weighted"): (_PYRNG, _NO_SUB),
    ("par", "k"): (_PYRNG, _NO_SUB),
}


def _run_witness_config() -> dict[str, pa.Table]:
    return fx.run_generate_tables(_witness_config())


def _summary(reports: dict[w.ColumnKey, w.ColumnReport]) -> dict[Any, Any]:
    return {k: (r.selection, r.subpaths) for k, r in reports.items()}


def test_today_baseline_is_observed_per_column(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", _THRESHOLD)
    reports = w.classify_columns(monkeypatch, _run_witness_config)
    assert _summary(reports) == TODAY_BASELINE
    assert all(r.evidence == w.REAL for r in reports.values())


def test_baseline_with_marked_pools_reads_indices_back_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", _THRESHOLD)
    reports = w.classify_columns(monkeypatch, _run_witness_config, marking=True)
    assert _summary(reports) == TODAY_BASELINE


def test_observer_is_transparent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observed (unmarked) output is byte-identical to the unobserved run."""
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", _THRESHOLD)
    plain = _run_witness_config()
    with w.end_to_end(monkeypatch) as witness:
        observed = _run_witness_config()
    assert {k: table_fingerprint(t) for k, t in observed.items()} == {
        k: table_fingerprint(t) for k, t in plain.items()
    }
    witness.classify(observed)


def test_real_threshold_pooled_fixture_is_kernel_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pooled labels above came from a lowered threshold on the legacy path; the REAL
    50_000-row fixture is eligible, so it runs on the unified core and rides the index kernel."""
    reports = w.classify_columns(monkeypatch, lambda: fx.run_generate_tables(fx.cfg_pooled_faker()))
    kernel = w.COMPILED_INDEX if _companion() is not None else w.PYTHON_INDEX_REFERENCE
    assert {k: (r.selection, r.subpaths) for k, r in reports.items()} == {
        ("pooled", c): (frozenset({kernel}), frozenset({w.POOL_CONSTRUCTION}))
        for c in ("city", "last", "street")
    }


def test_live_columns_never_report_pool_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    reports = w.classify_columns(monkeypatch, lambda: fx.run_generate_tables(fx.cfg_live_faker()))
    for report in reports.values():
        assert w.POOL_CONSTRUCTION not in report.subpaths
        assert report.selection == _LIVE


def test_unwitnessed_kinds_are_refused_not_labelled_no_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(w.WitnessError, match="outside the witnessed taxonomy"):
        w.classify_columns(monkeypatch, lambda: fx.run_generate_tables(fx.cfg_grouped_windowed()))


def test_a_selection_appearing_in_a_no_selection_column_is_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sequence` is declared no-selection. If it started drawing from a Python RNG (even one fully
    consumed), the witness refuses to call it no_selection instead of silently relabelling it."""
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", _THRESHOLD)
    with w.end_to_end(monkeypatch) as witness:
        rng_module = synthesize.random

        def drawing_sequence(col: dict[str, Any], n: int) -> list[str]:
            return [str(v) for v in rng_module.Random(3).choices(["x", "y"], k=n)]

        monkeypatch.setattr(synthesize, "_sequence", drawing_sequence)
        tables = _run_witness_config()
    with pytest.raises(w.WitnessError, match="sequence must not select"):
        witness.classify(tables)


# -- mutants: each must trip the witness -----------------------------------------------------


def _install_outer(monkeypatch: pytest.MonkeyPatch, target: Any, name: str, replace: Any) -> None:
    """Wrap `target.name` AFTER the witness hooks so the witness sees the real call first."""
    inner = getattr(target, name)
    monkeypatch.setattr(target, name, replace(inner))


def _trips_witness(
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
        tables = _run_witness_config()
    with pytest.raises(w.WitnessError, match=match):
        witness.classify(tables)


def _discard_numpy(inner: Any) -> Any:
    """Run the sampler (the witness records its marked result), then discard it and return the same
    values with the marks stripped, as if recomputed elsewhere."""

    def sample(self: PoolSampler, pool: Any, n: int, **kw: Any) -> pd.Series:
        marked = inner(self, pool, n, **kw)
        return pd.Series([w.strip_mark(v) for v in marked.tolist()])

    return sample


def _recompute_numpy(inner: Any) -> Any:
    """Ignore the sampler result; select with a different RNG from the same marked pool."""

    def sample(self: PoolSampler, pool: Any, n: int, **kw: Any) -> pd.Series:
        inner(self, pool, n, **kw)
        idx = np.random.default_rng(12345).integers(0, pool.size, size=n)
        return pd.Series(pool.values[idx])

    return sample


def test_numpy_discarded_result_trips_the_witness(monkeypatch: pytest.MonkeyPatch) -> None:
    _trips_witness(
        monkeypatch,
        lambda mp: _install_outer(mp, PoolSampler, "sample", _discard_numpy),
        match="numpy-selected",
        marking=True,
    )


def test_numpy_recompute_trips_the_witness(monkeypatch: pytest.MonkeyPatch) -> None:
    _trips_witness(
        monkeypatch,
        lambda mp: _install_outer(mp, PoolSampler, "sample", _recompute_numpy),
        match="numpy-selected",
        marking=True,
    )


def test_wrong_column_attribution_trips_the_witness(monkeypatch: pytest.MonkeyPatch) -> None:
    swap = {("w", "pooled"): ("w", "pooled_null"), ("w", "pooled_null"): ("w", "pooled")}
    _trips_witness(
        monkeypatch,
        lambda mp: None,
        match="numpy|pool|selected",
        attribution=lambda key: swap.get(key, key),
    )


def test_python_rng_discarded_result_trips_the_witness(monkeypatch: pytest.MonkeyPatch) -> None:
    def discard(inner: Any) -> Any:
        def categorical(col: dict[str, Any], n: int, *a: Any, **k: Any) -> list[Any]:
            return list(reversed(inner(col, n, *a, **k)))  # the draw ran; its order was dropped

        return categorical

    _trips_witness(
        monkeypatch,
        lambda mp: _install_outer(mp, synthesize, "_categorical", discard),
        match="python RNG selections",
    )


def test_python_rng_wrong_attribution_trips_the_witness(monkeypatch: pytest.MonkeyPatch) -> None:
    swap = {("w", "cat"): ("w", "cat_null"), ("w", "cat_null"): ("w", "cat")}
    _trips_witness(
        monkeypatch,
        lambda mp: None,
        match="python RNG selections",
        attribution=lambda k: swap.get(k, k),
    )


def test_live_provider_discarded_result_trips_the_witness(monkeypatch: pytest.MonkeyPatch) -> None:
    def discard(inner: Any) -> Any:
        def faker(col: dict[str, Any], n: int, *a: Any, **k: Any) -> list[Any]:
            out = inner(col, n, *a, **k)
            return ["constant"] * n if col["name"] == "live" else list(out)

        return faker

    _trips_witness(
        monkeypatch,
        lambda mp: _install_outer(mp, synthesize, "_faker", discard),
        match="live provider results",
    )


def test_live_provider_wrong_attribution_trips_the_witness(monkeypatch: pytest.MonkeyPatch) -> None:
    swap = {("w", "live"): ("w", "live_optout"), ("w", "live_optout"): ("w", "live")}
    _trips_witness(
        monkeypatch,
        lambda mp: None,
        match="live provider results",
        attribution=lambda k: swap.get(k, k),
    )


def test_formula_recompute_trips_the_witness(monkeypatch: pytest.MonkeyPatch) -> None:
    def recompute(inner: Any) -> Any:
        def formula(col: dict[str, Any], n: int, *a: Any, **k: Any) -> list[Any]:
            inner(col, n, *a, **k)
            return [0] * n if not col.get("references") else [None] * n

        return formula

    _trips_witness(
        monkeypatch,
        lambda mp: _install_outer(mp, synthesize, "_formula", recompute),
        match="formula results",
    )


def test_hidden_selection_in_a_no_selection_column_trips_the_witness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`derived_aggregate` is declared no-selection; a draw it never consumes is rejected."""

    def mutate(mp: pytest.MonkeyPatch) -> None:
        inner = synthesize.generate_derived_aggregate_column

        def leaky(col: dict[str, Any], n: int, generated: Any) -> list[Any]:
            synthesize.random.Random(1).choices([1, 2], k=3)  # looked up now: the witness proxy
            return list(inner(col, n, generated))

        mp.setattr(synthesize, "generate_derived_aggregate_column", leaky)

    _trips_witness(monkeypatch, mutate, match="python RNG selections|must not select")


# --------------------------------------------------------------------------------------------
# sampler-level witness: compiled / python-reference index kernels
# --------------------------------------------------------------------------------------------

_SEED = b"\x01" * 8
_NS = "f0.ns"


def _int_source() -> pd.Series:
    return pd.Series([11, 12, None, 14, 11, 99, None, 7], dtype=object)


def _date_source() -> pd.Series:
    import datetime

    return pd.Series(
        [datetime.date(2024, 1, d) if d % 4 else None for d in range(1, 9)], dtype=object
    )


def _sampler_run(
    mp: pytest.MonkeyPatch,
    *,
    compiled: Any | None,
    evidence: str,
    source: pd.Series,
    shift: int = w.MARK_SHIFT,
) -> tuple[w.SamplerWitness, Any, pd.Series]:
    witness = w.SamplerWitness(mp, compiled=compiled, compiled_evidence=evidence, shift=shift)
    pool = w.marked_value_pool()
    out = witness.sample(pool, source, _NS, _SEED)
    return witness, pool, out


def test_stand_in_compiled_kernel_is_labelled_stand_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assertion logic WITHOUT the companion: a marked pure-Python stand-in kernel."""
    witness, pool, out = _sampler_run(
        monkeypatch, compiled=w.StandInCompiledKernel(), evidence=w.STAND_IN, source=_int_source()
    )
    report = witness.classify(pool, out)
    assert (report.label, report.evidence) == (w.COMPILED_INDEX, w.STAND_IN)


def test_stand_in_declined_value_class_falls_to_the_python_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    witness, pool, out = _sampler_run(
        monkeypatch, compiled=w.StandInCompiledKernel(), evidence=w.STAND_IN, source=_date_source()
    )
    report = witness.classify(pool, out)
    assert (report.label, report.evidence) == (w.PYTHON_INDEX_REFERENCE, w.REAL)
    assert any(e.payload.get("declined") == "native_type_not_admitted" for e in report.events)


def test_no_companion_routes_every_source_to_the_python_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for source in (_int_source(), _date_source()):
        witness, pool, out = _sampler_run(
            monkeypatch, compiled=None, evidence=w.REAL, source=source
        )
        assert witness.classify(pool, out).label == w.PYTHON_INDEX_REFERENCE


def test_null_source_positions_stay_null(monkeypatch: pytest.MonkeyPatch) -> None:
    witness, pool, out = _sampler_run(
        monkeypatch, compiled=w.StandInCompiledKernel(), evidence=w.STAND_IN, source=_int_source()
    )
    witness.classify(pool, out)
    assert [v is pd.NA for v in out.tolist()] == [
        v is None or v != v for v in _int_source().tolist()
    ]


def _mutated_sampler_trip(
    monkeypatch: pytest.MonkeyPatch, replace: Any, *, compiled: Any, evidence: str
) -> None:
    witness = w.SamplerWitness(monkeypatch, compiled=compiled, compiled_evidence=evidence)
    inner = _sampler._derive_pool_indices
    monkeypatch.setattr(_sampler, "_derive_pool_indices", replace(inner))
    pool = w.marked_value_pool()
    out = witness.sample(pool, _int_source(), _NS, _SEED)
    with pytest.raises(w.WitnessError, match="marked result"):
        witness.classify(pool, out)


def _discard_indices(inner: Any) -> Any:
    def derive(nonnull_source: pd.Series, **kw: Any) -> Any:
        inner(nonnull_source, **kw)  # the kernel runs and is recorded ...
        return np.zeros(len(nonnull_source), dtype=np.uint64)  # ... but its result is dropped

    return derive


def _recompute_indices(inner: Any) -> Any:
    def derive(nonnull_source: pd.Series, *, seed: bytes, namespace: str, pool_size: int) -> Any:
        inner(nonnull_source, seed=seed, namespace=namespace, pool_size=pool_size)
        fresh = reference_index_derivation().derive_index_batch(
            nonnull_source.tolist(), mask_key=seed, namespace=namespace, pool_size=pool_size
        )
        return fresh.to_numpy(zero_copy_only=False)  # recomputed unmarked, kernel result unused

    return derive


@pytest.mark.parametrize(
    "replace", [_discard_indices, _recompute_indices], ids=["discard", "recompute"]
)
def test_stand_in_mutants_trip_the_sampler_witness(
    monkeypatch: pytest.MonkeyPatch, replace: Any
) -> None:
    _mutated_sampler_trip(
        monkeypatch, replace, compiled=w.StandInCompiledKernel(), evidence=w.STAND_IN
    )


def test_sampler_wrong_attribution_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    witness = w.SamplerWitness(
        monkeypatch, compiled=w.StandInCompiledKernel(), compiled_evidence=w.STAND_IN
    )
    pool = w.marked_value_pool()
    out_a = witness.sample(pool, _int_source(), _NS, _SEED)
    events_a = list(witness.events)
    witness.sample(pool, _date_source(), _NS, _SEED)
    events_b = list(witness.events)
    assert witness.classify(pool, out_a, events_a).label == w.COMPILED_INDEX
    with pytest.raises(w.WitnessError, match="marked result"):
        witness.classify(pool, out_a, events_b)  # column A attributed to column B's kernel run


def test_marking_kernel_is_transparent_at_zero_shift(monkeypatch: pytest.MonkeyPatch) -> None:
    """With shift 0 the wrapped path returns exactly what the unwrapped sampler returns."""
    pool = w.marked_value_pool()
    unwrapped = PoolSampler().sample(
        pool,
        len(_int_source()),
        mode=CardinalityMode.REUSE,
        seed=_SEED,
        source=_int_source(),
        namespace=_NS,
        deterministic=True,
    )
    _, _, wrapped = _sampler_run(
        monkeypatch,
        compiled=w.StandInCompiledKernel(),
        evidence=w.STAND_IN,
        source=_int_source(),
        shift=0,
    )
    assert wrapped.tolist() == unwrapped.tolist()


# -- the real compiled kernel (companion venv only) ------------------------------------------


@needs_companion
def test_real_compiled_kernel_executes_and_its_marked_result_is_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = _companion()
    witness, pool, out = _sampler_run(
        monkeypatch, compiled=compiled, evidence=w.REAL, source=_int_source()
    )
    report = witness.classify(pool, out)
    assert (report.label, report.evidence) == (w.COMPILED_INDEX, w.REAL)
    (executed,) = [
        e for e in report.events if e.label == w.COMPILED_INDEX and "result" in e.payload
    ]
    # The real kernel's own (unshifted) indices equal the python reference derivation.
    ref = reference_index_derivation().derive_index_batch(
        [v for v in _int_source().tolist() if v == v and v is not None],
        mask_key=_SEED,
        namespace=_NS,
        pool_size=pool.size,
    )
    assert executed.payload["unshifted"] == ref.to_pylist()


@needs_companion
def test_real_compiled_kernel_declines_dates_and_the_reference_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    witness, pool, out = _sampler_run(
        monkeypatch, compiled=_companion(), evidence=w.REAL, source=_date_source()
    )
    report = witness.classify(pool, out)
    assert report.label == w.PYTHON_INDEX_REFERENCE
    assert any(e.label == w.COMPILED_INDEX and "declined" in e.payload for e in report.events)


@needs_companion
@pytest.mark.parametrize(
    "replace", [_discard_indices, _recompute_indices], ids=["discard", "recompute"]
)
def test_real_compiled_mutants_trip_the_sampler_witness(
    monkeypatch: pytest.MonkeyPatch, replace: Any
) -> None:
    _mutated_sampler_trip(monkeypatch, replace, compiled=_companion(), evidence=w.REAL)


@needs_companion
def test_unwrapped_production_sampler_really_uses_the_compiled_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No marking at all: a counting wrapper on the production loader shows the compiled kernel is
    what `_derive_pool_indices` calls for an admitted source when the companion is installed."""
    calls: list[str] = []
    real = _sampler._compiled_index_kernel()
    assert real is not None
    kernel = real

    class Counting:
        def derive_index_batch(self, values: Any, **kw: Any) -> Any:
            calls.append("compiled")
            return kernel.derive_index_batch(values, **kw)

    monkeypatch.setattr(_sampler, "_compiled_index_kernel", lambda: Counting())
    PoolSampler().sample(
        w.marked_value_pool(),
        len(_int_source()),
        mode=CardinalityMode.REUSE,
        seed=_SEED,
        source=_int_source(),
        namespace=_NS,
        deterministic=True,
    )
    assert calls == ["compiled"]


# --------------------------------------------------------------------------------------------
# composite witness: direct derive + modulo
# --------------------------------------------------------------------------------------------

_COMPOSITES = [
    pytest.param(
        _person, _person.CompositePerson, "_pools", w.CompositeWitness.PERSON_SLICES, id="person"
    ),
    pytest.param(
        _name_email,
        _name_email.CompositeNameEmail,
        "_name_pools",
        w.CompositeWitness.NAME_EMAIL_SLICES,
        id="name_email",
    ),
]


def _composite_run(
    monkeypatch: pytest.MonkeyPatch,
    module: Any = _person,
    cls: Any = _person.CompositePerson,
    pools_method: str = "_pools",
    slices: dict[str, tuple[int, int]] | None = None,
) -> tuple[w.CompositeWitness, dict[str, pd.Series]]:
    witness = w.CompositeWitness(monkeypatch, module, cls, pools_method=pools_method, slices=slices)
    gen = cls(coherent_namespace="f0_ns", pool_size=64)
    source = pd.Series(["a", "b", None, "c", "a"])
    spec = fx._composite_spec(deterministic=True)
    return witness, gen.generate_bundle(spec, len(source), source=source, deterministic=True)


@pytest.mark.parametrize(("module", "cls", "pools_method", "slices"), _COMPOSITES)
def test_composite_derivation_is_observed_and_bypasses_the_index_kernel(
    monkeypatch: pytest.MonkeyPatch, module: Any, cls: Any, pools_method: str, slices: Any
) -> None:
    witness, bundle = _composite_run(monkeypatch, module, cls, pools_method, slices)
    report = witness.classify(bundle)
    assert report.label == w.COMPOSITE_DERIVE
    assert (report.index_kernel_calls, report.derive_calls) == (0, 4)


@pytest.mark.parametrize(("module", "cls", "pools_method", "slices"), _COMPOSITES)
def test_composite_discarded_derive_trips(
    monkeypatch: pytest.MonkeyPatch, module: Any, cls: Any, pools_method: str, slices: Any
) -> None:
    witness = w.CompositeWitness(monkeypatch, module, cls, pools_method=pools_method, slices=slices)
    inner = module.derive

    def other_latent(*a: Any, **kw: Any) -> bytes:
        return bytes(reversed(inner(*a, **kw)))  # the recorded latent is real; the one used differs

    monkeypatch.setattr(module, "derive", other_latent)
    gen = cls(coherent_namespace="f0_ns", pool_size=64)
    source = pd.Series(["a", "b", None, "c", "a"])
    bundle = gen.generate_bundle(
        fx._composite_spec(deterministic=True), 5, source=source, deterministic=True
    )
    with pytest.raises(w.WitnessError, match="not the recorded derive"):
        witness.classify(bundle)


def test_composite_wrong_attribution_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    witness, bundle = _composite_run(monkeypatch)
    swap = {"first_name": "last_name", "last_name": "first_name", "dob": "dob"}
    with pytest.raises(w.WitnessError, match="not the recorded derive"):
        witness.classify(bundle, attribution=lambda c: swap[c])


def test_composite_recompute_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    witness, bundle = _composite_run(monkeypatch)
    stale = [bytes(32)] * len(witness.derive_events)  # a recompute that ignores derive()'s output
    with pytest.raises(w.WitnessError, match="not the recorded derive"):
        witness.classify(bundle, latents=stale)


def test_composite_shared_index_kernel_use_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    witness, bundle = _composite_run(monkeypatch)
    witness.index_kernel_calls = 1  # as if the composite had been re-pointed at `_sampler`
    with pytest.raises(w.WitnessError, match="shared index kernel"):
        witness.classify(bundle)
