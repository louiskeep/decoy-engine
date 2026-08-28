# RNG draw-site inventory (native columnar-streaming program)

Status: record

This is the exhaustive catalog of every place the engine consumes randomness
to produce masked or generated output. It is the evidence base for the native
columnar-streaming determinism protocol (Task 0.3), which must reproduce each
site's byte sequence exactly. The machine-checkable companion, one `DrawSite`
entry per section here, lives in
`src/decoy_engine/execution/native/_determinism_protocol.py` (`DRAW_SITES`),
and `tests/native/test_draw_site_inventory_coverage.py` fails if a new
strategy, generation kind, or RNG-bearing file escapes the catalog.

This task adds a catalog, a data list, and a test only. It changes no masking
or generation behavior.

## How to read a site

Each site records the fields Task 0.3 versions by. The load-bearing ones:

- `seed_derivation`: the exact expression that seeds the draw today, verbatim
  from the code. This is what the protocol preserves.
- `call_shape`: whole-column (`permutation(n)`, `choices(k=n)`) versus per-row
  (`derive_index(...)`, `row_int(family, i)`).
- `identity`: what a single draw is keyed on (`row_index`, `source_value`,
  `group_key`, or `column` for a whole-column stream).
- `partitionable`: whether a partitioned or batched pass can reproduce the same
  value for a given output row as one serial pass.

### The partitionability rule

A site is partitionable when a row's output is a pure function of that row's
own key. Two shapes are NOT partitionable:

1. Whole-column streams (`permutation(n)`, `choices(k=n)`, `default_rng.random(n)`,
   `integers(size=n)`). Row `i`'s value depends on the position of every earlier
   draw in one stream, and a partition boundary breaks that ordering.
2. Per-group sequential streams (a monotone walk accumulated across a group).
   Splitting a group across partitions loses the running state.

Per-row source-keyed derivations (the whole `source_keyed_hmac` family) and
per-row-reseeded streams (reseeded from a stable per-row identity) ARE
partitionable.

## Summary

- 30 catalogued draw sites (plus 46 mirror call sites: V1/V2 engines,
  pandas/polars substrates, the out-of-core batched path, the delegation
  handlers, and the 9 identifier provider adapters).
- 19 partitionable, 11 not.
- Family breakdown: `source_keyed_hmac` 12, `numpy_pcg64` 8, `faker_seed_instance`
  3, `python_mt19937` 3, `per_row_reseed` 2, `per_group_stream` 1,
  `gen_derive_context` 1.
- 1 site flagged uncertain: `mask.windowed_date` (per-row seed keys on the
  enumerate index, so partitionability depends on the native executor pinning a
  global row number; see that section).

### Three keyed primitives

Every `source_keyed_hmac` site derives from one of three primitives in
`decoy_engine.determinism._derive`, all built on the same HKDF-SHA256 +
HMAC-SHA256 envelope:

- `derive(seed, namespace, source)`: the 32-byte digest (used raw or hex).
- `derive_index(seed, namespace, source, *, pool_size)`: the digest reduced to
  an index in `[0, pool_size)`.
- `derive_value(seed, namespace, source, *, domain)`: `domain.from_bytes(derive(...))`,
  a domain-typed wrapper the `providers_v2` identifier adapters use.

The determinism envelope for every keyed site is HKDF-SHA256 (RFC 5869) extract
plus HMAC-SHA256 (RFC 2104) with `SEED_PROTOCOL_VERSION = 6` mixed into the HMAC
input, implemented in `decoy_engine.determinism._derive`. numpy sites follow the
NEP-19 `default_rng` (PCG64) seed-stability contract; Python sites use the
CPython Mersenne Twister via `random.Random`.

## Two determinism roots

The engine keys masked output and generated output from two different roots,
both named honestly in the catalog's `entropy_root` field.

- Masking: `entropy_root = mask_key`. `StrategyContext.mask_key` is the 8-byte
  `job_seed` verbatim when no KeyProvider secret is configured (the pre-GA
  path), or a 32-byte HKDF root derived from a real secret (DE-02). `derive`
  and `derive_index` accept both lengths.
- Generation: `entropy_root = job_seed`. The 8-byte generation seed is threaded
  as `seed` and turned into per-column, per-family, per-row material by
  `GenDeriveContext` (`generators/derivation.py`), which is itself catalogued as
  the `gen_derive_context` substrate.

---

## Family: source_keyed_hmac (12 sites)

The HMAC/HKDF digest IS the pseudo-randomness. There is no RNG object and no
stream position. Every site keys on the source value (or, for `code_set` in
generation mode, the row index) and is therefore partitionable: a batch
reproduces a row's output from that row's key alone. This family is the reason
the out-of-core route already works, and it is the shape the native executor
should push everything else toward.

### mask.hash
`kernel/_scalar.py:75`. `derive(seed, namespace, canonicalize_derive_source(value))`,
emitted as `.hex()[:truncate]`. Joinability-preserving. Null/NaN rows emit null
and consume no derivation.

### mask.categorical_deterministic
`execution/_strategies/_categorical.py:187`. Uniform path:
`derive_index(mask_key, namespace, _canonicalize_source(value), pool_size=len(categories))`.
Weighted path: `derive_index(..., pool_size=_WEIGHTED_CDF_RES)` then a bisect over
the CDF. Mirrors: the out-of-core categorical kernel
(`execution/out_of_core/_mask_group_b.py:336`) and the polars handler
(`execution/polars/_strategies/_categorical.py:113`).

### mask.fpe
`transforms/fpe.py:344`. 8-round type-II Feistel permutation with an HMAC-SHA256
round function; the per-column key is `derive(mask_key, namespace, FPE_KEY_LABEL)`.
A keyed bijection (reversible), home-rolled HMAC-SHA256 Feistel, NOT NIST FF1.
Out-of-core mirror at `_mask_group_b.py:132`.

### mask.date_shift
`transforms/date_shift.py:120`. Keyed path:
`min_days + (HMAC-SHA256(column_key, value)[:8] % range_size)`. The execution
handler derives the column key via `derive(mask_key, namespace, value)`
(`execution/_strategies/_date_shift.py:185`). Legacy no-key configs fall back to
`md5(value)` (`transforms/date_shift.py:108`), preserved for reproducibility.

### mask.bucket_perturb
`transforms/bucket_perturb.py:118`. `derive(job_seed, namespace, _canonicalize_source(value_str))`
gives a within-bucket offset per value.

### mask.group_key
`transforms/group_key.py:171`. `derive(seed, namespace, source)` per distinct
group-by source, giving a 32-byte group key. Generation reuses the same call
(the `group_key` generation kind maps here).

### mask.code_set
`transforms/code_set.py:592`.
`derive_index(hmac_key=derive(mask_key, namespace, _KEYED_SALT), namespace, source, pool_size=hole_candidate_count)`.
Mask mode keys on the source value; generation mode keys on the row index. Both
are per-row and partitionable.

### mask.joint_mask_keyed_row
`reference_tables/_types.py:87`. `keyed_row` selects a row at
`HMAC(hmac_key, key_value) % row_count` over the id-sorted reference table, with
`hmac_key = derive(mask_key, namespace, _KEYED_ROW_SOURCE)` (the joint_mask
consumer at `transforms/joint_mask.py:223`). Deterministic WITHIN a table
version only: the modular index shifts if `row_count` changes.

### mask.text_mask_date_shift
`transforms/text_mask.py:308`. A detected free-text date span shifts by
`int.from_bytes(span_key[:8]) % range_size`, where the span key is
`HMAC-SHA256(mask_key, matched_text)`. The span text keys the shift.

### mask.faker
`execution/_strategies/_faker.py:102`. The masking `faker` strategy. The
value-visible draw is the pool SELECTION: `PoolSampler.sample` runs a per-row
`derive_index(mask_key, namespace, canonical(source), pool.size)` over a
provider value pool. Deterministic selection re-keys onto `mask_key`; the pool
BUILD stays on `job_seed` (see `gen.pool_build_faker`). Non-deterministic mode
selects with `np.random.default_rng` off `job_seed` and is not partitionable.

### gen.pool_deterministic
`generation/pool/_sampler.py:225`.
`derive_index(seed, namespace, canonical, pool_size)` per non-null source row.
The bundle path (`_sampler.py:355`) and the pool adapter
(`generation/pool/_pool_adapter.py:156`) share the same per-row derive.

### gen.identifier_deterministic
`providers_v2/identifiers/_ssn.py:159`. The 9 synthetic-identifier adapters
(`ssn`, `npi`, `ein`, `iban`, `pan`, `icd10`, `mrn`, `ndc`, `cusip`) share this
shape:
`derive_value(seed=spec.seed, namespace=spec.namespace, source=canonical, domain=SsnDomain(rng_config=spec.extra))`,
where `derive_value(seed, ns, src, domain) = domain.from_bytes(derive(seed, ns, src))`.
Per-row source-keyed, so partitionable. These adapters are `poolable=False`
(`generate_batch` raises in deterministic mode), so they are NOT subsumed by
`gen.pool_deterministic`: each is a genuinely separate per-row site. `spec.seed`
carries `mask_key` on the masking side and `job_seed` on pure generation, both
valid `derive` IKM lengths. Wired into output via the provider registry
(`generation/pool/_builder.py` for the non-deterministic build path,
`composite/_provider.py` imports `NpiDomain`). Mirrors: the other 8 adapters'
`derive_value` call.

---

## Family: numpy_pcg64 (8 sites)

Draws from a `numpy.random.Generator` (`default_rng`, PCG64). Most are
whole-column vector draws and are therefore NOT partitionable. The exception is
`mask.windowed_date`, which builds a fresh per-row Generator seeded by an
HMAC of the row index.

### mask.shuffle
`execution/_strategies/_shuffle.py:56`. Whole-column, multiset-preserving
permutation. Seed:
`int.from_bytes(derive(mask_key, plan.namespace, column.encode("utf-8"))[:8], "big")`,
then `default_rng(seed).permutation(len(non_na_values))`. NOT partitionable: a
partition sees only its slice and cannot reproduce the global permutation order.
Nulls are excluded before the draw. Non-deterministic mode uses an unseeded
`default_rng()`. Polars mirror at `execution/polars/_strategies/_shuffle.py:57`.

### mask.categorical_nondeterministic
`execution/_strategies/_categorical.py:215`. `np.random.default_rng()` (unseeded)
then `integers(0, len(categories), n)`. Non-deterministic by contract: output
differs run to run. Polars mirror at `_strategies/_categorical.py:139`.

### mask.windowed_date (UNCERTAIN)
`transforms/windowed_date.py:209`. Per-row seed:
`int.from_bytes(derive(seed, namespace, i.to_bytes(8, "big"))[:8], "big")`, then a
fresh `default_rng(row_seed)` drawing `integers(min_days, max_days + 1)` (one
draw for `uniform`, two for `early`/`late`). The seed keys on the enumerate
index `i`, NOT the source value. This is partitionable ONLY if the native
executor preserves the same global row index `i` under partitioning; if `i`
resets per batch, the sequence diverges. The out-of-core mirror
(`execution/out_of_core/_mask_group_c.py`) already feeds a row index into the
derive seed, which is the proof of concept. Task 0.3 must pin `i` to a global
row number. Flagged uncertain for that reason.

### gen.null_probability
`generation/synthesize.py:521`.
`np.random.default_rng(GenDeriveContext.for_column(...).base_int("np")).random(n) < null_prob`.
This one draw IS the null decision: one float per row from a single contiguous
stream, so row `i` depends on `n` and stream position. NOT partitionable. V1
mirror at `generators/columns.py:221`.

### gen.distribution_snapshot
`generators/_distribution.py:141`. `default_rng(col_seed)` then whole-column
`choice(k, size=num_rows, p=probs)`, `uniform(lo, hi)`, or `random(num_rows)`.
Numeric, categorical, and datetime samplers all draw whole-column vectors
(`:141`, `:266`, `:386`). NOT partitionable.

### gen.pool_nondeterministic
`generation/pool/_sampler.py:122`.
`np.random.default_rng(int.from_bytes(seed, "big"))` then
`integers(0, pool.size, size=n)` or `permutation(...)[:k]`. Seeded but
stream-positional across the whole output, so NOT partitionable.

### gen.identifier_nondeterministic
`providers_v2/identifiers/_ssn.py:165`. Every identifier adapter has two unseeded
`np.random.default_rng()` draws: one in `generate()` (per row) and one in
`generate_batch()` (per batch), across all 9 adapters. Non-deterministic by the
S4 random-by-default contract: output differs run to run, so NOT partitionable.
Mirrors: the second `generate_batch` draw and the other 8 adapters.

### gen.composite_build_pool
`generation/composite/_provider.py:116`.
`np.random.default_rng(int.from_bytes(seed, "big"))` (seed = `spec.seed` or
`b"\x00" * 8`) drawing `integers` and `bytes(8)` to fill a bundle value pool
before sampling. Build-side, not per output row. Mirrors: `_name_email.py:134`,
`_custom.py:206`, `_address.py:96`, `_person.py:142`.

---

## Family: python_mt19937 (3 sites)

Draws from a stdlib `random.Random`. All three advance one stream across the
column (whole-column `choices(k=n)` or a per-row `choice`/`choices` loop on a
single instance), so none is partitionable: a partition cannot resume a Python
RNG mid-stream.

### gen.categorical
`generation/synthesize.py:433`.
`random.Random(GenDeriveContext.for_column(...).base_int("py")).choices(cats, weights=weights, k=n)`:
one whole-column call. V1 mirror at `generators/columns.py:412`.

### gen.reference
`generation/synthesize.py:624`. `random.Random(col_seed)` then per row
`choice(ref_vals)` or `choices(..., k=1)`; the `sequential` distribution draws
nothing (`ref_vals[i % len]`). Cardinality repair
(`generators/columns.py:596-656`) adds `shuffle`/`choices` on the same stream.
V1 mirror at `generators/columns.py:507`.

### mask.formula
`transforms/formula.py:55`. One `random.Random(formula_seed)` where
`formula_seed = int(sha256(f"{col_name}|{expr}").hexdigest()[:16], 16)`, exposed
into the eval scope as `randint`/`choice`/`random` and shared across all rows
via `column.apply`. Whether a row consumes a draw depends on the formula body,
so the stream is order-dependent and NOT partitionable. Note the seed is
self-derived from the formula text and column name, NOT the `mask_key` or
namespace, so this site is independent of the run key by design
(`entropy_root = none`).

---

## Family: per_row_reseed (2 sites)

One `random.Random` reseeded every row from a stable per-row key. Cross-row
order does not matter, so both are partitionable even though a row can draw a
variable number of values internally (the reseed contains it).

### gen.formula_per_row
`generators/columns.py:202`. Per row:
`local_seed = gen_ctx.row_int("py", i); row_rng.seed(local_seed); self.faker.seed_instance(gen_ctx.row_int("faker", i))`,
then `safe_eval(formula, scope)`. The referenced-formula post-pass
(`generators/_formula.py:144`) and the V2 delegation
(`generation/synthesize.py:559`) mirror it.

### gen.statistical_per_row
`generation/statistical/_sample.py:260`. `rng.seed(col_seed + i)` per row (the
legacy per-row idiom, not `GenDeriveContext`), then an inverse-CDF or weighted
draw. Reseed-per-row makes any chunking byte-identical to a serial pass, the WS4
chunk-safety contract. `freetext` draws a variable number of `randrange` calls
within a row, but the per-row reseed contains it.

---

## Family: faker_seed_instance (3 sites)

`Faker.seed_instance(seed)` detaches the instance onto its own `random.Random`
and reseeds it, then a provider method draws. Draw count per call varies by
provider, but each site reseeds from a per-row or per-span key, so output is a
pure function of that key.

### gen.faker_per_row
`generation/synthesize.py:490`. Per row:
`faker_inst.seed_instance(gen_ctx.row_int("faker", i))` then `provider_func(**kwargs)`.
`row_int` is an HMAC over the faker family key and `i`. Partitionable. V1 mirror
at `generators/columns.py:329` is byte-identical.

### mask.text_mask_faker
`transforms/text_mask.py:262`. `seed = int.from_bytes(span_key[:4], "big")` where
`span_key = HMAC(mask_key, matched_text)`, then `Faker().seed_instance(seed)` and
a provider method per detected span. 4-byte seed keyspace. Partitionable: each
span reseeds from its own span key.

### gen.pool_build_faker
`generation/pool/_builder.py:161`. The Faker pool builder. Pool seed:
`derive(job_seed, "pool/{provider}/{locale}/{namespace}", config_hash)[:8]`
(`_derive_pool_seed`, `_builder.py:57`), threaded as `ProviderSpec.seed` into
`adapter.generate_batch`. Build-time draw. Partitionable in the sense that pool
identity is a pure function of `(job_seed, provider, locale, config, namespace)`,
so a cached pool and a rebuilt pool of the same identity are value-identical. The
physical seed happens in the provider adapter: `providers_v2/_faker_adapter.py:224`
(`fake.seed_instance(int.from_bytes(spec.seed, "big"))`) and, for the Mimesis
adapter, a fresh `Generic(locale, seed=int.from_bytes(spec.seed))` per batch
(`providers_v2/mimesis/_adapter.py:167`). Both are catalogued as mirrors here.

---

## Family: per_group_stream (1 site)

### mask.grouped_series_monotone_walk
`transforms/grouped_series.py:274`. Per group label `g`, one seed
`int.from_bytes(derive(seed, namespace, str(g).encode("utf-8", errors="replace"))[:8], "big")`
seeds `default_rng(g_seed)`, which is then advanced per row within the group by
`integers(step, max_step + 1)` and accumulated. NOT partitionable: the walk is a
sequential stream ordered by `(group, order_by)`, and a partition that splits a
group cannot reproduce the cumulative sum. The `cumcount` generator in the same
module is a deterministic-no-draw sibling (position within the sorted group).

---

## Family: gen_derive_context (1 site)

### gen.derive_context
`generators/derivation.py:158`. Not a draw itself: the keying layer every
generation family (`py`, `np`, `faker`) is seeded from. `for_column` resolves one
32-byte column root by one of four paths (fresh `os.urandom(32)`; legacy
`derive_key("col:" + name)`; keyed `derive_key("gen:" + strategy_config_fingerprint(cfg))`;
unkeyed `sha256(job_seed.to_bytes(8) + fingerprint)`). Then
`family_key = HMAC(root, b"fam:" + family)`, `base_int(family)` is the full-width
family key, and `row_int(family, i) = HMAC(family_key, family, extra=i.to_bytes(8))`.
`row_int` is a pure function of `(root, family, i)` and is partitionable;
`base_int` seeds whole-column consumers that may not be. `strategy_config_fingerprint`
excludes the display name and null probability, so a rename never shifts output.

---

## Deterministic, no draw

The following live masking strategies and generation kinds consume no
randomness and are recorded in the coverage maps against the
`deterministic_no_draw` sentinel, so they are accounted for rather than silently
omitted:

- Masking: `passthrough`, `redact`, `truncate`, `bucketize`, `top_code`,
  `geo_generalize`, `derived`, `derived_aggregate`, `text_redact`, `nested`
  (delegates to a child handler).
- Generation: `sequence`, `derived`, `derived_aggregate`.

## Out of scope (surveyed, not output-producing)

These consume randomness but not to produce masked or generated output, so they
are outside the determinism envelope this program governs. They are noted here
so the exclusion is deliberate, not an oversight:

- Profiling and sampling: `profile/_source.py`, `profile/_walk.py`,
  `execution/_chunked_profile.py` (a fixed `random.Random(0)` over row sampling).
- Evaluation metrics: `quality/synth_report.py` (DCR computation).
- Test fixtures: `storm/eval/fixtures.py`.
- Determinism primitives and Faker plumbing: `determinism/_derive.py`,
  `internal/faker_setup.py`, `internal/crypto.py`, `expressions/_safe_eval.py`
  (the eval-scope factory; the actual formula draws are catalogued at the
  formula sites).
- Orchestration and estimation that only thread seeds or quote a call shape in a
  string: `execution/_chunked.py`, `execution/_chunked_fk.py` (a diagnostic
  message string quotes `derive(...)`), `execution/_pipeline.py`,
  `execution/out_of_core/_spill_estimate.py`, and the `providers_v2` base
  adapter / package `__init__` / errors modules (which document the three keyed
  primitives in prose but hold no draw). Every provider draw that produces output
  (`SSN`, `NPI`, `EIN`, `IBAN`, `PAN`, `ICD-10`, `MRN`, `NDC`, `CUSIP`) IS
  catalogued above; nothing in `providers_v2` that emits a value is excluded.

## How the coverage test enforces completeness

`tests/native/test_draw_site_inventory_coverage.py`:

- Cross-checks the coverage maps against three LIVE registries: masking against
  `execution._strategies.SCALAR_HANDLERS`, generation against
  `config._tables.GENERATE_TYPES`, and the synthetic-identifier providers against
  the `providers_v2` `ProviderRegistry` (every registered provider whose adapter
  is an identifier adapter class must be in `PROVIDER_IDENTIFIER_SITES`). A new
  strategy, generation kind, or identifier provider fails until catalogued.
- Runs a static scan over seven output-producing roots, now including
  `providers_v2`, matching the three keyed primitives (`derive`, `derive_index`,
  `derive_value`) plus the numpy/Python RNG constructors, `seed_instance`, the
  whole-column numpy ops, and the Mimesis `Generic(` seeded constructor. Prose
  (docstrings and comments) is stripped first so only real call sites count. Any
  matched file must be a catalogued `call_site`/mirror or one of five allowlisted
  non-output files, so a new RNG-bearing source file fails until catalogued.

## Protocol freeze (Task 0.3)

Task 0.3 builds one `DrawSiteProvider` per catalogued site
(`execution/native/_draw_site_providers.py`, re-exported from
`_determinism_protocol.py`) and proves via an exact goldens gate
(`tests/native/test_determinism_goldens.py`) that each provider reproduces what
the current engine draws at that site. Every gate test invokes the REAL shipped
transform / handler / primitive (never a second copy of the same formula) and
drives the provider on the same identity. 19 draw sites route through the
shipped code:

- 18 reproduce the shipped OUTPUT byte-for-byte: `mask.shuffle`
  (`ShuffleStrategyHandler`), `mask.hash` (`hash_array`), `mask.group_key`
  (`apply_group_key`), `mask.bucket_perturb` (`apply_bucket_perturb`),
  `mask.date_shift` (`DateShiftStrategyHandler`), `mask.categorical_deterministic`
  (`CategoricalStrategyHandler`, uniform + weighted), `mask.code_set`
  (`_pick_from_seq`, mask mode), `mask.joint_mask_keyed_row`
  (`ReferenceTable.keyed_row`), `gen.pool_deterministic` + `mask.faker`
  (`PoolSampler.sample`), `gen.identifier_deterministic` (`SsnAdapter`),
  `mask.grouped_series_monotone_walk` (`_apply_monotone_walk`), `mask.windowed_date`
  (`apply_windowed_date`), `gen.categorical` / `gen.reference` /
  `gen.null_probability` / `gen.faker_per_row` (`synthesize.py`),
  `gen.statistical_per_row` (`sample_column`).
- 1 is keyed-material: `mask.fpe`. The provider emits the per-column Feistel KEY
  (`derive(mask_key, namespace, FPE_KEY_LABEL)`); the ciphertext is reproduced by
  driving that key through the shipped `fpe_encrypt_value` and matching the real
  `FpeStrategyHandler` output. The Feistel arithmetic is transform semantics,
  deferred to Task 0.4's pure-Python reference.

The compound source-keyed sites (`mask.fpe` key, `mask.code_set` and
`mask.joint_mask_keyed_row` two-step keyed selection) have dedicated providers
that reproduce the shipped derivation exactly, including the correct key source
(a fixed `FPE_KEY_LABEL`, an intermediate `_KEYED_SALT` / `_KEYED_ROW_SOURCE`
`derive` step, and the `hmac_hex` modular selection). Drift tests pin the three
inlined byte-constants and the inlined `hmac_hex` / `hole_resolve` to the shipped
symbols. The remaining sites are proven at the seed-derivation level in
`tests/native/test_determinism_protocol.py`: whole-column and per-group global
sites (`mask.shuffle`, `mask.grouped_series_monotone_walk`, `gen.categorical`,
`gen.reference`, `gen.null_probability`, `gen.distribution_snapshot`,
`gen.pool_nondeterministic`, `gen.composite_build_pool`, `mask.formula`) refuse a
partitioned request with `site_not_partitionable`; the two unseeded
non-deterministic sites (`mask.categorical_nondeterministic`,
`gen.identifier_nondeterministic`) refuse reproduction with
`site_not_reproducible`. With the gate green, the protocol is FROZEN: each
site's `provider_version` below is locked, and any change to a seed derivation,
call shape, or version is a `SEED_PROTOCOL_VERSION`-class event.

The `unit_float_from_bits53(raw_u64)` primitive extracts the upper 53 bits of a
FULL 64-bit value (`(raw_u64 >> 11) / 2**53`), matching NumPy's own `random()`
construction and always `< 1.0` (the all-ones input maps to `(2**53 - 1) / 2**53`).

Registry: exactly one provider per catalogued `draw_site_id` (30 sites; 19
partitionable, 11 not). An import-time invariant fails if the registry drifts
from `DRAW_SITES`.

| draw_site_id                      | family              | partitionable | provider_version |
| --------------------------------- | ------------------- | ------------- | --------------------------------------------------------------- |
| gen.faker_per_row                 | faker_seed_instance | yes           | seed_protocol_v6 (GenDeriveContext); Faker seed_instance |
| gen.pool_build_faker              | faker_seed_instance | yes           | seed_protocol_v6 (pool_seed via derive); Faker/provider adapter |
| mask.text_mask_faker              | faker_seed_instance | yes           | Faker (seed_instance detaches a per-instance random.Random) |
| gen.derive_context                | gen_derive_context  | yes           | seed_protocol_v6 |
| gen.composite_build_pool          | numpy_pcg64         | no            | seed_protocol_v6; numpy NEP-19 PCG64 |
| gen.distribution_snapshot         | numpy_pcg64         | no            | seed_protocol_v6 (GenDeriveContext); numpy NEP-19 PCG64 |
| gen.identifier_nondeterministic   | numpy_pcg64         | no            | numpy NEP-19 PCG64 |
| gen.null_probability              | numpy_pcg64         | no            | seed_protocol_v6 (GenDeriveContext); numpy NEP-19 PCG64 |
| gen.pool_nondeterministic         | numpy_pcg64         | no            | seed_protocol_v6; numpy NEP-19 PCG64 |
| mask.categorical_nondeterministic | numpy_pcg64         | no            | numpy NEP-19 PCG64 |
| mask.shuffle                      | numpy_pcg64         | no            | seed_protocol_v6; numpy NEP-19 PCG64 |
| mask.windowed_date                | numpy_pcg64         | yes           | seed_protocol_v6; numpy NEP-19 PCG64 |
| mask.grouped_series_monotone_walk | per_group_stream    | no            | seed_protocol_v6; numpy NEP-19 PCG64 |
| gen.formula_per_row               | per_row_reseed      | yes           | seed_protocol_v6 (GenDeriveContext); CPython MT + Faker |
| gen.statistical_per_row           | per_row_reseed      | yes           | CPython Mersenne Twister (no numpy; bit-stable inverse-CDF) |
| gen.categorical                   | python_mt19937      | no            | seed_protocol_v6 (GenDeriveContext); CPython Mersenne Twister |
| gen.reference                     | python_mt19937      | no            | seed_protocol_v6 (GenDeriveContext); CPython Mersenne Twister |
| mask.formula                      | python_mt19937      | no            | CPython Mersenne Twister |
| gen.identifier_deterministic      | source_keyed_hmac   | yes           | seed_protocol_v6 |
| gen.pool_deterministic            | source_keyed_hmac   | yes           | seed_protocol_v6 |
| mask.bucket_perturb               | source_keyed_hmac   | yes           | seed_protocol_v6 |
| mask.categorical_deterministic    | source_keyed_hmac   | yes           | seed_protocol_v6 |
| mask.code_set                     | source_keyed_hmac   | yes           | seed_protocol_v6 |
| mask.date_shift                   | source_keyed_hmac   | yes           | seed_protocol_v6 |
| mask.faker                        | source_keyed_hmac   | yes           | seed_protocol_v6 |
| mask.fpe                          | source_keyed_hmac   | yes           | seed_protocol_v6 |
| mask.group_key                    | source_keyed_hmac   | yes           | seed_protocol_v6 |
| mask.hash                         | source_keyed_hmac   | yes           | seed_protocol_v6 |
| mask.joint_mask_keyed_row         | source_keyed_hmac   | yes           | seed_protocol_v6 |
| mask.text_mask_date_shift         | source_keyed_hmac   | yes           | seed_protocol_v6 |

## References

- RFC 5869 (HKDF-SHA256), RFC 2104 (HMAC-SHA256): the keyed-derivation envelope.
- NumPy NEP-19 (`numpy.random.default_rng`): the seed-stability contract for the
  `numpy_pcg64` sites.
- CPython `random.Random` (Mersenne Twister MT19937): the seed-stability contract
  for the `python_mt19937` / `per_row_reseed` sites.
- `SEED_PROTOCOL_VERSION` (`determinism/_derive.py`): the single compatibility
  knob mixed into every keyed HMAC input; currently 6.
