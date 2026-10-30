"""Generation-side DrawSite entries of the RNG draw-site inventory.

Per-row Faker reseeds, whole-column and sequential python Random draws, the
per-row formula and statistical reseeds, the whole-column numpy post-passes,
the value-pool sampler and the composite bundle-pool build, in catalogue
order, plus the ``GenDeriveContext`` seed-derivation substrate.
``_determinism_protocol.py`` splices them into ``DRAW_SITES``; the
concatenation order there is part of the determinism contract.
"""

from __future__ import annotations

from decoy_engine.execution.native._draw_site_types import _V6, DrawSite

GEN_DRAW_SITES: tuple[DrawSite, ...] = (
    # -- Generation: Faker per-row reseed ------------------------------------
    DrawSite(
        draw_site_id="gen.faker_per_row",
        family="faker_seed_instance",
        call_site="generation/synthesize.py:490",
        entropy_root="job_seed",
        seed_derivation='faker_inst.seed_instance(gen_ctx.row_int("faker", i))',
        api_operation="Faker.seed_instance(row_int) then provider_func(**kwargs)",
        call_shape="per row: seed_instance(row_int('faker', i)); provider_func(...)",
        consumes_variable_draws=True,
        identity="row_index",
        null_draw_behavior="every row seeds+draws; null_probability is a separate post-pass",
        partitionable=True,
        config_fingerprint_source="strategy_config_fingerprint(column_config)",
        provider_version=f"{_V6} (GenDeriveContext); Faker seed_instance",
        notes=(
            "row_int('faker', i) = HMAC over the faker family key and i. V1 mirror at "
            "generators/columns.py:329 is byte-identical. Provider draw count varies by "
            "provider but each row reseeds from its own HMAC(i)."
        ),
        mirror_call_sites=("generators/columns.py:329",),
    ),
    # -- Generation: whole-column python Random choices ----------------------
    DrawSite(
        draw_site_id="gen.categorical",
        family="python_mt19937",
        call_site="generation/synthesize.py:433",
        entropy_root="job_seed",
        seed_derivation=(
            'random.Random(GenDeriveContext.for_column(derive_key, col, seed).base_int("py"))'
        ),
        api_operation="random.Random(col_seed).choices",
        call_shape="rng.choices(cats, weights=weights, k=n)  # single whole-column call",
        consumes_variable_draws=False,
        identity="column",
        null_draw_behavior="null_probability applied as a separate post-pass",
        partitionable=False,
        config_fingerprint_source="strategy_config_fingerprint(column_config)",
        provider_version=f"{_V6} (GenDeriveContext); CPython Mersenne Twister",
        notes="One choices(k=n) stream over the whole column. V1 mirror generators/columns.py:412.",
        mirror_call_sites=("generators/columns.py:412",),
    ),
    # -- Generation: reference / FK sampling (sequential python Random) ------
    DrawSite(
        draw_site_id="gen.reference",
        family="python_mt19937",
        call_site="generation/synthesize.py:624",
        entropy_root="job_seed",
        seed_derivation='random.Random(GenDeriveContext.for_column(...).base_int("py"))',
        api_operation="random.Random(col_seed).choice / .choices per row",
        call_shape="per row: rng.choice(ref_vals) | rng.choices(..., k=1)  # sequential stream",
        consumes_variable_draws=True,
        identity="column",
        null_draw_behavior="sequential distribution draws nothing (index i % len); random/weighted draw per row",
        partitionable=False,
        config_fingerprint_source="strategy_config_fingerprint(column_config)+sorted parent pool",
        provider_version=f"{_V6} (GenDeriveContext); CPython Mersenne Twister",
        notes=(
            "One Random advanced across all rows: a partition cannot resume mid-stream. "
            "Cardinality repair (generators/columns.py:596-656) adds rng.shuffle/choices "
            "on the same stream. V1 mirror generators/columns.py:507."
        ),
        mirror_call_sites=("generators/columns.py:507", "generators/columns.py:596"),
    ),
    # -- Generation: per-row reseeded formula / faker scope ------------------
    DrawSite(
        draw_site_id="gen.formula_per_row",
        family="per_row_reseed",
        call_site="generators/columns.py:202",
        entropy_root="job_seed",
        seed_derivation=(
            'local_seed = gen_ctx.row_int("py", i); row_rng.seed(local_seed); '
            'self.faker.seed_instance(gen_ctx.row_int("faker", i))'
        ),
        api_operation="random.Random reseeded per row; Faker.seed_instance per row",
        call_shape="per row: row_rng.seed(row_int('py', i)); safe_eval(formula, scope)",
        consumes_variable_draws=True,
        identity="row_index",
        null_draw_behavior="every row reseeds; whether random/faker is consumed depends on the formula",
        partitionable=True,
        config_fingerprint_source="strategy_config_fingerprint(column_config)",
        provider_version=f"{_V6} (GenDeriveContext); CPython MT + Faker",
        notes=(
            "One Random instance reseeded each row from HMAC(i), so cross-row order does "
            "not matter. Referenced-formula post-pass mirror at generators/_formula.py:144."
        ),
        mirror_call_sites=(
            "generators/_formula.py:144",
            "generation/synthesize.py:559",
        ),
    ),
    # -- Generation: statistical sampler per-row reseed ----------------------
    DrawSite(
        draw_site_id="gen.statistical_per_row",
        family="per_row_reseed",
        call_site="generation/statistical/_sample.py:260",
        entropy_root="job_seed",
        seed_derivation="rng.seed(col_seed + i)  # one random.Random reseeded per row",
        api_operation="random.Random reseeded per row; .random / .choices / .randrange",
        call_shape="per row: rng.seed(col_seed + i); inverse-CDF / weighted draw",
        consumes_variable_draws=True,
        identity="row_index",
        null_draw_behavior="every row draws; sampler emits a value for each row",
        partitionable=True,
        config_fingerprint_source="StatisticalSpec (snapshot_digest-pinned) + col_seed",
        provider_version="CPython Mersenne Twister (no numpy; bit-stable inverse-CDF)",
        notes=(
            "col_seed + i (legacy per-row idiom, not GenDeriveContext). Reseed-per-row "
            "makes any chunking byte-identical to a serial pass. freetext draws a variable "
            "number of randrange calls within the row, but the per-row reseed contains it."
        ),
    ),
    # -- Generation: null-probability post-pass (whole-column numpy) ---------
    DrawSite(
        draw_site_id="gen.null_probability",
        family="numpy_pcg64",
        call_site="generation/synthesize.py:521",
        entropy_root="job_seed",
        seed_derivation=('np.random.default_rng(GenDeriveContext.for_column(...).base_int("np"))'),
        api_operation="numpy.random.default_rng(col_seed).random(n)",
        call_shape="null_mask = rng.random(len(out)) < null_prob  # whole-column",
        consumes_variable_draws=False,
        identity="column",
        null_draw_behavior="this IS the null decision: one float per row from a single stream",
        partitionable=False,
        config_fingerprint_source="strategy_config_fingerprint(column_config)+null_probability",
        provider_version=f"{_V6} (GenDeriveContext); numpy NEP-19 PCG64",
        notes=(
            "One contiguous random(n) stream: row i's float depends on n and stream "
            "position, so a partition cannot reproduce it without the full prefix. V1 "
            "mirror generators/columns.py:221."
        ),
        mirror_call_sites=("generators/columns.py:221",),
    ),
    # -- Generation: distribution-snapshot sampler (whole-column numpy) ------
    DrawSite(
        draw_site_id="gen.distribution_snapshot",
        family="numpy_pcg64",
        call_site="generators/_distribution.py:141",
        entropy_root="job_seed",
        seed_derivation='np.random.default_rng(self._column_ctx(name, cfg).base_int("np"))',
        api_operation="numpy.random.default_rng(col_seed).choice / .uniform / .random",
        call_shape="rng.choice(k, size=num_rows, p=probs); rng.uniform(lo, hi)  # whole-column",
        consumes_variable_draws=False,
        identity="column",
        null_draw_behavior="degenerate snapshots emit nulls without drawing",
        partitionable=False,
        config_fingerprint_source="strategy_config_fingerprint(column_config)+snapshot",
        provider_version=f"{_V6} (GenDeriveContext); numpy NEP-19 PCG64",
        notes=(
            "Numeric, categorical, and datetime samplers all draw whole-column vectors "
            "(generators/_distribution.py:141, :266, :386)."
        ),
        mirror_call_sites=(
            "generators/_distribution.py:266",
            "generators/_distribution.py:386",
        ),
    ),
    # -- Generation: value-pool sampler --------------------------------------
    DrawSite(
        draw_site_id="gen.pool_deterministic",
        family="source_keyed_hmac",
        call_site="generation/pool/_sampler.py:225",
        entropy_root="job_seed",
        seed_derivation="derive_index(seed=seed, namespace=namespace, source=canonical, pool_size=pool.size)",
        api_operation="determinism.derive_index per non-null source row",
        call_shape="pool_values[derive_index(...)]  # per row keyed on source value",
        consumes_variable_draws=False,
        identity="source_value",
        null_draw_behavior="null source rows re-emit null and consume no index",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)+pool build config",
        provider_version=_V6,
        notes=(
            "Bundle path (sample_bundle, _sampler.py:355) and the pool adapter "
            "(_pool_adapter.py:156) use the same per-row derive_index."
        ),
        mirror_call_sites=(
            "generation/pool/_sampler.py:355",
            "generation/pool/_pool_adapter.py:156",
        ),
    ),
    DrawSite(
        draw_site_id="gen.pool_nondeterministic",
        family="numpy_pcg64",
        call_site="generation/pool/_sampler.py:122",
        entropy_root="job_seed",
        seed_derivation='np.random.default_rng(int.from_bytes(seed, "big"))',
        api_operation="numpy.random.default_rng(seed_int).integers / .permutation",
        call_shape="rng.integers(0, pool.size, size=n) | rng.permutation(...)[:k]  # whole-column",
        consumes_variable_draws=False,
        identity="column",
        null_draw_behavior="REUSE/UNIQUE/MATCH/SCALE draw over whole output; nulls scattered after",
        partitionable=False,
        config_fingerprint_source="pool build config + cardinality mode",
        provider_version=f"{_V6}; numpy NEP-19 PCG64",
        notes=(
            "Seeded but stream-positional: integers(size=n) and permutation() advance one "
            "stream, so a partition cannot resume mid-sequence. Masking reuse is "
            "mask.faker_nondeterministic."
        ),
    ),
    # -- Generation: composite bundle-pool build -----------------------------
    DrawSite(
        draw_site_id="gen.composite_build_pool",
        family="numpy_pcg64",
        call_site="generation/composite/_provider.py:116",
        entropy_root="job_seed",
        seed_derivation='np.random.default_rng(int.from_bytes(seed, "big"))  # seed = spec.seed or b"\\x00"*8',
        api_operation="numpy.random.default_rng(seed_int).integers / .bytes",
        call_shape="rng.integers(0, len(names), size=n); rng.bytes(8)  # whole-pool build",
        consumes_variable_draws=False,
        identity="none",
        null_draw_behavior="build-time pool construction; not per output row",
        partitionable=False,
        config_fingerprint_source="ProviderSpec(seed, extra config)",
        provider_version=f"{_V6}; numpy NEP-19 PCG64",
        notes=(
            "Build-side draw that fills the value pool before sampling. Mirrors: "
            "_name_email.py:134, _custom.py:206, _address.py:96. Output rows are then "
            "drawn by the pool sampler entries above."
        ),
        mirror_call_sites=(
            "generation/composite/_name_email.py:134",
            "generation/composite/_custom.py:206",
            "generation/composite/_address.py:96",
            "generation/composite/_person.py:142",
        ),
    ),
)

GEN_SUBSTRATE_DRAW_SITES: tuple[DrawSite, ...] = (
    # -- The seed-derivation substrate ---------------------------------------
    DrawSite(
        draw_site_id="gen.derive_context",
        family="gen_derive_context",
        call_site="generators/derivation.py:158",
        entropy_root="job_seed",
        seed_derivation=(
            'root = derive_key("gen:" + strategy_config_fingerprint(cfg)) | '
            "sha256(job_seed.to_bytes(8) + fingerprint) | os.urandom(32) if fresh; "
            'family_key = HMAC(root, b"fam:"+family); '
            "row_int = HMAC(family_key, family, extra=i.to_bytes(8))"
        ),
        api_operation="GenDeriveContext.for_column / .base_int / .row_int",
        call_shape="base_int(family) whole-column seed; row_int(family, i) per-row seed",
        consumes_variable_draws=False,
        identity="none",
        null_draw_behavior="substrate only; produces no output directly",
        partitionable=True,
        config_fingerprint_source="strategy_config_fingerprint(column_config)",
        provider_version=_V6,
        notes=(
            "Not a draw itself: the keying layer every generation family (py/np/faker) is "
            "seeded from. row_int is a pure function of (root, family, i), so it is "
            "partitionable; base_int seeds whole-column consumers that may not be. "
            "determinism: fresh swaps the root to os.urandom(32) (within-run stable)."
        ),
    ),
)
