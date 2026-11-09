# GEN-REL: relational generation that keeps its promises

Status: plan

Rules consulted: 00-universal, risk-and-exceptions, development-loop, code-review, testing,
architecture, feature-dev, cross-cutting-concerns, api-and-compatibility, exploration, reuse-first,
security, config-secrets-and-supply-chain, documentation, verification, profiles/local-operations,
catalogs/recurring-defects

Base: engine main `84f4290d`. Roadmap row 24n. Engine only; the CLI docs follow in the GEN-CLI plan.

**Prerequisites.** FK-FANOUT E2a/E2b (row 24h) merge first, because they edit `_reference` and the
reference options; line numbers below are from `84f4290d` and will move. GEN-STRICT (row 24k) merges
first too: it owns the validate-time weights check for a sequence parent and hands two follow-ups
here (F2 weights against the real parent at run, F4 per-parent bounds). Nothing on main raises
`reference_weights_length_mismatch` today.

**Guarantee.** A generate config that asks for a parent/child shape either gets exactly that shape or
is refused with a coded reason. Per-parent bounds hold or the run stops; an empty parent under
`orphan_policy: fail` stops the run; a table can reference itself as a tree; a child date can be
anchored to its parent's date; a parent column can aggregate its own children.
**Non-goals:** fitted (statistical) references, which FK-FANOUT owns; many-to-many junctions, which
GEN-UNIQUE owns; the streamed route learning any new shape (it declines them); pinned mode learning
any new shape (it refuses them); a general cross-table expression language.
**Design notes:** one new config module and one new `generation/relational/` sub-package. The
cardinality repair moves out of `generators/columns.py` into that sub-package. The config module is
the one registry for relationship-policy lookup and generate-column input edges. Validation,
reproducibility and deferred-aggregate checks consume that registry through package exports from
`decoy_engine.config`. The runtime pool check is the one choke point for resident, streamed and pinned
reference draws. A guard test removes one call from each route and must fail. No new public Python
API is added at the supported top-level facade. The config package export is engine plumbing; the
only new user surface is the YAML keys in "Behavior".

## Why

The 2026-11-08 CLI generation QA (CLI `docs/records/2026-11-08-cli-qa-generation.md`, items H5 to H7)
found that the relational options promise more than they deliver:

- `min_per_parent > max_per_parent` is dropped without a word, and `max_per_parent: 6` alone gave a
  parent 7 children.
- `row_count: 0` on the parent gives null foreign keys even under `orphan_policy: fail`
  (`generation/_synthesize_derived.py:145`).
- A table cannot reference itself: the graph check treats the self-edge as a cycle
  (`config/_pipeline.py:216`), so manager/employee and category trees cannot be generated.
- A child date cannot follow its parent's date (a service date before the customer's birth date),
  because `windowed_date` only anchors to a column in its own table.
- `derived_aggregate` is a table-wide scalar (`transforms/derived_aggregate.py:125`), so "order
  total = sum of its line items" cannot be expressed.

The over-max has two causes in the repair (`generators/columns.py:552`):

- The final fill `rng.choices(eligible, k=remaining)` (`generators/columns.py:650`) draws with
  replacement and ignores each parent's remaining room, so it can push a parent past max even when
  the bounds are feasible. A direct call with 3 parents, 20 rows and max 7 broke max on 17 of 200
  seeds.
- When every parent is at max it over-fills on purpose and warns; that happens only when the bounds
  are infeasible.

Both are fixed in E1: the fill respects room, and infeasible bounds are refused.

## Behavior

### E1: bounds and empty parents

- **Repair moves and becomes exact.** `_apply_cardinality_bounds` moves to
  `generation/relational/_cardinality.py`; `generators/columns.py` keeps a thin call and its census
  entry ratchets down. The final fill keeps an array of parents with room and removes a parent when
  its last slot is used. It allocates O(P + N), not O(max * P), and no parent passes max. After the
  repair an explicit postcondition checks the final per-parent counts against both bounds and raises
  `runtime.reference_bounds_unsatisfied` if one is broken. It is not a Python `assert`, so `-O`
  cannot remove it.
  - **Bytes.** Output changes for a bounded reference whenever the repair's fill step runs, on the
    v2 path and on the V1 `ColumnGenerator` path, which calls the same repair
    (`generators/columns.py:542`). The repair move itself leaves unbounded references byte-identical;
    the separate empty-parent and weights refusals are listed below. The slice report lists every
    re-pinned snapshot (pre-GA hard change, `release.py`).
- **Bounds conflict.** `min_per_parent > max_per_parent` (both set, max above 0) is refused at
  validate with `config.generate_per_parent_bounds_conflict`.
- **Bounds and nulls.** `null_probability` on a reference column with a positive `min_per_parent` or
  `max_per_parent` is refused at validate with `config.generate_per_parent_null_conflict`, because
  `_apply_null_probability` runs after every column type (`generation/synthesize.py:321`) and would
  undo the repair. An explicit zero remains off and keeps today's behavior. GEN-UNIQUE refuses the
  same pairing for `unique` (`config/_generate_unique.py:54`).
- **Infeasible bounds.** Zero keeps today's meaning of "unset". With positive bounds, `P` distinct
  non-null parent keys and `N` child rows, feasibility is exactly `min * P <= N` and (max unset or
  `N <= max * P`) (pigeonhole). Equality is feasible on both sides.
  - At validate, only the max side is checked, against the parent table's `row_count` as an upper
    bound on `P`: `max * row_count < N` is refused for any parent column type. The min side needs the
    real `P`, which a nullable or repeated parent column can shrink, so it waits for the run. The
    comparison goes through `unique_capacity_ok(max * row_count, N)`
    (`generation/pool/_capacity.py:32`), the existing domain-versus-rows rule, with the
    `cross_table_unique_problems` pattern from `config/_generate_unique.py`.
    Code `config.generate_per_parent_infeasible`.
  - At run, both sides are checked on the real `parent_keys` list before the draw:
    `runtime.generate_per_parent_infeasible`. The exception is built in one function. Its message is
    the registered fixed summary plus table and column names. It contains no key, count, bound,
    weight or other configured or generated value.
  - When `P = 0` (or the parent `row_count` is 0) the feasibility check is skipped and the
    empty-parent rule below decides.
- **Empty parent.** When the parent key list is empty and `N > 0`:
  - a relationship covering this column pair with `orphan_policy: fail` refuses with
    `runtime.reference_parent_pool_empty`;
  - `orphan_policy: warn` keeps the nulls and logs `warning.reference_parent_pool_empty` through the
    generation logger (the channel `generation/_fidelity_gate.py:198` uses; AT-6 asserts the code in
    the captured record);
  - `preserve`, `remap` and no relationship keep today's nulls and Arrow bytes.
  - The central relationship-policy lookup iterates every `RelationshipConfig.children` entry and
    matches `(parent.table, parent.columns, child.table, child.columns)` against
    `(reference_table, [reference_column], table, [column])`. The existing orphan-policy conflict
    check is extended to generate relationships, so one column cannot resolve to two policies.
    `run_generation` and `_generate_tables_from_config` build the lookup once and pass the matched
    policy to the draw. No relationship keeps the current null behavior.
  - **Pinned mode.** Pinned compile supplies an empty profile relationship tuple
    (`generation/_pinned_compile.py:187`), while `generation/_pinned_execute.py:196` reaches the
    reference draw with per-column config only. Pinned compile therefore uses the same central
    lookup and writes the resolved policy as engine-only compiled metadata. Pinned execute strictly
    validates that metadata against the existing orphan-policy tokens, removes it from the column
    config before `GenDeriveContext` fingerprints the column, and passes it as a separate draw
    argument. Thus a non-empty reference keeps its old values and bytes. A user YAML column cannot
    set the internal field.
  - **Gating step.** E1's first test proves a relationship that names generate tables validates and
    compiles. If it does not, E1 stops before the empty-parent part and reports to the orchestrator,
    with the choice between letting relationships name generate tables and a per-column
    `on_empty_parent` key.
- **Weights at run (GEN-STRICT F2).** `distribution: weighted` with a weights list whose length
  differs from `P` refuses with `runtime.reference_weights_length_mismatch` instead of falling back
  to uniform.
- **One helper for every route.** The feasibility, weights and empty-parent checks live in one
  function, `check_reference_pool(col, keys, n, policy)`. `_reference` and `ReferenceStream`
  (`generation/_column_streams.py:191`) must call it before choosing any value. Pinned execute reaches
  `_reference`, so it uses the same check. `ReferenceStream` runs in the shared streaming core for
  every eligible plan (`generation/_streamed.py:1`). Its uniform fallback at
  `generation/_column_streams.py:196` goes. A sentry inventories both direct callers and the pinned
  execute route to prevent an allowlisted helper bypass.
- **Probe scale.** `downscale_config` (`execution/_probe_scale.py:100`) keeps the bounds but loosens
  them to the scaled sizes. With scaled `N'` and scaled parent `row_count` `P'`:
  - `P' = 0`: bounds are left as they are; the empty-parent rule decides.
  - Parent key is a `sequence` with no `null_probability` (so distinct keys equal `P'`):
    `min' = min(min or 0, N' // P')` and
    `max' = max(max or 0, (N' + P' - 1) // P')`.
  - Any other parent column (distinct keys unknown until run): `min' = 0` and `max' = N'`.

  Only a reference that already has a positive bound is rewritten. Both scaled fields are written so
  a positive-size probe remains on the bounded-reference decline route
  (`generation/_admission.py:179`) and never sees false infeasibility. The calculation uses exact
  integer ceiling division, not float `ceil`.
- **Reason phases.** Validate-time refusals use the `config` catalog (phase CONFIGURATION).
  Run-time refusals use the `runtime` catalog, whose allowed phases are MASK, GENERATE, FINALIZE and
  UNKNOWN; these occurrences are GENERATE. The warning uses the `warning` catalog at GENERATE. None
  uses `plan`, whose only phase is COMPILE (`reasons/_catalog.py:36`). Same-name codes in two catalogs
  have precedent (`orphan_policy_conflict` in `plan` and `runtime`). **V2-LOG follow-up:** the existing
  `plan.reference_parent_pool_empty` is raised at run by
  `generation/statistical/_fanout.py:296`. FK-FANOUT owns it; this change does not copy it.

### E2: self-reference trees

- A `reference` column whose `reference_table` is its own table is a **self-reference**. The config
  graph check and the generate dependency graph (`generation/synthesize.py:115`) skip that one edge;
  any other cycle is still refused.
- `reference_column` must name an earlier `sequence` column in the same table with no
  `null_probability`. Anything else refuses with `config.generate_self_reference_key_invalid`.
- The draw is a random recursive tree (Smythe and Mahmoud, 1995): rows `0..root_count-1` are roots
  (null); row `i` picks a parent uniformly from the keys of rows `0..i-1`. Every chain ends at a root,
  so there is no cycle by construction. It reads the key from the table's own `generated` snapshot,
  so the dispatch calls a new `self_reference(col, n, seed, derive_key, generated)` instead of
  `_reference`, which has no `generated` argument.
- New option `root_count` (int, default 1). When `row_count > 0`, `1 <= root_count <= row_count`;
  when `row_count` is 0 the option is ignored. Out of range refuses with the existing
  `config.generate_option_range` (`reasons/config/_part2.py:41`). `root_count` on a reference that is
  not a self-reference refuses with the existing `config.generate_option_unknown`.
- Only `distribution: random` and an optional `max_per_parent` are allowed.
  - With `max_per_parent`, row `i` picks uniformly from earlier rows still under max. The earlier rows
    always have room (`i*(max-1) + root_count >= 1` with `max >= 1`), so no draw can stall. The open
    rows are kept in an array with swap-remove, and each new non-root row is added after its parent is
    chosen. Each pick is constant time. With a cap the tree is a degree-bounded variant, not the
    uniform recursive tree the citation describes; the docstring says so.
  - `sequential`, `weighted`, `min_per_parent` and `null_probability` on the self-reference column
    refuse with `config.generate_self_reference_option_unsupported`.
- Routes: the streaming admission already declines it (`reference_to_own_table`,
  `generation/_admission.py:194`), so it runs in memory. Pinned mode refuses it with
  `plan.pinned_relational_option_unsupported` at compile. The pinned execute preflight uses the
  same pure shape predicate but raises `runtime.pinned_relational_option_unsupported` at GENERATE
  for a deserialized or hand-built plan that bypassed compile.
- Probe: `downscale_config` clamps an explicit `root_count` to the scaled table row count when that
  count is positive. A zero-row table keeps the option because runtime ignores it. This prevents a
  valid full config from becoming invalid only in the probe.

### E3a: parent-anchored dates

- **`windowed_date` with `anchor_via`.** `anchor_via` names an earlier `reference` column in this
  table; `anchor` then names a date column in that reference's parent table. Each child date is the
  parent row's anchor plus the usual `min_days..max_days` window. A null FK gives a null date, the
  same as a null same-table anchor.
- The parent key must be a `sequence` column with no `null_probability`, else
  `config.generate_anchor_via_parent_not_unique`.
- The `anchor_via` column must reference another table. A self-reference there would read a parent
  row of the table being generated, which is not in `pools` yet; it refuses with
  `config.generate_anchor_via_self_reference`.
- The central generate-input edge registry treats `anchor` as a same-table input when `anchor_via`
  is absent and as two cross-table inputs when it is present. Its consumers are:
  - `plan/_checks_windowed_date.py:122` checks `anchor` against the parent table's columns instead
    (edit to an existing module; `plan/` gets no new module);
  - the streaming admission (`generation/_admission.py:203`) declines any column with `anchor_via`
    (`windowed_date_anchor_via`), so a same-named child column can never be used by mistake;
  - pinned mode (`generation/_pinned_compile.py:168`) refuses `anchor_via` with
    `plan.pinned_relational_option_unsupported`; pinned execute uses the runtime twin if a supplied
    plan bypassed compile, so `_pinned_execute.py` never reaches a column it cannot look up;
  - the reproducibility graph (`generation/_reproducibility.py:58`), which consumes those edges so a
    child is not reported reproducible when its parent anchor or lookup FK is not, without also
    reading a same-named child column.
- **Docs.** The `windowed_date` reference page, the reason catalog entries and the CHANGELOG.

### E3b: per-parent aggregates

- **Per-parent `derived_aggregate`.** On the parent table, `from_table` names a child table and `via`
  names the child's `reference` column back to this table; `column` is then a child column. Each
  parent row gets `op` over its own children (SQL `GROUP BY` with a `LEFT JOIN`).
  - `via.reference_table` must be this parent table and its `reference_column` must be an earlier
    `sequence` column here with no `null_probability`. Otherwise the mapping from a child key to one
    parent row is ambiguous and validation refuses with
    `config.generate_aggregate_parent_not_unique`.
  - Computed with one pandas `groupby` over the child, then reindexed to the parent keys.
  - Childless parents get 0 for `sum` and `count`, null for `mean`, `min` and `max`. NaN from pandas
    becomes `None` before the column is written.
  - The column is filled in a post-pass after every child table exists, and the parent Arrow table is
    rebuilt from the filled values before return. A later-filled column cannot be read before that
    pass. The central generate-input edge registry covers `reference_column`, same-table `anchor`,
    `anchor_via`, `condition_on`, `group_by`, `order_by`, `derived` expressions, formula references,
    scalar or per-parent aggregates, and future option-registry entries with a column `ref_kind`.
    Any edge to a per-parent aggregate refuses with
    `config.generate_aggregate_read_before_fill`. A guard adds a new `ref_kind="column"` option and
    must fail until the edge registry handles it.
  - `null_probability` on a per-parent aggregate refuses with
    `config.generate_per_parent_null_conflict`: `_apply_null_probability`
    (`generation/synthesize.py:321`) would null the placeholders and the post-pass would overwrite
    them, so the option would be dropped without a word.
- The same input-edge registry treats `column` as a same-table input without `from_table` and as the
  child-table input with it:
  - `plan/_checks_derived_aggregate.py:115` checks `column` against the child table;
  - the streaming admission already declines `derived_aggregate` (`generation/_admission.py:46`);
  - pinned compile refuses the per-parent form with `plan.pinned_relational_option_unsupported`, and
    pinned execute uses the runtime twin for a supplied plan that bypassed compile;
  - the reproducibility graph consumes the child's value and `via` reference edges.
- **Docs.** The `derived_aggregate` and `reference` pages, the reason catalog entries and the
  CHANGELOG.

Every config outside the named intentional-change cells produces byte-identical Arrow output. The
cells are: a positive per-parent bound whose repair fill runs; conflicting or infeasible bounds; a
bounded reference with null injection; a weighted reference whose weights do not match the real
parent pool; and a non-empty child with an empty parent covered by `orphan_policy: fail`. New-key
configs have no old byte contract. `warn` adds one value-free log record but keeps today's null
Arrow bytes. `preserve`, `remap` and no matching relationship keep both behavior and Arrow bytes.
A pinned plan with a matching generate relationship gets a new fingerprint because the policy now
determines empty-parent behavior; AT-1 proves that its non-empty generated Arrow bytes do not move.

## Pre-mortem (failure modes)

| Risk | Guard |
|---|---|
| A refusal or repair change alters bytes outside the named intentional-change cells | Byte-identity corpus over existing fixtures plus explicit near-neighbor configs; exclusions are enumerated by config name and reason (AT-1) |
| The repair still breaks max on a feasible request | Room-aware fill plus an explicit final postcondition; AT-4 runs 200 seeds |
| Nulls applied after the repair break the bounds or a tree key | `null_probability` refused with bounds and on tree and `anchor_via` keys (AT-2b, AT-9) |
| The run-time check uses `row_count` instead of the real distinct count | The run check reads `parent_keys` (`generation/_synthesize_derived.py:88`), the same list the draw uses (AT-4) |
| One route enforces the empty-parent or weights rule and the other does not | One helper for both; route parity test (AT-6, AT-13) |
| A same-named child column silently stands in for a parent anchor | Streaming declines `anchor_via`; pinned refuses it (AT-10, AT-11) |
| A child is reported reproducible though its parent input is not | Reproducibility graph includes cross-table inputs (AT-11b) |
| Self-reference skips more than the self-edge and lets a real two-table cycle through | AT-7 keeps `a -> b -> a` refused next to an accepted self-edge |
| A per-parent aggregate is read before it is filled and sees nulls | Static read-before-fill refusal (AT-12) |
| A refusal message leaks a key value | Value-free message test over every new code (AT-15) |
| The probe flags a feasible config as infeasible or changes route | Bounds loosened, not stripped (AT-5) |
| `root_count` is valid at full size but exceeds the probe row count | Probe clamps it to the scaled positive row count (AT-5) |
| A final safety check vanishes under optimized Python | Explicit exception branch, plus a `python -O` subprocess cell (AT-4) |

## Acceptance tests

Each test is red on the current build or on the named mutant.

| # | Test | Red on |
|---|---|---|
| AT-1 | Byte-identity: every existing generate fixture plus near-neighbor configs produces the same Arrow bytes unless its name appears in the intentional-change list above. The report lists each exclusion and reason. It includes valid weighted references, non-empty pinned references with a relationship policy, empty parents under `preserve`, `remap` and no relationship, zero-valued bounds, ordinary same-table dates and scalar aggregates | a mutant that changes draw order, lets compiled metadata enter seed derivation, changes zero-bound semantics or branches on a new key when it is absent |
| AT-2 | `min_per_parent: 4, max_per_parent: 2` refuses at validate with `config.generate_per_parent_bounds_conflict` | current build (accepted silently) |
| AT-2b | `null_probability: 0.1` with `max_per_parent: 3` refuses with `config.generate_per_parent_null_conflict` | current build |
| AT-3 | Parent `row_count: 10`, child of 20: `max: 1` refuses at validate with `config.generate_per_parent_infeasible`; `min: 3` refuses at run with `runtime.generate_per_parent_infeasible`; `min: 2, max: 2` passes with every parent at exactly 2 | current build; a mutant that refuses either equality boundary |
| AT-4 | A parent with `row_count: 4` but 3 real distinct keys and a child of 20 passes the conservative validate check at `max: 6`, then refuses at run; `max: 7` over 200 seeds never gives a parent more than 7 children. A very large max does not allocate proportional to max. The explicit final check still raises in a `python -O` subprocess mutant that corrupts one repaired count | current build (17 of 200 direct-repair seeds break max, H6); a row-count-instead-of-real-P mutant; a multiset-of-all-slots mutant; a Python-assert mutant |
| AT-5 | `downscale_config` keeps bounded references bounded with loosened values; the probe of an AT-3 passing config does not refuse and is declined by streaming admission like the full run; a parent `row_count: 0` does not divide by zero; a categorical parent gets `min' = 0, max' = N'`; a self-reference with `root_count` above scaled `N'` clamps and probes; exact integer ceiling works above float precision | a mutant that strips or keeps bounds unscaled; a mutant that applies the sequence formula to every parent; a mutant that leaves `root_count`; a float-ceiling mutant |
| AT-6 | Parent `row_count: 0`, child `N > 0`, and a matching relationship child entry at `orphan_policy: fail`: resident eligible core, bounded streamed sink, forced legacy whole-table route and pinned compile plus execute all refuse with `runtime.reference_parent_pool_empty`. Pinned fingerprints differ when the policy differs. `warn` gives nulls and one captured value-free log record carrying `warning.reference_parent_pool_empty`; `preserve`, `remap` and no relationship give today's nulls. A relationship with two children selects the right child, and conflicting duplicate policies refuse through the existing conflict mechanism. The gating test first proves the generate relationship validates and compiles | current build (nulls under `fail`, H6); a policy-omitted-from-fingerprint mutant; a first-child-only lookup mutant; omission of any route call |
| AT-7 | `employees.manager_id -> employees.id` validates; `a.x -> b.y` with `b.y -> a.x` still refuses as a cycle | current build (self-edge refused, H5); a mutant that drops every cycle check |
| AT-8 | Self-reference over 200 rows and 10 seeds: rows below `root_count` are null, every other parent key belongs to an earlier row, every chain reaches a root | a mutant drawing from all rows |
| AT-9 | Self-reference with `max_per_parent: 2`: no key has more than 2 children over 10 seeds; `weighted`, `sequential`, `min_per_parent` and `null_probability` each refuse with `config.generate_self_reference_option_unsupported`; a non-sequence or nullable key refuses with `config.generate_self_reference_key_invalid`; `row_count: 0` with the default or an otherwise out-of-range `root_count` validates; `root_count: 0` on 10 rows refuses with `config.generate_option_range`; `root_count` on a non-self reference refuses with `config.generate_option_unknown` | a mutant ignoring max or failing to add each new row to the open set; current build |
| AT-10 | Pinned compile refuses a self-reference, an `anchor_via` column and a per-parent aggregate with `plan.pinned_relational_option_unsupported`. Pinned execute independently refuses a deserialized or hand-built plan carrying any of them with `runtime.pinned_relational_option_unsupported` at GENERATE. A tampered internal orphan-policy field of the wrong type or an unknown token also gets the runtime refusal before draw | a mutant that guards compile only, reuses the plan code at execute, or parses stored metadata laxly |
| AT-11 | `anchor_via` child date is within `[parent.birth + min_days, parent.birth + max_days]` for every row, on a child whose deliberately different same-named `birth` column would fail that range; the streaming admission declines the column; a non-sequence parent key refuses with `config.generate_anchor_via_parent_not_unique`; an `anchor_via` naming a self-reference refuses with `config.generate_anchor_via_self_reference` | current build (no such option); a mutant that reads the same-named child column |
| AT-11b | A child whose `anchor_via` parent anchor is not reproducible is reported not reproducible. Existing configs without `anchor_via` keep the exact old reproducibility report | a mutant that keeps `_SIBLING_KEYS` same-table only; a refactor that changes old dependency edges |
| AT-12 | Per-parent `sum`, `count`, `mean`, `min`, `max` match a pandas `groupby` with a left join; childless parents get 0, 0, `None`, `None`, `None` (not NaN); the final returned parent Arrow table contains the filled values. A repeated or nullable parent key refuses with `config.generate_aggregate_parent_not_unique`. Each realistic reader class in the input-edge registry refuses an aggregate read before fill. `null_probability` on the aggregate refuses with `config.generate_per_parent_null_conflict` | current build (table-wide scalar); mutants filling childless `mean` with 0, leaving NaN, returning the placeholder Arrow table, or omitting one edge class |
| AT-13 | Weighted reference with 3 weights against 4 distinct parent keys refuses at run with `runtime.reference_weights_length_mismatch` on the resident eligible core, streamed sink, forced legacy route and pinned execute | current build (uniform fallback, H7); omission of any route call |
| AT-14 | Every new code is in the correct config, plan, runtime or warning catalog with the `since` the Frozen contracts rule gives it. Compile and execute use the plan and runtime twins of the pinned relational refusal. The registry version follows the rule in Frozen contracts | a mutant that raises an unregistered code or puts an execute refusal in `plan` |
| AT-15 | Every new refusal and warning contains only its fixed summary plus table and column names. Sentinel keys, counts, bounds, weights and other configured or generated values are absent from `str(exc)`, logs and reason payloads. Every new safe exception has `__cause__ is None` and `__context__ is None` | mutants that format a key, count or bound, or raise inside an `except` block |
| AT-16 | Seed determinism: the same seed gives the same repair, tree, anchored dates and aggregates; a different seed changes the tree | a mutant using module-global `random` |

AT-1 runs in every slice. Each slice runs its applicable mutations on its final head: E1 restores the
with-replacement fill, drops the runtime feasibility check, removes each pool-check route call and
uses a Python `assert`; E2 draws tree parents from all rows and skips every cycle edge instead of the
self-edge only; E3a lets streaming admit `anchor_via` and restores the same-table dependency; E3b
fills childless `mean` with 0, returns the placeholder table and removes each realistic input-edge
class. Each mutation must turn a test red. A later source edit voids the mutation result.

## Class table

| # | Class | Where it can bite here | Closing structure | Sentry or property test | Mutation |
|---|---|---|---|---|---|
| 1 | Wrong value | Per-parent counts above max; aggregate over the wrong children | Room-aware fill, explicit final postcondition, pigeonhole refusal; `groupby` over the FK | AT-3, AT-4, AT-12 | restore the with-replacement fill; fill `mean` with 0 |
| 2 | Cleanup | none: no files, handles or temp state are created | N/A, the new code is pure over in-memory lists | AT-1 shows no new artifact | none needed |
| 3 | State | Nulls applied after the repair; the post-pass reads a column not filled yet | `null_probability` refusals; static read-before-fill refusal; post-pass after the topo order completes | AT-2b, AT-12 | move the post-pass before the child |
| 4 | Route divergence | Empty-parent or weights rule on one route only; streaming anchoring to a same-named column | One `check_reference_pool` helper; streaming declines `anchor_via` | AT-6, AT-11, AT-13 parity | route the refusal through one route; admit `anchor_via` |
| 5 | Referential integrity | Child FK pointing at no parent; a self-reference loop | Draw only from `parent_keys`; earlier-row-only tree | AT-6, AT-8 | draw from all rows |
| 6 | Untrusted input | Bounds, `root_count` and weights from YAML | Option specs with `_int_at_least`; validate-time refusals | AT-2, AT-9 | remove the `root_count` lower bound |
| 7 | TOCTOU / check-then-use | Validate passes on `row_count`, run sees fewer distinct keys | Run-time recheck on the real `parent_keys` list | AT-3 (min side), AT-4 | drop the run check |
| 8 | Weak test | A seed-dependent test that passes by luck | AT-4 runs 200 seeds; each AT is red on the current build or a named mutant | the mutation list above | each listed mutation |
| 9 | Refactor drift | Moving the repair into `relational/_cardinality.py` changes bytes beyond the fill fix | The move lands as its own commit with AT-1 green before the fill changes | AT-1 byte identity | reseed in the moved code |
| 10 | Frozen contract | Reason registry, generate option names, Arrow output | New codes only, additive options, registry version per the rule in Frozen contracts | AT-14, AT-1 | raise an unregistered code |

Recurring-defect catalog check: no regex is added; every new option and the compiled orphan-policy
metadata use exact type checks; AT-15 checks empty exception cause and context; no new wire version or
scanner is added; cross-package callers use the config package export, not `_generate_relational`;
CI commands have no failure-swallowing form; raw YAML and compiled pinned metadata keep separate
entry checks over one runtime core; mutations run on the final head; the pool-helper sentry checks its
callers by exact path and name; no cap, census value or allowlist is raised. **V2-LOG follow-up:** the
unchanged `PinnedPlan.from_dict` reader coerces several stored fields and does not reject unknown
shape. GEN-REL does not widen that behavior, and its new metadata is decoded strictly at execute.

## Reuse inventory

- **Established solutions searched first.** Code search found the existing cardinality repair,
  option registry, parent-key extractor, reason catalogs and pandas aggregate implementation. Online
  search found Smythe and Mahmoud, [*A Survey of Recursive Trees*](https://statistics.columbian.gwu.edu/hosam-mahmoud),
  Theory of Probability and Mathematical Statistics 51 (1995), 1 to 27, and the official pandas
  [groupby reference](https://pandas.pydata.org/docs/reference/groupby.html). E2 follows the standard
  recursive-tree construction, where each later label chooses an earlier label. E3b uses pandas'
  maintained split-apply-combine implementation. Custom code remains necessary for Decoy's seeded
  draws, option validation, route parity, value-free reasons and post-fill ordering.
- `parent_keys` (`generation/_synthesize_derived.py:88`): the one parent key list for the draw, the
  feasibility check and the weights check.
- `_apply_cardinality_bounds` (`generators/columns.py:552`): moved, with only the fill step changed.
- `unique_capacity_ok` (`generation/pool/_capacity.py:32`): the validate-time max-side check.
- `GenDeriveContext.for_column(...).base_int("py")`: the per-column seed for every new draw.
- The option registry `TypeOptions`/`OptionSpec` (`config/_generate_options.py:230`) and GEN-STRICT's
  extras checks for the new keys.
- The new config-owned relationship-policy and input-edge registry: the one structural description
  used by validation, legacy generation, the streaming core, pinned compile and reproducibility.
- `_windowed_date_generate` (`generation/_grouped_windowed_generators.py:67`) for the window; only
  the anchor lookup is new.
- `AGGREGATE_OPS` (`transforms/derived_aggregate.py:61`) for the allowed ops; the per-parent path
  maps them onto one pandas `groupby`.
- The logger channel of `generation/_fidelity_gate.py:198` for the `warn` policy.

## Enumerations

### Exit paths

- Validate refusals (`config` catalog): `config.generate_per_parent_bounds_conflict`,
  `config.generate_per_parent_null_conflict`, `config.generate_per_parent_infeasible`,
  `config.generate_self_reference_key_invalid`, `config.generate_self_reference_option_unsupported`,
  `config.generate_anchor_via_parent_not_unique`, `config.generate_anchor_via_self_reference`,
  `config.generate_aggregate_parent_not_unique`, `config.generate_aggregate_read_before_fill`, and
  the existing `config.generate_option_range` and `config.generate_option_unknown` for `root_count`,
  raised through the config problem machinery called by the pipeline validator next to
  `config/_pipeline.py:216`.
- Compile refusals: `plan.pinned_relational_option_unsupported` from
  `generation/_pinned_compile.py:168`; existing `plan/_checks_windowed_date.py:122` and
  `plan/_checks_derived_aggregate.py:115` codes, now branch-aware.
- Run refusals (`runtime` catalog): `runtime.generate_per_parent_infeasible`,
  `runtime.reference_parent_pool_empty`, `runtime.reference_weights_length_mismatch`,
  and `runtime.reference_bounds_unsatisfied` (explicit postcondition), from the draw in
  `generation/_synthesize_derived.py:105` and `generation/_column_streams.py:191`;
  `runtime.pinned_relational_option_unsupported` from pinned execute preflight.
- Warning: `warning.reference_parent_pool_empty` under `orphan_policy: warn`.

### Routes

- In-memory generate (`generation/synthesize.py:76`): every slice.
- Streaming core (`generation/_streamed.py:1`, `generation/_column_streams.py:191`): E1 adds the
  shared pool checks; it declines repair, self-reference, `anchor_via` and `derived_aggregate`
  (`generation/_admission.py:178`, `generation/_admission.py:203`, `generation/_admission.py:46`).
- Pinned mode (`generation/_pinned_compile.py:168`): refuses the new shapes and carries the
  relationship policy for the empty-parent rule (`generation/_pinned_compile.py:187`,
  `generation/_pinned_execute.py:196`).
- Probe (`execution/_probe_scale.py:100`): loosens per-parent bounds and clamps self-reference
  `root_count`.

### FK shapes

- Single-column FK to another table: bounds, empty parent, weights (E1).
- Self-referencing FK: E2.
- Composite FKs: not generated today (`reference_column` is one column); unchanged.
- FK used as a lookup path: `anchor_via` (E3a) and `via` (E3b).

### Fields

- Existing: `reference_table`, `reference_column`, `distribution`, `weights`, `min_per_parent`,
  `max_per_parent` (`config/_generate_options.py:230`), `null_probability` on any column.
- New: `root_count` (reference), `anchor_via` (windowed_date), `from_table` and `via`
  (derived_aggregate). `anchor` and `column` change meaning only when the new key is present.

### Output surfaces

- Generated Arrow tables: FK values, dates and aggregate values.
- Pinned plan bytes and fingerprint when a generate relationship policy becomes a pinned input.
- Reason records and the registry (`reasons/_registry.py:24`).
- Generation log records (`warn` policy).
- The reproducibility report (`generation/_reproducibility.py:58`).
- Probe output (bounds loosened).

## Frozen contracts

- Reason registry: additive codes only. If REG-RELEASE (`plan/reg-release`) has merged, codes use
  `since=UNRELEASED`, slices do not change `REGISTRY_VERSION` and the release stamps it once.
  Otherwise each slice takes the next minor at build time, as today, and renumbers at merge.
- Generate option names: additive. No existing key is renamed or removed.
- Output bytes: unchanged outside the named intentional-change cells (AT-1). Each changed fixture or
  snapshot is listed by config name and reason. Bounded-reference fill changes land once in E1.
  Pinned fingerprints intentionally change when a matching relationship policy becomes an input;
  non-empty Arrow bytes remain fixed.
- `orphan_policy` Literal (`config/_relationships.py:21`): unchanged values; generate now honors
  `fail` and `warn`.

## Placement

| Change | File | CODEMAP "Where Things Go" row | Existing neighbor |
|---|---|---|---|
| Relational config checks, policy lookup and input edges | new `config/_generate_relational.py`; export from `config/__init__.py` | Config models | `config/_generate_unique.py` (`Problem` shape) |
| New option keys | `config/_generate_options.py` | Config models | reference, windowed_date and derived_aggregate entries |
| Self-edge skip and relational validator call | `config/_pipeline.py` (`_reference_graph_valid`) | Config models | cycle DFS |
| Relational package surface | new `generation/relational/__init__.py` | Generation, updated to name `generation/relational/` | `generation/pool/` named sub-package precedent |
| Repair, pool check and explicit postcondition | new `generation/relational/_cardinality.py` | Generation, updated relational sub-package | `_synthesize_derived._reference` |
| Self-reference tree draw | new `generation/relational/_self_reference.py` | Generation, updated relational sub-package | reference dispatch in `synthesize.py` |
| `anchor_via` lookup | new `generation/relational/_parent_lookup.py` | Generation, updated relational sub-package | `_windowed_date_generate` |
| Per-parent aggregate post-pass | new `generation/relational/_aggregate.py` | Generation, updated relational sub-package | `synthesize._generate_tables_from_config` |
| CODEMAP placement declaration | `CODEMAP.md` Generation row | N/A; this edit makes that row place the new sub-package | current Generation row |
| Self-edge skip in runtime dependency graph | `generation/synthesize.py:115` | Generation | `deps` build |
| Streaming checks and `anchor_via` decline | `generation/_column_streams.py`, `generation/_admission.py` | Generation | `ReferenceStream`, `classify_windowed_date` |
| Pinned refusals and compiled policy | `generation/_pinned_compile.py`, `generation/_pinned_execute.py` | Generation | windowed-date branch; `_generate_column` call |
| Reproducibility inputs | `generation/_reproducibility.py` | Generation | current sibling dependency scan |
| Branch-aware plan checks | `plan/_checks_windowed_date.py`, `plan/_checks_derived_aggregate.py` | Plan compile and checks | existing checks; no new plan module |
| Probe loosening | `execution/_probe_scale.py` | Pipeline spine and routes | `downscale_config` |
| Reason codes | existing `reasons/config/_part2.py`, `reasons/runtime/_part2.py`, `reasons/plan/_part3.py`, `reasons/warning/_part1.py` | Reason codes | existing generate codes |
| Config tests | new `tests/unit/config/test_generate_relational.py` | Unit tests | `tests/unit/config/` peers |
| Cardinality tests | new `tests/unit/generation/relational/test_cardinality.py` | Unit tests | mirrors `generation/relational/_cardinality.py` |
| Tree tests | new `tests/unit/generation/relational/test_self_reference.py` | Unit tests | mirrors `generation/relational/_self_reference.py` |
| Parent lookup tests | new `tests/unit/generation/relational/test_parent_lookup.py` | Unit tests | mirrors `generation/relational/_parent_lookup.py` |
| Aggregate tests | new `tests/unit/generation/relational/test_aggregate.py` | Unit tests | mirrors `generation/relational/_aggregate.py` |
| Route acceptance tests | new `tests/unit/generation/test_relational_routes.py` | Unit tests | existing generation route tests |
| Pool choke-point sentry | new `tests/sentry/defects/test_reference_pool_choke_point.py` | Sentry test | recurring-defect sentries |
| Input-edge sentry | new `tests/sentry/defects/test_generate_input_edges.py` | Sentry test | option-read sentry precedent |

`generation/` has 23 direct Python modules, so the new code goes in a named
`generation/relational/` sub-package and adds only the package directory at the direct level.
`config/` has 22 direct Python modules and reaches 23. `plan/` gets edits to two existing modules and
no new one; its private-module freeze remains 30. The hard source cap is 700
(`tests/sentry/test_module_size.py:10`). `generators/columns.py` is 658 lines and its exact census
entry is at `tests/sentry/test_module_size.py:85`; it shrinks by the moved repair and the census
ratchets down. `synthesize.py` is 500 lines. Each new hand-written module stays below 600 lines, and
no cap, allowlist or census value is raised.

## Slices

| Slice | Branch | Size | Content | Gate |
|---|---|---|---|---|
| E1 | `feat/gen-rel-e1` | about 350 lines | Gating relationship test; central policy lookup; repair move (own commit, AT-1 green), bounded fill and explicit postcondition; bounds, null and feasibility checks; empty-parent policy and weights through one helper on every route; probe loosening; AT-1 to AT-6, AT-13 to AT-15 | Full suite: reason-manifest and snapshot changes are frozen-contract triggers in `testing.md`; no registry-version stamp |
| E2 | `feat/gen-rel-e2` | about 240 lines | Self-edge skip, self-reference tree, `root_count`, probe clamp, option refusals, pinned refusal, AT-7 to AT-9, the self-reference cell of AT-10, AT-16 (tree part) | Full suite for reason-manifest change; no registry-version stamp |
| E3a | `feat/gen-rel-e3a` | about 250 lines | Central input edges, `anchor_via` across config, plan checks, streaming admission, pinned compile and reproducibility; docs; the anchor cell of AT-10, AT-11, AT-11b | Full suite for reason-manifest change; no registry-version stamp |
| E3b | `feat/gen-rel-e3b` | about 300 lines | Per-parent aggregate post-pass, unique parent-key rule, read-before-fill edge check, plan check, pinned refusal, reproducibility inputs; docs and CHANGELOG; the aggregate cell of AT-10, AT-12, AT-16 | Full suite for reason-manifest change; no registry-version stamp |

Each slice keeps AT-1 green and starts from main after the previous one merges.

## Engineering calls made

These follow standard behavior or ordering and are not owner questions:

- `root_count` defaults to 1, a single tree (the usual org-chart shape).
- `anchor_via` needs a `sequence` parent key until GEN-UNIQUE merges; accepting `unique: true` keys
  is a follow-up after it.
- Childless parents: 0 for `sum` and `count`, `None` for `mean`, `min` and `max` (SQL `LEFT JOIN`
  with `COALESCE` on the additive ops only).

## Owner questions

1. **Empty parent without `fail`.** Keep nulls and log a coded warning under `warn`, or refuse under
   every policy? Recommended: keep nulls plus the warning; `preserve` and `remap` stay silent.
2. **Infeasible bounds.** Refuse, or keep best effort with a warning? Recommended: refuse; a warning
   still hands back data that breaks the stated contract.
3. **YAML naming.** Recommended: explicit keys `anchor_via`, `from_table` and `via` over a dotted
   `anchor: customers.birth_date`, so the option registry keeps one meaning per key.
4. **Bounded-reference bytes.** E1 changes a bounded config when the repaired fill runs, and refuses
   an infeasible bounded config instead of over-filling it. This applies to the v2 path and the V1
   `ColumnGenerator` path alike. Accept the one-time change pre-GA? Recommended: yes, with every
   re-pinned snapshot listed in the slice report.
