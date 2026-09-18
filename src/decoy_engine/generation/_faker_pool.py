"""GP2: generation-local pool bridge for scalar Faker ``faker_type`` columns.

Generation's per-row ``faker`` path (``synthesize.py::_faker``) pays a fresh
``Faker.seed_instance`` + provider call on every row (~9-18k rows/s). Masking
already solved this shape by POOLING: build a bounded ``ValuePool`` once,
select from it vectorized. This module ports that pattern to generation.

It is deliberately NOT the V2 ``PoolBuilder``/``ProviderRegistry`` machinery
masking's ``faker`` strategy uses: that seam expects a registered V2 semantic
provider name (``person_email``, ``company_name``, ...), and generation's raw
``faker_type`` values (``first_name``, ``city``, ``pyint``, ...) are not
registered there. Instead, for a closed allowlist of exact-semantic scalar
types, this builds the pool directly from the SAME reflection surface
``_faker``'s per-row path already resolves through
(``decoy_engine.internal.faker_setup``), then wraps the result into a
``ValuePool`` and reuses the shared ``PoolSampler`` (REUSE mode only --
generation is full-frame, not chunked, so there is no per-row source value to
key a deterministic ``derive_index`` draw on; see the S6 plan's Codex round-1
finding R2).

Determinism (Codex round-2/3 spec B): both the pool build and the selection
draw are keyed off the SAME per-column ``GenDeriveContext`` root
(``strategy_config_fingerprint``-derived, so rename-invariant per R3.10) via
two disjoint HMAC label domains, ``"faker_pool_build"`` and
``"faker_pool_selection"``. Build seeds a FRESH ``make_faker(locale)``
instance once (mirrors ``providers_v2/_faker_adapter.py:224``); selection
seeds ``PoolSampler``'s non-deterministic ``numpy.random.default_rng`` path.
Neither seed can be recovered from the other, and two pooled columns with
different config never collapse onto one selection stream (their
fingerprints, and therefore their roots, differ).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
from faker import VERSION as _FAKER_VERSION
from faker import Faker

from decoy_engine.generation.pool._cardinality import CardinalityMode
from decoy_engine.generation.pool._runtime_pool_size import DEFAULT_POOL_SIZE
from decoy_engine.generation.pool._sampler import PoolSampler
from decoy_engine.generation.pool._value_pool import ValuePool, _freeze_array
from decoy_engine.generators.derivation import GenDeriveContext
from decoy_engine.internal.faker_setup import make_faker, resolve_pool_provider

# Codex round-2 spec C, ROUND-3 confirmed: an IMMUTABLE closed allowlist, not
# "reflection exists => poolable". Exact-semantic, non-key, non-structured
# scalar payload types only. Everything else -- the ~190-type long tail, any
# potential unique-key type (email, uuid*, ssn, phone_number, user_name,
# url/domain_name/hostname, street_address/postcode, ean/isbn/upc, ...) --
# stays on the per-row path in `_faker`. A new type is NEVER added here by
# inference; it needs its own gate round.
POOL_ELIGIBLE_FAKER_TYPES: frozenset[str] = frozenset(
    {
        "first_name",
        "last_name",
        "name",
        "prefix",
        "suffix",
        "city",
        "state",
        "country",
        "job",
        "company",
    }
)

# Faker widening ADDITION slice (2026-09-18): a SEPARATE closed allowlist,
# keyed on (faker_type, locale) rather than type alone. The harness
# (scripts/faker-determinism) certified these 29 additional types as
# deterministic and reuse-safe, but only in the 5 locales it actually ran --
# unlike POOL_ELIGIBLE_FAKER_TYPES above (legacy, global across all locales),
# admitting one of these types outside its certified locale would pool an
# untested provider. Embedded verbatim from the harness's
# `certified_pairs.json` (kwargs={}, legacy types filtered out); a new pair
# is NEVER added by inference -- it needs its own harness certification +
# gate round, matching POOL_ELIGIBLE_FAKER_TYPES's philosophy.
POOL_ELIGIBLE_LOCALE_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("administrative_unit", "de_DE"),
        ("administrative_unit", "en_GB"),
        ("administrative_unit", "en_US"),
        ("administrative_unit", "es_ES"),
        ("administrative_unit", "fr_FR"),
        ("bs", "de_DE"),
        ("bs", "en_GB"),
        ("bs", "en_US"),
        ("bs", "es_ES"),
        ("bs", "fr_FR"),
        ("catch_phrase", "de_DE"),
        ("catch_phrase", "en_GB"),
        ("catch_phrase", "en_US"),
        ("catch_phrase", "es_ES"),
        ("catch_phrase", "fr_FR"),
        ("city_prefix", "en_GB"),
        ("city_prefix", "en_US"),
        ("city_prefix", "fr_FR"),
        ("city_suffix", "de_DE"),
        ("city_suffix", "en_GB"),
        ("city_suffix", "en_US"),
        ("city_suffix", "es_ES"),
        ("city_suffix", "fr_FR"),
        ("company_suffix", "de_DE"),
        ("company_suffix", "en_GB"),
        ("company_suffix", "en_US"),
        ("company_suffix", "es_ES"),
        ("company_suffix", "fr_FR"),
        ("country_code", "de_DE"),
        ("country_code", "en_GB"),
        ("country_code", "en_US"),
        ("country_code", "es_ES"),
        ("country_code", "fr_FR"),
        ("current_country", "de_DE"),
        ("current_country", "en_GB"),
        ("current_country", "en_US"),
        ("current_country", "es_ES"),
        ("current_country", "fr_FR"),
        ("first_name_female", "de_DE"),
        ("first_name_female", "en_GB"),
        ("first_name_female", "en_US"),
        ("first_name_female", "es_ES"),
        ("first_name_female", "fr_FR"),
        ("first_name_male", "de_DE"),
        ("first_name_male", "en_GB"),
        ("first_name_male", "en_US"),
        ("first_name_male", "es_ES"),
        ("first_name_male", "fr_FR"),
        ("first_name_nonbinary", "de_DE"),
        ("first_name_nonbinary", "en_GB"),
        ("first_name_nonbinary", "en_US"),
        ("first_name_nonbinary", "es_ES"),
        ("first_name_nonbinary", "fr_FR"),
        ("job_female", "de_DE"),
        ("job_female", "en_GB"),
        ("job_female", "en_US"),
        ("job_female", "es_ES"),
        ("job_female", "fr_FR"),
        ("job_male", "de_DE"),
        ("job_male", "en_GB"),
        ("job_male", "en_US"),
        ("job_male", "es_ES"),
        ("job_male", "fr_FR"),
        ("last_name_female", "de_DE"),
        ("last_name_female", "en_GB"),
        ("last_name_female", "en_US"),
        ("last_name_female", "es_ES"),
        ("last_name_female", "fr_FR"),
        ("last_name_male", "de_DE"),
        ("last_name_male", "en_GB"),
        ("last_name_male", "en_US"),
        ("last_name_male", "es_ES"),
        ("last_name_male", "fr_FR"),
        ("last_name_nonbinary", "de_DE"),
        ("last_name_nonbinary", "en_GB"),
        ("last_name_nonbinary", "en_US"),
        ("last_name_nonbinary", "es_ES"),
        ("last_name_nonbinary", "fr_FR"),
        ("military_state", "en_US"),
        ("name_female", "de_DE"),
        ("name_female", "en_GB"),
        ("name_female", "en_US"),
        ("name_female", "es_ES"),
        ("name_female", "fr_FR"),
        ("name_male", "de_DE"),
        ("name_male", "en_GB"),
        ("name_male", "en_US"),
        ("name_male", "es_ES"),
        ("name_male", "fr_FR"),
        ("name_nonbinary", "de_DE"),
        ("name_nonbinary", "en_GB"),
        ("name_nonbinary", "en_US"),
        ("name_nonbinary", "es_ES"),
        ("name_nonbinary", "fr_FR"),
        ("prefix_female", "de_DE"),
        ("prefix_female", "en_GB"),
        ("prefix_female", "en_US"),
        ("prefix_female", "es_ES"),
        ("prefix_female", "fr_FR"),
        ("prefix_male", "de_DE"),
        ("prefix_male", "en_GB"),
        ("prefix_male", "en_US"),
        ("prefix_male", "es_ES"),
        ("prefix_male", "fr_FR"),
        ("prefix_nonbinary", "de_DE"),
        ("prefix_nonbinary", "en_GB"),
        ("prefix_nonbinary", "en_US"),
        ("prefix_nonbinary", "es_ES"),
        ("prefix_nonbinary", "fr_FR"),
        ("state_abbr", "en_US"),
        ("street_name", "de_DE"),
        ("street_name", "en_GB"),
        ("street_name", "en_US"),
        ("street_name", "es_ES"),
        ("street_name", "fr_FR"),
        ("street_suffix", "de_DE"),
        ("street_suffix", "en_GB"),
        ("street_suffix", "en_US"),
        ("street_suffix", "es_ES"),
        ("street_suffix", "fr_FR"),
        ("suffix_female", "de_DE"),
        ("suffix_female", "en_GB"),
        ("suffix_female", "en_US"),
        ("suffix_female", "es_ES"),
        ("suffix_female", "fr_FR"),
        ("suffix_male", "de_DE"),
        ("suffix_male", "en_GB"),
        ("suffix_male", "en_US"),
        ("suffix_male", "es_ES"),
        ("suffix_male", "fr_FR"),
        ("suffix_nonbinary", "de_DE"),
        ("suffix_nonbinary", "en_GB"),
        ("suffix_nonbinary", "en_US"),
        ("suffix_nonbinary", "es_ES"),
        ("suffix_nonbinary", "fr_FR"),
    }
)

# Plan correction (2026-09-17): the original 1000 came from the GP1 parallelism
# spike's crossover, which is the wrong number here -- pool build cost is
# dominated by the fixed ~10k-call pool_size (DEFAULT_POOL_SIZE), not by n, so
# pooling a column between roughly 1k and 25k-30k rows actually ran SLOWER than
# the per-row loop (measured: n=1000 pooled ~1.8k rows/s vs per-row ~9.2k rows/s;
# crossover observed around n~25k-30k). 50000 sits safely past that measured
# crossover with margin, so no row count regresses. This is a conservative
# placeholder, not the true optimum: a GP1 probe run on the reference host should
# refine it back down toward the real crossover.
N_THRESHOLD = 50_000

# GenDeriveContext.family_bytes labels (Codex round-2/3 spec B). Frozen wire
# format: changing either string reseeds every pooled column that ships
# today, which is a SEED_PROTOCOL_VERSION-worthy break, not a casual rename.
_BUILD_FAMILY = "faker_pool_build"
_SELECTION_FAMILY = "faker_pool_selection"


def pool_eligible(faker_type: str, requested_locale: Any, n: int, *, opted_out: bool) -> bool:
    """Cheap, instance-independent gate: should `_faker` attempt pooling?

    This is NOT the full eligibility decision -- it doesn't know yet whether
    `faker_type` is custom-overridden or available for the effective locale.
    That check has to happen against the actual pool-build Faker instance
    under one lock acquisition (`resolve_pool_provider`, Codex round-3 spec
    C), which `build_and_sample` does. `pooled: true` never overrides this
    gate: an ineligible `faker_type` or a below-threshold `n` stays per-row
    regardless of what the column sets (Codex round-2 spec: AUTO + opt-out
    ONLY, no forced-on knob).

    Locale-aware (faker-widening ADDITION slice): `POOL_ELIGIBLE_FAKER_TYPES`
    is still the legacy global allowlist -- one of those 10 types pools in
    ANY locale, exactly as before this slice, and is never locale-checked.
    A type from `POOL_ELIGIBLE_LOCALE_PAIRS` pools only in its own certified
    locale. `requested_locale` is taken AS REQUESTED, not resolved through
    `make_faker`: an absent/falsy locale maps to Faker's own default
    (`en_US`) for admission purposes, a `str` locale is checked as given
    (an uncertified or invalid string just misses the set), and anything
    else -- most importantly a locale LIST, which `make_faker` accepts but a
    frozenset of `(type, str)` tuples cannot hash against -- declines to the
    per-row path rather than raising.
    """
    if opted_out or n < N_THRESHOLD:
        return False
    if faker_type in POOL_ELIGIBLE_FAKER_TYPES:
        return True
    if requested_locale is None:
        locale = "en_US"
    elif isinstance(requested_locale, str):
        locale = requested_locale
    else:
        return False
    return (faker_type, locale) in POOL_ELIGIBLE_LOCALE_PAIRS


def try_pool(
    col: dict[str, Any],
    n: int,
    seed: int,
    derive_key: Any,
    instance_default_locale: str | None,
    faker_kwargs: dict[str, Any],
    *,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
) -> list[Any] | None:
    """`_faker`'s one-call entry point into the GP2 pool bridge.

    Returns `None` when the column is ineligible (cheap gate) or the locked
    resolver forced a fallback (`build_and_sample`); either way `_faker` falls
    through to its unchanged per-row loop. Keeping this dispatch here (not
    inline in `synthesize.py`) keeps that file -- allowlisted near its own LOC
    ceiling -- a thin caller; this module carries the branching. Recomputes
    `col.get("locale")` rather than taking it as a param (cheap, and `_faker`
    needs the value too either way).

    `provider_snapshot` (5a-faker, additive) forwards straight through to
    `build_and_sample`'s own resolver call; `None` (the default) resolves
    against the live custom-provider registry exactly as before.
    """
    faker_type = col.get("faker_type", "word")
    requested_locale = col.get("locale") or instance_default_locale
    if not pool_eligible(faker_type, requested_locale, n, opted_out=col.get("pooled") is False):
        return None
    gen_ctx = GenDeriveContext.for_column(
        derive_key=derive_key, column_config=col, fallback_seed=seed
    )
    return build_and_sample(
        faker_type=faker_type,
        faker_kwargs=faker_kwargs,
        n=n,
        gen_ctx=gen_ctx,
        effective_locale=col.get("locale") or instance_default_locale,
        provider_snapshot=provider_snapshot,
    )


def build_pool_values(
    faker_type: str,
    effective_locale: str | None,
    build_seed: bytes,
    pool_size: int,
    faker_kwargs: dict[str, Any],
    *,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
) -> tuple[list[Any], bool, bool]:
    """The production pool-BUILD seam (determinism harness slice 0, C0).

    Extracted from `build_and_sample` so the determinism harness can build
    the exact same ordered `pool_size`-length value list production does,
    without also running the selection draw. `build_and_sample` calls this
    (via `_build_and_sample_returning_pool`, one call, no second divergent
    build) rather than restating the build inline -- see that function.

    A FRESH `Faker` instance, never the per-row path's cached/shared one:
    the pool is built exactly once from one `seed_instance` call, so there
    is no per-thread reuse hazard to guard against (Codex round-2 spec B
    mirrors `providers_v2/_faker_adapter.py:224`). `make_faker` (not raw
    `Faker(...)`) preserves the invalid-locale-falls-back-to-en_US contract.

    Returns `(values, exact_name_available, custom_override_present)`.
    `values` is `[]` when `faker_type` is custom-overridden or unavailable
    for `effective_locale` -- callers branch on the two bool flags, never on
    `values` truthiness, to tell "genuinely empty pool" apart (impossible
    today: `pool_size` is always > 0) from "build declined".
    """
    faker_inst = make_faker(effective_locale)
    provider_callable, exact_name_available, custom_override_present = resolve_pool_provider(
        faker_inst, faker_type, provider_snapshot=provider_snapshot
    )
    if custom_override_present or not exact_name_available:
        return [], exact_name_available, custom_override_present
    assert provider_callable is not None  # noqa: S101 -- exact_name_available guarantees this

    faker_inst.seed_instance(int.from_bytes(build_seed, "big", signed=False))
    raw_values = [provider_callable(**faker_kwargs) for _ in range(pool_size)]
    return raw_values, exact_name_available, custom_override_present


def _build_and_sample_returning_pool(
    *,
    faker_type: str,
    faker_kwargs: dict[str, Any],
    n: int,
    build_seed: bytes,
    selection_seed: bytes,
    effective_locale: str | None,
    pool_size: int,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
) -> tuple[list[Any] | None, list[Any] | None, bool, bool]:
    """Build the pool ONCE (`build_pool_values`) and sample from that exact
    pool -- never a second, independently-built pool (determinism harness
    slice 0, C2). `build_and_sample` delegates here and discards
    `pool_values`; the determinism worker calls this directly so the digest
    it takes of `pool_values` and the digest it takes of `sampled_output`
    are guaranteed to come from the SAME build, not two builds compared for
    equality (which cannot bind two runtime builds under mutable
    custom-provider registration or other process-local state).

    Returns `(pool_values, sampled_output, exact_name_available,
    custom_override_present)`; the first two are `None` together when the
    build declined (mirrors `build_and_sample`'s `None` return).
    """
    raw_values, exact_name_available, custom_override_present = build_pool_values(
        faker_type,
        effective_locale,
        build_seed,
        pool_size,
        faker_kwargs,
        provider_snapshot=provider_snapshot,
    )
    if custom_override_present or not exact_name_available:
        return None, None, exact_name_available, custom_override_present

    values = _freeze_array(np.array(raw_values, dtype=object))
    pool = ValuePool(
        values=values,
        provider=f"gen.faker_pool.{faker_type}",
        locale=effective_locale or "default",
        config_hash="",
        seed=build_seed,
        size=pool_size,
        build_time_ms=0.0,
        backend_type="faker",
        backend_version=_FAKER_VERSION,
        distinct_count=len(set(values.tolist())),
    )
    sampled = PoolSampler().sample(
        pool, n, mode=CardinalityMode.REUSE, seed=selection_seed, deterministic=False
    )
    return values.tolist(), sampled.tolist(), exact_name_available, custom_override_present


def build_and_sample(
    *,
    faker_type: str,
    faker_kwargs: dict[str, Any],
    n: int,
    gen_ctx: GenDeriveContext,
    effective_locale: str | None,
    pool_size: int = DEFAULT_POOL_SIZE,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
) -> list[Any] | None:
    """Build a bounded value pool and REUSE-sample `n` values from it.

    Returns `None` (never raises) when the locked resolver snapshot shows
    `faker_type` is custom-overridden or unavailable for `effective_locale`
    -- the caller must fall through to its own unchanged per-row loop, which
    still carries the unknown -> `word` fallback for a genuinely unrecognized
    name; this function does not apply that fallback itself.

    `provider_snapshot` (5a-faker, additive): forwarded to
    `resolve_pool_provider` so a caller comparing this pooled build against
    a separate call (e.g. the shadow-parity oracle) resolves custom
    overrides against the SAME captured registry state on both sides,
    instead of two independent live reads. `None` (the default) resolves
    against the live registry exactly as before.

    Delegates to `_build_and_sample_returning_pool` (determinism harness
    slice 0, C0) for the actual build + selection, discarding the pool it
    also returns -- this function's own contract is the selection output
    only. Behavior-preserving: see
    `tests/unit/generation/test_faker_pool_seam_characterization.py` for the
    pre-extraction byte-identical proof.
    """
    build_seed = gen_ctx.family_bytes(_BUILD_FAMILY)[:8]
    selection_seed = gen_ctx.family_bytes(_SELECTION_FAMILY)[:8]
    if build_seed == selection_seed:
        # Astronomically unlikely for distinct HMAC labels; a real collision
        # here means family_bytes stopped being label-disjoint, which would
        # silently collapse the build and selection draws onto one stream.
        raise RuntimeError(
            "faker_pool_build/faker_pool_selection HMAC domains collided -- "
            "GenDeriveContext.family_bytes is no longer label-disjoint"
        )

    _pool_values, sampled_output, _exact_name_available, _custom_override_present = (
        _build_and_sample_returning_pool(
            faker_type=faker_type,
            faker_kwargs=faker_kwargs,
            n=n,
            build_seed=build_seed,
            selection_seed=selection_seed,
            effective_locale=effective_locale,
            pool_size=pool_size,
            provider_snapshot=provider_snapshot,
        )
    )
    return sampled_output


__all__ = [
    "N_THRESHOLD",
    "POOL_ELIGIBLE_FAKER_TYPES",
    "POOL_ELIGIBLE_LOCALE_PAIRS",
    "build_and_sample",
    "build_pool_values",
    "pool_eligible",
    "try_pool",
]
