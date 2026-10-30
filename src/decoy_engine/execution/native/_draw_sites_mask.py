"""Masking-side DrawSite entries of the RNG draw-site inventory.

The whole-column permutation, the source-keyed HMAC family, the
span-keyed Faker, the per-group stream and the row-index-reseeded sites, in
catalogue order, plus the ``mask.formula`` site. ``_determinism_protocol.py``
splices them into ``DRAW_SITES``; the concatenation order there is part of
the determinism contract.
"""

from __future__ import annotations

from decoy_engine.execution.native._draw_site_types import _V6, DrawSite

MASK_DRAW_SITES: tuple[DrawSite, ...] = (
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
    ),
    DrawSite(
        draw_site_id="mask.categorical_deterministic",
        family="source_keyed_hmac",
        call_site="execution/_strategies/_categorical.py:198",
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
        ),
    ),
    DrawSite(
        draw_site_id="mask.categorical_nondeterministic",
        family="source_keyed_hmac",
        call_site="execution/_strategies/_categorical.py:198",
        entropy_root="mask_key",
        seed_derivation="derive_index(mask_key, namespace, encode_int(row_offset + i), pool_size=n)",
        api_operation="determinism.derive_index -> index in [0, pool_size)",
        call_shape="categories[derive_index(...)]  # per non-null row; weighted: bisect over CDF",
        consumes_variable_draws=False,
        identity="row_index",
        null_draw_behavior="null rows emit null; the null still consumes its row ordinal",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)+categories+weights",
        provider_version=_V6,
        notes="i is the handler-frame ordinal, not the source value; see the inventory doc.",
        mirror_call_sites=("execution/native/_operator_step.py:335",),
    ),
    DrawSite(
        draw_site_id="mask.fpe",
        family="source_keyed_hmac",
        call_site="transforms/fpe.py:406",
        entropy_root="mask_key",
        seed_derivation="derive(mask_key, namespace, FF1_KEY_LABEL)  # per-column AES-256 FF1 key",
        api_operation="NIST SP 800-38G FF1 (10 rounds, AES-256 CBC-MAC PRF; see transforms/_ff1.py)",
        call_shape="fpe_encrypt_value(value, key, tweak)  # per value, format-preserving",
        consumes_variable_draws=False,
        identity="source_value",
        null_draw_behavior="null rows pass through unmasked; no keyed draw",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)+alphabet/format",
        provider_version=_V6,
        notes=(
            "Keyed bijection, NIST SP 800-38G FF1 (Task 5.2, replacing the retired "
            "home-rolled HMAC-SHA256 Feistel). Reversible, but FF1 is unauthenticated: "
            "unmask always reports reversed_unverified for this strategy."
        ),
        mirror_call_sites=(
            "execution/_strategies/_fpe.py:176",
            "execution/out_of_core/_mask_group_b.py:153",
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
)

# The mask.formula site (its own random.Random over the mask side).
MASK_FORMULA_DRAW_SITE = DrawSite(
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
