"""Seam-level characterization of `build_pool_values` (C0, plan section
VERIFY item 1's second and third bullets).

The production-boundary guard in `test_faker_pool_seam_characterization.py`
only proves `build_and_sample`'s SAMPLED output survived the extraction --
`PoolSampler.sample` draws a strict subset of pool positions, so a bug that
corrupted an unselected pool entry could hide behind that test alone (the
plan's own rationale for requiring seam-level coverage too). This module
covers the complete ordered pool `build_pool_values` returns, under both
default and non-default `faker_kwargs`, and proves the pool
`build_and_sample` samples from is the SAME object the seam reports (one
build, not two).
"""

from __future__ import annotations

from decoy_engine.generation import _faker_pool
from decoy_engine.generators.derivation import GenDeriveContext


def _gen_ctx(faker_type: str, locale: str | None, kwargs: dict, seed: int) -> GenDeriveContext:
    col = {
        "name": "v",
        "type": "faker",
        "faker_type": faker_type,
        "locale": locale,
        "faker_kwargs": kwargs,
    }
    return GenDeriveContext.for_column(derive_key=None, column_config=col, fallback_seed=seed)


def test_pool_values_deterministic_and_full_length_default_kwargs() -> None:
    """The complete ordered pool (not just the sampled subset) is
    reproducible across two independent calls sharing the same build_seed,
    and has exactly `pool_size` entries -- default (empty) kwargs."""
    gen_ctx = _gen_ctx("city", "en_US", {}, seed=1)
    build_seed = gen_ctx.family_bytes(_faker_pool._BUILD_FAMILY)[:8]

    values_a, exact_a, custom_a = _faker_pool.build_pool_values(
        "city", "en_US", build_seed, 200, {}
    )
    values_b, exact_b, custom_b = _faker_pool.build_pool_values(
        "city", "en_US", build_seed, 200, {}
    )

    assert exact_a is True and custom_a is False
    assert exact_b is True and custom_b is False
    assert len(values_a) == 200
    assert values_a == values_b


def test_pool_values_non_default_kwargs_change_the_pool() -> None:
    """`faker_kwargs` is threaded into every provider call in the pool loop
    (`build_and_sample`'s "explicit seam arg" contract) -- a kwarg that
    actually changes provider output must produce a DIFFERENT pool, not a
    silently-ignored one. `country_code(representation=...)` is not on the
    production pool-eligible allowlist, but `build_pool_values` itself is
    general-purpose (the allowlist gate lives in `pool_eligible`/`try_pool`,
    not here), so it is a valid seam-level probe for kwargs threading."""
    gen_ctx = _gen_ctx("country_code", "en_US", {}, seed=2)
    build_seed = gen_ctx.family_bytes(_faker_pool._BUILD_FAMILY)[:8]

    default_values, exact_d, custom_d = _faker_pool.build_pool_values(
        "country_code", "en_US", build_seed, 100, {}
    )
    alpha3_values, exact_3, custom_3 = _faker_pool.build_pool_values(
        "country_code", "en_US", build_seed, 100, {"representation": "alpha-3"}
    )

    assert exact_d is True and not custom_d
    assert exact_3 is True and not custom_3
    assert len(default_values) == len(alpha3_values) == 100
    assert all(len(v) == 2 for v in default_values), "alpha-2 default must stay 2 chars"
    assert all(len(v) == 3 for v in alpha3_values), "alpha-3 kwarg must actually take effect"
    assert default_values != alpha3_values


def test_unavailable_type_returns_empty_pool_with_false_flag() -> None:
    """`state` has no provider for `ja_JP` (verified against the installed
    Faker locale data, same fixture the existing draw-site tests use) --
    `build_pool_values` must report `exact_name_available=False` and an
    empty pool, never raise."""
    gen_ctx = _gen_ctx("state", "ja_JP", {}, seed=3)
    build_seed = gen_ctx.family_bytes(_faker_pool._BUILD_FAMILY)[:8]
    values, exact, custom = _faker_pool.build_pool_values("state", "ja_JP", build_seed, 50, {})
    assert values == []
    assert exact is False
    assert custom is False


def test_seam_pool_is_the_exact_pool_build_and_sample_samples_from() -> None:
    """The pool `_build_and_sample_returning_pool` returns must be the SAME
    build `build_and_sample`'s selection draws from -- reconstructing the
    selection by hand (numpy default_rng over the returned pool) must equal
    `build_and_sample`'s real output. This is the "single build" proof: it
    fails if a future change made the seam and the selection build two
    independent pools instead of sharing one."""
    import numpy as np

    faker_type, locale, kwargs, n = "job", "en_US", {}, 137
    gen_ctx = _gen_ctx(faker_type, locale, kwargs, seed=4)
    build_seed = gen_ctx.family_bytes(_faker_pool._BUILD_FAMILY)[:8]
    selection_seed = gen_ctx.family_bytes(_faker_pool._SELECTION_FAMILY)[:8]

    pool_values, sampled_output, exact, custom = _faker_pool._build_and_sample_returning_pool(
        faker_type=faker_type,
        faker_kwargs=kwargs,
        n=n,
        build_seed=build_seed,
        selection_seed=selection_seed,
        effective_locale=locale,
        pool_size=_faker_pool.DEFAULT_POOL_SIZE,
    )
    assert exact is True and not custom
    assert pool_values is not None and sampled_output is not None

    shipped = _faker_pool.build_and_sample(
        faker_type=faker_type,
        faker_kwargs=kwargs,
        n=n,
        gen_ctx=gen_ctx,
        effective_locale=locale,
    )
    assert shipped == sampled_output

    rng = np.random.default_rng(int.from_bytes(selection_seed, "big", signed=False))
    indices = rng.integers(0, _faker_pool.DEFAULT_POOL_SIZE, size=n)
    reconstructed = [pool_values[i] for i in indices]
    assert reconstructed == shipped, (
        "selecting from the seam's own reported pool must reproduce build_and_sample's "
        "output -- the selection did not draw from the pool the seam returned"
    )
