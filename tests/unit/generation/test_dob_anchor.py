"""Seed-stable dob generation (generation/_dob_anchor.py).

Acceptance for docs/plans/2026-10-18-gen-dob-anchor.md. The F0 oracle check (production anchor,
freeze bypassed) is run via ``F0_DOB_ANCHOR_CHECK`` in tests/parity/conftest.py:

    F0_DOB_ANCHOR_CHECK=poison  pytest tests/parity/test_phase_f_f0_*.py
    F0_DOB_ANCHOR_CHECK=noclock pytest tests/parity/test_phase_f_f0_oracle.py -k composite_person
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import faker
import pandas as pd
import pytest

from decoy_engine.generation import _dob_anchor
from decoy_engine.generation._dob_anchor import DOB_ANCHOR, anchored_date_of_birth
from decoy_engine.generation.composite._person import CompositePerson
from decoy_engine.providers_v2._adapter import ProviderSpec
from decoy_engine.providers_v2._faker_adapter import FakerAdapter
from tests.parity._f0_fixtures import config, frozen_faker_clock, run_generate_tables, table
from tests.unit.generation._dob_anchor_helpers import (
    GOLDEN_PATH,
    run_composite_person_masking,
    simulated_system_date,
)

_SEED = (0x0123456789).to_bytes(8, "big")
_DAY_A = dt.date(2027, 3, 9)
_DAY_B = dt.date(2031, 11, 30)


def _spec(*, deterministic: bool = False, seed: bytes | None = _SEED) -> ProviderSpec:
    return ProviderSpec(locale="en_US", deterministic=deterministic, namespace="dob", seed=seed)


# ---- engine paths (each builds FRESH instances/pools) -------------------------------------------


def _per_row() -> list[Any]:
    adapter = FakerAdapter()
    out = []
    for i in range(6):
        fake = faker.Faker("en_US")
        fake.seed_instance(1000 + i)
        out.append(adapter._faker_call("person_dob", _spec(), fake))
    return out


def _pooled() -> list[Any]:
    return list(FakerAdapter().generate_batch("person_dob", spec=_spec(), count=40))


def _composite() -> list[Any]:
    gen = CompositePerson(coherent_namespace="dob", pool_size=64)
    src = pd.Series(["a", "b", "c", "d"])
    out = gen.generate_bundle(_spec(deterministic=True), 4, source=src, deterministic=True)
    return out["dob"].tolist()


def _formula(expr: str = "date_of_birth()") -> list[Any]:
    cfg = config([table("t", 5, [{"name": "d", "type": "formula", "formula": expr}])])
    return run_generate_tables(cfg)["t"].column("d").to_pylist()


PATHS = {"per_row": _per_row, "pooled": _pooled, "composite": _composite}


# ---- anchor engaged ----------------------------------------------------------------------------


def test_anchor_constant() -> None:
    if dt.date(2026, 10, 16) != DOB_ANCHOR:
        raise AssertionError(DOB_ANCHOR)


def test_engine_dob_equals_anchored_helper_under_same_seed() -> None:
    for i in range(6):
        a = faker.Faker("en_US")
        a.seed_instance(1000 + i)
        b = faker.Faker("en_US")
        b.seed_instance(1000 + i)
        assert FakerAdapter()._faker_call("person_dob", _spec(), a) == anchored_date_of_birth(b)


# ---- cross-date seed stability -----------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(PATHS))
def test_seed_stable_across_system_dates(path: str) -> None:
    with simulated_system_date(_DAY_A):
        first = PATHS[path]()
    with simulated_system_date(_DAY_B):
        second = PATHS[path]()
    assert first == second
    assert all(isinstance(v, dt.date) for v in first)


@pytest.mark.parametrize("path", sorted(PATHS))
def test_unanchored_provider_is_date_dependent(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-fix detection: routing the engine back to Faker's own method makes the stability test
    above fail, so that test bites."""
    from faker.providers import date_time

    monkeypatch.setattr(_dob_anchor, "anchored_date_of_birth", _dob_anchor_unanchored)
    monkeypatch.setattr(
        "decoy_engine.providers_v2._faker_adapter.FakerAdapter._faker_call",
        _legacy_faker_call,
    )
    assert date_time is not None
    with simulated_system_date(_DAY_A):
        first = PATHS[path]()
    with simulated_system_date(_DAY_B):
        second = PATHS[path]()
    assert first != second


def _dob_anchor_unanchored(fake: Any, *a: Any, **k: Any) -> Any:
    return fake.date_of_birth(*a, **k)


def _legacy_faker_call(self: FakerAdapter, provider: str, spec: ProviderSpec, fake: Any = None):
    """The pre-change body: bound Faker method, wall clock and all."""
    from decoy_engine.providers_v2 import _faker_adapter as m

    if fake is None:
        fake = self._faker(spec.locale)
    method = getattr(fake, m._FAKER_METHOD_MAP[provider])
    return method(**dict(m._FAKER_DEFAULT_KWARGS.get(provider, {})), **spec.extra)


# ---- differential vs upstream ------------------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 7, 42, 20261016, 987654321])
@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"minimum_age": 18, "maximum_age": 65},
        {"minimum_age": 0, "maximum_age": 1},
        {"minimum_age": 30, "maximum_age": 30},
        {"maximum_age": 5},
    ],
)
def test_matches_upstream_at_frozen_anchor(seed: int, kwargs: dict[str, int]) -> None:
    up = faker.Faker("en_US")
    up.seed_instance(seed)
    mine = faker.Faker("en_US")
    mine.seed_instance(seed)
    with frozen_faker_clock():
        expected = [up.date_of_birth(**kwargs) for _ in range(25)]  # type: ignore[arg-type]
    actual = [anchored_date_of_birth(mine, **kwargs) for _ in range(25)]
    if expected != actual:
        raise AssertionError("anchored dob diverged from upstream faker.date_of_birth")
    if up.random.getstate() != mine.random.getstate():
        raise AssertionError("Faker RNG state diverged from upstream after the draw")


def test_validation_matches_upstream() -> None:
    fk = faker.Faker()
    cases: list[tuple[tuple[Any, ...], type[Exception]]] = [
        ((None, "1", 5), TypeError),
        ((None, 1, "5"), TypeError),
        ((None, -1, 5), ValueError),
        ((None, 1, -5), ValueError),
        ((None, 10, 5), ValueError),
    ]
    for args, exc in cases:
        with pytest.raises(exc) as up:
            fk.date_of_birth(*args)
        with pytest.raises(exc) as mine:
            anchored_date_of_birth(fk, *args)
        assert str(up.value) == str(mine.value)


# ---- coverage: no path reaches the unanchored provider method ----------------------------------


def _install_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    from faker.providers import date_time

    calls: list[tuple[Any, ...]] = []
    original = date_time.Provider.date_of_birth

    def spy(self: Any, *a: Any, **k: Any) -> Any:
        calls.append((a, tuple(sorted(k.items()))))
        return original(self, *a, **k)

    monkeypatch.setattr(date_time.Provider, "date_of_birth", spy, raising=False)
    return calls


def test_no_dob_path_reaches_unanchored_method(monkeypatch: pytest.MonkeyPatch) -> None:
    # Spy FIRST: Faker instances bind provider methods at construction, so everything below is
    # built after the patch.
    calls = _install_spy(monkeypatch)
    with simulated_system_date(_DAY_A):
        results = {name: fn() for name, fn in PATHS.items()}
        results["formula"] = _formula()
        results["formula_positional"] = _formula("date_of_birth(None, 18, 65)")
        results["formula_keyword"] = _formula("date_of_birth(minimum_age=18, maximum_age=65)")
    assert calls == []
    for name, vals in results.items():
        assert all(v is not None for v in vals), name
        assert all(isinstance(v, dt.date) for v in vals), name
    # Outputs are the anchored values, not merely non-null.
    ref = faker.Faker("en_US")
    ref.seed_instance(int.from_bytes(_SEED, "big"))
    assert results["pooled"] == [anchored_date_of_birth(ref) for _ in range(40)]


def test_spy_detects_unanchored_method(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spy is live: calling Faker's own method records, so a missed seam would be caught."""
    calls = _install_spy(monkeypatch)
    faker.Faker().date_of_birth()
    assert len(calls) == 1


# ---- formula regression ------------------------------------------------------------------------


def test_formula_positional_and_keyword_both_anchored() -> None:
    pos = _formula("date_of_birth(None, 18, 65)")
    kw = _formula("date_of_birth(minimum_age=18, maximum_age=65)")
    assert all(v is not None for v in pos + kw)
    # Same helper, positional vs keyword, same seed: identical draw.
    a, b = faker.Faker(), faker.Faker()
    a.seed_instance(5)
    b.seed_instance(5)
    assert anchored_date_of_birth(a, None, 18, 65) == anchored_date_of_birth(
        b, minimum_age=18, maximum_age=65
    )
    # Formula output is seed-stable across system dates.
    with simulated_system_date(_DAY_A):
        pos_a = _formula("date_of_birth(None, 18, 65)")
    with simulated_system_date(_DAY_B):
        pos_b = _formula("date_of_birth(None, 18, 65)")
    assert pos_a == pos_b == pos
    # Age window is anchor-relative: 18..65 years before DOB_ANCHOR (one extra year at the cap).
    for v in pos:
        d = dt.date.fromisoformat(str(v)) if not isinstance(v, dt.date) else v
        assert dt.date(DOB_ANCHOR.year - 66, 10, 16) <= d <= dt.date(DOB_ANCHOR.year - 18, 10, 16)


# ---- masking unchanged -------------------------------------------------------------------------


def test_composite_person_masking_matches_pre_change_golden_without_freeze() -> None:
    golden = json.loads(GOLDEN_PATH.read_text())
    with simulated_system_date(_DAY_B):  # a different calendar day; no F0 freeze
        actual = run_composite_person_masking()
    assert actual == golden


# ---- symbol guard ------------------------------------------------------------------------------


def test_symbol_guard_raises_not_asserts(monkeypatch: pytest.MonkeyPatch) -> None:
    from faker.providers import date_time

    from decoy_engine.errors import DecoyError

    monkeypatch.delattr(date_time, "change_year")
    with pytest.raises(DecoyError):
        _dob_anchor._check_faker_symbols()
