"""Provider-backed DrawSite entries of the RNG draw-site inventory.

Sites whose draw goes through a provider layer: the pool-backed masking
Faker and the Faker value-pool build behind it (the V2 PoolBuilder /
ProviderRegistry seam), and the providers_v2 synthetic-identifier adapters.
In catalogue order; ``_determinism_protocol.py`` splices them into
``DRAW_SITES`` and the concatenation order there is part of the determinism
contract.
"""

from __future__ import annotations

from decoy_engine.execution.native._draw_site_types import _V6, DrawSite

PROVIDER_BACKED_DRAW_SITES: tuple[DrawSite, ...] = (
    # -- Masking: pool-backed Faker (build + select) -------------------------
    DrawSite(
        draw_site_id="mask.faker",
        family="source_keyed_hmac",
        call_site="execution/_strategies/_faker.py:129",
        entropy_root="mask_key",
        seed_derivation=(
            "PoolSampler.sample(..., seed=ctx.mask_key) -> per-row "
            "derive_index(mask_key, namespace, canonical(source), pool.size)  # deterministic mode"
        ),
        api_operation="PoolSampler.sample (derive_index, deterministic selection)",
        call_shape="pool_values[derive_index(mask_key, namespace, canonical(source), pool.size)]",
        consumes_variable_draws=False,
        identity="source_value",
        null_draw_behavior="null source rows re-emit null and consume no index",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)+provider+pool build config",
        provider_version=_V6,
        notes=(
            "The masking 'faker' strategy, deterministic selection: the draw is the pool SELECTION "
            "(gen.pool_deterministic); the pool BUILD is gen.pool_build_faker, on job_seed. "
            "Non-deterministic reuse is mask.faker_nondeterministic; its other modes are "
            "whole-column numpy draws. Native (2.3): derive_index_batch per chunk."
        ),
        mirror_call_sites=("generation/pool/_sampler.py:225",),
    ),
    # -- Generation: Faker value-pool build (build-time draw) ----------------
    DrawSite(
        draw_site_id="gen.pool_build_faker",
        family="faker_seed_instance",
        call_site="generation/pool/_builder.py:161",
        entropy_root="job_seed",
        seed_derivation=(
            "pool_seed = derive(job_seed, 'pool/{provider}/{locale}/{namespace}', "
            "config_hash)[:8]  # _derive_pool_seed (_builder.py:57)"
        ),
        api_operation="adapter.generate_batch(spec=ProviderSpec(seed=pool_seed))",
        call_shape="build 'size' fresh values via a pool_seed-seeded Faker/provider batch",
        consumes_variable_draws=True,
        identity="none",
        null_draw_behavior="build-time; the pool is filled before any output row is drawn",
        partitionable=True,
        config_fingerprint_source="PoolBuilder.identity_for(provider, seed, locale, config, namespace)",
        provider_version=f"{_V6} (pool_seed via derive); Faker/provider adapter",
        notes=(
            "The Faker pool builder the spec calls out. Pool identity is a pure function "
            "of (job_seed, provider, locale, config, namespace), so a cached and a rebuilt "
            "pool of the same identity are value-identical (partition-safe). The physical "
            "seed_instance is in the Faker adapter (providers_v2/_faker_adapter.py:224: "
            'fake.seed_instance(int.from_bytes(spec.seed, "big"))); the Mimesis adapter '
            "seeds a fresh Generic(locale, seed=int.from_bytes(spec.seed)) per batch "
            "(providers_v2/mimesis/_adapter.py:167)."
        ),
        mirror_call_sites=(
            "providers_v2/_faker_adapter.py:224",
            "providers_v2/mimesis/_adapter.py:167",
        ),
    ),
    # -- Providers_v2: synthetic-identifier adapters -------------------------
    DrawSite(
        draw_site_id="gen.identifier_deterministic",
        family="source_keyed_hmac",
        call_site="providers_v2/identifiers/_ssn.py:159",
        entropy_root="mask_key",
        seed_derivation=(
            "derive_value(seed=spec.seed, namespace=spec.namespace, source=canonical, "
            "domain=SsnDomain(rng_config=spec.extra))  where derive_value(seed, ns, src, "
            "domain) = domain.from_bytes(derive(seed, ns, src))"
        ),
        api_operation="determinism.derive_value -> domain.from_bytes(HMAC-SHA256 digest)",
        call_shape="derive_value(...) per non-null source row; poolable=False",
        consumes_variable_draws=False,
        identity="source_value",
        null_draw_behavior="deterministic mode only when source_value is present; null passes to the unseeded path",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)+domain(rng_config=spec.extra)",
        provider_version=_V6,
        notes=(
            "The 9 synthetic-identifier adapters (ssn, npi, ein, iban, pan, icd10, mrn, "
            "ndc, cusip) share this shape. derive_value is a THIRD keyed primitive beside "
            "derive/derive_index: a thin domain-typed wrapper over derive "
            "(determinism/_derive.py:342). poolable=False, so these are NOT subsumed by "
            "gen.pool_deterministic: each is a genuinely separate per-row source-keyed "
            "site. spec.seed carries mask_key on the masking side and job_seed on pure "
            "generation; both are valid derive IKM lengths. Mirrors list the other 8 "
            "adapters' derive_value call."
        ),
        mirror_call_sites=(
            "providers_v2/identifiers/_npi.py:131",
            "providers_v2/identifiers/_ein.py:200",
            "providers_v2/identifiers/_iban.py:244",
            "providers_v2/identifiers/_pan.py:133",
            "providers_v2/identifiers/_icd10.py:127",
            "providers_v2/identifiers/_mrn.py:131",
            "providers_v2/identifiers/_ndc.py:139",
            "providers_v2/identifiers/_cusip.py:190",
        ),
    ),
    DrawSite(
        draw_site_id="gen.identifier_nondeterministic",
        family="numpy_pcg64",
        call_site="providers_v2/identifiers/_ssn.py:165",
        entropy_root="none",
        seed_derivation="np.random.default_rng()  # unseeded, S4 random-by-default contract",
        api_operation="numpy.random.default_rng() then the provider's generate_random(rng=...)",
        call_shape="per row in generate(); per batch in generate_batch()",
        consumes_variable_draws=True,
        identity="none",
        null_draw_behavior="non-deterministic path draws fresh values; not reproducible",
        partitionable=False,
        config_fingerprint_source="none(non-deterministic)",
        provider_version="numpy NEP-19 PCG64",
        notes=(
            "Every identifier adapter has two unseeded default_rng() draws: one in "
            "generate() (per row) and one in generate_batch() (per batch), across all 9 "
            "adapters. Non-deterministic by contract (output differs run to run)."
        ),
        mirror_call_sites=(
            "providers_v2/identifiers/_ssn.py:178",
            "providers_v2/identifiers/_npi.py:137",
            "providers_v2/identifiers/_ein.py:206",
            "providers_v2/identifiers/_iban.py:250",
            "providers_v2/identifiers/_pan.py:139",
            "providers_v2/identifiers/_icd10.py:133",
            "providers_v2/identifiers/_mrn.py:137",
            "providers_v2/identifiers/_ndc.py:145",
            "providers_v2/identifiers/_cusip.py:196",
        ),
    ),
)
