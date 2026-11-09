Status: plan

# GEN-UNIQUE: unique generated columns and unique junction pairs (roadmap row 24m)

Owner: Cam. Author: claudeworkerbee (Opus). Date: 2026-11-09. Scope: engine plus CLI exit mapping and help,
V1 (owner decision 2026-11-08). Revision 3 (review rounds 1 and 2 applied).

Rules consulted: `dev-rules/README.md`, `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`
(PLAN: choke point, Placement, Design notes, Established solutions), `feature-dev.md`, `architecture.md`,
`reuse-first.md`, `testing.md` (test depth, escalation triggers), `cross-cutting-concerns.md`,
`api-and-compatibility.md`, `code-review.md`, `workspace-and-vcs.md`, `scope-discipline.md`, `verification.md`,
`documentation.md`, `catalogs/recurring-defects.md`; the plan contract
(`30_Resources/Decoy-Plan-Contract-and-Defect-Catalog.md`); `LOCAL-BUILDER-BRIEF.md`; the V1 bar (a function
break, data loss or a value leak to non-admins blocks; a second copy of a central mechanism, or a new module in a
flat folder over 25 modules, always blocks); engine `CLAUDE.md` (cite the established method in the module
docstring; modules aim for 600 lines, hard max 700); engine `docs/compatibility-contract.md:180-182` (same input
plus same seed gives byte-identical output within a major); engine `docs/reasons.md` (an addition raises the
registry minor, one fixture per minor); the GEN-STRICT option registry (`config/_generate_options.py`, engine
#430); the FK fan-out plan (`docs/plans/2026-11-08-qa-fk-fanout.md`, branch `plan/qa-fk-fanout`, slices E2a and
E2b) for the shared `_reference` code and its "open an option key in the commit that wires it" rule. Code read at
engine `main` `f118ee02` and CLI `main` `9f75282`.

Source: first-user generation QA (CLI `docs/records/2026-11-08-cli-qa-generation.md`), High H2: "No uniqueness
control. At 200,000 rows: `email` 22.3% duplicates, `user_name` 39.2%, `name` 95.3% (9,399 distinct). Default
`pooled: true` caps names at ~9.3k distinct at 50k rows; `pooled: false` gives 40k but is 2x slower. `pooled`
appears in no help. No `unique: true`. A junction table got 17 duplicate (emp, project) pairs in 300 rows." Also
user expectation B5: "Ids and emails are unique when they look like ids and emails."

Terms. The **domain** of a column is the set of distinct values its generator can emit. A column is **unique**
when no two non-null cells are equal (SQL `UNIQUE` semantics: nulls never collide). A **junction pair** is two
`reference` columns of one generate table whose value pairs must not repeat. `P` is the count of distinct non-null
keys in a referenced parent column after the parent is generated (`_reference` builds it at
`generation/_synthesize_derived.py:127-137`).

## 1. FRAME

### Problem

No generator can promise distinct values. Per-row Faker draws collide at the birthday rate; `pooled` Faker draws
from a 10,000-value pool (`generation/pool/_runtime_pool_size.py:25`), so distinct names cap near that number;
`categorical` and `reference` draw with replacement (`generation/synthesize.py:346-365`,
`generation/_synthesize_derived.py:143-160`); two `reference` columns of a junction table are drawn independently,
so pairs repeat. The engine already has uniqueness for masking pools (`CardinalityMode.UNIQUE`,
`generation/pool/_cardinality.py:22`; the shared capacity rule `unique_capacity_ok`,
`generation/pool/_capacity.py:26`; refusal code `runtime.uniqueness_impossible`, raised at
`generation/pool/_sampler.py:307`), but nothing on the generation side reaches it.

### Guarantee (what V1 promises)

1. A generate column with `unique: true` has no two equal non-null values, for types `sequence`, `categorical`,
   `faker` and `reference`.
2. A generate table with `unique_together: [[a, b]]` has no two rows with the same non-null `(a, b)`, where `a` and
   `b` are `reference` columns of that table.
3. When the domain is provably too small, `decoy validate` refuses with exit 1 and a named code. When the domain
   is only known at run time, `decoy run` refuses with exit 1 and `uniqueness_impossible` before writing output.
   The run never emits a duplicate and never silently drops `unique`.
4. A config without `unique` and `unique_together` gives byte-identical output to `f118ee02`, and its serialized
   Plan and fingerprints are unchanged.
5. Adding `unique: true` to one column changes no other column's values. For `faker`, a row whose first draw does
   not repeat an earlier emitted value keeps the value the same column has with `pooled: false`.
6. `unique` on any other type (`formula`, `statistical`, `derived`, `derived_aggregate`, `grouped_series`,
   `windowed_date`, `group_key`) is refused at validate with a named code, never ignored.
7. `pooled` is documented: what it does, when it applies, and that `unique: true` turns pooling off.

### Invariants (as properties)

- **U1 distinct.** For every accepted config and seed, each `unique` column's non-null values are pairwise
  distinct, and each `unique_together` pair's non-null value pairs are pairwise distinct.
- **U2 refuse, never weaken.** Every path that cannot meet U1 raises a coded error before any output is
  published; no path falls back to drawing with replacement.
- **U3 old input, old bytes.** Output, serialized Plan, `config_json` and every fingerprint for a config without
  the new keys equal `f118ee02`'s.
- **U4 local change.** For configs `C` and `C'` differing only by `unique: true` on column `x`, every column other
  than `x` has equal values between `C` and `C'`. Equal values, not equal schemas: the whole-table route infers
  `null` for a zero-row table or an all-null column where the streamed core types it (`_admission.py:18-20`;
  `synthesize.py:176`), a gap that exists today for every declined plan.
- **U5 faker prefix.** For a `unique` faker column, over the values before the null post-pass,
  `out[i] == plain[i]` exactly when `plain[i] is None` or `plain[i]` is not in the non-null values of `out[:i]`,
  where `plain` is the same column with
  `pooled: false` and without `unique`. (Without `pooled: false`, a pool-eligible type at 50,000 rows or more is
  pooled in `plain`, `_faker_pool.py:230`, `:239-264`, and the per-row draws differ.)
- **U6 one capacity rule.** Every validate-time and run-time size check calls
  `unique_capacity_ok(domain, rows, null_reusable=False)`; no second
  comparison of domain against rows exists. Its docstring (`pool/_capacity.py:1-33`) speaks of non-null rows; E1
  rewords it to "rows that must hold distinct values" and documents that an in-domain reusable SQL null makes the
  capacity unbounded, which covers both callers without treating the later null post-pass as capacity.
- **U8 one parent key list.** The unique reference draw and the pair draw read the parent keys through one
  `parent_keys(pools, table, column)` helper extracted from `_reference` (`_synthesize_derived.py:127-137`), in the
  same insertion order. `KeyPool` (`_column_streams.py:90-111`) builds the streamed equivalent from Arrow chunks; it
  is an existing pair, recorded as F6 and not widened here.
- **U7 value-free refusals.** No refusal message carries a generated or parent value; messages name table,
  column, type, counts and row index only (roadmap 24a rule).

### Non-goals (V1)

- `unique` on `statistical`, `formula`, `derived` and the grouped types. Their domains are continuous or computed;
  refused by name (guarantee 6). Follow-up F1.
- Unique tuples wider than two columns, or pairs that include a non-`reference` column. Follow-up F2.
- Streaming or windowed generation of a unique column. V1 runs such plans on the whole-table route (section 3,
  "Routes"). Follow-up F3.
- Case-insensitive or normalized uniqueness (`Bob@x.com` vs `bob@x.com`). Uniqueness is Python equality on the
  emitted value. Follow-up F4.
- Uniqueness across tables or across runs.

### Failure modes (each has an acceptance test in section 7)

| Case | Behavior |
|---|---|
| Domain too small, known at validate | `categorical` without a reusable `None` and with `row_count` above its distinct category count; `reference` with `row_count` above the parent table's `row_count` (an upper bound on `P`); a pair with `row_count` above the product of the two parent `row_count`s. `decoy validate` refuses with `config.generate_unique_domain_too_small`, exit 1. |
| Domain too small, known only at run | `reference` whose parent has fewer distinct non-null keys than `row_count` (parent nulls, repeats or a non-unique parent column, including an all-null parent, which the check sees before `_reference`'s empty-pool return at `_synthesize_derived.py:139-140`); a pair whose `P_a * P_b` is below `row_count`; a `faker` type whose domain runs out. `decoy run` refuses with `runtime.uniqueness_impossible`, exit 1, before the sink commits. |
| Near-full domain, retry cost | Faker makes at most 1,000 attempts for one row (Faker's own `_UNIQUE_ATTEMPTS`). At fill fraction `f` a row expects `1 / (1 - f)` attempts, and all 1,000 attempts collide with probability about `exp(-1000 r / D)` when `r` values remain unused. For `D = 9,999`, stopping with 160 values unused gives a union bound below `1e-6`; this is a probability, not guaranteed capacity, and the guide says so. Expected provider calls to fill `n` of `D` are `D * (H_D - H_{D-n})`; the hard cap is 1,000 calls per requested row. `categorical`, `reference` and pairs never retry: they sample without replacement. |
| Nulls with unique | Nulls never collide. A `None` returned by a Faker provider is never put in the seen set. A categorical domain containing `None` samples each distinct category at most once, then may fill remaining rows with `None`; it still calls `unique_capacity_ok(..., null_reusable=True)`. `null_probability` is allowed on a `unique` column, but its post-pass nulls rows after values are drawn, so that capacity check uses `row_count` with `null_reusable=False` (owner question 3). Pair members refuse `null_probability`, because the pair skips the per-column post-pass. |
| Determinism across chunked runs | A plan with a unique column or pair is declined by the streamed core (`_admission.py`, reason `unique` or `unique_pair`) and refused by the windowed reader (Class B, `_window_admission.py`). It runs on the whole-table route only, so its bytes are a function of config and seed. `decoy run --chunked` already refuses generate configs. Declining keeps U4 at value level: the whole-table generators make the same draws as the streamed columns (`generation/_column_streams.py:7-19`; `tests/parity/test_phase_f_fr0_route_independence.py:182-195`). |
| Junction product smaller than `row_count` | Refused at validate from parent `row_count`s when the product is provably small, else at run from `P_a * P_b`. |
| Unique plus a conflicting option | `categorical` `weights`; `reference` `distribution: weighted`, `min_per_parent`, `max_per_parent`, or `distribution: fitted` (once FK-FANOUT E2b lands); `pooled: true` (checked in `_options_valid`, `config/_tables.py:299-316`, because `pooled` is a declared field that `option_problems` never sees); a pair member that also sets `unique`, `null_probability`, `determinism: fresh`, a non-`random` distribution or the cardinality repair keys. Refused at validate with `config.generate_unique_conflict`. |
| Pinned generation | Column `unique` on `categorical` and `reference` runs through the same `_generate_column` path (`generation/_pinned_execute.py:183-209`). `unique_together` has no place in `PinnedTable` and is refused at pinned compile with `plan.pinned_unique_pair_unsupported`. `faker` is already refused there. |
| Memory | `decoy run` generates through `execution/_pipeline_generate_mask.py:128-133` without `budget_bytes`, so the ledger reservation in `_run_legacy` (`_streamed.py:527-537`) runs only for callers that pass a budget. The check every caller gets is the generation floor (`_bounds.py:312-328`, `generation_floor_shortfall`). The seen-set bytes of a unique faker column and the working set of `random.sample` (a selected set of up to `n` ints, or a list of the population when it is small) join both, through one `unique_set_bytes`. The refusals keep today's codes and exits (`generation_budget_exceeded` is a `DecoyError`, exit 3 in the CLI today); mapping it to exit 5 is owner question 6. |
| Probe downscaling | The CLI probe scales row counts down (`execution/_probe_scale.py:100-121`), and a scaled junction can exceed the product of its scaled parents. The probe refusal is reported as a probe outcome, not a run failure; AT-14 has a cell for it. |

## 2. Established solutions

- **Faker unique proxy.** `fake.unique.<provider>()` keeps a per-instance seen set and retries a repeated value up
  to `_UNIQUE_ATTEMPTS = 1000` times, then raises `UniquenessException("Got duplicated values after 1,000
  iterations.")` (Faker `faker/proxy.py:18`, `:394-400`; docs: "after a number of attempts to find a unique value,
  Faker will throw a UniquenessException"). V1 copies the rule (seen set, 1,000 attempts, then refuse) but not the
  proxy itself: the proxy's retries reuse the instance's advancing state, which would make row `i` depend on how
  many retries earlier rows took in a way the per-row seeding (`synthesize.py:438-441`) cannot reproduce. V1
  seeds attempt `k` of row `i` from its own derived seed.
- **Sampling without replacement.** CPython `random.Random.sample(population, k)` returns `k` distinct positions:
  a partial Fisher-Yates over a list when the population is small, else rejection against a selected set
  (`Lib/random.py`, `sample`). The docs recommend `sample(range(N), k)` "for sampling from a large population";
  memory is `O(k)` either way (a small population is copied to a list, `random.py:458-466`, a large one keeps a
  selected set). V1 uses it for `categorical` (population: the distinct categories),
  `reference` (population: the parent key list) and pairs (population: `range(P_a * P_b)`, decoded as
  `(idx // P_b, idx % P_b)`). No retries, so the cost is linear in `row_count`. This is the permutation method the
  masking pool already uses for `UNIQUE` (`rng.permutation(...)[:required]`, `generation/pool/_sampler.py:315`).
- **SDV.** SDV (1.x) expresses uniqueness in metadata, not constraints: a bridge table declares a composite primary
  key, and the old `UniqueBridgeTable(table_name, foreign_key_columns)` constraint is deprecated in its favor
  ("To enforce that the connections are unique, you can denote a composite key for the bridge table in your
  metadata", docs.sdv.dev, Deprecated Constraints). Older SDV enforced constraints it could not transform by
  reject sampling ("samples as usual and discards rows that violate the constraint", docs.sdv.dev, Constraint
  Logic). V1 follows the declarative shape (`unique_together` on the table, owner question 1) and avoids reject
  sampling where an exact sampler exists.
- **Rejection versus permutation.** Rejection costs `D * (H_D - H_{D-n})` draws to place `n` of `D` values, which
  is cheap far from full and unbounded at full; permutation sampling costs `n` draws but needs the domain
  enumerable. V1 uses permutation wherever the domain is a known list (categorical, reference, pairs) and rejection
  only where it is not (Faker).

## 3. Design notes

### Choke points

| Concern | The one place | Why there |
|---|---|---|
| Is the domain big enough | `unique_capacity_ok(domain, rows, null_reusable=False)` (`generation/pool/_capacity.py:26`) | Already the single rule shared by masking compile and runtime (DE-11); the additive flag keeps SQL-null capacity in that rule instead of creating a second comparison |
| Run-time unique draws | new `generation/_unique.py` (`unique_categorical`, `unique_faker`, `unique_reference`, `unique_pair`) | One module for the four draws; `synthesize.py` and `_synthesize_derived.py` each gain a branch that calls it |
| Run-time uniqueness refusal | `raise_uniqueness_impossible(...)` in `generation/pool/_errors.py`, exported by `generation.pool` | The existing masking raise at `generation/pool/_sampler.py:305-314` and every new generation refusal call the same value-free builder; its signature accepts names, counts and row numbers, never generated or parent values |
| Parent keys | `parent_keys(pools, table, column)` in `_synthesize_derived.py`, extracted from `_reference` (`:127-137`) | Both `_reference` and `unique_pair` read it; without it `_unique.py` would hold a third copy beside `KeyPool` |
| Validate-time checks | `unique` registered per type in `GENERATE_OPTIONS` (`config/_generate_options.py:203-287`); table and cross-table checks in new `config/_generate_unique.py`, called from one `TableConfig` validator and one `PipelineConfig` validator | GEN-STRICT's registry is the per-type key surface; domain checks need `row_count` and the parent tables, which only the table and pipeline validators see |
| Pooling off for unique | one predicate `pool_opted_out(col)` in `generation/_faker_pool.py`, replacing the five `col.get("pooled") is False` reads (`_faker_pool.py:302`, `_window_admission.py:416` and `:457`, `_column_streams.py:310`, `_streamed.py:191`) | Five inline copies today; a sixth reading `unique` would drift |
| Route decision | `classify_plan` (`generation/_admission.py:149`, inner `classify` near `:217`) and `_class_b_reason` (`generation/_window_admission.py:439`) | The two existing admission choke points |

### Config surface

```yaml
tables:
  - name: users
    row_count: 200000
    generate_columns:
      - {name: user_id, type: sequence, start: 1, unique: true}
      - {name: email, type: faker, faker_type: email, unique: true}
      - {name: plan, type: categorical, categories: [a, b, c]}
  - name: assignments
    row_count: 300
    unique_together: [[emp_id, project_id]]
    generate_columns:
      - {name: emp_id, type: reference, reference_table: employees, reference_column: emp_id}
      - {name: project_id, type: reference, reference_table: projects, reference_column: project_id}
```

- `unique` is a flat option registered for every generate type (GEN-STRICT keeps keys closed per type). Its check
  is `StrictBool`-like (`True` or `False` only; `"true"` and `1` refused), and `unique` joins `_NONE_MEANS_ABSENT`
  (`config/_generate_options.py:289-308`) so option validation and generation treat `unique: null` as not given;
  like other extra nulls, it may remain explicit in a dump. For `sequence`, `categorical`, `faker`
  and `reference` it is accepted; for the other seven it is refused with `config.generate_unique_unsupported`;
  the type name goes in the `ConfigRuleError` message, since `problem_details` emits a fixed summary per code.
  `unique: false` has the same generation behavior as an omitted key.
- `sequence` with `unique: true` changes nothing: `start + i * step` with `step != 0` (GEN-STRICT refuses 0) is
  already distinct, and `pad_length` pads without truncating (`synthesize.py:336-342`). It stays on the streamed
  core.
- `unique_together` is a `TableConfig` field, `list[list[str]]`, default empty, omitted from dumps when empty by a
  wrap serializer (the `protected_quarantine` precedent, `config/_pipeline.py:151-161`), so no existing
  `config_json` gains a key. The serializer comes with a `__get_pydantic_json_schema__` hook calling
  `recover_serialization_schema` (precedents `config/_tables.py:215-223`, `config/_pipeline.py:145-149`), or the
  serialization-mode schema collapses to a generic object. V1 rules: each entry names exactly two distinct `reference` columns of this table; a
  column is in at most one entry; members keep default `distribution: random` and set none of `unique`,
  `null_probability`, `determinism: fresh`, `weights`, `min_per_parent`, `max_per_parent`. The validate-time
  distinct-category pass uses equality over the list, never `set()`, so supported unhashable categories do not
  raise `TypeError`. `None` is recorded separately as a reusable null, not counted as a colliding value.
- `unique` joins `_EXCLUDED_FROM_FINGERPRINT` (`generators/derivation.py:52-59`) beside `pooled`, so a unique
  column keeps its seed root. That is what makes U5 true for Faker and keeps `unique: false` byte-identical to
  absent. No accepted config carries `unique` today (GEN-STRICT refuses unknown keys), so the exclusion moves no
  existing root.

### Draws

- **Faker.** Pooling is off (`pool_opted_out`). Row `i` attempt 0 is today's draw:
  `seed_instance(row_int("faker", i))` (`synthesize.py:438-441`). If the value is already in the seen set, attempt
  `k` (1 to 999) reseeds from `row_int("faker_unique", i * 1000 + k - 1)`, a new HMAC family, so retries never
  reuse a row seed. `faker_unique` and `unique_pair` are registered in `GEN_FAMILIES` (`generators/derivation.py:186`)
  and the `base_int`/`row_int` docstring (`:303-307`) lists them. A `None` result is emitted without entering the
  seen set. After 1,000 attempts: `raise_uniqueness_impossible` with the row index and the distinct count so far.
  Values must be hashable; a provider returning an unhashable value is refused by the same code, raised after the
  `except TypeError` block closes so the traceback does not chain the hash error.
- **Categorical.** `random.Random(base_int("py")).sample(distinct_categories, n)`, where `distinct_categories` keeps
  first-seen order (as `_reference` does at `_synthesize_derived.py:127-137`). If that list contains `None`, sample
  up to its length and fill any remaining rows with `None`, so non-null values stay distinct and repeated SQL nulls
  are allowed. The same seed root as today, a different draw.
- **Reference.** After `parent_keys` returns: the capacity check runs first, before the empty-pool return
  (`:139-140`), so an all-null parent refuses instead of emitting all `None`. Then `random` distribution uses
  `rng.sample(ref_vals, n)`; `sequential` keeps `ref_vals[i]` and adds only the refusal when `n > P` instead of
  wrapping. The branch sits before the `distribution` loop (today `:143`).
- **Pair.** In the table loop of `_generate_tables_from_config` (`synthesize.py:144-157`), at whichever member is
  declared first (call it `a`) the loop calls `unique_pair(...)`, assigns `data[a]`, and keeps `b`'s values in a
  local `pending` dict. When the loop reaches `b` it assigns `data[b]` from `pending` instead of calling
  `_generate_column`. Assigning at each member's own turn keeps the declared column order (`pa.table(data)` takes
  dict insertion order, `synthesize.py:176`) and keeps `b` unreadable to columns declared before it, as today. The
  pair seed is `GenDeriveContext.for_column(with_table(col_a, name)).base_int("unique_pair")` (`with_table` is
  required, `generators/derivation.py:68-89`). Indices are `rng.sample(range(P_a * P_b), n)` after a guard that
  refuses `P_a * P_b > sys.maxsize` (the `range` length limit) with the same code; `a = ref_a[idx // P_b]`,
  `b = ref_b[idx % P_b]`. The referenced-formula post-pass (`synthesize.py:162-175`) reads finished `data`, so it
  sees both members.

Refusals go through `raise_uniqueness_impossible(table, column, type, needed, available, row=None)`, which raises
the existing `GenerationError(code=reasons.runtime.UNIQUENESS_IMPOSSIBLE)` (`generation/pool/_errors.py:14`) with a value-free
message, for example: "users.first_name: unique faker type 'first_name' repeated a value 1,000 times at row 9,412
(9,411 distinct so far); its values run out before row_count 50,000. Lower row_count, drop unique, or use a type
with more values."

### Routes

| Route | V1 behavior for a unique column or pair |
|---|---|
| Whole-table (`synthesize._generate_tables_from_config`, via `_streamed._run_legacy`, `generation/_streamed.py:507`) | Runs the draws above |
| Streamed core (`_streamed.run_generation`, `:319`) | `classify_plan` declines with `unique` (column) or `unique_pair` (member); the plan goes whole-table, as `determinism_fresh` does today (`_admission.py:225-226`). `sequence` stays admitted |
| Windowed `generate_rows` (`generation/window.py:290-295`) | `_class_b_reason` returns `unique` or `unique_pair`, checked before its `null_injection` test (`:451`) so the reason names the real cause; it takes the table's `unique_together` as a new argument from `classify_window_table` (`:360`); `sequence` stays exempt |
| Pinned (`_pinned_execute._run`) | Column `unique` runs through `_generate_column`; `unique_together` refused at compile |
| V1 `ColumnGenerator` (`generators/columns.py`) | No production generate caller; untouched |
| Native | No generation kernel exists (FR3 shelved); the draw-site catalogue gains entries only |

`unique_set_bytes(rows, payload)` (one new function in `generation/_bounds.py` beside `faker_pool_bytes`) prices
the seen set and the `random.sample` working set. The peak calculation adds the largest one-column unique working
set for a table, not the sum across sequential column draws. That peak joins the generation floor
(`_bounds.py:312-328`), which every caller including `decoy run` gets, and the legacy ledger reservation
(`_streamed.py:527-537`) when `budget_bytes` is set.

### CLI

- `decoy run`: a `GenerationError` whose `code` is `uniqueness_impossible` exits 1 (`EXIT_USAGE`), not 3. The CLI
  maps by exception type (`src/decoy/cli/run.py:993-1040`), and other `GenerationError` codes (`dtype_unsafe`,
  `timezone_naive_datetime` and others raised by masking) must stay at 3, so the check is
  `isinstance(exc, GenerationError) and exc.code == "uniqueness_impossible"`, with `GenerationError` imported from
  the top-level `decoy_engine` export (`__init__.py:166`). This also moves the masking pool's runtime refusal from 3
  to 1 (owner question 2).
- `decoy explain unique`: what `unique` and `unique_together` do, which types take them, the validate and run
  refusals, and `pooled` (applies to `faker` only; automatic at 50,000 rows and up for the certified types
  (`generation/_faker_pool.py:230`, `:239-264`); caps distinct values near 10,000; `pooled: false` opts out;
  `pooled: true` has no effect; `unique: true` turns it off).

### Native and the draw-site inventory

Four new catalogue entries in `execution/native/_draw_sites_gen.py`: `gen.faker_unique_retry` (per-row reseed,
family `faker_unique`), `gen.categorical_unique`, `gen.reference_unique` and `gen.unique_pair` (each one
`random.Random(...).sample`). The pinned digests in `tests/native/test_draw_sites_catalogue_pinned.py` are
re-pinned in the same commit after showing the old catalogue reproduces the old digests.

## 4. Behavior

| Input | Today | After |
|---|---|---|
| `email` faker, 200,000 rows, `unique: true` | key refused (GEN-STRICT) | 0 duplicates; first-draw rows equal today's no-unique output |
| `first_name` faker, 50,000 rows, `unique: true` | key refused | `decoy run` exit 1, `uniqueness_impossible` at the row the names run out |
| `name` faker, 50,000 rows, no `unique` | pooled, about 9.4k distinct | unchanged bytes |
| `categorical` 5 categories, 5 rows, `unique: true` | key refused | a permutation of the 5 |
| same with 6 rows | key refused | `decoy validate` exit 1, `generate_unique_domain_too_small` |
| `reference` to a 100-row `sequence` parent, child 100 rows, `unique: true` | key refused | every parent key once |
| same, child 101 rows | key refused | `decoy validate` exit 1 |
| parent with `null_probability: 0.5`, child 80 rows, `unique: true` | key refused | `decoy run` exit 1 (P near 50) |
| junction 300 rows over 30 x 10 parents, `unique_together` | key refused (TableConfig forbids extras) | all 300 pairs once |
| same with 301 rows | key refused | `decoy validate` exit 1 |
| `unique: true` on `formula` | key refused as unknown | refused as `generate_unique_unsupported` naming `formula` |
| config with neither key | today's bytes | today's bytes |

## 5. Enumerations (from the code at engine `f118ee02`, CLI `9f75282`)

### 5.1 Routes that generate a column

- Whole-table: `generation/synthesize.py:144-157` loop, `_generate_column` dispatch at `:268-301`.
- Streamed core: `generation/_streamed.py:319-400`, columns from `generation/_column_streams.py:246-289`,
  admission `generation/_admission.py:149-247`.
- Windowed: `generation/window.py:280-297`, admission `generation/_window_admission.py:360` and `:439-480`.
- Pinned: `generation/_pinned_execute.py:183-209`, compile refusals `generation/_pinned_compile.py:121-180`.
- Referenced-formula post-pass: `generation/_referenced_formula.py`, which reads finished columns; pairs are
  finished before it runs.
- Native: N/A, no generation kernel; catalogue only.

### 5.2 Exit paths

- Validate: `GenerateColumnConfig._options_valid` (`config/_tables.py:299`) for the per-type `unique` checks; the new
  `TableConfig` and `PipelineConfig` validators for `unique_together` and domain sizes; all surface as
  `ValidationError` or `ConfigRuleError` (exit 1).
- Run: `raise_uniqueness_impossible` raises inside generation, before `pa.table(...)` for the table and before any sink commit;
  the streamed sink is not opened on the whole-table route, and nothing is published until generation returns.
- Memory: the generation floor and, with a budget, the ledger (section 3).
- Pinned compile: `PinnedModeError` for `unique_together`.
- CLI: exit 1 for `uniqueness_impossible` only; other `GenerationError` codes unchanged.
- No file, lease or temp resource is added, so no cleanup path is added.

### 5.3 FK shapes

- Single `reference` column with `unique`: one child row per parent key at most (one-to-one or one-to-zero).
- Junction pair over two different parents: each `(a, b)` at most once.
- Junction pair over one parent twice (`friendships.user_a`, `user_b` both to `users`): allowed; `(x, x)` pairs are
  possible and `(x, y)` differs from `(y, x)`. Recorded in the docs; excluding self-pairs is follow-up F5.
- Self-reference: rejected as a cycle today (`config/_pipeline.py:317-319`), unchanged.
- Fitted fan-out (`distribution: fitted`, FK-FANOUT E2b): refused with `unique` and on pair members.

### 5.4 Fields (every key this plan adds)

- Column option `unique` (registered for all 11 generate types; accepted for 4).
- `TableConfig.unique_together: list[list[str]]`, omitted from dumps when empty.
- `_EXCLUDED_FROM_FINGERPRINT` gains `unique`.
- Reasons: `config.generate_unique_domain_too_small`, `config.generate_unique_conflict`,
  `config.generate_unique_unsupported`, `plan.pinned_unique_pair_unsupported`; reuse `runtime.uniqueness_impossible`.
- Admission reasons `unique`, `unique_pair`; Class B reasons `unique`, `unique_pair`.
- Seed families `faker_unique`, `unique_pair` in `GEN_FAMILIES`.
- Draw sites: four entries (section 3).

### 5.5 Output surfaces

- Generated cells (CSV, Parquet, in-memory Arrow).
- Refusal messages at validate and run (value-free, U7); CLI exit codes.
- `GenerationResult.route` and `declines` (`legacy:<table>.<col>:unique`).
- JSON schema from `closed_key_schema` (`config/_generate_options.py:365`) and `decoy schema`: gains `unique` per type
  and `unique_together` per table.
- `decoy explain unique` text.
- Logs: none added.

## 6. Class table

| # | Class | Where it applies | Structure that closes it | Proof | Mutation that must turn it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output | refusal messages; the seen set | the one run-time builder accepts only names, counts and row index; config checks use fixed summaries; the seen set never leaves the function | AT-10 marker corpus and refusal-builder sentry | M1: bypass the builder and put the repeated value in the faker message |
| 2 | Cleanup missing on an exit path | run-time refusal mid-table | N/A for resources: the feature opens, stages and leases nothing. The related risk is a partial result, closed by raising, never returning a prefix | AT-2, AT-7: no output file and no partial table after a refusal | M2: catch the refusal and return the drawn prefix padded with `None` (AT-2 red) |
| 3 | State machine and concurrency | the seen set and the retry counter | function-local, created per column per run; no module state | AT-1 run twice in one process gives equal bytes | M3: hoist the seen set to module scope (AT-1 second run red) |
| 4 | Route parity and silent fallback | streamed, windowed, pinned, whole-table; the five pooled reads; the parent key list | one admission decline, one Class B reason, one `pool_opted_out` predicate, one `parent_keys`, one draw module | AT-7 route matrix; `test_phase_f_audit_no_legacy.py` matrix cells; AT-9 AST sentry | M4: keep one inline `pooled` read (AT-9 AST sentry red) |
| 5 | Referential integrity | unique and pair draws over parent keys | draws only from `parent_keys`; refusal when `P` is short, including `P = 0` | AT-4, AT-5: every child key exists in its parent; all-null parent refuses; pair-population spy pins the exclusive stop | M5: draw pair indices from `range(P_a * P_b + 1)` (AT-5 population spy red) |
| 6 | Untrusted producer | YAML values for `unique` and `unique_together` | strict bool, exact list-of-two-names shape, names resolved in the table | AT-3, AT-5, AT-8 refusal cells | M6: accept `unique: "true"` (AT-8 red) |
| 7 | Check then use (TOCTOU) | validate-time bound vs run-time `P` | validate uses provable upper bounds only; the run re-checks the real `P` | AT-4 parent-null cell passes validate and refuses at run | M7: skip the run-time check when validate passed (AT-4 red) |
| 8 | Weak tests | "zero duplicates" on small data | every distinctness test also runs the same config without `unique` and asserts duplicates exist | AT-1, AT-5 non-vacuity lines | M8: turn the unique branch into the plain draw (AT-1 red) |
| 9 | Refactor drift | configs without the keys; the `pool_opted_out` swap; the `parent_keys` extraction; the column order of a pair table | additive keys; serializer omission; predicate and helper equal to the old code when the keys are absent; assignment at each member's turn | AT-8 golden bytes and Plan bytes; AT-5 order cell | M9: dump `unique_together: []` (AT-8 Plan cell red) |
| 10 | Frozen contract drift | seed fingerprint, Plan and `config_json` bytes, reason registry, draw-site catalogue, CLI exit code | exclusion list change with no existing input moved; registry minor plus fixture; catalogue re-pin; exit change named as owner question 2 | AT-8, `test_reason_minor_fixtures.py`, `test_draw_sites_catalogue_pinned.py`, AT-12 | M10: leave `unique` out of `_EXCLUDED_FROM_FINGERPRINT` (AT-1 U5 cell red) |

## 7. Acceptance tests (red first)

Each is written and recorded red before the slice that turns it green.

- **AT-1 faker distinct (E2).** `email` and `user_name` at 200,000 rows and `name` at 60,000 rows (pool-eligible)
  with `unique: true`: zero duplicates. Non-vacuity: the same columns without `unique` have duplicates. U5, with
  `plain` the same column with `pooled: false` and no `unique`, compared before the null pass (`null_probability`
  unset): for every row `out[i] == plain[i]` exactly when `plain[i] is None` or `plain[i]` is not in the non-null
  values of `out[:i]`. Two runs in one process
  are byte-equal.
- **AT-2 faker too small (E2, C1).** `first_name` at 50,000 rows with `unique: true`: `decoy validate` exits 0;
  `decoy run` exits 1 with `uniqueness_impossible`, the message names `users.first_name`, a row index and a
  distinct count, and no output file exists.
- **AT-3 categorical (E1, E2).** 5 categories, 5 rows: a permutation. 6 rows: validate refuses
  `generate_unique_domain_too_small`. Duplicate category entries count once. `weights` with `unique`: refused
  `generate_unique_conflict`. Categories `[None, "a"]` at 4 rows emit `"a"` at most once and fill the rest with
  `None`, with no duplicate non-null value.
- **AT-4 reference (E1, E2).** Parent `sequence` 100 rows: child 100 rows uses every key once; child 101 rows
  refused at validate; parent with `null_probability: 0.5` and child 80 rows passes validate and refuses at run;
  parent with `null_probability: 1.0` refuses at run (never all `None`).
  `sequential` with `unique` and `n <= P` equals today's sequential output. Every child value exists in the
  parent.
- **AT-5 junction pair (E1, E2).** 30 x 10 parents, 300 rows: each pair exactly once; 301 rows refused at validate;
  the QA shape (300 rows over larger parents) gives 0 duplicate pairs while the same config without
  `unique_together` gives at least 1. One parent used twice gives distinct ordered pairs. A table declared `[a, x, b]` with `unique_together: [[b, a]]`
  writes columns in the order `a, x, b`, and `x` is unchanged versus the same table without the pair. Member rules (non-reference
  member, three names, a name not in the table, a member in two entries, a member with `unique`,
  `null_probability` or `determinism: fresh`) each refuse with `generate_unique_conflict`. Parents whose product
  exceeds `sys.maxsize` refuse with `uniqueness_impossible`, not `OverflowError`. A unit spy on the population
  passed to `sample` asserts its exclusive stop is exactly `P_a * P_b`.
- **AT-6 nulls (E2).** `categorical` with `unique` and `null_probability: 0.3`: non-null values distinct, several
  nulls present; `row_count` one above the category count with `null_probability: 0.5` still refused at validate.
  A controlled Faker provider that always returns `None` emits the requested null rows without retry or refusal.
- **AT-7 routes and determinism (E2).** `classify_plan` declines unique columns with `unique`, pair members with
  `unique_pair`, and admits `sequence` with `unique: true` (bytes equal to the same column without it);
  `generate_rows` refuses Class B `unique`; `generate_tables` and `generate_tables_streamed` (bounded sink) give
  equal bytes; adding `unique: true` to one column leaves every other column byte-identical (U4); a run refused
  mid-table publishes nothing.
- **AT-8 old configs and strict values (E1).** The golden generation corpus is byte-identical; serialized Plan,
  `config_json` and pinned fingerprints for configs without the keys are unchanged; `unique: "true"` and `1` are
  refused; `unique: null` is accepted as semantically absent and has the same output as an omitted key, while its
  explicit null may remain in `config_json`; `decoy schema` in serialization mode still lists `TableConfig`'s fields.
- **AT-9 pooled (E1, E2).** `pooled: true` with `unique: true` refused; `first_name` at 60,000 rows with `unique`
  builds no pool (spy on `_faker_pool.build_and_sample`). An AST sentry over engine `src/` finds no `"pooled"`
  subscript or `.get("pooled")` outside `pool_opted_out`, however the call is written (catalog class 5, normalized
  scanners).
- **AT-10 value-free refusals (E1, E2).** Seed a marker string in categories, parent keys and a custom
  provider's output; trigger every refusal; no message contains the marker. An AST sentry permits construction of
  `GenerationError(code=UNIQUENESS_IMPOSSIBLE)` only inside `raise_uniqueness_impossible`, and verifies that both
  the masking sampler and new generation draws call that builder.
- **AT-11 pinned (E2).** `categorical` and `reference` with `unique` give equal bytes on pinned and whole-table
  routes; `unique_together` refused at pinned compile.
- **AT-12 retry bound and memory (E2).** A controlled fake records `seed_instance`: attempt 0 for one row returns
  an already-seen token and attempt 1 returns a fresh token, proving the retry uses `faker_unique`; an always-colliding
  fake refuses after exactly 1,000 attempts for that row with `uniqueness_impossible`. A `budget_bytes` too small
  for the seen set refuses `generation_budget_exceeded`; a host floor below the largest per-table
  `unique_set_bytes` peak refuses through `generation_floor_shortfall`, while two sequential unique columns reserve
  the larger working set rather than their sum.
- **AT-13 unsupported types (E1).** `unique: true` on each of the seven other types refuses
  `generate_unique_unsupported` naming the type; `unique: false` on them is accepted and changes nothing.
- **AT-14 CLI (C1).** Exit codes for AT-2 (1) and AT-3 (1); another `GenerationError` code (`dtype_unsafe`) still
  exits 3; `generation_budget_exceeded` exits per owner question 6; `decoy explain unique` mentions `unique`,
  `unique_together` and `pooled`; `decoy schema` shows both keys; a probe over a scaled-down junction reports a
  probe outcome, not a crash.

Mutations (each must turn the named test red): M1 value in a message (AT-10); M2 return the drawn prefix padded
with `None` instead of raising (AT-2); M3 module-scope seen set (AT-1); M4 one inline pooled read left (AT-9 AST
sentry); M5 off-by-one pair population
(AT-5 population spy); M6 accept a string bool (AT-8); M7 skip the run-time `P` check (AT-4); M8 plain draw in the unique branch
(AT-1); M9 dump an empty `unique_together` (AT-8); M10 leave `unique` out of the fingerprint exclusion (AT-1); M11 retry from
`row_int("faker", i)` again (AT-12 controlled retry); M12 patch `unique_capacity_ok` to always return
`True` (every too-small cell in AT-3, AT-4 and AT-5 loses its coded refusal); M13 assign `data[b]` at `a`'s turn
(AT-5 order cell); M14 return before the capacity check on an empty parent (AT-4 all-null cell).

## 8. Owner questions

Owner answers (Cam, 2026-11-09): "go with recommended". Every question below takes its recommended option.

1. The pair key is a table field `unique_together: [[a, b]]` (Django's name, a SQL table `UNIQUE (a, b)`).
   Alternative: `primary_key: [a, b]`, SDV's composite-key spelling, which also claims "this is the row key".
   Recommended: `unique_together`, because a junction table may also have its own id column.
2. `uniqueness_impossible` at run time exits 1 (usage: the fix is in the config) instead of 3. This also changes
   the masking pool's runtime refusal from 3 to 1. Recommended: 1 for both. Alternative: 1 only when raised by
   generation.
3. With `null_probability`, the capacity check counts every row, not the expected non-null rows, so validate can
   decide it and a lucky null draw never decides success. Recommended: every row.
4. Plans with a unique column (other than `sequence`) run on the whole-table route in V1, so very large unique
   runs hit the memory refusal (exit 5) sooner than streamed runs would. Streaming with a carried seen set is
   follow-up F3. Recommended: defer.
5. `pooled: true` together with `unique: true` is refused rather than letting `unique` win silently. Recommended:
   refuse.
6. `generation_budget_exceeded` is a `DecoyError` and exits 3 in the CLI today; only the two out-of-core codes
   exit 5 (CLI `src/decoy/cli/gate/_run.py:31`, `src/decoy/cli/run.py:977-992`). Recommended: map it to 5 in C1, since it is a memory refusal.
   Alternative: leave it at 3 and say so in the guide.

## 9. Reuse inventory

- `unique_capacity_ok` (`generation/pool/_capacity.py:26`), the only size rule (docstring reworded, U6).
- `parent_keys`, extracted from `_reference` (U8).
- `runtime.uniqueness_impossible` and `GenerationError` (`generation/pool/_errors.py:14`), with the existing
  masking raise at `generation/pool/_sampler.py:305-314` folded into the new shared refusal builder.
- Permutation sampling as the masking `UNIQUE` sampler does it (`generation/pool/_sampler.py:315`), via CPython
  `random.Random.sample`.
- `GenDeriveContext.base_int` and `row_int` families (`generators/derivation.py:315-324`) for every new seed.
- `_reference`'s parent key list (`generation/_synthesize_derived.py:127-137`), not rebuilt.
- GEN-STRICT `OptionSpec` registry and `closed_key_schema` for the new key and its schema.
- The `protected_quarantine` omit-when-unset serializer (`config/_pipeline.py:151-161`).
- The `determinism_fresh` admission decline and the Class B reason list as the route precedent.
- `faker_pool_bytes` beside the new `unique_set_bytes` in `generation/_bounds.py`.
- The F-audit matrix (`tests/parity/_f_audit_matrix.py`) for the two new decline reasons.

## 10. Placement (checked against `CODEMAP.md`)

| Change | File | CODEMAP |
|---|---|---|
| Unique draws | new `generation/_unique.py` (about 180 lines; `generation/` holds 22 flat modules plus `__init__` now, 23 flat modules plus `__init__` after) | CODEMAP line 94 places generation helpers here; add a row beside `_synthesize_derived.py` at line 46 |
| Shared run-time refusal | `generation/pool/_errors.py`, `generation/pool/_sampler.py`, `generation/pool/__init__.py` | CODEMAP line 94 places generation pool helpers here |
| Faker and categorical branches, pair hook in the table loop | `generation/synthesize.py` (500 to about 525) | line 45 row gains "and the unique pair hook" |
| Reference branch, `parent_keys` | `generation/_synthesize_derived.py` (179 to about 195) | line 46 row names `parent_keys` |
| `pool_opted_out` | `generation/_faker_pool.py` | line 55 row unchanged |
| Declines | `generation/_admission.py` (266 to about 275), `generation/_window_admission.py` (493 to about 500) | rows 49 and GEN-API unchanged |
| Seen-set bytes | `generation/_bounds.py` (361 to about 372) | line 52 row unchanged |
| Pinned refusal | `generation/_pinned_compile.py` (326 to about 334) | line 53 row gains `unique_together` in its rejection list |
| `unique` option per type | `config/_generate_options.py` (382 to about 410) | config row unchanged |
| `unique_together` field and serializer | `config/_tables.py` (419 to about 440) | config row unchanged |
| Table and cross-table checks | new `config/_generate_unique.py` (about 140 lines; `config/` holds 20 flat modules plus `__init__` now, 21 flat modules plus `__init__` after) | CODEMAP line 81 places config validators here; update that row to name it beside `_generate_options.py` |
| Domain-size validator hook | `config/_pipeline.py` (378 to about 386) | unchanged |
| Fingerprint exclusion, seed families | `generators/derivation.py` | line 127 row unchanged |
| Secret-material census | `scripts/secret_material_census.py` data, rewritten with `--write` after `_faker_pool.py` lines move (`:448-458`) | n/a |
| Reason codes | `reasons/config/`, `reasons/plan/_part1.py` (the `PinnedModeError` block, beside the other `pinned_*` codes), registry fixture | n/a |
| Catalogue entries | `execution/native/_draw_sites_gen.py` (285 to about 345) plus pinned digests | n/a |
| Methodology entry | `docs/methodology-registry.yaml` | n/a |
| Reference doc | `docs/guides/generate-uniqueness.md` (unique, `unique_together`, `pooled`) | docs index |
| CLI | `src/decoy/cli/run.py` exit tuple, `src/decoy/cli/explain.py` topic | CLI CODEMAP unchanged |

No module is added to a flat folder over 25. No new or touched hand-written Python engine file passes 600 lines.
CLI `src/decoy/cli/run.py` is already 1,748 lines at `9f75282`; C1 adds only the bounded exit-mapping branch and
does not create that pre-existing size debt.

## 11. Frozen contracts touched

- **Determinism**: unchanged for every config without the new keys (U3). `_EXCLUDED_FROM_FINGERPRINT` gains
  `unique`; no accepted config carries it today, so no root moves.
- **Serialized Plan, `config_json`, pinned fingerprints**: unchanged when the keys are absent; `unique_together`
  omitted when empty.
- **Config JSON schema**: additive keys.
- **Reason registry**: four codes, one registry minor and fixture; renumbered at merge if another slice takes the
  minor.
- **Draw-site catalogue**: four entries, digests re-pinned.
- **CLI exit codes**: `uniqueness_impossible` moves from 3 to 1 (owner question 2). The exit integers themselves
  do not change.

## 12. Slices

| Slice | Repo | Size | Content | Gate |
|---|---|---|---|---|
| E0 | engine | S | four reason codes, registry minor and fixture | T1 plus the reason sentries |
| E1 | engine | M | `config/_generate_unique.py` check functions and the strict `unique` check, unit-tested directly; `pool_opted_out` swap and `parent_keys` extraction (behavior-preserving while `unique` is unregistered); `unique_set_bytes`; `unique_capacity_ok` null flag and docstring; shared uniqueness refusal builder plus the masking sampler conversion; secret census `--write`; AT-8 golden and Plan cells, AT-9 and AT-10 AST sentries | T1 |
| E2 | engine | M | register `unique` per type and the `unique_together` field (same commit as the draws, so no head accepts a key it ignores); `_unique.py`; the branches and the pair hook; declines; budget; pinned refusal; fingerprint exclusion; catalogue re-pin; F-audit cells; AT-1 to AT-13 | full suite (serialization, draw-site catalogue, fingerprint contract) |
| D1 | engine | S | `docs/guides/generate-uniqueness.md`, methodology entry, CODEMAP rows | docs build |
| C1 | CLI | S | exit mapping, `decoy explain unique`, engine pin bump; AT-14 | full suite (version pin) |

Order: E0, E1, E2; D1 with or after E2; C1 after an engine release carries E2. Every head is runnable: before E2
the keys are refused as unknown, exactly as today.

Collisions:
- FK-FANOUT E2a/E2b edit `_reference` (a `fitted` branch after the pool build, before `for_column`) and the
  `reference` option registry. This plan's reference branch sits in the same place and its conflict rule names
  `fitted`. Whichever merges second rebases: the two branches are exclusive by the conflict rule, and the
  `fitted` refusal lands with whichever slice adds the later of the two.
- GEN-C1 (row 24i) changes the column seed derivation in `generators/derivation.py`; the `unique` exclusion is a
  one-line set entry and rebases either way. U5 is stated against the seed root of the merge base.
- GEN-STRICT follow-ons and GEN-CATALOG (row 24s) read the same option registry; `unique` appears there for free.
- GEN-FIT-SHAPE (row 24l) re-pins the same draw-site catalogue; digests are recomputed on the merged tree.

Worker rules for every slice: name the class of each finding and fix the class; write the generated tests first
and record them red; attack your own diff by mutating each new mechanism once (section 7 list) and show the test
goes red; run baselines in the same environment as the branch with `uv.lock`; never widen a threshold, skip a test
or loosen a bound to pass; if a new defect of a class already in the catalog appears, stop and report it.

## 13. Pre-mortem

- **A reviewer calls `_unique.py` a second pool mechanism.** It holds no pool, cache, capacity rule or error
  builder: it calls `unique_capacity_ok` and the shared refusal builder, and samples like the masking sampler. M12
  against AT-3 through AT-5 keeps the size rule single; the AT-10 sentry keeps the refusal builder single.
- **U5 breaks after GEN-C1.** GEN-C1 changes roots for every column, so `plain` moves with `out`; U5 compares two
  runs on the same base and stays true.
- **Faker runs slowly near a full domain.** Expected work rises toward about `D * ln(1000)` calls near the point
  where a row is likely to exhaust its 1,000 attempts; the hard bound is 1,000 calls per requested row. The guide
  states both instead of promising a run-time duration from the measured per-row rate (`_faker_pool.py:225-226`).
- **The streamed decline surprises a large job.** The route and decline are in `GenerationResult.declines`; owner
  question 4 records the trade.
- **`random.Random.sample` changes in a future CPython.** The repo already pins draws to CPython `random` (the
  statistical sampler, `categorical`); the determinism suite on the supported Python versions is the tripwire.
- **An old serialized Plan carried an ignored `unique` key.** Pre-GA hard delete; such a Plan is refused by
  GEN-STRICT on load today, so the exclusion cannot move a root that still runs.

## 14. Follow-ups (recorded, not in V1)

- F1 `unique` for `statistical` categorical kinds and for `formula` with an enumerable domain.
- F2 unique tuples over more than two columns or over non-reference columns.
- F3 streamed and windowed unique columns with a carried seen set (prefix-stable in row order).
- F4 normalized uniqueness (case-folded emails).
- F5 a pair option that excludes self-pairs when both members reference one parent.
- F6 one parent key builder for `_reference` and the streamed `KeyPool`.
