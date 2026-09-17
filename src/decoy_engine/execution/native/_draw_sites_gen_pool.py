"""GP2 generation-local Faker pool DrawSite data siblings.

Split out of ``_determinism_protocol.py`` per that module's own
decomposition-target comment ("split ``DRAW_SITES`` into ``_draw_sites_mask.py``
/ ``_draw_sites_gen.py`` data siblings re-exported here if it grows further"):
``_determinism_protocol.py`` is allowlisted at its recorded LOC ceiling
(``tests/sentry/test_module_size.py``), so a new catalogued draw site lands
here and is spliced into ``DRAW_SITES`` at the bottom of that module, instead
of regrowing an already-capped file.

Two entries: the GP2 pool BUILD and REUSE-SELECTION sites for a closed
allowlist of scalar ``faker_type`` generate columns -- see
``generation/_faker_pool.py`` for the mechanism these catalogue.
"""

from __future__ import annotations

from decoy_engine.execution.native._determinism_protocol import _V6, DrawSite

GEN_POOL_DRAW_SITES: tuple[DrawSite, ...] = (
    # -- Generation: GP2 generation-local Faker pool BUILD --------------------
    # NOT gen.pool_build_faker: that site is the V2 PoolBuilder/ProviderRegistry
    # seam masking's `faker` strategy uses. Generation's raw `faker_type` values
    # (first_name, city, ...) are not V2-registered providers, so `_faker`
    # bridges a CLOSED ALLOWLIST of them through a generation-local builder
    # instead (`generation/_faker_pool.py`) that reuses the shared ValuePool +
    # PoolSampler primitives without going through PoolBuilder/ProviderRegistry.
    DrawSite(
        draw_site_id="gen.faker_pool_build",
        family="faker_seed_instance",
        call_site="generation/_faker_pool.py:176",
        entropy_root="job_seed",
        seed_derivation=(
            'build_seed = GenDeriveContext.for_column(...).family_bytes("faker_pool_build")[:8]; '
            'faker.seed_instance(int.from_bytes(build_seed, "big", signed=False))'
        ),
        api_operation="Faker.seed_instance(build_seed_int) then provider_callable() x pool_size",
        call_shape="build 'pool_size' fresh values via ONE seed_instance + a batch of calls",
        consumes_variable_draws=True,
        identity="none",
        null_draw_behavior="build-time; the pool is filled before any output row is drawn",
        partitionable=True,
        config_fingerprint_source="strategy_config_fingerprint(column_config)",
        provider_version=f"{_V6} (GenDeriveContext.family_bytes); Faker seed_instance",
        notes=(
            "Eligibility (closed allowlist + n >= N_THRESHOLD + not opted out via "
            "`pooled: false`, further gated by a locked resolver snapshot for custom-"
            "override/locale availability) decides whether THIS site or gen.faker_per_row "
            "fires for a given faker column -- GEN_KIND_TO_SITE maps the `faker` kind to "
            "gen.faker_per_row only and cannot express that split; the predicate-aware "
            "runtime check lives in test_pooled_faker_draw_sites.py. Build and selection "
            "(gen.faker_pool_selection) are keyed off the SAME column root via disjoint "
            "HMAC label domains, so neither seed is derivable from the other."
        ),
    ),
    # -- Generation: GP2 generation-local Faker pool REUSE SELECTION ----------
    DrawSite(
        draw_site_id="gen.faker_pool_selection",
        family="numpy_pcg64",
        call_site="generation/_faker_pool.py:192",
        entropy_root="job_seed",
        seed_derivation=(
            "selection_seed = GenDeriveContext.for_column(...).family_bytes("
            '"faker_pool_selection")[:8]'
        ),
        api_operation="PoolSampler.sample(pool, n, mode=REUSE, seed=selection_seed, deterministic=False)",
        call_shape="rng.integers(0, pool.size, size=n)  # whole-column, via PoolSampler",
        consumes_variable_draws=False,
        identity="column",
        null_draw_behavior="generation has no source column; every row draws a pool index",
        partitionable=False,
        config_fingerprint_source="strategy_config_fingerprint(column_config)",
        provider_version=f"{_V6} (GenDeriveContext.family_bytes); numpy NEP-19 PCG64",
        notes=(
            "REUSE only (Codex round-1 finding R2): generation is full-frame, not "
            "chunked, so there is no per-row source value to key a deterministic "
            "derive_index draw on the way mask.faker/gen.pool_deterministic do. "
            "Mechanically identical to gen.pool_nondeterministic (same PoolSampler "
            "REUSE branch) but keyed from THIS column's own family_bytes label, never "
            "job_seed directly, so two pooled columns with different config never "
            "collapse onto one selection stream."
        ),
        mirror_call_sites=("generation/pool/_sampler.py:136",),
    ),
)

__all__ = ["GEN_POOL_DRAW_SITES"]
