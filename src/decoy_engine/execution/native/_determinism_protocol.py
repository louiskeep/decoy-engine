"""Machine-checkable RNG draw-site inventory (native program, Task 0.1).

This module is the single source of truth the native columnar-streaming
determinism protocol (Task 0.3) is built against. Every place the engine
consumes randomness to produce masked or generated output is catalogued
here as one ``DrawSite`` entry, recording the EXACT seed derivation and
call shape in the code today. Task 0.3 must reproduce each entry's byte
sequence exactly, so accuracy of ``seed_derivation`` and ``call_shape`` is
the contract. The prose companion is ``docs/native/draw-site-inventory.md``.

This module is pure data plus small helpers. It imports nothing from the
masking or generation hot paths and changes no behavior.

Partitionability rule (the property the native batched executor cares
about): a site is ``partitionable`` when a partitioned or batched pass can
reproduce the exact same value for a given output row as a single serial
pass. Per-row source-keyed derivations are partitionable (row output is a
pure function of that row's key). Whole-column ``permutation(n)`` /
``choices(k=n)`` streams and per-group sequential streams are NOT: a row's
value depends on the position of every earlier draw in the stream, which a
partition boundary breaks. Per-row-reseeded streams are partitionable when
the reseed key is a stable per-row identity (row index or source value).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from decoy_engine.determinism import SEED_PROTOCOL_VERSION

# ---------------------------------------------------------------------------
# Allowed enums. The coverage test asserts every entry draws only from these.
# ---------------------------------------------------------------------------

# RNG families. Each names the determinism MECHANISM that Task 0.3 preserves,
# not merely the library: two numpy sites can differ in whether they reseed
# per row, per group, or draw one whole-column stream.
FAMILIES: frozenset[str] = frozenset(
    {
        "numpy_pcg64",  # draws from a numpy.random.Generator (default_rng / PCG64)
        "python_mt19937",  # draws from a stdlib random.Random (Mersenne Twister)
        "per_row_reseed",  # one Random reseeded every row from a per-row key
        "source_keyed_hmac",  # the HMAC/HKDF digest itself IS the pseudo-randomness
        "per_group_stream",  # one RNG stream advanced sequentially within a group
        "faker_seed_instance",  # Faker.seed_instance(...) then a provider draw
        "gen_derive_context",  # the GenDeriveContext seed-derivation substrate
    }
)

# The run-level entropy root each site is ultimately seeded from. The engine's
# real names: ``mask_key`` is ``StrategyContext.mask_key`` (the 8-byte job_seed
# verbatim when no KeyProvider secret is present, else a 32-byte HKDF root);
# ``job_seed`` is the 8-byte generation seed the generation path threads as
# ``seed``. ``none`` is a genuinely unseeded (non-deterministic-mode) site.
ENTROPY_ROOTS: frozenset[str] = frozenset({"mask_key", "job_seed", "none"})

# What a single draw is keyed on.
IDENTITIES: frozenset[str] = frozenset(
    {
        "row_index",  # the row's positional index within the column/frame
        "global_row_number",  # a row number that must survive partition boundaries
        "source_value",  # the input cell value (mask-side, keyed by content)
        "group_key",  # the partition/group label
        "column",  # whole-column single stream (no per-row key)
        "none",  # substrate / build-time, no per-output-row identity
    }
)

# Sentinel for a live strategy or generation kind that provably draws no
# randomness (identity transform, deterministic slice, expression eval).
DETERMINISTIC_NO_DRAW = "deterministic_no_draw"

_V6 = f"seed_protocol_v{SEED_PROTOCOL_VERSION}"


@dataclass(frozen=True)
class DrawSite:
    """One place randomness is consumed to produce masked or generated output.

    Fields are the Task 0.1 record. ``seed_derivation`` and ``call_shape`` are
    verbatim-faithful to the code at ``call_site`` and are the exact behavior
    Task 0.3 preserves. ``notes`` and ``uncertain`` are additive: ``uncertain``
    flags a site whose behavior could not be fully determined from the code.
    """

    draw_site_id: str
    family: str
    call_site: str
    entropy_root: str
    seed_derivation: str
    api_operation: str
    call_shape: str
    consumes_variable_draws: bool
    identity: str
    null_draw_behavior: str
    partitionable: bool
    config_fingerprint_source: str
    provider_version: str
    notes: str = ""
    uncertain: bool = False
    # Sibling call sites that implement the SAME mechanism byte-for-byte
    # (V1/V2 engines, pandas/polars substrates, in-core/out-of-core). Recorded
    # so the protocol knows every physical location that must move in lockstep.
    mirror_call_sites: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# The inventory. Grouped by family in the doc; here ordered mask sites first,
# then generation sites, then the derivation substrate.
# ---------------------------------------------------------------------------

DRAW_SITES: tuple[DrawSite, ...] = (
    # -- Masking: whole-column numpy permutation -----------------------------
    DrawSite(
        draw_site_id="mask.shuffle",
        family="numpy_pcg64",
        call_site="execution/_strategies/_shuffle.py:56",
        entropy_root="mask_key",
        seed_derivation=(
            "int.from_bytes(derive(ctx.mask_key, plan.namespace, "
            'column.encode("utf-8"))[:8], "big")'
        ),
        api_operation="numpy.random.default_rng(seed_int).permutation",
        call_shape="permutation(len(non_na_values))  # whole-column",
        consumes_variable_draws=False,
        identity="column",
        null_draw_behavior="nulls excluded before the draw; only non-null values are permuted",
        partitionable=False,
        config_fingerprint_source="namespace_registry(plan.namespace)+column_name",
        provider_version=f"{_V6}; numpy NEP-19 PCG64",
        notes=(
            "Whole-column multiset permutation. A partition sees only its slice, "
            "so it cannot reproduce the global permutation order. Non-deterministic "
            "mode uses an unseeded default_rng()."
        ),
        mirror_call_sites=("execution/polars/_strategies/_shuffle.py:57",),
    ),
    # -- Masking: source-keyed HMAC family (per-row, partitionable) ----------
    DrawSite(
        draw_site_id="mask.hash",
        family="source_keyed_hmac",
        call_site="kernel/_scalar.py:75",
        entropy_root="mask_key",
        seed_derivation="derive(seed, namespace, canonicalize_derive_source(value))",
        api_operation="determinism.derive -> HMAC-SHA256 digest hex",
        call_shape="derive(...).hex()[:truncate]  # per value",
        consumes_variable_draws=False,
        identity="source_value",
        null_draw_behavior="null/NaN rows emit null and consume no derivation",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)+truncate",
        provider_version=_V6,
        notes="The HMAC digest IS the output; no RNG object. Joinability-preserving.",
        mirror_call_sites=("execution/polars/_strategies/_hash.py:53",),
    ),
    DrawSite(
        draw_site_id="mask.categorical_deterministic",
        family="source_keyed_hmac",
        call_site="execution/_strategies/_categorical.py:187",
        entropy_root="mask_key",
        seed_derivation=(
            "derive_index(ctx.mask_key, plan.namespace, _canonicalize_source(value), "
            "pool_size=len(categories))  # weighted path uses pool_size=_WEIGHTED_CDF_RES"
        ),
        api_operation="determinism.derive_index -> index in [0, pool_size)",
        call_shape="categories[derive_index(...)]  # per value; weighted: bisect over CDF",
        consumes_variable_draws=False,
        identity="source_value",
        null_draw_behavior="null rows emit null and consume no derivation",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)+categories+weights",
        provider_version=_V6,
        notes=(
            "Deterministic mode. Out-of-core path reuses the same derive_index over the "
            "plan category pool (execution/out_of_core/_mask_group_b.py:336)."
        ),
        mirror_call_sites=(
            "execution/out_of_core/_mask_group_b.py:336",
            "execution/out_of_core/_mask_group_b.py:346",
            "execution/polars/_strategies/_categorical.py:113",
        ),
    ),
    DrawSite(
        draw_site_id="mask.categorical_nondeterministic",
        family="numpy_pcg64",
        call_site="execution/_strategies/_categorical.py:215",
        entropy_root="none",
        seed_derivation="np.random.default_rng()  # unseeded, non-deterministic contract (M2)",
        api_operation="numpy.random.default_rng().integers / .choice",
        call_shape="rng.integers(0, len(categories), n)  # whole-column",
        consumes_variable_draws=False,
        identity="column",
        null_draw_behavior="nulls handled around the draw; unseeded so not reproducible",
        partitionable=False,
        config_fingerprint_source="none(non-deterministic)",
        provider_version="numpy NEP-19 PCG64",
        notes="Non-deterministic contract: output differs run to run by design.",
        mirror_call_sites=("execution/polars/_strategies/_categorical.py:139",),
    ),
    DrawSite(
        draw_site_id="mask.fpe",
        family="source_keyed_hmac",
        call_site="transforms/fpe.py:344",
        entropy_root="mask_key",
        seed_derivation="derive(mask_key, namespace, FPE_KEY_LABEL)  # per-column Feistel key",
        api_operation="8-round type-II Feistel permutation (HMAC-SHA256 round function)",
        call_shape="fpe_encrypt_value(value, key, tweak)  # per value, format-preserving",
        consumes_variable_draws=False,
        identity="source_value",
        null_draw_behavior="null rows pass through unmasked; no keyed draw",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)+alphabet/format",
        provider_version=_V6,
        notes="Keyed bijection, home-rolled HMAC-SHA256 Feistel (NOT NIST FF1). Reversible.",
        mirror_call_sites=(
            "execution/_strategies/_fpe.py:135",
            "execution/out_of_core/_mask_group_b.py:132",
        ),
    ),
    DrawSite(
        draw_site_id="mask.date_shift",
        family="source_keyed_hmac",
        call_site="transforms/date_shift.py:120",
        entropy_root="mask_key",
        seed_derivation=(
            "min_days + (int.from_bytes(hmac_new(key, val, sha256).digest()[:8], "
            '"big") % range_size)  # keyed path; legacy path uses md5(val)'
        ),
        api_operation="HMAC-SHA256(column_key, value) reduced mod range_size",
        call_shape="per-value integer day offset in [min_days, max_days]",
        consumes_variable_draws=False,
        identity="source_value",
        null_draw_behavior="unparseable/null dates returned unchanged; no draw",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)+min_days+max_days",
        provider_version=_V6,
        notes=(
            "Execution handler derives the column key via derive(mask_key, namespace, "
            "value) (execution/_strategies/_date_shift.py:185). Legacy no-key configs "
            "use md5(value) (transforms/date_shift.py:108)."
        ),
        mirror_call_sites=("execution/_strategies/_date_shift.py:185",),
    ),
    DrawSite(
        draw_site_id="mask.bucket_perturb",
        family="source_keyed_hmac",
        call_site="transforms/bucket_perturb.py:118",
        entropy_root="mask_key",
        seed_derivation="derive(job_seed, namespace, _canonicalize_source(value_str))",
        api_operation="determinism.derive -> within-bucket offset",
        call_shape="per-value bucket position from the derived digest",
        consumes_variable_draws=False,
        identity="source_value",
        null_draw_behavior="null rows emit null; no derivation",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)+bucket_size",
        provider_version=_V6,
    ),
    DrawSite(
        draw_site_id="mask.group_key",
        family="source_keyed_hmac",
        call_site="transforms/group_key.py:171",
        entropy_root="mask_key",
        seed_derivation="derive(seed, namespace, source)  # per group-by source bytes",
        api_operation="determinism.derive -> 32-byte group key",
        call_shape="raw_bytes = derive(...); per distinct group source",
        consumes_variable_draws=False,
        identity="source_value",
        null_draw_behavior="deterministic on source bytes; nulls encode as their source form",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)",
        provider_version=_V6,
    ),
    DrawSite(
        draw_site_id="mask.code_set",
        family="source_keyed_hmac",
        call_site="transforms/code_set.py:592",
        entropy_root="mask_key",
        seed_derivation=(
            "derive_index(hmac_key=derive(mask_key, namespace or 'code_set', _KEYED_SALT), "
            "namespace, source, pool_size=hole_candidate_count)"
        ),
        api_operation="determinism.derive_index over the code-set hole candidates",
        call_shape="per value (mask) or per row_index (gen-mode) modular index",
        consumes_variable_draws=False,
        identity="source_value",
        null_draw_behavior="null rows emit null; no index draw",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)+code_set_id",
        provider_version=_V6,
        notes="Gen-mode keys on row_index instead of source value; both are per-row.",
    ),
    DrawSite(
        draw_site_id="mask.joint_mask_keyed_row",
        family="source_keyed_hmac",
        call_site="reference_tables/_types.py:87",
        entropy_root="mask_key",
        seed_derivation=(
            "hmac_hex(hmac_key, key_value) % row_count, where hmac_key = "
            "derive(mask_key, namespace, _KEYED_ROW_SOURCE) (joint_mask.py:63)"
        ),
        api_operation="HMAC-SHA256 modular index into the id-sorted reference table",
        call_shape="keyed_row(key_value, hmac_key=...)  # per masked key value",
        consumes_variable_draws=False,
        identity="source_value",
        null_draw_behavior="selection keyed on the masked key value; deterministic",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)+reference_table_version",
        provider_version=_V6,
        notes=(
            "keyed_row is the primitive; joint_mask (transforms/joint_mask.py:223) is "
            "the consumer. Deterministic WITHIN a table version only (index shifts if "
            "row_count changes)."
        ),
        mirror_call_sites=("transforms/joint_mask.py:223",),
    ),
    DrawSite(
        draw_site_id="mask.text_mask_date_shift",
        family="source_keyed_hmac",
        call_site="transforms/text_mask.py:308",
        entropy_root="mask_key",
        seed_derivation=(
            'min_days + (int.from_bytes(span_key[:8], "big") % range_size); '
            "span_key = HMAC-SHA256(mask_key, matched_text)"
        ),
        api_operation="HMAC span key reduced mod range_size",
        call_shape="per-detected-span date offset",
        consumes_variable_draws=False,
        identity="source_value",
        null_draw_behavior="unparseable spans returned unchanged; no draw",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)+detector+min/max_days",
        provider_version=_V6,
        notes="Free-text detector spans; the span text keys the shift.",
    ),
    # -- Masking: Faker seeded from a source-keyed span ----------------------
    DrawSite(
        draw_site_id="mask.text_mask_faker",
        family="faker_seed_instance",
        call_site="transforms/text_mask.py:262",
        entropy_root="mask_key",
        seed_derivation='seed = int.from_bytes(span_key[:4], "big")  # span_key = HMAC(mask_key, text)',
        api_operation="Faker().seed_instance(seed) then provider method()",
        call_shape="fake.seed_instance(seed); method()  # per detected span",
        consumes_variable_draws=True,
        identity="source_value",
        null_draw_behavior="non-matching text untouched; only detected spans draw",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)+detector->faker method",
        provider_version="Faker (seed_instance detaches a per-instance random.Random)",
        notes=(
            "4-byte seed keyspace. Provider draw count varies by provider, but each span "
            "reseeds from its own span key, so output is a pure function of the span text."
        ),
    ),
    # -- Masking: per-group sequential stream --------------------------------
    DrawSite(
        draw_site_id="mask.grouped_series_monotone_walk",
        family="per_group_stream",
        call_site="transforms/grouped_series.py:274",
        entropy_root="mask_key",
        seed_derivation=(
            'int.from_bytes(derive(seed, namespace, str(g).encode("utf-8", '
            'errors="replace"))[:8], "big")  # one seed per group label g'
        ),
        api_operation="numpy.random.default_rng(g_seed).integers, advanced per row in group",
        call_shape="group_rng.integers(step, max_step + 1)  # sequential within a group",
        consumes_variable_draws=False,
        identity="group_key",
        null_draw_behavior="cumcount generator draws nothing; monotone_walk draws per non-first row",
        partitionable=False,
        config_fingerprint_source="namespace_registry(namespace)+group_by+order_by+step bounds",
        provider_version=f"{_V6}; numpy NEP-19 PCG64",
        notes=(
            "The walk is a sequential stream ordered by (group, order_by). A partition "
            "that splits a group cannot reproduce the cumulative sum. cumcount is a "
            "deterministic-no-draw sibling generator in the same module."
        ),
    ),
    # -- Masking: per-row numpy seeded by row index --------------------------
    DrawSite(
        draw_site_id="mask.windowed_date",
        family="numpy_pcg64",
        call_site="transforms/windowed_date.py:209",
        entropy_root="mask_key",
        seed_derivation='int.from_bytes(derive(seed, namespace, i.to_bytes(8, "big"))[:8], "big")',
        api_operation="numpy.random.default_rng(row_seed).integers, fresh Generator per row",
        call_shape="row_rng.integers(min_days, max_days + 1)  # 1 draw (uniform) or 2 (early/late)",
        consumes_variable_draws=True,
        identity="row_index",
        null_draw_behavior="every row draws; anchor is required per row",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)+anchor+min/max_days+distribution",
        provider_version=f"{_V6}; numpy NEP-19 PCG64",
        notes=(
            "Per-row seed keys on the enumerate() index i, NOT the source value. "
            "Partitionable ONLY if the native executor preserves the same global row "
            "index i under partitioning; if i resets per batch the sequence diverges. "
            "Task 0.3 must pin i to a global row number. Out-of-core mirror at "
            "execution/out_of_core/_mask_group_c.py already feeds row_index into the "
            "derive_index seed."
        ),
        uncertain=True,
        mirror_call_sites=("execution/out_of_core/_mask_group_c.py:43",),
    ),
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
            "stream, so a partition cannot resume mid-sequence."
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
    # -- Masking: pool-backed Faker (build + select) -------------------------
    DrawSite(
        draw_site_id="mask.faker",
        family="source_keyed_hmac",
        call_site="execution/_strategies/_faker.py:102",
        entropy_root="mask_key",
        seed_derivation=(
            "select_seed = ctx.mask_key if plan.deterministic else ctx.job_seed; "
            "PoolSampler.sample(..., seed=select_seed) -> per-row derive_index over the pool "
            "(non-deterministic mode: np.random.default_rng off job_seed)"
        ),
        api_operation="PoolSampler.sample (derive_index deterministic; default_rng otherwise)",
        call_shape="pool_values[derive_index(mask_key, namespace, canonical(source), pool.size)]",
        consumes_variable_draws=False,
        identity="source_value",
        null_draw_behavior="null source rows re-emit null and consume no index",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)+provider+pool build config",
        provider_version=_V6,
        notes=(
            "The masking 'faker' strategy. The value-visible draw is the pool SELECTION "
            "(mask.faker), backed by gen.pool_deterministic / gen.pool_nondeterministic. "
            "The pool BUILD (fresh Faker values) is gen.pool_build_faker. Deterministic "
            "selection re-keys onto mask_key; the build stays on job_seed."
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


# ---------------------------------------------------------------------------
# Coverage maps. The test cross-checks these against the LIVE registries
# (execution._strategies.SCALAR_HANDLERS and config._tables.GENERATE_TYPES),
# so adding a strategy or generation kind without cataloguing it fails.
# Each value is a DrawSite.draw_site_id or the DETERMINISTIC_NO_DRAW sentinel.
# ---------------------------------------------------------------------------

MASK_STRATEGY_TO_SITE: dict[str, str] = {
    "passthrough": DETERMINISTIC_NO_DRAW,
    "redact": DETERMINISTIC_NO_DRAW,
    "truncate": DETERMINISTIC_NO_DRAW,
    "bucketize": DETERMINISTIC_NO_DRAW,
    "top_code": DETERMINISTIC_NO_DRAW,
    "geo_generalize": DETERMINISTIC_NO_DRAW,
    "derived": DETERMINISTIC_NO_DRAW,
    "derived_aggregate": DETERMINISTIC_NO_DRAW,
    "text_redact": DETERMINISTIC_NO_DRAW,
    "nested": DETERMINISTIC_NO_DRAW,  # delegates to a child handler; no own draw
    "hash": "mask.hash",
    "shuffle": "mask.shuffle",
    "categorical": "mask.categorical_deterministic",  # + mask.categorical_nondeterministic
    "fpe": "mask.fpe",
    "date_shift": "mask.date_shift",
    "bucket_perturb": "mask.bucket_perturb",
    "group_key": "mask.group_key",
    "code_set": "mask.code_set",
    "joint_mask": "mask.joint_mask_keyed_row",
    "faker": "mask.faker",
    "formula": "mask.formula",
    "text_mask": "mask.text_mask_faker",  # + mask.text_mask_date_shift
    "grouped_series": "mask.grouped_series_monotone_walk",
    "windowed_date": "mask.windowed_date",
}

GEN_KIND_TO_SITE: dict[str, str] = {
    "sequence": DETERMINISTIC_NO_DRAW,
    "derived": DETERMINISTIC_NO_DRAW,
    "derived_aggregate": DETERMINISTIC_NO_DRAW,
    "group_key": "mask.group_key",  # group_key generation reuses the same derive()
    "faker": "gen.faker_per_row",
    "categorical": "gen.categorical",
    "reference": "gen.reference",
    "formula": "gen.formula_per_row",
    "statistical": "gen.statistical_per_row",
    "grouped_series": "mask.grouped_series_monotone_walk",
    "windowed_date": "mask.windowed_date",
}

# Synthetic-identifier providers (providers_v2). Cross-checked against the LIVE
# ProviderRegistry: the test enumerates every registered provider whose adapter
# is one of the identifier adapter classes and asserts the set equals these
# keys, so a new identifier adapter (a new derive_value / default_rng draw) fails
# until catalogued. Each has a deterministic (source_keyed_hmac) draw and a
# non-deterministic (unseeded default_rng) draw; the map points at the
# deterministic site, and gen.identifier_nondeterministic covers the other.
PROVIDER_IDENTIFIER_SITES: dict[str, str] = {
    "synthetic_ssn": "gen.identifier_deterministic",
    "synthetic_npi": "gen.identifier_deterministic",
    "synthetic_ein": "gen.identifier_deterministic",
    "synthetic_iban": "gen.identifier_deterministic",
    "synthetic_pan": "gen.identifier_deterministic",
    "synthetic_icd10": "gen.identifier_deterministic",
    "synthetic_mrn": "gen.identifier_deterministic",
    "synthetic_ndc": "gen.identifier_deterministic",
    "synthetic_cusip": "gen.identifier_deterministic",
}

# The mask.formula site (its own random.Random over the mask side).
_MASK_FORMULA = DrawSite(
    draw_site_id="mask.formula",
    family="python_mt19937",
    call_site="transforms/formula.py:55",
    entropy_root="none",
    seed_derivation=(
        'formula_seed = int(sha256(f"{col_name}|{expr}").hexdigest()[:16], 16); '
        "rng = random.Random(formula_seed)"
    ),
    api_operation="random.Random(formula_seed) exposed as randint/choice/random in the scope",
    call_shape="column.apply(lambda v: safe_eval(expr, make_mask_globals(rng), {'value': v}))",
    consumes_variable_draws=True,
    identity="column",
    null_draw_behavior="null cells skipped by pd.isna; a non-null row draws only if the formula calls rng",
    partitionable=False,
    config_fingerprint_source="sha256(column_name + '|' + formula_text)",
    provider_version="CPython Mersenne Twister",
    notes=(
        "One Random shared across all rows via column.apply, seeded from the formula "
        "text and column name (NOT the mask_key or namespace). Whether a row consumes a "
        "draw depends on the formula body, so the stream is order-dependent and NOT "
        "partitionable. Non-deterministic across mask_key by design (self-seeded)."
    ),
)

DRAW_SITES = (*DRAW_SITES, _MASK_FORMULA)


def draw_site_by_id(draw_site_id: str) -> DrawSite:
    """Return the single ``DrawSite`` with ``draw_site_id`` (raises if absent)."""
    for site in DRAW_SITES:
        if site.draw_site_id == draw_site_id:
            return site
    raise KeyError(draw_site_id)


__all__ = [
    "DETERMINISTIC_NO_DRAW",
    "DRAW_SITES",
    "ENTROPY_ROOTS",
    "FAMILIES",
    "GEN_KIND_TO_SITE",
    "IDENTITIES",
    "MASK_STRATEGY_TO_SITE",
    "PROVIDER_IDENTIFIER_SITES",
    "DrawSite",
    "draw_site_by_id",
]
