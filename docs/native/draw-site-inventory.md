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

- 28 catalogued draw sites (plus 25 mirror call sites: V1/V2 engines,
  pandas/polars substrates, and the out-of-core batched path).
- 18 partitionable, 10 not.
- Family breakdown: `source_keyed_hmac` 11, `numpy_pcg64` 7, `faker_seed_instance`
  3, `python_mt19937` 3, `per_row_reseed` 2, `per_group_stream` 1,
  `gen_derive_context` 1.
- 1 site flagged uncertain: `mask.windowed_date` (per-row seed keys on the
  enumerate index, so partitionability depends on the native executor pinning a
  global row number; see that section).

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

## Family: source_keyed_hmac (11 sites)

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

---

## Family: numpy_pcg64 (7 sites)

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
so a cached pool and a rebuilt pool of the same identity are value-identical.

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

## References

- RFC 5869 (HKDF-SHA256), RFC 2104 (HMAC-SHA256): the keyed-derivation envelope.
- NumPy NEP-19 (`numpy.random.default_rng`): the seed-stability contract for the
  `numpy_pcg64` sites.
- `SEED_PROTOCOL_VERSION` (`determinism/_derive.py`): the single compatibility
  knob mixed into every keyed HMAC input; currently 6.
