Status: record

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, performance,
testing, verification

# Phase F FR3: generator-to-algorithm inventory

Plan section 4 of `2026-10-18-phase-f-rebaseline-f1-f3.md` requires this inventory before any
generation selection moves onto the compiled index kernel. It classifies every selection site by
mode and state and says, per row, whether the site adopts the kernel without changing its
distribution. The same rows live as data in `tests/parity/_fr3_inventory.py`;
`tests/parity/test_phase_f_fr3_inventory.py` checks the data against the executed-backend witness
and checks that this table lists every row with the same decision.

## What "adopt" means

A site adopts the kernel when its draw is a pure function of the column key and the global row
number:

- uniform over `n` choices: `index = derive_index(column_key, "gen/select/uniform", row, n)`;
- weighted: a uniform integer `u` in `[0, 2**32)` from `derive_index(column_key,
  "gen/select/weighted", row, 2**32)`, looked up with a right bisect in the cumulative-weight
  thresholds (inverse-transform sampling, as masking's deterministic weighted categorical does at
  a coarser 1e6 resolution).

The column key is `GenDeriveContext.family_bytes("kernel_select/v1")`, a 32-byte HMAC sub-key of the
column's own root, so columns with different configuration never share a stream and a rename does
not move a value. The compiled kernel runs when the companion is installed; without it the
byte-identical Python reference runs, so the values are the same on every host. Pool construction
stays Python.

A site that the mapping cannot represent faithfully is declined, and the decision reads only the
configuration. No weighted distribution is flattened and no sequence is turned into a draw.

## Inventory

| Site | Generator | Code | Mode / state | Decision | Backend after FR3 | Distribution reasoning |
|---|---|---|---|---|---|---|
| `sequence.arithmetic` | sequence | `_column_streams.SequenceStream / synthesize._sequence` | value = prefix + zfill(start + global_row * step) + suffix | **UNCHANGED** | no_selection | Not a draw. A uniform kernel draw would destroy order, step and uniqueness. The companion has no arithmetic op and the arithmetic over the global row index is already exact, so it stays and its golden is byte-identical. Order, uniqueness, step and padding are asserted by the sequence test. |
| `categorical.uniform` | categorical | `_column_streams.CategoricalStream` | no weights, list of n >= 1 categories | **ADOPT** | kernel | index = (HMAC-64 of the global row) mod n, so every list position has probability 1/n up to a modulo bias of at most n / 2**64. A duplicated category keeps its list multiplicity, as random.choices does. |
| `categorical.weighted` | categorical | `_column_streams.CategoricalStream + _kernel_select.WeightedIndexer` | weights of length n, finite and non-negative, positive total, every positive weight owns at least one of 2**32 slots | **ADOPT** | kernel | Inverse-transform sampling. The kernel draws a uniform integer u in [0, 2**32) (2**32 divides 2**64, so u is exactly uniform) and the category is the right bisect of u in the cumulative-weight thresholds. Category i owns the slice [T[i-1], T[i]); each probability is within 2**-32 of its configured share. A 90/10 split stays 90/10; it is never flattened to 50/50. |
| `categorical.weighted_unrepresentable` | categorical | `_column_streams.CategoricalStream (Python fallback)` | weights of the wrong length, negative, non-finite, a zero total, or a positive weight below one 2**-32 slot | **DECLINE** | python_rng_selection | The CDF mapping cannot represent the weights faithfully, so the column keeps random.choices with the configured weights (and its legacy errors). The decision reads the configuration only, never the host. |
| `categorical.empty_or_not_a_list` | categorical | `_column_streams.CategoricalStream (Python fallback)` | categories empty, or not a list/tuple | **DECLINE** | python_rng_selection | There is no finite index set to draw over (or the legacy generator iterates a string's characters); the Python path keeps its behavior. |
| `faker.pooled.reuse` | faker (pooled) | `_column_streams.PooledFakerStream / _faker_pool.PooledSelector` | allowlisted type, table rows >= N_THRESHOLD, not opted out, non-deterministic REUSE | **ADOPT** | kernel | Selection is uniform over the 10_000-entry pool: index = (HMAC-64 of the global row) mod 10_000. A value repeated in the pool keeps its multiplicity, so each distinct value's frequency is its pool share. Pool CONSTRUCTION (the Faker provider calls) stays Python; the kernel selects indices only. |
| `faker.pooled.cardinality.unique` | faker (pooled) | `pool/_sampler.PoolSampler.sample(mode=UNIQUE)` | selection without replacement | **DECLINE** | not_reachable_from_generation | UNIQUE is a permutation of distinct pool values, not an independent index draw; independent kernel draws would repeat values and break the distinctness guarantee. Generation never requests it (REUSE only), so there is no generation site to move. |
| `faker.pooled.cardinality.match_scale` | faker (pooled) | `pool/_sampler.PoolSampler.sample(mode=MATCH_SOURCE_CARDINALITY / SCALE_SOURCE_CARDINALITY)` | distinct count derived from a source column | **DECLINE** | not_reachable_from_generation | These modes need a source column's cardinality; generation has no source. Masking-only and untouched. |
| `faker.pooled.cardinality.reuse_deterministic` | faker (pooled) | `pool/_sampler.PoolSampler._deterministic` | REUSE keyed on a canonical source value | **UNCHANGED** | not_reachable_from_generation | Already rides the compiled index kernel on the masking path, byte-identical to the reference (index KAT). Generation has no source value to key on, so it is not a generation site and its output is unchanged. |
| `faker.per_row` | faker (live) | `_column_streams.PerRowFakerStream` | below N_THRESHOLD, type not allowlisted, locale list, opted out, or no exact provider | **DECLINE** | live_provider_generation | One Faker provider call per row, seeded per row. There is no finite pool to select from, so the kernel does not apply. Python by definition (Faker providers stay Python). |
| `faker.custom_provider` | faker (custom) | `faker_setup.get_faker_providers / resolve_pool_provider` | a registered custom provider callable | **DECLINE** | live_provider_generation | An arbitrary callable; never pooled (custom override declines pooling) and never kernelized. |
| `reference.random` | reference | `_column_streams.ReferenceStream + _kernel_select` | distribution random (or unrecognized), no cardinality repair | **ADOPT** | kernel | Uniform over the parent's first-seen, null-dropped key list: index = (HMAC-64 of the global row) mod len(keys). Every drawn value is a parent key by construction, so FK membership holds. |
| `reference.sequential` | reference | `_column_streams.ReferenceStream` | distribution sequential | **UNCHANGED** | no_selection | keys[global_row mod len(keys)]: index arithmetic, not a draw. Order is the contract, so a kernel draw would break it. Unchanged. |
| `reference.weighted` | reference | `_column_streams.ReferenceStream + _kernel_select.WeightedIndexer` | distribution weighted, weights the same length as the key list (a wrong length falls back to uniform, as before) | **ADOPT** | kernel | Same inverse-CDF mapping as categorical.weighted over the key list, so the configured weights are preserved; the wrong-length fallback is the uniform draw it always was. |
| `reference.weighted_unrepresentable` | reference | `_column_streams.ReferenceStream (Python fallback)` | right-length weights the CDF mapping declines | **DECLINE** | python_rng_selection | Same rule as categorical.weighted_unrepresentable: keep random.choices rather than approximate the weights. |
| `reference.cardinality_repair` | reference | `synthesize._reference + ColumnGenerator._apply_cardinality_bounds` | min_per_parent / max_per_parent set | **DECLINE** | python_rng_selection | The plan is ineligible for the unified core (repair revisits earlier assignments), so it runs the legacy whole-table generator and keeps its golden. Its online-allocation redesign is a deferred slice. |
| `null_probability.mask` | any generator | `_column_streams.NullMaskedStream / synthesize._apply_null_probability` | Bernoulli mask over rows | **UNCHANGED** | null_mask | A per-row coin flip, not a selection from a set; it stays on default_rng and is recorded as the null_mask sub-path. Null positions are asserted equal to the legacy path's. |
| `windowed_date.offset` | windowed_date | `transforms/windowed_date.apply_windowed_date` | per-row offset in a window | **UNCHANGED** | unobserved | Already position-keyed (FR1) and its own derivation; outside the F3 selection targets. |
| `python_expression_families` | formula, referenced_formula, derived, derived_aggregate, statistical, grouped_series, group_key | `synthesize._generate_column` | expression, aggregate, snapshot sampler, grouped transform | **DECLINE** | formula_expression | Pandas/Python expression evaluation or a pinned-snapshot sampler. They stay Python by plan section 4. |
| `legacy.whole_table` | any generator in a plan with an ineligible column | `synthesize._generate_tables_from_config` | whole-plan legacy path | **DECLINE** | python_rng_selection | Eligibility is whole-plan (plan section 2). The legacy output is its own golden, so its random.choices / NumPy selections are untouched. This is the Python reference staying reachable for non-admitted cells. |
| `composite.deterministic_multi_slot` | composite_person, composite_name_email, composite_address, composite_custom, composite_provider | `generation/composite/_person.py, _name_email.py, _address.py, _custom.py, _provider.py` | deterministic: one derive() latent split into 8-byte slots, each taken mod a pool length | **DECLINE** | composite_direct_derive | Not reachable from generate_tables. One derive() call feeds several independent slots (first, last, domain, dob), which is the shared-latent coherence design. The index kernel consumes only bytes[0:8], so routing it would change the seeded result and split the latent. No multi-slot compiled kernel exists. Goldens unchanged. |
| `composite.deterministic_single_index` | composite_city_state_zip | `composite/_city_state_zip.py via PoolSampler.sample_bundle(deterministic=True)` | one shared index per row from _derive_pool_indices | **UNCHANGED** | not_reachable_from_generation | Already rides the shared index kernel (compiled or reference), parity-preserving by the index KAT. Not a generation site; output unchanged. |
| `composite.nondeterministic_bundle` | all composites | `composite/*.build_pool + PoolSampler.sample_bundle(deterministic=False)` | seeded REUSE over a bundle pool (default_rng) | **DECLINE** | not_reachable_from_generation | Composite-level sampler, not a generate_tables column. Moving it would change the seeded bundle result without a generation caller to benefit; its goldens are unchanged. |

## Decisions in one line each

- Adopted: `categorical.uniform`, `categorical.weighted`, `faker.pooled.reuse`,
  `reference.random`, `reference.weighted`.
- Not a selection, bytes unchanged: `sequence.arithmetic`, `reference.sequential`,
  `null_probability.mask`, `windowed_date.offset`, `faker.pooled.cardinality.reuse_deterministic`,
  `composite.deterministic_single_index`.
- Declined, stays Python: weighted sets the CDF cannot hold, empty or non-list categories,
  per-row and custom Faker, the cardinality modes generation never requests, cardinality repair,
  expression families, the legacy whole-table path, and the composite samplers.

## Composite selection

None of the composite generators is reachable from `generate_tables` (the F0 matrix records this).
`composite_city_state_zip` already rides the shared kernel through `PoolSampler.sample_bundle`, so
its output is parity-preserved and unchanged. The other deterministic composites split one
`derive()` latent into several slots, which the single-index kernel cannot reproduce without
changing the seeded result, so they are declined. The composite F0 fixtures are not re-frozen.

## Measured effect (1M rows, one column, resident, companion installed, 4-core devbox)

Baseline is `origin/main` (3c1bbabb); each figure is the median of three runs of
`run_generation`, one column per run, end to end including Faker pool construction.

| Column | Before | After | Ratio |
|---|---|---|---|
| pooled Faker (`city`, 10_000 pool) | 0.59 s | 1.25 s | 0.47x |
| categorical, uniform | 0.09 s | 0.73 s | 0.12x |
| categorical, weighted 50/30/15/5 | 0.16 s | 0.74 s | 0.22x |
| reference, random | 0.30 s | 0.73 s | 0.41x |
| reference, weighted | 1.30 s | 0.75 s | 1.7x |
| sequence (control, unchanged) | 0.23 s | 0.23 s | 1.0x |

A mixed job of four pooled Faker columns and two categorical columns at 1M rows went from 3.55 s to
7.24 s (0.49x).

The compiled index kernel costs about 670 ns per row on one thread (194 ns per row on four
threads, `native_threads=4`): it is a per-row HMAC-SHA256. The draws it replaces cost about 3 ns per
row for NumPy `integers` (pooled Faker), about 100 to 130 ns per row for `random.choices`
(categorical), and about 290 ns per row for the per-row `rng.choice` loop (reference). Only the
weighted reference, which called `rng.choices(k=1)` once per row, gets faster. The kernel is the
winning backend for masking because it replaces the per-row Python HMAC; against a vectorized
RNG it is slower, with or without threads.
