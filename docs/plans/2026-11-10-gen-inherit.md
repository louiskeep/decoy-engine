# GEN-INHERIT: child columns that follow their parent row, and conditional categoricals in YAML

Status: plan

Rules consulted: 00-universal, risk-and-exceptions, development-loop, code-review, testing,
architecture, feature-dev, cross-cutting-concerns, api-and-compatibility, exploration, reuse-first,
security, documentation, verification, profiles/local-operations, catalogs/recurring-defects

Base: engine main `2aec8be8`. Roadmap row 24r. Engine only; CLI help picks the new type and keys up
from `generate_option_catalog()` (GEN-CLI, row 24o).

**Prerequisites.**
- E1 (`inherit`) needs GEN-REL E3a (row 24n, plan `docs/plans/gen-rel.md` on `plan/gen-rel`):
  the parent-row lookup `generation/relational/_parent_lookup.py`, the central generate-input edge
  registry `config/_generate_relational.py`, the parent-key uniqueness rule and
  `plan.pinned_relational_option_unsupported`. None of it is on main yet; E1 starts after E3a merges.
- E2 (conditional `categorical`) needs GEN-COND-CAP E1b (row 24w, engine PR #461,
  `feat/gen-cond-cap-e1b` `00b1018b`): `_select_table`, `conditional_fallback_counts`,
  `DISTINCT_PARENT_CAP`, `warn_on_unseen_parents`, `emit_generation_warning` and
  `warning.generation_condition_parent_unseen`. E2 also leans on GEN-STRICT E2 (row 24k) for the
  validate-time forward-reference refusal of `ref_kind="column"` options. E2 does not need GEN-REL.
- Line numbers are from `2aec8be8` and from `00b1018b` for #461 code, and will move.

**Guarantee.** A child column can carry its parent row's value through the FK the child already
draws (`claims.state` equals the state of the member that `claims.member_id` points at, on every
row). A categorical column can declare its category mix per value of an earlier column in YAML,
with no fitted snapshot. Rows whose parent value has no declared mix draw from the column's
top-level mix and produce the same value-free warning a fitted `condition_on` produces.

**Non-goals:** a second FK mechanism (the child's existing `reference` column is the only link);
composite FKs; inheriting across two hops in one column (chain two `inherit` columns instead);
range keys on conditional categoricals (`18-34`; derive a band column first); conditioning on two
columns at once; the streamed route admitting either feature; pinned mode admitting `inherit`;
mask-side changes.

**Design notes:** one new generate type (`inherit`) and two new `categorical` keys
(`condition_on`, `by`). `inherit` is a thin consumer of GEN-REL's parent lookup and edge registry;
the conditional categorical is a thin consumer of GEN-COND-CAP's selection and warning seam. No new
registry, no second warning path, no new FK concept.

## Why

The 2026-11-08 generation completeness audit (CLI `docs/records/2026-11-08-generation-completeness-
audit.md`) lists both gaps under section C (the roadmap row cites section B; the items are C1 and
C8):

- C1: a child cannot inherit a parent attribute. `claims.member_state` matched its member's state
  on 6% of rows, which is chance. Denormalized tables (a claim carrying the member's state, an order
  line carrying the order's currency) are common in every industry the audit covered.
- C8: `condition_on` works only from a fitted snapshot. A user with no data cannot say "tier
  depends on region" or "diagnosis mix depends on age band".

Today's workarounds give wrong data silently: an independent `categorical` for `state` produces
claims in states their member never lived in.

## Behavior

### E1: `inherit`

```yaml
tables:
  - name: members
    generate_columns:
      - {name: member_id, type: sequence, start: 1}
      - {name: state, type: categorical, categories: [CA, NY, TX]}
  - name: claims
    generate_columns:
      - {name: claim_id, type: sequence, start: 1}
      - {name: member_id, type: reference, reference_table: members, reference_column: member_id}
      - {name: member_state, type: inherit, via: member_id, column: state}
```

- `via` (required) names an earlier `reference` column in this table. `column` (required) names a
  column of that reference's `reference_table`.
- Each row gets the value of `column` in the parent row whose `reference_column` equals this row's
  `via` value. A null `via` value gives null. No random draw (`DETERMINISTIC_NO_DRAW`).
- The parent row is found by GEN-REL E3a's lookup (the one `anchor_via` uses): the parent key must
  be unique, by the same rule and with the same refusal GEN-REL defines for `anchor_via`. A
  self-referencing `via` is refused by the same rule as a self-referencing `anchor_via` (the parent
  table is not in `pools` while it is being generated).
- Values are copied from the parent's emitted Arrow table (`pools[...]`, set at
  `generation/synthesize.py:208`), so the child column has the parent column's Arrow type, nulls and
  any declared output type. The child column is built with that type explicitly; Arrow inference is
  not used (an all-null child, or a decimal parent, would infer a different type).
- Works with every reference distribution, including fitted fan-out (FK-FANOUT E2b, engine #452):
  the lookup reads the FK value, not how it was drawn.
- Chains work across tables: `claim_lines.member_state` can `inherit` `claims.member_state`, because
  the parent table is complete before the child starts (`generation_order`,
  `generation/synthesize.py:213`).
- Refusals:
  - `via` missing, not a `reference` column, or later in the table: GEN-STRICT's existing
    `generate_option_*` codes and GEN-STRICT E2's forward-reference refusal.
  - `column` not in the parent table: a new compile check, `plan.generate_inherit_column_missing`,
    beside GEN-REL's branch-aware `anchor` check in `plan/_checks_windowed_date.py`.
  - `column` naming a GEN-REL per-parent aggregate (filled in a post-pass): GEN-REL's
    `config.generate_aggregate_read_before_fill`, through the edge registry.
  - `null_probability` on `inherit`: allowed and applied after the copy, as on every type.
  - `unique: true`: refused by `unique_type_problems` (`inherit` is not in `UNIQUE_TYPES`,
    `config/_generate_unique.py:55`).
- Routes: streamed admission declines it (`ADMISSION_MATRIX` entry `(False, "reads a parent row by
  FK; no cross-table row seam")`); pinned compile refuses it with GEN-REL's
  `plan.pinned_relational_option_unsupported`, and pinned execute uses GEN-REL's runtime twin.
- Edges: the central input-edge registry records `via` as a same-table input and `column` as a
  cross-table input of the reference's parent table. Generation order, the reproducibility graph
  (`generation/_reproducibility.py:58`), plan checks and `numeric_plan` read edges from there. The
  existing `_SIBLING_KEYS` entry `"column"` must not treat `inherit.column` as a sibling; AT-6
  checks a same-named sibling is never read.
- Typing: `READER_KEYS["inherit"] = (("via", "inherit.via"),)` with
  `Reader(True, "matched by value against the parent key, whose type it copies")`. The parent column
  is read the way `reference.reference_column` reads its parent (`Reader(True, "copies the parent
  cell and its type")`, `generation/_numeric_typing.py:133`), so `numeric_plan` gives the `inherit`
  column the parent column's planned type through `planned()` (line 246), and a same-table
  `derived` that reads `member_state` sees what the parent emitted.

### E2: conditional `categorical` in YAML

```yaml
- name: region
  type: categorical
  categories: [north, south]
- name: tier
  type: categorical
  categories: [basic, plus, premium]   # the fallback mix
  weights: [5, 3, 2]
  condition_on: region                  # an earlier column in this table
  by:
    north: {weights: [2, 3, 5]}                          # same categories, different mix
    south: {categories: [basic, plus], weights: [9, 1]}  # its own categories
```

- `condition_on` names an earlier column in the table (any type, including an `inherit` column, so
  "tier by the member's region" is E1 plus E2). `by` maps a parent value to an entry with
  `weights`, or `categories` and optional `weights`.
- Each row looks up its parent value through #461's `_select_table`
  (`generation/statistical/_sample.py`, the one place the parent is keyed): the key is `str(parent)`,
  so `1` matches `"1"`, `true` parents match `"True"`, and a null parent matches `"None"`. A row whose
  key has no entry draws from the top-level `categories` / `weights`.
- The draw uses the column's one `random.Random(base_int("py"))` and one `choices` call per row
  (`generation/synthesize.py:389`). `choices` consumes one `random()` per element with or without
  weights, so a conditional column whose every entry equals the top-level mix gives the same bytes
  as the plain column (AT-9). A categorical without `condition_on` is unchanged byte for byte.
- When any row fell back, the run raises `warning.generation_condition_parent_unseen` once for the
  column through `warn_on_unseen_parents` and `emit_generation_warning`, with the same detail keys
  as the statistical case and `provider: "categorical"`. The distinct-key count stops at
  `DISTINCT_PARENT_CAP`. No cell value or parent value is in the record or the log line.
- Checks (config time, the registry, value-free):
  - `by` without `condition_on`, or `condition_on` without `by`: `missing` on the absent key
    (the conditional-required table GEN-TYPES adds, or the inline precedent in `option_problems` if
    GEN-TYPES has not merged).
  - `by` not a mapping with string keys: `generate_option_type` (YAML `1:` must be quoted `"1":`;
    a JSON plan would turn an int key into text and break the match after a round trip).
  - An entry with keys other than `categories` / `weights`, or with neither:
    `generate_option_unknown` / `generate_option_type` at `("by", <key>, ...)`.
  - Entry weights: `_weights`'s rule, and length equal to the entry's categories (or the top-level
    categories when the entry has none): `generate_weights_invalid`.
  - `unique: true` with `condition_on`: `generate_unique_conflict` from `column_conflict_problems`
    (`config/_generate_unique.py:88`); a unique draw has no per-row mix.
- Arrow type: `_categorical_type` (`generation/_admission.py:95`) checks the top-level and every
  entry's categories together; mixed types refuse as a mixed plain list does today.
- Routes: resident and pinned both pass `data` to `_generate_column`
  (`generation/_pinned_execute.py:206`), so both support it, and `warn_on_unseen_parents` is called
  on both. The streamed route declines a categorical with `condition_on` (`(False, "reads a sibling;
  no sibling-input seam yet")`, the `derived` reason), from the categorical branch of the admission
  classifier (`generation/_admission.py:247`).
- `condition_on` is already in `_SIBLING_KEYS`. `READER_KEYS["categorical"]` is empty today
  (`generation/_numeric_typing.py:145`); E2 adds `("condition_on", "categorical.condition_on")` and
  registers `Reader(False, "matched against declared keys as text")`, the same verdict as
  `statistical.condition_on` (line 132). A parent read by a conditional categorical is then typed
  exactly as one read by a fitted `condition_on` (AT-11).

## Pre-mortem (failure modes)

| # | Failure | Where it would bite | Guard |
|---|---|---|---|
| P1 | `inherit` reads a same-named sibling instead of the parent column | silent wrong values when both tables have `state` | edges come only from GEN-REL's registry; AT-6 plants a same-named sibling |
| P2 | Parent key not unique, child picks an arbitrary row | inherited values drift with row order | GEN-REL's uniqueness rule, shared; AT-3 |
| P3 | Child Arrow type differs from the parent | Parquet schema mismatch, `1.0` in CSV | explicit parent type; AT-4 covers int with nulls, decimal, date, all-null |
| P4 | A second parent lookup grows beside GEN-REL's | two behaviors for one FK | E1 calls `_parent_lookup` only; sentry AT-12 |
| P5 | Conditional categorical keyed differently from fitted `condition_on` | `1` matches in one and not the other | both go through `_select_table`; AT-10 |
| P6 | Fallback rows go unreported, or are reported with a value | silent skew, or data in logs | `warn_on_unseen_parents` reused; AT-8 sentinel check |
| P7 | Conditional draw consumes a different number of random values | plain categorical bytes move, or prefix drift | one `choices` per row; AT-9 byte identity |
| P8 | JSON round trip changes `by` keys in pinned mode | pinned output differs from resident | string keys only; AT-7 pinned parity |
| P9 | The registry change for `by` entries opens a nested-key hole | unknown nested key silently ignored | nested keys checked in the registry; AT-2 |
| P10 | GEN-REL E3a lands with `anchor_via`-specific code names | `inherit` refusals say "anchor_via" | ask GEN-REL to name them for the lookup (Engineering calls); AT-3 checks the code text |

## Acceptance tests

| AT | What | Red on base / mutant |
|---|---|---|
| AT-1 | `inherit` appears in `GENERATE_TYPES`, the catalog, `ADMISSION_MATRIX`, `REJECTED_TYPES`, `READER_KEYS`, `GEN_KIND_TO_SITE`, `_GEN_ONLY`, the capability matrix | red on base |
| AT-2 | Registry: `inherit` needs `via` and `column`; `by` / `condition_on` pairing; nested entry keys, weights length and types per the E2 list, each with its code and location | red on base; mutant: skip nested key check |
| AT-3 | Non-unique parent key and self-referencing `via` refuse with GEN-REL's codes; missing parent column refuses with `plan.generate_inherit_column_missing` | red on base |
| AT-4 | For 1,000 claims over 50 members, every `member_state` equals its member's `state`; types kept for int with nulls, decimal, date and an all-null parent column; null FK gives null | red on base; mutant: draw a random parent row |
| AT-5 | Chain: `claim_lines` inherits `claims.member_state`, which inherits `members.state`; all three agree on every row | red on base |
| AT-6 | A child table that also has its own `state` column: `inherit.column: state` reads the parent, and the reproducibility report lists the parent edge, not the sibling | mutant: resolve `column` in the child |
| AT-7 | Route parity: resident and pinned give identical tables for a conditional categorical; streamed declines it; `inherit` is refused in pinned with `plan.pinned_relational_option_unsupported` | red on base |
| AT-8 | Unseen parent values: rows fall back to the top-level mix, one `warning.generation_condition_parent_unseen` with `provider: categorical`, counts right, a sentinel parent value absent from record, log line and exception chain | red on base; mutant: drop the call |
| AT-9 | Conditional categorical whose every entry equals the top-level mix is byte-identical to the plain column for 10 seeds; plain categorical goldens unchanged | mutant: draw `k=n` once per parent value |
| AT-10 | Int, bool and null parents match `"1"`, `"True"` and `"None"` the same way a fitted `condition_on` does | mutant: key by `repr` |
| AT-11 | A sequence parent read by a conditional categorical gets the same planned type as under a fitted `condition_on`, and rows still match `"1"` keys | red on base; mutant: register the reader as safe |
| AT-12 | Sentry: only `generation/relational/_parent_lookup.py` maps FK values to parent rows (AST scan for the lookup function's callers; no other module builds a key-to-row index over `pools`) | mutant: inline a dict lookup in the `inherit` generator |
| AT-13 | E1 plus E2: `tier` conditioned on an inherited `region` follows each member's region's mix within 4 standard errors at 50k rows | red on base |

## Class table

| # | Class | Where it can bite here | Closing structure | Sentry or property test | Mutation |
|---|---|---|---|---|---|
| 1 | Wrong value | Inherited value from the wrong parent row; wrong mix for a parent value | One parent lookup; one keyed selector | AT-4, AT-5, AT-13 | random parent row; key by `repr` |
| 2 | Cleanup | none: no files, handles or temp state | N/A, both features are pure over in-memory lists and pools | AT-4 shows no new artifact | none needed |
| 3 | State | Conditional tables rebuilt per row, or a shared RNG across columns | Tables built once per column; one `random.Random` per column | AT-9 byte identity | reseed per parent value |
| 4 | Route divergence | Pinned and resident differ; streamed half-supports a feature | Both routes call `_generate_column` with `data`; explicit streamed declines; explicit pinned refusal for `inherit` | AT-7 parity | drop `data` on the pinned call |
| 5 | Referential integrity | Child value not the parent row's value; non-unique parent key | GEN-REL uniqueness rule and lookup | AT-3, AT-4 | allow a non-unique key |
| 6 | Untrusted input | Nested `by` entries from YAML; parent values in warnings | Registry checks nested keys and weights; warning carries counts only | AT-2, AT-8 | quote the parent value in the log line |
| 7 | TOCTOU / check-then-use | Parent column checked at compile, read at run from a different table | The edge registry resolves the parent table once; the run reads `pools` by that name | AT-6 | resolve the table again at run from `column` |
| 8 | Weak test | Agreement asserted on a few rows | AT-4 checks every row; AT-13 at 50k rows within 4 standard errors | the mutation column | each listed mutation |
| 9 | Refactor drift | Generalizing #461's helpers from `StatisticalSpec` to plain tables changes statistical output | The generalization lands as its own commit with #461's tests and statistical goldens unchanged | `test_condition_fallback.py`, AT-10 | change `_select_table`'s keying |
| 10 | Frozen contract | Type Literal, catalog, warning detail shape, reason registry | Additive type and keys; warning code reused with its detail keys unchanged; one new plan code | AT-1, AT-8, registry sentries | rename a detail key |

Recurring-defect catalog check: "second registry" (no new list; GEN-REL edges and GEN-STRICT
options are extended), "second mechanism" (AT-12 lookup sentry; the warning goes through
`emit_generation_warning`), "value in error text" (AT-8), "same-named sibling" (AT-6), "route
forgot the new branch" (AT-7).

## Reuse inventory

| Need | Reused | Source |
|---|---|---|
| parent row by FK value | GEN-REL E3a `_parent_lookup` (with `anchor_via`) | `docs/plans/gen-rel.md` E3a, on `plan/gen-rel` |
| parent-key uniqueness, self-reference rule | GEN-REL E3a refusals | same |
| cross-table input edges, generation order, reproducibility | GEN-REL central input-edge registry | `config/_generate_relational.py` (planned); `generation/_reproducibility.py:58` today |
| pinned refusal | `plan.pinned_relational_option_unsupported` and its runtime twin | GEN-REL E2/E3a |
| parent pool | `pools[...]` and `parent_keys` | `generation/synthesize.py:208`; `generation/_synthesize_derived.py:95` |
| per-row table selection | `_select_table` | `generation/statistical/_sample.py` on #461 |
| fallback counting, cap | `conditional_fallback_counts`, `DISTINCT_PARENT_CAP`, `FallbackCounts` | same |
| warning | `warn_on_unseen_parents`, `emit_generation_warning`, `warning.generation_condition_parent_unseen` | `generation/_statistical_column.py`, `generation/_emit.py` on #461 |
| option checks, weights rule, catalog | `GENERATE_OPTIONS`, `_weights`, `generate_option_catalog` | `config/_generate_options.py:230`, `:195`, `:415` |
| categorical draw | `random.Random(base_int("py")).choices` | `generation/synthesize.py:389` |
| established method | conditional categorical = a conditional probability table (CPT) of a Bayesian network with one parent, sampled ancestrally in topological order; `inherit` = a lookup join (SQL `LEFT JOIN` on a unique key) | Koller and Friedman, Probabilistic Graphical Models, ch. 3 and 12.1; SDV HMA1 parent-to-child propagation |

## Enumerations

### Exit paths

- Config refusals through `option_problems` from `_options_valid` (`config/_tables.py:305`):
  `inherit` keys, `by` / `condition_on` pairing, nested entries, weights.
- `generate_unique_conflict` from `column_conflict_problems` (`config/_generate_unique.py:88`).
- GEN-REL's relational refusals (parent key not unique, self-reference, read-before-fill) for
  `inherit`, through the edge registry.
- Compile refusal `plan.generate_inherit_column_missing` (new); pinned compile
  `plan.pinned_relational_option_unsupported` (`generation/_pinned_compile.py:52` `REJECTED_TYPES`).
- Streamed admission declines (fall back to resident).
- Warning `warning.generation_condition_parent_unseen` (reused); no run failure is added.

### Routes

- Resident: `_generate_column` dispatch (`generation/synthesize.py:313` categorical, new `inherit`
  branch beside `reference` at line 321).
- Pinned: `generation/_pinned_execute.py:206` (conditional categorical runs; `inherit` refused).
- Streamed: `generation/_admission.py:247` classifier declines both.

### FK shapes

- Single-column FK to another table, unique parent key: `inherit` supported.
- Self-referencing FK: refused (GEN-REL rule).
- Fitted fan-out FK (`distribution: fitted`): supported; the lookup reads values only.
- Composite FK: N/A because generation draws one-column FKs only (`reference_column` is one column).

### Fields

- `inherit`: `via`, `column`; common `null_probability`, `determinism` (ignored, no draw).
- `categorical`: `condition_on`, `by` (entries: `categories`, `weights`).
- New plan code `generate_inherit_column_missing`; reused warning code and detail keys.

### Output surfaces

- Generated tables (CSV, Parquet, fixed width, `decoy.mask()`): inherited values with the parent's
  type; conditional categorical values.
- `ExecutionResult.warnings` and `on_warning`: the reused fallback warning with
  `provider: "categorical"`.
- Generation log: the reused value-free summary line.
- `generate_option_catalog()` and the JSON schema: the new type and keys.
- Reproducibility report and `docs/capability-matrix.md` (regenerated).

## Frozen contracts

| Contract | Change | Kind |
|---|---|---|
| `GenerateColumnConfig.type` Literal (`config/_tables.py:277`) | + `inherit` | additive |
| Option registry and catalog | + `inherit` entry; + `condition_on`, `by` on `categorical` | additive |
| `warning.generation_condition_parent_unseen` detail | unchanged keys; new `provider` value `categorical` | additive value |
| Reason registry | + `plan.generate_inherit_column_missing`; next minor at merge | additive |
| Draw-site catalogue | `inherit` maps to `DETERMINISTIC_NO_DRAW`; no new site | additive |
| Plain `categorical` output | unchanged bytes (AT-9) | none |

## Placement

Checked against `CODEMAP.md` "Where Things Go" rows: Config models, Plan compile and checks,
Generation (with GEN-REL's `generation/relational/` sub-package), Native companion bridge, Reason
codes, Sentry test, Unit tests, Plans, Changelog fragments.

| Change | File | CODEMAP row | Existing neighbor |
|---|---|---|---|
| type Literal | `config/_tables.py` | Config models | `GENERATE_TYPES` |
| `inherit` options; `condition_on`, `by` and the nested-entry check on categorical | `config/_generate_options.py` (or GEN-TYPES' `config/_generate_type_checks.py` if merged) | Config models | `categorical` entry at `:249` |
| `inherit` edges | `config/_generate_relational.py` (GEN-REL) | Config models | `anchor_via` edges |
| `inherit` generator | new `generation/relational/_inherit.py` | Generation (relational sub-package) | `_parent_lookup.py` |
| conditional categorical draw | `generation/synthesize.py` `_categorical` (+ about 15 lines) | Generation | the plain draw |
| selection and counts taking plain tables | `generation/statistical/_sample.py` (#461 helpers, generalized) | Generation | `_conditional_lookup` |
| warning for categorical | `generation/_statistical_column.py` `warn_on_unseen_parents` (accept categorical) | Generation | the statistical branch |
| registries | `_admission.py`, `_pinned_compile.py`, `_numeric_typing.py`, `_bounds.py`, `execution/native/_determinism_protocol.py`, `execution/native/_capabilities.py` | Generation; Native companion bridge | one entry each |
| compile check | `plan/_checks_windowed_date.py` neighbor in the same module family; no new plan module | Plan compile and checks | GEN-REL's branch-aware `anchor` check |
| reason code | `reasons/plan/_part3.py` | Reason codes | GEN-REL's plan codes |
| lookup sentry | `tests/sentry/defects/test_parent_lookup_single_site.py` | Sentry test | GEN-REL's pool choke-point sentry |
| tests | `tests/unit/generation/relational/test_inherit.py`, `tests/unit/generation/test_conditional_categorical.py`, `tests/unit/config/test_generate_options.py` | Unit tests | GEN-REL tests, `test_condition_fallback.py` |

Module counts: generation/ gains no direct module (`_inherit.py` is in GEN-REL's sub-package);
config/ and plan/ gain none. `synthesize.py` is 559 lines on main and about 573 with #461; E2 adds
about 15 lines of dispatch. If that crosses 600, the conditional draw moves into a sub-package
module (`generation/types/_categorical.py` if GEN-TYPES has created `generation/types/`) rather than
raising a census entry.

## Slices

| Slice | Branch | Size | Content | Gate |
|---|---|---|---|---|
| E2 | `feat/gen-inherit-e2` | S/M (about 200 lines plus tests) | conditional categorical: registry keys and nested checks, #461 helper generalization (own commit), draw, warning, admission decline, typing; AT-2 (E2 part), AT-7 (categorical), AT-8 to AT-11 | T1 impact set on the Mac; T2 on the exact head; reason or warning detail change is a frozen-contract trigger only if keys change (they do not), so no full suite unless the census moves |
| E1 | `feat/gen-inherit-e1` | M (about 250 lines plus tests) | `inherit`: type, registry, edge registry entry, `_inherit.py` over `_parent_lookup`, compile check, registries, capability matrix regen, lookup sentry; AT-1 to AT-7, AT-12, AT-13 | full suite: new reason code (registry manifest) and a new sentry |

Order: E2 first, as soon as #461 merges (it does not wait for GEN-REL). E1 after GEN-REL E3a
merges. Each slice takes the next registry minor at merge and adds `changelog.d/gen-inherit-e<N>.md`
(`### Added`).

## Engineering calls made

- `inherit` reuses the child's existing `reference` column as the only link; no implicit FK
  discovery and no `parent.column` dotted syntax, matching GEN-REL Q3's explicit-keys answer.
- The parent row is found only through GEN-REL's `_parent_lookup`; a sentry keeps it the one site.
- Ask GEN-REL (via claudeorchestrator) to name E3a's refusal codes for the lookup, not for
  `anchor_via` (for example `config.generate_parent_lookup_key_not_unique`,
  `config.generate_parent_lookup_self_reference`), so `inherit` reuses them. If E3a has merged with
  `anchor_via` names, E1 reuses them as they are rather than adding parallel codes.
- Conditional categorical keys by `str(parent)` through `_select_table`, the fitted case's rule,
  so YAML and fitted conditioning behave the same.
- Unseen parent values fall back to the top-level mix with the shared warning, the same as the
  fitted case (Owner question 2 offers the strict alternative).
- `by` keys must be strings; YAML number and bool keys are refused with a hint to quote them, since
  a JSON plan would otherwise change them.
- `inherit` copies the parent's emitted Arrow type; `output_type` (GEN-TYPES E2) is not accepted
  on `inherit` for the same reason it is not accepted on `reference`.

## Owner questions

Recommended answer first.

1. **Type name.** Recommend `inherit` (the roadmap row's name; reads as "inherit `state` via
   `member_id`"). Alternatives: `from_parent` (but the mask side already uses `from_parent` for
   "this FK takes the masked parent key", a different meaning) or `parent_attribute`.
2. **Unseen parent value in a YAML conditional categorical.** Recommend falling back to the
   top-level mix with the shared `generation_condition_parent_unseen` warning, matching fitted
   `condition_on`. Alternative: refuse at run when any row's parent value has no entry, which is
   stricter but makes every new parent value a hard failure.
3. **Key name for the per-value mixes.** Recommend `by:` beside `condition_on`. Alternative:
   `categories_by:` (longer, self-describing) or a list of `{when: value, categories, weights}`
   rows (allows non-string keys but is wordier).
4. **Order.** Recommend shipping E2 before E1 because it waits only on #461, not on GEN-REL.
   Alternative: hold both for one release note after GEN-REL.
