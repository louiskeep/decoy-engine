# GEN-INHERIT: child columns that follow their parent row, and conditional categoricals in YAML

Status: plan

Rules consulted: 00-universal, risk-and-exceptions, development-loop, code-review, testing,
architecture, feature-dev, cross-cutting-concerns, api-and-compatibility, exploration, reuse-first,
security, observability-and-resilience, documentation, verification, profiles/local-operations,
catalogs/recurring-defects

Base: engine main `1155b20c` (includes GEN-COND-CAP E1b, #461). Roadmap row 24r. Engine only; CLI help picks the new type and keys up
from `generate_option_catalog()` (GEN-CLI, row 24o).

Risk: R2. The work adds config and reason contracts and changes cross-table generated data.
Required evidence is the reviewed plan, slice T0 and T1, independent T2 with the named mutations,
and the one trigger-bound full-suite run per slice. The owner is the approver; Dennis and Codex are
the independent plan gates.

**Prerequisites.**
- E1 (`inherit`) needs GEN-REL E3a (row 24n, plan `docs/plans/gen-rel.md` on `plan/gen-rel`):
  the parent-row lookup `generation/relational/_parent_lookup.py`, the central generate-input edge
  registry `config/_generate_relational.py`, the parent-key uniqueness rule and
  `plan.pinned_relational_option_unsupported`. None of it is on main yet; E1 starts after E3a merges.
- E2 (conditional `categorical`) uses GEN-COND-CAP E1b (row 24w, #461, merged in `1155b20c`):
  `_select_table` (`generation/statistical/_sample.py:293`), `DISTINCT_PARENT_CAP` (`:309`),
  `FallbackCounts` (`:312`), `conditional_fallback_counts` (`:323`), `warn_on_unseen_parents`
  (`generation/_statistical_column.py:117`), `emit_generation_warning`
  (`generation/pool/_events.py:52`) and `warning.generation_condition_parent_unseen`.
- E2 waits for GEN-STRICT E2 (row 24k, plan `docs/plans/2026-11-09-gen-strict.md` on
  `plan/gen-strict`, step 4; in build on `feat/gen-strict-e2`). On main nothing consumes
  `OptionSpec.ref_kind` (`config/_generate_options.py:59-63`) and
  `plan.generate_column_forward_reference` is defined (`reasons/plan/_part3.py:89`) but never
  raised; the only forward check is statistical's own (`plan/_checks.py:390`). GEN-STRICT E2 adds
  the one registry-driven forward check for every key with a `ref_kind`, and the one mixed-category
  check (`plan.generate_categories_mixed`, its step 2b). E2 registers its keys there and adds no
  check of either kind. At E2 build start, the builder confirms from the merged source and
  GEN-STRICT's seeded guard that the forward-reference step iterates the option registry for every
  `OptionSpec.ref_kind`; a hard-coded key list does not satisfy this entry condition, and E2 does
  not start until it is corrected in GEN-STRICT. E2 does not need GEN-REL.
- Line numbers are from `1155b20c` and will move.

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
  `generation/synthesize.py:220`), so the child column has the parent column's Arrow type, nulls and
  any declared output type. The child column is built with that type explicitly; Arrow inference is
  not used (an all-null child, or a decimal parent, would infer a different type). Today
  `arrow_table` (`generation/_statistical_column.py:92-114`) types only fitted statistical columns
  and infers every other column; E1 gives it the `inherit` columns' parent types (a mapping of
  column name to `pa.DataType` taken from `pools[parent].schema`) so it builds those with
  `pa.array(values, type=...)`. Only the resident call site passes it; pinned refuses `inherit`.
- Works with every reference distribution, including fitted fan-out (FK-FANOUT E2b, engine #452):
  the lookup reads the FK value, not how it was drawn.
- Chains work across tables: `claim_lines.member_state` can `inherit` `claims.member_state`, because
  the parent table is complete before the child starts (`generation_order`,
  `generation/synthesize.py:225`).
- Refusals:
  - `via` missing, not a `reference` column, or later in the table: GEN-STRICT's existing
    `generate_option_*` codes and GEN-STRICT E2's forward-reference refusal (`via` registers
    `ref_kind="column"`). `inherit.column` registers no `ref_kind`: it names a column of the parent
    table, and a same-table forward check would test it against the child table (AT-6).
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
  `Reader(True, "matched by value against the parent key, whose type it copies")`. The entry is
  required: `reader_ok` treats a type missing from `READER_KEYS` as "may read anything"
  (`generation/_numeric_typing.py:269-271`) and would drop every numeric candidate in the child
  table. `column` is not listed, because it names a parent-table column and `_names_read` would
  match a same-named sibling.
- Readers of the inherited copy are checked with the parent. `numeric_plan` groups each column
  under `_reference_root` (`generation/_numeric_typing.py:169-181`, grouped at `:225-227`) and keeps
  a root numeric only while `reader_ok` passes for every member (`:316-322`). Today
  `_reference_root` follows only `type: reference`, so an `inherit` column would be its own root,
  never a member of the parent's group, and a child `formula.references`, `group_by` or
  `windowed_date.anchor` reading it would never be checked: a parent `sequence` planned `int64`
  would reach a child formula that expects text. E1 makes `_reference_root` also follow `inherit`:
  it first looks up `via` in the same table. If `via` is absent or that column is not
  `type: reference`, the function returns the incoming key unchanged, just as it already does when
  the current column is absent. Otherwise it hops to `(via.reference_table, inherit.column)`.
  Chains of `inherit` and `reference` hops resolve to one root and the loop's cycle stop still
  holds. The `inherit` column then takes the parent's planned type through `planned()` (`:246-258`)
  and its readers vote on the parent's type (AT-14).

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
  (`generation/statistical/_sample.py:293`, the one place the parent is keyed): the key is `str(parent)`,
  so `1` matches `"1"`, `true` parents match `"True"`, and a null parent matches `"None"`. A row whose
  key has no entry draws from the top-level `categories` / `weights`.
- The draw uses the column's one `random.Random(base_int("py"))` and one `choices` call per row
  (`_categorical`, `generation/synthesize.py:401-424`). `choices` consumes one `random()` per
  element with or without weights, so the conditional column makes the same number of draws as a
  plain column of the same length. It does not give the plain column's bytes: the column seed hashes
  the column config (`strategy_config_fingerprint`, `generators/derivation.py:142-180`), and
  `condition_on` and `by` are not in `_EXCLUDED_FROM_FINGERPRINT` (`generators/derivation.py:52`).
  `_EXCLUDED_FROM_FINGERPRINT` must not change: adding either key would move the seed of every
  existing fitted statistical `condition_on` column. A categorical without `condition_on` is
  unchanged byte for byte (AT-9).
- Per-row draws at 100M rows: each entry's `cum_weights` (`itertools.accumulate`, what `choices`
  computes from `weights` on every call) is built once per column, and the row draw passes
  `cum_weights=`. That gives the same bytes as passing `weights` and avoids rebuilding the
  cumulative list per row. An entry with no weights uses the unweighted call, as the plain column
  does.
- When any row fell back on the resident route, the run raises
  `warning.generation_condition_parent_unseen` once for the column through `warn_on_unseen_parents`
  (called at `generation/synthesize.py:176`) and `emit_generation_warning`, with the same detail
  keys as the statistical case and `provider: "categorical"`. The distinct-key count stops at
  `DISTINCT_PARENT_CAP`. No cell value or parent value is in the record or the log line.
- Pinned route: fallback rows draw from the top-level mix exactly as on resident, and no warning is
  raised. `execute_pinned` takes no `on_warning` (`generation/_pinned_execute.py:174-230`) and does
  not call `warn_on_unseen_parents`, which is the state fitted statistical `condition_on` is in
  today. E2 keeps the one emitter and adds no pinned warning path; a pinned warning for both
  providers is V2-LOG (see Review round 1 resolution).
- Checks (config time, the registry, value-free):
  - `by` without `condition_on`, or `condition_on` without `by`: `missing` on the absent key
    (the conditional-required table GEN-TYPES adds, or the inline precedent in `option_problems` if
    GEN-TYPES has not merged).
  - `by` not a mapping with string keys: `generate_option_type` (YAML `1:` must be quoted `"1":`;
    a JSON plan would turn an int key into text and break the match after a round trip). The
    message for a bool key says to write the quoted key `"True"` (or `"False"`), not `true`: YAML
    reads `true` as a bool and quoted `"true"` would never match, because parents are keyed by
    `str(parent)`, which gives `"True"`. The message names the key position only, never the key.
  - An entry with keys other than `categories` / `weights`, or with neither:
    `generate_option_unknown` / `generate_option_type` at `("by", <key>, ...)`.
  - Entry weights: `_weights`'s rule, and length equal to the entry's categories (or the top-level
    categories when the entry has none): `generate_weights_invalid`.
  - `unique: true` with `condition_on`: `generate_unique_conflict` from `column_conflict_problems`
    (`config/_generate_unique.py:88`); a unique draw has no per-row mix.
- Mixed category types: GEN-STRICT E2's one check (`plan.generate_categories_mixed`, which calls
  `pa.array` on the categories) runs over the top-level list plus every entry's list together, the
  values the run's `pa.table` inference will see. E2 extends that check's input; it adds no second
  check. `_categorical_type` (`generation/_admission.py:95`) is a routing and typing predicate, not a
  validity rule; streamed admission declines a conditional column before calling it, and
  `numeric_plan`'s `planned()` (`generation/_numeric_typing.py:257`) calls it only when the column is
  a typing root. E2 has it read the same union so `planned()` matches the drawn values.
- Routes: resident and pinned both pass `data` to `_generate_column`
  (`generation/synthesize.py:292`; pinned `generation/_pinned_execute.py:206`), so both draw the
  same rows; only resident warns (above). The streamed route declines a categorical with
  `condition_on` (`(False, "reads a sibling; no sibling-input seam yet")`, the `derived` reason),
  from the categorical branch of the admission classifier (`generation/_admission.py:247`), before
  `_categorical_type` runs.
- Run-time missing parent: before any selection or fallback, conditional `_categorical` checks
  membership of `condition_on` in `generated`. If the key is absent, both resident and pinned
  raise `GenerationError` with `runtime.categorical_condition_column_unavailable` and a fixed
  value-free message that may name only the table, column and config key. It never substitutes an
  empty list and never silently sends every row to the top-level mix. This is one new code in the
  central reason registry. The existing `plan.statistical_condition_column_unavailable` code is
  not reused because its frozen symbol and summary explicitly describe a statistical column.
- Forward reference: `categorical.condition_on` registers `ref_kind="column"`, so GEN-STRICT E2's
  registry walk refuses a later or missing parent with `plan.generate_column_forward_reference`.
  E2 adds the FM8 run-time red test for a direct call whose generated-data mapping lacks that key;
  the exact outcome is the value-free runtime refusal above, not all-fallback output.
- `condition_on` is already in `_SIBLING_KEYS` (`generation/_reproducibility.py:58`).
  `READER_KEYS["categorical"]` is empty today (`generation/_numeric_typing.py:145`); E2 adds
  `("condition_on", "categorical.condition_on")` and registers `Reader(False, "matched against
  declared keys as text")`, the same verdict as `statistical.condition_on` (`:132`). A parent read by a conditional categorical is then typed
  exactly as one read by a fitted `condition_on` (AT-11).

## Pre-mortem (failure modes)

| # | Failure | Where it would bite | Guard |
|---|---|---|---|
| P1 | `inherit` reads a same-named sibling instead of the parent column | silent wrong values when both tables have `state` | edges come only from GEN-REL's registry; AT-6 plants a same-named sibling |
| P2 | Parent key not unique, child picks an arbitrary row | inherited values drift with row order | GEN-REL's uniqueness rule, shared; AT-3 |
| P3 | Child Arrow type differs from the parent | Parquet schema mismatch, `1.0` in CSV | explicit parent type; AT-4 covers int with nulls, decimal, date, all-null |
| P4 | A second parent lookup grows beside GEN-REL's | two behaviors for one FK | E1 calls `_parent_lookup` only; sentry AT-12 in GEN-REL's choke-point sentry |
| P5 | Conditional categorical keyed differently from fitted `condition_on` | `1` matches in one and not the other | both go through `_select_table`; AT-10 |
| P6 | Fallback rows go unreported, or are reported with a value | silent skew, or data in logs | `warn_on_unseen_parents` reused; AT-8 sentinel check |
| P7 | Conditional draw consumes a different number of random values, or a builder adds `condition_on` / `by` to `_EXCLUDED_FROM_FINGERPRINT` to force equal bytes | prefix drift; every fitted statistical `condition_on` column's seed moves | one `choices` per row (same number of draws, not the plain column's bytes); the exclusion list does not change; AT-9 with its fingerprint mutant |
| P8 | JSON round trip changes `by` keys in pinned mode | pinned output differs from resident | string keys only; AT-7 pinned parity |
| P9 | The registry change for `by` entries opens a nested-key hole | unknown nested key silently ignored | nested keys checked in the registry; AT-2 |
| P10 | GEN-REL E3a lands with `anchor_via`-specific code names | `inherit` refusals say "anchor_via" | ask GEN-REL to name them for the lookup (Coordination); AT-3 checks the code text |
| P11 | Readers of an `inherit` column are left out of numeric typing | a parent `sequence` planned `int64` reaches a child formula that expects text: wrong output or a raise | `_reference_root` follows `inherit` to the parent root; AT-14 |
| P12 | Pinned fallback rows expected to warn | a pinned user sees no fallback warning | stated as the pinned behavior (same as fitted statistical today), V2-LOG; AT-8 is resident-only for the warning |
| P13 | Missing `condition_on` data becomes an empty parent list | every row silently draws from the fallback mix after a compile bypass | membership check and value-free runtime refusal before `_select_table`; AT-15 |

## Acceptance tests

| AT | What | Red on base / mutant |
|---|---|---|
| AT-1 | `inherit` appears in `GENERATE_TYPES`, the catalog, `ADMISSION_MATRIX`, `REJECTED_TYPES`, `READER_KEYS`, `GEN_KIND_TO_SITE`, `_GEN_ONLY`, the capability matrix | red on base |
| AT-2 | Registry: `inherit` needs `via` and `column`; `by` / `condition_on` pairing; nested entry keys, weights length and types per the E2 list, each with its code and location | red on base; mutant: skip nested key check |
| AT-3 | Non-unique parent key and self-referencing `via` refuse with GEN-REL's codes; missing parent column refuses with `plan.generate_inherit_column_missing` | red on base |
| AT-4 | For 1,000 claims over 50 members, every `member_state` equals its member's `state`; types kept for int with nulls, decimal, date and an all-null parent column; null FK gives null | red on base; mutant: draw a random parent row |
| AT-5 | Chain: `claim_lines` inherits `claims.member_state`, which inherits `members.state`; all three agree on every row | red on base |
| AT-6 | A child table that also has its own `state` column: `inherit.column: state` reads the parent, and the reproducibility report lists the parent edge, not the sibling. `inherit.column` carries no `ref_kind`: a child whose `inherit.column` names a parent column declared nowhere in the child passes the forward-reference check, and `inherit.via` naming a later column is refused by it | mutants: resolve `column` in the child; tag `inherit.column` with `ref_kind="column"` |
| AT-7 | Route parity: resident and pinned give identical tables for a conditional categorical (including fallback rows); streamed declines it; `inherit` is refused in pinned with `plan.pinned_relational_option_unsupported`. Warnings are not compared across routes | red on base |
| AT-8 | Resident route, unseen parent values: rows fall back to the top-level mix, one `warning.generation_condition_parent_unseen` with `provider: categorical`, counts right, a sentinel parent value absent from record, log line and exception chain. A bool `by` key refuses with a message that says to write `"True"` | red on base; mutant: drop the call |
| AT-9 | Conditional categorical whose every entry equals the top-level mix: for 10 seeds its output equals `random.Random(s).choices(cats, weights, k=n)`, where `s` is the conditional column's own `base_int("py")` (from its own config, `condition_on` and `by` included). Plain categorical and fitted statistical `condition_on` goldens unchanged | mutants: draw `k=n` once per parent value; add `condition_on` and `by` to `_EXCLUDED_FROM_FINGERPRINT` (statistical golden red) |
| AT-10 | Int, bool and null parents match `"1"`, `"True"` and `"None"` the same way a fitted `condition_on` does | mutant: key by `repr` |
| AT-11 | A sequence parent read by a conditional categorical gets the same planned type as under a fitted `condition_on`, and rows still match `"1"` keys. Baseline cells also pin `planned()` for plain categorical columns with numeric and text categories, proving E2 leaves their planned types unchanged | red on base; mutants: register the reader as safe; route every categorical through the conditional union |
| AT-12 | Sentry, added to GEN-REL's `tests/sentry/defects/test_reference_pool_choke_point.py` rather than a new module: only `generation/relational/_parent_lookup.py` maps FK values to parent rows (AST scan: no other module under `generation/` builds a key-to-row dict or index over a `pools[...]` table). Scoped to dict or index construction keyed by a pool column, so GEN-REL E3b's per-parent `groupby` and `reindex` in its aggregate post-pass do not trip it | mutant: inline a dict lookup in the `inherit` generator; E3b's aggregate module stays green |
| AT-13 | E1 plus E2: `tier` conditioned on an inherited `region` follows each member's region's mix within 4 standard errors at 50k rows | red on base |
| AT-14 | Numeric typing through `inherit`: parent `members.member_id` is a `sequence` (a numeric candidate); `claims.member_code` inherits it; a child `formula` lists `member_code` in `references` and treats it as text. `numeric_plan` keeps `members.member_id` text, so both tables emit text, and the formula output matches the text oracle. Without the child formula, `member_id` stays `int64` and `member_code` copies `int64`. Direct malformed-config cells prove `_reference_root` returns the incoming key unchanged when `via` is absent or names a non-`reference` column | red on base; mutants: `_reference_root` stops at `inherit`; dereference a missing or non-reference `via` |
| AT-15 | FM8 run-time guard: call resident and pinned conditional categorical generation with `condition_on` absent from generated data. Both refuse with `runtime.categorical_condition_column_unavailable`; no rows are returned, `str(exc)` contains no sentinel data value, and `__cause__` and `__context__` are `None` | red on base; mutant: use `generated.get(condition_on, [])` and fall back every row |

## Class table

| # | Class | Where it can bite here | Closing structure | Sentry or property test | Mutation |
|---|---|---|---|---|---|
| 1 | Wrong value | Inherited value from the wrong parent row; wrong mix for a parent value | One parent lookup; one keyed selector | AT-4, AT-5, AT-13 | random parent row; key by `repr` |
| 2 | Cleanup | none: no files, handles or temp state | N/A, both features are pure over in-memory lists and pools | AT-4 shows no new artifact | none needed |
| 3 | State | Conditional tables rebuilt per row, or a shared RNG across columns | Tables and `cum_weights` built once per column; one `random.Random` per column | AT-9 draw identity | reseed per parent value |
| 4 | Route divergence | Pinned and resident differ; streamed half-supports a feature; typing differs from the parent; one route accepts missing condition data | Both routes call `_generate_column` with `data` and share the missing-key refusal; explicit streamed declines; explicit pinned refusal for `inherit`; pinned has no warning, stated; `_reference_root` follows `inherit` | AT-7 parity, AT-14, AT-15 | drop `data` on the pinned call; stop `_reference_root` at `inherit`; default a missing parent to `[]` |
| 5 | Referential integrity | Child value not the parent row's value; non-unique parent key | GEN-REL uniqueness rule and lookup | AT-3, AT-4 | allow a non-unique key |
| 6 | Untrusted input | Nested `by` entries from YAML; parent values in warnings | Registry checks nested keys and weights; warning carries counts only | AT-2, AT-8 | quote the parent value in the log line |
| 7 | TOCTOU / check-then-use | Parent column checked at compile, read at run from a different table | The edge registry resolves the parent table once; the run reads `pools` by that name | AT-6 | resolve the table again at run from `column` |
| 8 | Weak test | Agreement asserted on a few rows | AT-4 checks every row; AT-13 at 50k rows within 4 standard errors | the mutation column | each listed mutation |
| 9 | Refactor drift | Generalizing #461's helpers from `StatisticalSpec` to plain tables changes statistical output | The generalization lands as its own commit with #461's tests and statistical goldens unchanged | `test_condition_fallback.py`, AT-10 | change `_select_table`'s keying |
| 10 | Frozen contract | Type Literal, catalog, warning detail shape, reason registry | Additive type and keys; warning code reused with its detail keys unchanged; one new plan code and one new runtime code | AT-1, AT-8, AT-15, registry sentries | rename a detail key; omit the runtime code from the manifest |

Recurring-defect catalog check: "second registry" (no new list; GEN-REL edges and GEN-STRICT
options are extended), "second mechanism" (AT-12 lookup sentry; the warning goes through
`emit_generation_warning`), "value in error text" and "no exception context" (AT-8, AT-15),
"same-named sibling" (AT-6), "route forgot the new branch" (AT-7).

## Established solutions and reuse inventory

| Need | Reused | Source |
|---|---|---|
| parent row by FK value | GEN-REL E3a `_parent_lookup` (with `anchor_via`) | `docs/plans/gen-rel.md` E3a, on `plan/gen-rel` |
| parent-key uniqueness, self-reference rule | GEN-REL E3a refusals | same |
| cross-table input edges, generation order, reproducibility | GEN-REL central input-edge registry | `config/_generate_relational.py` (planned); `generation/_reproducibility.py:58` today |
| pinned refusal | `plan.pinned_relational_option_unsupported` and its runtime twin | GEN-REL E2/E3a |
| parent pool | `pools[...]` and `parent_keys` | `generation/synthesize.py:220`; `generation/_synthesize_derived.py:95` |
| per-row table selection | `_select_table` | `generation/statistical/_sample.py` on #461 |
| fallback counting, cap | `conditional_fallback_counts`, `DISTINCT_PARENT_CAP`, `FallbackCounts` | same |
| warning | `warn_on_unseen_parents`, `emit_generation_warning`, `warning.generation_condition_parent_unseen` | `generation/_statistical_column.py:117`, `generation/pool/_events.py:52` |
| forward-reference and mixed-category checks | GEN-STRICT E2's registry-driven `ref_kind` check and `plan.generate_categories_mixed` | `docs/plans/2026-11-09-gen-strict.md` steps 2b and 4, on `plan/gen-strict` |
| run-time coded generation refusal | `GenerationError` and the runtime reason registry | `generation/pool/_errors.py`; `reasons/runtime/_part2.py` |
| numeric typing groups | `_reference_root`, `numeric_plan` | `generation/_numeric_typing.py:169`, `:184` |
| option checks, weights rule, catalog | `GENERATE_OPTIONS`, `_weights`, `generate_option_catalog` | `config/_generate_options.py:230`, `:195`, `:415` |
| categorical draw | `random.Random(base_int("py")).choices` | `generation/synthesize.py:424` |
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
- Resident and pinned run-time refusal `runtime.categorical_condition_column_unavailable` when a
  compile bypass leaves `condition_on` absent from generated data.
- Streamed admission declines (fall back to resident).
- Warning `warning.generation_condition_parent_unseen` (reused) for present but undeclared parent
  values; this fallback is distinct from the missing-column run failure.

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
- New plan code `generate_inherit_column_missing`; new runtime code
  `categorical_condition_column_unavailable`; reused warning code and detail keys.

### Output surfaces

- Generated tables (CSV, Parquet, fixed width, `decoy.mask()`): inherited values with the parent's
  type; conditional categorical values.
- `ExecutionResult.warnings` and `on_warning`: the reused fallback warning with
  `provider: "categorical"`.
- Resident and pinned failures: value-free `GenerationError` with
  `runtime.categorical_condition_column_unavailable` when generated data lacks the configured
  conditioning column.
- Generation log: the reused value-free summary line.
- `generate_option_catalog()` and the JSON schema: the new type and keys.
- Reproducibility report and `docs/capability-matrix.md` (regenerated).

## Frozen contracts

| Contract | Change | Kind |
|---|---|---|
| `GenerateColumnConfig.type` Literal (`config/_tables.py:277`) | + `inherit` | additive |
| Option registry and catalog | + `inherit` entry; + `condition_on`, `by` on `categorical` | additive |
| `warning.generation_condition_parent_unseen` detail | unchanged keys; new `provider` value `categorical` | additive value |
| Reason registry | + `plan.generate_inherit_column_missing`; + `runtime.categorical_condition_column_unavailable`; next minor for each slice at merge | additive |
| Draw-site catalogue | `inherit` maps to `DETERMINISTIC_NO_DRAW`; no new site | additive |
| Plain `categorical` output | unchanged bytes (AT-9) | none |
| Plain `categorical` planned type | unchanged for numeric and text categories (AT-11) | none |

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
| explicit Arrow type for `inherit` columns | `generation/_statistical_column.py` `arrow_table` (+ a parent-type mapping argument) | Generation | the fitted statistical typing branch |
| `_reference_root` follows `inherit`; `READER_KEYS` entries | `generation/_numeric_typing.py` | Generation | the `reference` hop |
| warning for categorical | `generation/_statistical_column.py` `warn_on_unseen_parents` (accept categorical) | Generation | the statistical branch |
| registries | `_admission.py`, `_pinned_compile.py`, `_bounds.py`, `execution/native/_determinism_protocol.py`, `execution/native/_capabilities.py` | Generation; Native companion bridge | one entry each |
| compile check | `plan/_checks_windowed_date.py` neighbor in the same module family; no new plan module | Plan compile and checks | GEN-REL's branch-aware `anchor` check |
| plan reason code | `reasons/plan/_part3.py` | Reason codes | GEN-REL's plan codes |
| runtime reason code | `reasons/runtime/_part2.py` | Reason codes | existing `GenerationError` codes |
| lookup sentry | GEN-REL's `tests/sentry/defects/test_reference_pool_choke_point.py` (one more case; no new module) | Sentry test | GEN-REL's pool choke-point cases |
| tests | `tests/unit/generation/relational/test_inherit.py`, `tests/unit/generation/test_conditional_categorical.py`, `tests/unit/config/test_generate_options.py` | Unit tests | GEN-REL tests, `test_condition_fallback.py` |

Module counts: generation/ gains no direct module (`_inherit.py` is in GEN-REL's sub-package);
config/ and plan/ gain none. `synthesize.py` is 571 lines on `1155b20c`; E2 adds about 15 lines of
dispatch. If that crosses 600, the conditional draw moves into a sub-package
module (`generation/types/_categorical.py` if GEN-TYPES has created `generation/types/`) rather than
raising a census entry.

## Slices

| Slice | Branch | Size | Content | Gate |
|---|---|---|---|---|
| E2 | `feat/gen-inherit-e2` | S/M (about 200 lines plus tests) | conditional categorical: registry keys and nested checks, #461 helper generalization (own commit), draw with precomputed `cum_weights`, missing-parent runtime refusal, resident warning, admission decline, typing, `ref_kind` registration and the union input to GEN-STRICT's mixed-category check; AT-2 (E2 part), AT-7 (categorical), AT-8 to AT-11, AT-15 | full suite: new runtime reason code changes the reason manifest; T1 impact set and T2 run on the exact head |
| E1 | `feat/gen-inherit-e1` | M (about 250 lines plus tests) | `inherit`: type, registry, edge registry entry, `_inherit.py` over `_parent_lookup`, explicit `arrow_table` type, `_reference_root` hop, compile check, registries, capability matrix regen, lookup sentry case; AT-1 to AT-7, AT-12 to AT-14 | full suite: new reason code (registry manifest) and a sentry change |

Order: E2 first, after GEN-STRICT E2 merges (#461 is already on main; E2 does not wait for
GEN-REL), subject to the registry-walk entry condition in Prerequisites. E1 after GEN-REL E3a
merges, and after E3b if AT-12 is to be checked against E3b's
aggregate module (otherwise the AT-12 scope note is checked when E3b lands). Each slice takes the next registry minor at merge and adds `changelog.d/gen-inherit-e<N>.md`
(`### Added`).

## Engineering calls made

- `inherit` reuses the child's existing `reference` column as the only link; no implicit FK
  discovery and no `parent.column` dotted syntax, matching GEN-REL Q3's explicit-keys answer.
- The parent row is found only through GEN-REL's `_parent_lookup`; a sentry keeps it the one site.
- GEN-REL refusal codes and the uniqueness rule: see Coordination with GEN-REL below.
- Conditional categorical keys by `str(parent)` through `_select_table`, the fitted case's rule,
  so YAML and fitted conditioning behave the same.
- A missing generated `condition_on` key is not an unseen parent value. It refuses before fallback
  with the central runtime reason code and a value-free `GenerationError`.
- Unseen parent values fall back to the top-level mix with the shared warning on the resident
  route, the same as the fitted case (Owner question 2, decided).
- `by` keys must be strings; YAML number and bool keys are refused with a hint to quote them
  (`"1"`, `"True"`), since a JSON plan would otherwise change them.
- `inherit` copies the parent's emitted Arrow type; `output_type` (GEN-TYPES E2) is not accepted
  on `inherit` for the same reason it is not accepted on `reference`.

## Coordination with GEN-REL

Requests to the GEN-REL plan author (via claudeorchestrator), recorded here so E1 does not grow a
parallel rule:

- One function for the parent-key uniqueness rule. GEN-REL states the same rule twice under two
  codes, `config.generate_anchor_via_parent_not_unique` (E3a, `docs/plans/gen-rel.md:195` on
  `plan/gen-rel` `5c04393a`) and `config.generate_aggregate_parent_not_unique` (E3b, `:221`). Ask
  GEN-REL to keep the rule in one function in `config/_generate_relational.py` that takes the
  caller's code, so `anchor_via`, the aggregates and `inherit` share one predicate.
- Code names. Ask GEN-REL to name E3a's codes for the lookup rather than for `anchor_via` (for
  example `config.generate_parent_lookup_key_not_unique`,
  `config.generate_parent_lookup_self_reference`), so `inherit` reuses them. If E3a has merged with
  `anchor_via` names, E1 reuses them as they are rather than adding parallel codes.
- AT-12 is one more case in GEN-REL's choke-point sentry, scoped so E3b's aggregate `groupby` and
  `reindex` stay green.

## Owner questions

All four are decided on the recommended answer under the owner's standing approval of 2026-11-10
(take the recommended option). The alternatives are kept for the record.

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

## Review round 1 resolution

Dennis round 1 (on `cd1d45e6`, read against `1155b20c` and `plan/gen-rel` `5c04393a`): NO-GO, two
blocking, two MEDIUM, six V2-LOG. Each claim was checked against `1155b20c`.

- B1 (AT-9 could not pass; easy fix moves existing seeds). Confirmed: `strategy_config_fingerprint`
  hashes the column config (`generators/derivation.py:142-180`) and `_EXCLUDED_FROM_FINGERPRINT`
  (`:52`) holds neither `condition_on` nor `by`. The reviewer's path `generation/generators/` is
  `generators/` on main. Fixed: E2 behavior now promises the same number of draws, not the plain
  column's bytes; AT-9 compares to `random.Random(<own base_int("py")>).choices(cats, weights,
  k=n)` plus unchanged plain and statistical goldens; the exclusion list is stated frozen, with a
  mutant that adds the keys; P7 corrected.
- B2 (`inherit` readers left out of numeric typing). Confirmed: `_reference_root` follows only
  `type: reference` (`generation/_numeric_typing.py:169-181`) and `reader_ok` scans same-table
  readers of group members (`:264-286`). Fixed at the source: `_reference_root` also follows
  `inherit` to the parent column, so the inherited copy joins the parent's group and its readers
  vote; new P11 and AT-14; `arrow_table` (`generation/_statistical_column.py:92`) and
  `_numeric_typing.py` added to the Placement table.
- M3 (pinned has no warning path). Confirmed: `warn_on_unseen_parents` is called only at
  `generation/synthesize.py:176`; `execute_pinned` has no `on_warning`
  (`generation/_pinned_execute.py:174-230`). Chose the smaller option that keeps one emitter:
  pinned fallback draws the same rows and raises no warning, as fitted statistical does today.
  AT-7 no longer compares warnings; AT-8 is resident-only. V2-LOG: a pinned `on_warning` through
  `emit_generation_warning` for both providers.
- M4 (forward-reference prerequisite not on main). Confirmed: no consumer of `ref_kind`;
  `plan.generate_column_forward_reference` defined at `reasons/plan/_part3.py:89`, never raised;
  statistical's own check at `plan/_checks.py:390`. GEN-STRICT E2 step 4 is registry-driven over
  every key with a `ref_kind`, so it covers `categorical.condition_on` once E2 registers the key;
  no third check. E2 is sequenced after GEN-STRICT E2. `inherit.via` registers `ref_kind="column"`;
  `inherit.column` registers none; AT-6 covers both.
- V2-LOG items folded in: base and cites refreshed to `1155b20c` (#461 merged); emitter path
  `generation/pool/_events.py:52`; `cum_weights` precomputed per entry (same bytes); AT-12 moved
  into GEN-REL's `test_reference_pool_choke_point.py` with the E3b scope; bool-key message says
  `"True"`; Owner Q1 to Q4 marked decided. Partly rebutted: `_categorical_type` does run for a
  conditional column, from `planned()` at `generation/_numeric_typing.py:257` when the column is a
  typing root; it is not a validity check, though, so the cross-entry type check moved to
  GEN-STRICT E2's `plan.generate_categories_mixed`, and `_categorical_type` reads the same union so
  typing matches. The GEN-REL one-function request is under Coordination with GEN-REL.

## Codex plan gate

Reviewed the whole plan against engine `origin/main` `1155b20c`, GEN-STRICT
`origin/plan/gen-strict` `5fa3008d`, GEN-REL `origin/plan/gen-rel` `5c04393a`, the CODEMAP placement
rules, the recurring-defect catalog and the owner-set V1 and DESIGN bars. Dennis round 2 was GO;
this gate folds in all four of his closing notes.

- **V1 fixed:** E2 now has a build-start entry condition that inspects the merged GEN-STRICT code
  and seeded guard to prove forward references are discovered by iterating `OptionSpec.ref_kind` in
  the option registry, not by a hard-coded key list.
- **V1 fixed:** FM8 now has a named resident and pinned run-time outcome when generated data lacks
  `condition_on`: value-free `GenerationError` with the one new central reason
  `runtime.categorical_condition_column_unavailable`. AT-15 proves it never becomes silent
  all-fallback output and that the exception has no value-bearing context chain.
- **V1 fixed:** the `_reference_root` `inherit` hop is total. A missing `via` or a `via` that is not
  a `reference` column returns the incoming key unchanged; AT-14 pins both cells.
- **V1 fixed:** AT-11 now pins the planned types of ordinary numeric and text categorical columns,
  so extending `_categorical_type` for `by` cannot alter plain categorical typing.
- **DESIGN checked:** the plan extends the GEN-STRICT option registry, GEN-REL input-edge registry,
  GEN-REL parent lookup, #461 selector and warning emitter, numeric typing root walk and central
  reason registry. It adds no second validator, lookup, warning path, edge registry or flat module
  to an overfull package. AT-12 remains the lookup choke-point guard.
- **V2-LOG:** pinned fallback warnings remain absent for both fitted statistical and YAML
  categorical providers, as recorded in round 1. Completeness beyond AT-12's realistic lookup
  shapes remains non-blocking under the standing sentry rule.

**Codex verdict: GO.** No unresolved V1 or change-created DESIGN finding remains in the plan.
