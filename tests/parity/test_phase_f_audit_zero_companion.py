"""Phase F / F-audit (E): generation makes ZERO compiled-companion calls, by design.

Two production mechanisms keep it that way, and both are non-deterministic pool selections:

- core: `_faker_pool.PooledSelector.select` draws `np.random.default_rng(seed).integers`;
- legacy whole-table: `_faker_pool._build_and_sample_returning_pool` calls
  `PoolSampler.sample(..., deterministic=False)`.

The compiled index kernel is reachable only through `PoolSampler._deterministic` ->
`_sampler._derive_pool_indices` (compiled when the companion is installed and the value class is
admitted, else the byte-identical Python reference). The e2e witness records a `numpy_selection`
label for ANY `PoolSampler.sample`, deterministic or not, so the absence of a compiled label proves
nothing. This file observes the deterministic-index ENTRY itself with a record-and-delegate spy
and asserts it AFTER the run: baselines must record zero, and a mutant on EITHER generation entry
point that re-points to the deterministic sampler (valid non-null homogeneous-string identities,
namespace, seed and pool, so it reaches real sampling) must trip the same invariant, as a compiled
call when the companion is installed and as a reference call when it is absent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd
import pytest

from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._index_ext import load_compiled_index_kernel
from decoy_engine.generation import _faker_pool, _plan_entry
from decoy_engine.generation.pool import _sampler
from decoy_engine.generation.pool._cardinality import CardinalityMode
from decoy_engine.generation.pool._sampler import PoolSampler
from decoy_engine.generation.pool._value_pool import ValuePool
from tests.parity import _f0_fixtures as fx
from tests.parity import _f_audit_cells as cells
from tests.parity._f_audit_witness import DeterministicIndexSpy
from tests.parity._fr0_support import plan_of

THRESHOLD = 90
FORBIDDEN = "deterministic-index path"


def _companion_installed() -> bool:
    try:
        load_compiled_index_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


COMPANION = ["installed", "absent"]


def _spy(mp: pytest.MonkeyPatch, companion: str) -> DeterministicIndexSpy:
    if companion == "installed" and not _companion_installed():
        pytest.skip("native companion not installed")
    if companion == "absent":
        mp.setattr(_sampler, "_compiled_index_kernel", lambda: None)
    return DeterministicIndexSpy().install(mp)


def core_pooled_config() -> dict[str, Any]:
    return cells.cfg_faker("city")


def legacy_pooled_config() -> dict[str, Any]:
    """A declined formula sends the whole plan, including the pooled column, to the legacy path."""
    pooled = cells.cfg_faker("city")["tables"][0]
    return fx.config([fx.table("lone", 10, [cells._FM]), pooled])


class _Reached:
    """Record-and-delegate counter on the NON-deterministic pool entry, so a baseline that
    never pooled cannot pass the zero assertion vacuously."""

    def __init__(self) -> None:
        self.calls = 0

    def install_core(self, mp: pytest.MonkeyPatch) -> None:
        real, outer = _faker_pool.PooledSelector.select, self

        def select(selector: Any, size: int) -> Any:
            outer.calls += 1
            return real(selector, size)

        mp.setattr(_faker_pool.PooledSelector, "select", select)

    def install_legacy(self, mp: pytest.MonkeyPatch) -> None:
        real, outer = _faker_pool._build_and_sample_returning_pool, self

        def build(**kw: Any) -> Any:
            outer.calls += 1
            return real(**kw)

        mp.setattr(_faker_pool, "_build_and_sample_returning_pool", build)


ENTRIES: dict[str, tuple[Callable[[], dict[str, Any]], str]] = {
    "core": (core_pooled_config, "install_core"),
    "legacy": (legacy_pooled_config, "install_legacy"),
}


def _generate(config: dict[str, Any]) -> None:
    tables = _plan_entry.generate_tables(plan_of(config))
    assert sum(t.num_rows for t in tables.values()) > 0


@pytest.mark.parametrize("companion", COMPANION)
@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_baseline_generation_never_enters_the_deterministic_index_path(
    entry: str, companion: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    build, hook = ENTRIES[entry]
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", THRESHOLD)
    spy = _spy(monkeypatch, companion)
    reached = _Reached()
    getattr(reached, hook)(monkeypatch)
    _generate(build())
    assert reached.calls > 0, "the pooled selection never ran, so the zero below proves nothing"
    assert spy.total() == 0
    spy.assert_generation_never_entered(f"{entry}/{companion}")


# --------------------------------------------------------------------------------------------
# mutants: re-point each generation entry point to the deterministic sampler
# --------------------------------------------------------------------------------------------


def _deterministic_sample(values: Any, n: int) -> list[Any]:
    """Select from `values` through the deterministic sampler with a VALID call: one non-null
    homogeneous-string identity per output row, a namespace, an 8-byte seed and a real pool."""
    arr = np.asarray(values, dtype=object)
    pool = ValuePool(
        values=arr,
        provider="f_audit.mutant",
        locale="default",
        config_hash="",
        seed=b"\x01" * 8,
        size=len(arr),
        build_time_ms=0.0,
        backend_type="faker",
        backend_version="f-audit",
        distinct_count=len(set(arr.tolist())),
    )
    source = pd.Series([f"identity-{i}" for i in range(n)])
    out = PoolSampler().sample(
        pool,
        n,
        mode=CardinalityMode.REUSE,
        seed=b"\x01" * 8,
        source=source,
        namespace="f_audit/mutant",
        deterministic=True,
    )
    return list(out.tolist())


def mutate_core(mp: pytest.MonkeyPatch) -> None:
    def select(self: _faker_pool.PooledSelector, size: int) -> list[Any]:
        return _deterministic_sample(self._values, size)

    mp.setattr(_faker_pool.PooledSelector, "select", select)


def mutate_legacy(mp: pytest.MonkeyPatch) -> None:
    real = _faker_pool._build_and_sample_returning_pool

    def build(**kw: Any) -> Any:
        values, _sampled, exact, custom = real(**kw)
        if values is None:
            return values, None, exact, custom
        return values, _deterministic_sample(values, kw["n"]), exact, custom

    mp.setattr(_faker_pool, "_build_and_sample_returning_pool", build)


MUTANTS = {"core": mutate_core, "legacy": mutate_legacy}


@pytest.mark.parametrize("companion", COMPANION)
@pytest.mark.parametrize("entry", sorted(MUTANTS))
def test_a_mutant_on_either_entry_point_trips_the_same_forbidden_path_invariant(
    entry: str, companion: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    build = ENTRIES[entry][0]
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", THRESHOLD)
    spy = _spy(monkeypatch, companion)
    MUTANTS[entry](monkeypatch)
    _generate(build())
    # The mutant reached REAL deterministic sampling, not an incidental numpy or validation path.
    assert spy.index_entries >= 1 and spy.deterministic_calls >= 1
    if companion == "installed":
        assert spy.compiled_calls >= 1 and spy.reference_calls == 0
    else:
        assert spy.reference_calls >= 1 and spy.compiled_calls == 0
    with pytest.raises(AssertionError, match=FORBIDDEN):
        spy.assert_generation_never_entered(f"{entry}-mutant/{companion}")


def test_the_spy_delegates_so_a_mutant_still_returns_pool_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", THRESHOLD)
    spy = _spy(monkeypatch, "absent")
    mutate_core(monkeypatch)
    tables = _plan_entry.generate_tables(plan_of(core_pooled_config()))
    assert spy.total() > 0
    assert all(isinstance(v, str) and v for v in tables["t"].column("f").to_pylist())


def test_the_real_threshold_pooled_fixture_also_records_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No lowered threshold: the shipped 50_000-row pooled fixture through the public entry."""
    spy = DeterministicIndexSpy().install(monkeypatch)
    tables = _plan_entry.generate_tables(plan_of(fx.cfg_pooled_faker()))
    assert tables["pooled"].num_rows >= _faker_pool.N_THRESHOLD
    spy.assert_generation_never_entered("real-threshold")
