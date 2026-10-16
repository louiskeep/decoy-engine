"""Phase F F0, deliverable 3.2 (part 2): the invariants F1 (bounded generation) must preserve.

Each invariant is a CHECK FUNCTION that raises AssertionError when the property is violated. Each
check is then run twice:

- clean, against today's code, where it must pass; and
- mutated through the production seam it guards (pytest `monkeypatch` on the same module
  attribute the production caller looks up; no `src/` change), where it MUST raise. A mutation that
  did not trip its check would mean the check is vacuous; `_trips` makes that a test failure.

The invariants (brief section 3.2):

1. Pool admission is a function of the individual TABLE's `row_count` (`_faker_pool.py:239`,
   `pool_eligible(..., n, ...)` called from `try_pool` with the table's `n`): not a job-wide sum
   and not a per-batch count.
2. Pool IDENTITY / build / selection is bound to GENERATION's seam
   (`_faker_pool._build_and_sample_returning_pool` + `GenDeriveContext` family-derived seeds).
   The masking resolver `pool/_identity.py:resolve_faker_pool_identity` is NOT consulted by
   `generate_tables` and is out of scope here (it is a separate masking contract).
3. Parent-pool ordering and cardinality repair for `reference`; post-pass ordering for
   referenced `formula`.
4. RNG progression and row ordinals are a function of (seed, GLOBAL row position), not of a batch
   start. Every column family but two is prefix-stable; the two that are not are whole-table by
   definition (`derived_aggregate`, and `reference` with min/max cardinality repair).
5. A generated parent reaches the masked child complete (see the `mixed_fk_generated_parent`
   fixture, through `run_pipeline`).

For F1: invariant 4's `naive_batch_local` is exactly what a first-draft partitioned generator does
(restart each batch at row 0). The test shows the oracle catches it, so F1 can use
`assert_matches_oracle` as its gate.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from functools import partial
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from decoy_engine.generation import _faker_pool, _referenced_formula, synthesize
from decoy_engine.generation import _plan_entry as plan_entry
from decoy_engine.generation.pool._sampler import PoolSampler
from decoy_engine.generators.columns import ColumnGenerator
from decoy_engine.generators.derivation import GenDeriveContext
from tests.parity import _f0_fixtures as fx
from tests.parity._f0_canonical import assert_matches_oracle
from tests.parity._f0_expected import POOL_SEAM
from tests.parity._f0_seams import PoolSeamLog, observe_pool_seam

# --------------------------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------------------------


def _oracle_run(fid: str) -> None:
    assert_matches_oracle(fid, fx.FIXTURES_BY_ID[fid].run())


def _trips(
    check: Callable[[], Any],
    *,
    match: str,
    expected: tuple[type[BaseException], ...] = (AssertionError,),
) -> None:
    """The mutated run must violate the invariant, for the stated reason. A pass means the check is
    vacuous; an unrelated exception or message means the mutation broke something else."""
    with pytest.raises(expected, match=match):
        check()


def _city(name: str = "c", **extra: Any) -> dict[str, Any]:
    return {"name": name, "type": "faker", "faker_type": "city", **extra}


def _column(cfg_tables: list[dict[str, Any]], table: str, column: str, **kw: Any) -> list[Any]:
    out = fx.run_generate_tables(fx.config(cfg_tables), **kw)
    return out[table].column(column).to_pylist()


# --------------------------------------------------------------------------------------------
# 1. Pool admission = the table's own row_count
# --------------------------------------------------------------------------------------------

# Thresholds are lowered so the invariant runs in milliseconds; the SAME `pool_eligible` /
# `try_pool` code runs, and the real threshold is pinned separately (N_THRESHOLD == 50_000 in the
# oracle test, where the 50_000-row pooled fixture takes the real path).
_THRESHOLD = 120
_ADMISSION_TABLES = {"small_a": 80, "small_b": 80, "big": 150}  # job-wide sum 310 >= 120


def _admission_config(*, pooled: bool | None) -> dict[str, Any]:
    extra: dict[str, Any] = {} if pooled is None else {"pooled": pooled}
    return fx.config(
        [fx.table(name, rows, [_city("c", **extra)]) for name, rows in _ADMISSION_TABLES.items()]
    )


def _admission_observation(
    mp: pytest.MonkeyPatch, mutate: Callable[[pytest.MonkeyPatch], None] | None = None
) -> tuple[PoolSeamLog, dict[str, pa.Table], dict[str, pa.Table]]:
    mp.setattr(_faker_pool, "N_THRESHOLD", _THRESHOLD)
    # Live reference first, before any mutation: `pooled: False` forces the per-row path and does
    # not change the seed root (the fingerprint excludes it), so it is the per-row truth.
    live_reference = fx.run_generate_tables(_admission_config(pooled=False))
    if mutate is not None:
        mutate(mp)
    with observe_pool_seam(mp) as log:
        actual = fx.run_generate_tables(_admission_config(pooled=None))
    return log, actual, live_reference


def _check_admission_on_table_total(
    log: PoolSeamLog, actual: dict[str, pa.Table], live_reference: dict[str, pa.Table]
) -> None:
    assert [c.n for c in log.calls] == [_ADMISSION_TABLES["big"]], (
        f"expected exactly one pool build, for the 150-row table; got n={[c.n for c in log.calls]}"
    )
    for name in ("small_a", "small_b"):
        assert actual[name].equals(live_reference[name]), f"{name} must stay on the per-row path"
    assert not actual["big"].equals(live_reference["big"]), "the 150-row table must be pooled"
    assert actual["big"].column("c").to_pylist() == log.calls[0].sampled


def test_admission_is_decided_on_each_tables_own_row_count(monkeypatch: pytest.MonkeyPatch) -> None:
    _check_admission_on_table_total(*_admission_observation(monkeypatch))


def _wrap_admission(mp: pytest.MonkeyPatch, rewrite_n: Callable[[int], int]) -> None:
    real = _faker_pool.pool_eligible

    def wrapped(faker_type: str, requested_locale: Any, n: int, *, opted_out: bool) -> bool:
        return real(faker_type, requested_locale, rewrite_n(n), opted_out=opted_out)

    mp.setattr(_faker_pool, "pool_eligible", wrapped)


def test_job_wide_sum_admission_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    total = sum(_ADMISSION_TABLES.values())
    log, actual, live = _admission_observation(
        monkeypatch, lambda mp: _wrap_admission(mp, lambda n: total)
    )
    _trips(
        lambda: _check_admission_on_table_total(log, actual, live),
        match="expected exactly one pool build",
    )


def test_per_batch_admission_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    log, actual, live = _admission_observation(
        monkeypatch, lambda mp: _wrap_admission(mp, lambda n: n // 4)
    )
    _trips(
        lambda: _check_admission_on_table_total(log, actual, live),
        match="expected exactly one pool build",
    )


def test_admission_threshold_edges_use_the_table_total() -> None:
    assert _faker_pool.N_THRESHOLD == 50_000
    assert not _faker_pool.pool_eligible("city", None, 49_999, opted_out=False)
    assert _faker_pool.pool_eligible("city", None, 50_000, opted_out=False)
    assert not _faker_pool.pool_eligible("city", None, 50_000, opted_out=True)
    # An ineligible type never pools however large the table is.
    assert not _faker_pool.pool_eligible("email", None, 10**7, opted_out=False)
    # A certified (type, locale) pair pools only in its certified locale.
    assert _faker_pool.pool_eligible("street_name", "de_DE", 50_000, opted_out=False)
    assert not _faker_pool.pool_eligible("street_name", "ja_JP", 50_000, opted_out=False)


# --------------------------------------------------------------------------------------------
# 2. Pool identity / build / selection through generation's seam
# --------------------------------------------------------------------------------------------


def _pooled_log(
    mp: pytest.MonkeyPatch, mutate: Callable[[pytest.MonkeyPatch], None] | None = None
) -> PoolSeamLog:
    if mutate is not None:
        mutate(mp)  # applied BEFORE the observer so the observer wraps the mutated callable
    with observe_pool_seam(mp) as log:
        fx.run_generate_tables(fx.cfg_pooled_faker())
    return log


def _check_pool_identity(log: PoolSeamLog) -> None:
    assert [c.signature() for c in log.calls] == POOL_SEAM, "pool identity/build/selection moved"
    for call in log.calls:
        assert call.build_seed != call.selection_seed, "build and selection seeds must be disjoint"
        assert call.pool_size == 10_000
        assert call.pool_values is not None and len(call.pool_values) == call.pool_size
        assert call.sampled is not None
        picked = np.array(call.pool_values, dtype=object)[call.selection_indices()].tolist()
        assert call.sampled == picked, "selection is not default_rng(selection_seed).integers"
    assert log.build_pool_value_calls == len(POOL_SEAM) == 3


def test_pool_identity_matches_the_checked_in_generation_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _check_pool_identity(_pooled_log(monkeypatch))


def test_reordered_pool_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    real = _faker_pool.build_pool_values

    def reordered(*a: Any, **k: Any) -> Any:
        values, exact, custom = real(*a, **k)
        return list(reversed(values)), exact, custom

    log = _pooled_log(
        monkeypatch, lambda mp: mp.setattr(_faker_pool, "build_pool_values", reordered)
    )
    _trips(lambda: _check_pool_identity(log), match="pool identity/build/selection moved")


def test_swapped_build_and_selection_seeds_are_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    real = _faker_pool._build_and_sample_returning_pool

    def swapped(**kw: Any) -> Any:
        kw["build_seed"], kw["selection_seed"] = kw["selection_seed"], kw["build_seed"]
        return real(**kw)

    log = _pooled_log(
        monkeypatch, lambda mp: mp.setattr(_faker_pool, "_build_and_sample_returning_pool", swapped)
    )
    _trips(lambda: _check_pool_identity(log), match="pool identity/build/selection moved")


def test_resized_pool_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    real = _faker_pool._build_and_sample_returning_pool

    def resized(**kw: Any) -> Any:
        kw["pool_size"] = 5_000
        return real(**kw)

    log = _pooled_log(
        monkeypatch, lambda mp: mp.setattr(_faker_pool, "_build_and_sample_returning_pool", resized)
    )
    _trips(lambda: _check_pool_identity(log), match="pool identity/build/selection moved")


def _seam(mp: pytest.MonkeyPatch, cols: list[dict[str, Any]], **kw: Any) -> PoolSeamLog:
    mp.setattr(_faker_pool, "N_THRESHOLD", _THRESHOLD)
    with observe_pool_seam(mp) as log:
        fx.run_generate_tables(fx.config([fx.table("big", 150, cols)]), **kw)
    return log


def test_generation_does_not_consult_the_masking_pool_identity_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pool/_identity.py:resolve_faker_pool_identity` is the MASKING pool identity. Generation's
    identity is `_build_and_sample_returning_pool` + `GenDeriveContext`, so making the masking
    resolver explode must not touch a generated byte. (Mutating it is therefore not a valid
    generation mutation, which is why no invariant above does.)"""
    from decoy_engine.generation.pool import _identity

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("generation consulted the masking pool-identity resolver")

    monkeypatch.setattr(_identity, "resolve_faker_pool_identity", boom)
    log = _seam(monkeypatch, [_city()])
    assert len(log.calls) == 1


def test_locale_and_provider_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Effective locale: column locale, else instance default, else Faker default (None)."""
    assert _seam(monkeypatch, [_city()]).calls[0].effective_locale is None
    assert (
        _seam(monkeypatch, [_city()], instance_default_locale="fr_FR").calls[0].effective_locale
        == "fr_FR"
    )
    assert _seam(monkeypatch, [_city(locale="de_DE")]).calls[0].effective_locale == "de_DE"
    both = _seam(monkeypatch, [_city(locale="de_DE")], instance_default_locale="fr_FR")
    assert both.calls[0].effective_locale == "de_DE"
    # Provider resolution: a certified (type, locale) pair pools, an uncertified locale and an
    # explicit opt-out stay per-row (no pool call at all).
    street = {"name": "c", "type": "faker", "faker_type": "street_name"}
    assert len(_seam(monkeypatch, [{**street, "locale": "de_DE"}]).calls) == 1
    assert len(_seam(monkeypatch, [{**street, "locale": "ja_JP"}]).calls) == 0
    assert len(_seam(monkeypatch, [_city(pooled=False)]).calls) == 0


def test_non_dict_faker_kwargs_are_dropped_before_the_pool_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _seam(monkeypatch, [_city(faker_kwargs="not-a-dict")])
    assert log.calls[0].faker_kwargs == {}


def test_seeds_follow_column_config_not_column_name_or_instance_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _seam(monkeypatch, [_city()]).calls[0]
    renamed = _seam(monkeypatch, [_city("renamed")]).calls[0]
    assert (renamed.build_seed, renamed.selection_seed) == (base.build_seed, base.selection_seed)
    # The instance default locale changes the pool's locale, not the seed root.
    inst = _seam(monkeypatch, [_city()], instance_default_locale="fr_FR").calls[0]
    assert inst.build_seed == base.build_seed
    assert inst.pool_digest() != base.pool_digest()
    # A different strategy config (faker_type, column locale, job seed) is a different root.
    other_type = _seam(
        monkeypatch, [{"name": "c", "type": "faker", "faker_type": "last_name"}]
    ).calls[0]
    other_locale = _seam(monkeypatch, [_city(locale="de_DE")]).calls[0]
    assert other_type.build_seed != base.build_seed
    assert other_locale.build_seed != base.build_seed
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", _THRESHOLD)
    with observe_pool_seam(monkeypatch) as log:
        fx.run_generate_tables(fx.config([fx.table("big", 150, [_city()])], seed=1))
    assert log.calls[0].build_seed != base.build_seed


# --------------------------------------------------------------------------------------------
# 3. Parent-pool ordering + repair, and referenced-formula post-pass ordering
# --------------------------------------------------------------------------------------------


def _check_reference_contract(tables: dict[str, pa.Table]) -> None:
    parent = tables["ref_parent"].column("pkey").to_pylist()
    ref_vals = list(dict.fromkeys(v for v in parent if v is not None))  # first-seen, null-dropped
    child = tables["ref_child"]
    seq = child.column("r_seq").to_pylist()
    assert seq == [ref_vals[i % len(ref_vals)] for i in range(len(seq))], "parent pool order moved"
    for col in ("r_random", "r_seq", "r_weighted", "r_bounded"):
        assert set(child.column(col).to_pylist()) <= set(ref_vals), f"{col} escaped the parent pool"
    counts = Counter(child.column("r_bounded").to_pylist())
    assert all(2 <= counts[v] <= 4 for v in ref_vals), f"cardinality repair moved: {dict(counts)}"
    assert_matches_oracle("reference", tables)


def test_reference_parent_pool_order_and_repair() -> None:
    _check_reference_contract(fx.run_generate_tables(fx.cfg_reference()))


def test_sorted_parent_pool_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    real = synthesize._reference

    def sorted_pool(
        col: dict[str, Any], n: int, seed: int, derive_key: Any = None, pools: Any = None
    ) -> Any:
        pools = dict(pools or {})
        parent = pools[col["reference_table"]]
        pools[col["reference_table"]] = parent.sort_by(col["reference_column"])
        return real(col, n, seed, derive_key, pools)

    monkeypatch.setattr(synthesize, "_reference", sorted_pool)
    _trips(
        lambda: _check_reference_contract(fx.run_generate_tables(fx.cfg_reference())),
        match="parent pool order moved",
    )


def test_skipped_cardinality_repair_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_repair(self: ColumnGenerator, values: Any, *a: Any, **k: Any) -> Any:
        return values

    monkeypatch.setattr(ColumnGenerator, "_apply_cardinality_bounds", no_repair)
    _trips(
        lambda: _check_reference_contract(fx.run_generate_tables(fx.cfg_reference())),
        match="cardinality repair moved",
    )


def test_child_before_parent_order_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """The config declares the child FIRST; the topological sort must put the parent ahead."""
    monkeypatch.setattr(synthesize, "_topo_sort", lambda deps: list(deps))
    _trips(
        lambda: _check_reference_contract(fx.run_generate_tables(fx.cfg_reference())),
        match="ref_parent",
        expected=(KeyError,),
    )


def _check_postpass_contract(tables: dict[str, pa.Table]) -> None:
    t = tables["rf"].to_pydict()
    for i in range(len(t["pid"])):
        assert t["rf_a"][i] is not None, "the post-pass did not fill rf_a"
        assert t["rf_a"][i].startswith(f"{t['grp'][i]}-{t['pid'][i]}-"), "rf_a lost its inputs"
        if t["rf_b"][i] is not None:  # null_probability applied over the finished value
            assert t["rf_b"][i].startswith(t["rf_a"][i] + "/b"), (
                "rf_b did not read the finished rf_a"
            )
        assert t["rf_d"][i] == f"d{i}"
        # rf_c reads rf_d, which is declared AFTER it: it sees the None placeholder as "".
        assert t["rf_c"][i] == "[]", "a forward reference must see the placeholder"
    assert_matches_oracle("referenced_formula", tables)


def test_referenced_formula_postpass_order() -> None:
    _check_postpass_contract(fx.run_generate_tables(fx.cfg_referenced_formula()))


def test_reversed_postpass_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    real = _referenced_formula.fill_referenced_formula_columns

    def reversed_order(
        gcols: list[dict[str, Any]], data: Any, seed: int, derive_key: Any = None
    ) -> None:
        real(list(reversed(gcols)), data, seed, derive_key)

    monkeypatch.setattr(_referenced_formula, "fill_referenced_formula_columns", reversed_order)
    _trips(
        lambda: _check_postpass_contract(fx.run_generate_tables(fx.cfg_referenced_formula())),
        match="rf_b did not read",
    )


def test_skipped_postpass_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _referenced_formula, "fill_referenced_formula_columns", lambda *a, **k: None
    )
    _trips(
        lambda: _check_postpass_contract(fx.run_generate_tables(fx.cfg_referenced_formula())),
        match="did not fill rf_a",
    )


# --------------------------------------------------------------------------------------------
# 4. RNG progression / row ordinals = f(seed, GLOBAL row position)
# --------------------------------------------------------------------------------------------

_P = {"name": "p", "type": "sequence", "start": 1}
_G = {"name": "g", "type": "categorical", "categories": ["a", "b"]}
_O = {"name": "o", "type": "formula", "formula": "i"}
_SNAP = str(fx.SNAPSHOT_PATH)
_PARENT_TABLE = fx.table(
    "par",
    10,
    [{"name": "k", "type": "categorical", "categories": list("abcdef"), "null_probability": 0.2}],
)


def _ref(dist: str, n: int, **extra: Any) -> tuple[list[dict[str, Any]], str]:
    child = fx.table(
        "t",
        n,
        [
            {
                "name": "c",
                "type": "reference",
                "reference_table": "par",
                "reference_column": "k",
                "distribution": dist,
                **extra,
            }
        ],
    )
    return [child, _PARENT_TABLE], "t"


# family -> (builder(n) -> (tables, table name, column name))
def _family(name: str, n: int) -> tuple[list[dict[str, Any]], str]:
    cols: dict[str, list[dict[str, Any]]] = {
        "sequence": [{"name": "c", "type": "sequence", "start": 5, "step": 2}],
        "categorical": [
            {
                "name": "c",
                "type": "categorical",
                "categories": list("abcd"),
                "weights": [4, 3, 2, 1],
            }
        ],
        "live_faker": [_city()],
        "formula": [{"name": "c", "type": "formula", "formula": "randint(1, 999)"}],
        "null_mask": [{"name": "c", "type": "sequence", "start": 1, "null_probability": 0.4}],
        "referenced_formula": [
            _P,
            {
                "name": "c",
                "type": "formula",
                "formula": "f'{p}-{randint(1, 9)}'",
                "references": ["p"],
            },
        ],
        "grouped_cumcount": [
            _G,
            _O,
            {"name": "c", "type": "grouped_series", "group_by": "g", "order_by": "o"},
        ],
        "grouped_walk": [
            _G,
            _O,
            {
                "name": "c",
                "type": "grouped_series",
                "group_by": "g",
                "order_by": "o",
                "generator": "monotone_walk",
                "start": 1,
                "max_step": 5,
            },
        ],
        "group_key": [_G, {"name": "c", "type": "group_key", "group_by": "g"}],
        "statistical": [{"name": "c", "type": "statistical", "snapshot_file": _SNAP}],
        "derived_aggregate": [
            {"name": "v", "type": "formula", "formula": "i"},
            {"name": "c", "type": "derived_aggregate", "op": "sum", "column": "v"},
        ],
    }
    if name == "statistical":
        cols["statistical"][0]["name"] = "amount"
    return [fx.table("t", n, cols[name])], ("amount" if name == "statistical" else "c")


def _generate_family(name: str, n: int) -> list[Any]:
    tables, col = _family(name, n)
    return fx.run_generate_tables(fx.config(tables))["t"].column(col).to_pylist()


PREFIX_STABLE = (
    "sequence",
    "categorical",
    "live_faker",
    "formula",
    "null_mask",
    "referenced_formula",
    "grouped_cumcount",
    "grouped_walk",
    "group_key",
    "statistical",
)


def _check_prefix_stable(
    family: str, generate: Callable[[str, int], list[Any]] = _generate_family
) -> None:
    whole = generate(family, 100)
    assert generate(family, 37) == whole[:37], f"{family}: row i is not a function of (seed, i)"
    assert generate(family, 64) == whole[:64]


@pytest.mark.parametrize("family", PREFIX_STABLE)
def test_row_value_depends_only_on_seed_and_global_position(family: str) -> None:
    _check_prefix_stable(family)


def test_pooled_selection_stream_is_prefix_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pooled selection is one `default_rng(selection_seed).integers(size=n)` stream, so the
    first k of n draws equal a size-k draw. A batch that re-creates the generator restarts it."""
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", 50)
    whole = _column([fx.table("t", 100, [_city()])], "t", "c")
    assert _column([fx.table("t", 60, [_city()])], "t", "c") == whole[:60]


@pytest.mark.parametrize("dist", ["random", "sequential", "weighted"])
def test_reference_without_repair_is_prefix_stable(dist: str) -> None:
    def col(n: int) -> list[Any]:
        tables, name = _ref(dist, n)
        return fx.run_generate_tables(fx.config(tables))[name].column("c").to_pylist()

    assert col(17) == col(40)[:17]


def test_whole_table_quantities_are_not_prefix_stable() -> None:
    """Characterization, not aspiration: these two are global by definition, so F1 needs a
    carried reduction (aggregate) or a whole-table pass (cardinality repair) for them."""
    assert (
        _generate_family("derived_aggregate", 37) != _generate_family("derived_aggregate", 100)[:37]
    )

    def repaired(n: int) -> list[Any]:
        tables, name = _ref("random", n, min_per_parent=2, max_per_parent=5)
        return fx.run_generate_tables(fx.config(tables))[name].column("c").to_pylist()

    assert repaired(17) != repaired(40)[:17]


def naive_batch_local(family: str, n: int, sizes: tuple[int, ...] = (40, 60)) -> list[Any]:
    """What a first-draft partitioned generator does: restart every batch at row 0."""
    assert sum(sizes) == n
    out: list[Any] = []
    for size in sizes:
        out += _generate_family(family, size)
    return out


@pytest.mark.parametrize(
    "family",
    [
        "sequence",
        "categorical",
        "live_faker",
        "formula",
        "null_mask",
        "referenced_formula",
        "group_key",
        "derived_aggregate",
    ],
)
def test_batch_local_partitioning_is_caught_by_the_oracle_check(family: str) -> None:
    """The row-ordinal check must trip on batch-local generation (non-vacuity of invariant 4)."""
    whole = _generate_family(family, 100)
    naive = naive_batch_local(family, 100)
    assert naive != whole, f"{family}: batch-local output equals the whole table; check is vacuous"
    _trips(
        lambda: _check_prefix_stable(
            family, lambda f, n: naive_batch_local(f, n, (n // 2, n - n // 2))
        ),
        match="not a function of",
    )


def test_reset_row_index_is_caught_through_the_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutate `GenDeriveContext.row_int` so the ordinal restarts every 5 rows (a batch-local index)
    and the live-faker, formula, referenced-formula and keyed fixtures must leave the oracle."""
    real = GenDeriveContext.row_int

    def reset(self: GenDeriveContext, family: str, i: int) -> int:
        return real(self, family, i % 5)

    monkeypatch.setattr(GenDeriveContext, "row_int", reset)
    for fid in ("live_faker", "formula", "referenced_formula", "keyed_derive"):
        _trips(
            partial(_oracle_run, fid),
            match="values differ|digest differs",
        )


def test_restarted_selection_stream_is_caught_through_the_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutate `PoolSampler.sample` so a 50_000-row selection is drawn as two independent halves
    (each re-creating `default_rng(seed)`), the batch-local stream restart."""
    real = PoolSampler.sample

    def halves(self: PoolSampler, pool: Any, n: int, **kw: Any) -> Any:
        first = real(self, pool, n // 2, **kw)
        second = real(self, pool, n - n // 2, **kw)
        import pandas as pd

        return pd.concat([first, second], ignore_index=True)

    monkeypatch.setattr(PoolSampler, "sample", halves)
    _trips(
        lambda: assert_matches_oracle("pooled_faker", fx.FIXTURES_BY_ID["pooled_faker"].run()),
        match="values differ|digest differs",
    )


# --------------------------------------------------------------------------------------------
# 5. Generated parent -> masked child (run_pipeline boundary)
# --------------------------------------------------------------------------------------------


def _check_fk_coupling(tables: dict[str, pa.Table]) -> None:
    parent_keys = set(tables["people"].column("id").to_pylist())
    child_keys = tables["accounts"].column("person_id").to_pylist()
    orphans = sum(1 for k in child_keys if k not in parent_keys)
    warning = tables["warnings"].to_pylist()
    assert [w["code"] for w in warning] == ["orphan_fk"]
    assert warning[0]["orphan_rows"] == orphans, "the mask side did not see the complete parent"
    assert_matches_oracle("mixed_fk_generated_parent", tables)


def test_generated_parent_reaches_the_masked_child_complete() -> None:
    _check_fk_coupling(fx.run_mixed_fk())


def test_parent_truncated_to_its_first_batch_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    real = plan_entry._generate_tables_from_config

    def first_batch_only(*a: Any, **k: Any) -> dict[str, pa.Table]:
        return {name: tbl.slice(0, 2) for name, tbl in real(*a, **k).items()}

    monkeypatch.setattr(plan_entry, "_generate_tables_from_config", first_batch_only)
    _trips(
        lambda: _check_fk_coupling(fx.run_mixed_fk()),
        match="complete parent|rows|values differ|digest differs",
    )


def test_perturbed_parent_keys_are_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(synthesize, "_categorical", lambda col, n, seed, derive_key=None: ["x"] * n)
    _trips(lambda: _check_fk_coupling(fx.run_mixed_fk()), match="values differ|digest differs")
