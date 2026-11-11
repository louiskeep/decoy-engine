# GEN-REL: relational generation that keeps its promises

Status: plan

Rules consulted: 00-universal, risk-and-exceptions, development-loop, workspace-and-vcs,
code-review, testing, architecture, feature-dev, bugfix, cross-cutting-concerns,
api-and-compatibility, exploration, reuse-first, security, config-secrets-and-supply-chain,
observability-and-resilience, data-and-migrations, scope-discipline, documentation, verification,
profiles/local-operations, catalogs/recurring-defects

Base: engine `origin/main` `9878d17e5bda3c0771645a803be86b397bb880b8`. Roadmap row 24n.
Engine only; the CLI docs follow in the GEN-CLI plan. All source locations and module counts below
were checked with `git show origin/main:<path>` and `git ls-tree origin/main` on 2026-11-11.

**Risk and approval.** R2. The work changes generated data, config and reason contracts, a pinned
read path, relationship-policy errors and cross-cutting validation. Required evidence is a reviewed
plan, red-first T0/T1 per slice, the named final-head mutations, independent T2, and one
trigger-bound full-suite run per slice. The owner is the approver. No process exception is planned.

**Current-main entry conditions.** FK-FANOUT, GEN-STRICT, GEN-TYPES E1/E2, GEN-UNIQUE and
GEN-COND-CAP are already on this base. Before E1, the builder records the exact rebased main SHA and
confirms `OptionSpec.ref_kind` is still consumed by
`plan/checks/_generate_columns.py::_check_forward_references`; if that single registry-driven walk
has moved, update the named choke point rather than adding a validator. REG-RELEASE
(`origin/plan/reg-release`) is not on this base and must merge before E1; if it has not, stop rather
than inventing a registry-version bump. Each later slice starts only after the prior slice merges.
GEN-INHERIT E1 starts only after E3a merges and consumes the lookup and reason contracts frozen here.

**Guarantee.** A generate config that asks for a parent/child shape either gets exactly that shape or
is refused with a coded reason. Per-parent bounds hold or the run stops; an empty parent under
`orphan_policy: fail` stops the run; a table can reference itself as a tree; a child date can be
anchored to its parent's date; a parent column can aggregate its own children.
**Non-goals:** changing fitted fan-out allocation; many-to-many junctions, which GEN-UNIQUE owns;
the streamed route learning the new relational shapes; pinned mode learning self-reference,
`anchor_via` or per-parent aggregate shapes; a general cross-table expression language.

**Design notes and choke points.** `OptionSpec.ref_kind` in `config/_generate_options.py` remains
the one descriptor for every generate-column input. Public
`iter_generate_input_refs(table, column) -> tuple[GenerateInputRef, ...]`, exported by `config`,
resolves only those descriptors. The existing strict-schema walk in
`plan/checks/_generate_columns.py` remains the one checker of the resolved table, column and order;
generation order, reproducibility and deferred-aggregate checks consume the same descriptors
without revalidating them. There is no second input-edge table in `_generate_relational.py`.
Relationship-policy parsing stays in its existing
`relationships/_graph.py` owner: extract `normalize_orphan_policy_lookup(config)` there, export it
from `decoy_engine.relationships`, and make both graph construction and generation consume it. The
capacity predicate moves from
`generation/pool/_capacity.py` to a foundation leaf used by config and generation, with the existing
`generation.pool.unique_capacity_ok` export retained as a compatibility facade. The cardinality
repair moves into one new `generation/relational/` sub-package and uses `value_key` for every map or
counter key. The runtime reference-pool check is the one choke point for resident, streamed, legacy
and pinned draws. `generation/relational/_parent_lookup.py` is the one FK-value-to-parent-row lookup
for E3a and GEN-INHERIT. Guard tests seed one realistic bypass of each choke point and must fail.
No new supported top-level Python API is added. The new user surface is only the YAML keys frozen
below.

## Why

The 2026-11-08 CLI generation QA (CLI `docs/records/2026-11-08-cli-qa-generation.md`, items H5 to H7)
found that the relational options promise more than they deliver:

- `min_per_parent > max_per_parent` is dropped without a word, and `max_per_parent: 6` alone gave a
  parent 7 children.
- `row_count: 0` on the parent gives null foreign keys even under `orphan_policy: fail`
  (`generation/_synthesize_derived.py:200`).
- A table cannot reference itself: the graph check treats the self-edge as a cycle
  (`config/_pipeline.py:218`), so manager/employee and category trees cannot be generated.
- A child date cannot follow its parent's date (a service date before the customer's birth date),
  because `windowed_date` only anchors to a column in its own table.
- `derived_aggregate` is a table-wide scalar (`transforms/derived_aggregate.py:157`), so "order
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

The owner ratified the recommended contracts on 2026-11-11. Empty-parent `warn` keeps nulls and
emits one coded warning, while `preserve` and `remap` stay silent. Infeasible positive bounds are
refused. The YAML keys are exactly `anchor_via`, `from_table` and `via`; dotted anchors are not
accepted. The bounded-reference output change is accepted once before GA on both generation paths,
with every re-pinned artifact listed in E1's slice report.

### E1: bounds and empty parents

- **Repair moves and becomes exact.** `_apply_cardinality_bounds` moves to
  `generation/relational/_cardinality.py`; `generators/columns.py` keeps a thin call and its census
  entry ratchets down. The final fill keeps an array of parents with room and removes a parent when
  its last slot is used. It allocates O(P + N), not O(max * P), and no parent passes max. After the
  repair an explicit postcondition checks the final per-parent counts against both bounds and raises
  `runtime.reference_bounds_unsatisfied` if one is broken. It is not a Python `assert`, so `-O`
  cannot remove it.
  Every `Counter`, dict and set in the moved repair is keyed by
  `config.value_key(value)`, including counts, deficits, tally, room and donor updates. The values
  written back remain the original parent objects. Raw list or dict parent values never become map
  keys. AT-4 runs both the v2 `_reference` caller and the V1 `ColumnGenerator` caller with list and
  dict keys.
  - **Ratified byte change.** The owner accepted the one-time pre-GA output change on 2026-11-11.
    Output changes for a positive bounded reference whenever the repaired fill runs, on the v2 path
    and on the V1 `ColumnGenerator` path, which both call the moved repair
    (`generation/_synthesize_derived.py:225`, `generators/columns.py:537`). The move alone leaves
    unbounded references byte-identical. The E1 slice report lists every re-pinned fixture or
    snapshot by path, config name and reason on both paths.
- **Bounds conflict.** `min_per_parent > max_per_parent` (both set, max above 0) is refused at
  validate with `config.generate_per_parent_bounds_conflict`.
- **Bounds and nulls.** `null_probability` on a reference column with a positive `min_per_parent` or
  `max_per_parent` is refused at validate with `config.generate_per_parent_null_conflict`, because
  `_apply_null_probability` runs after every column type (`generation/synthesize.py:528`) and would
  undo the repair. An explicit zero remains off and keeps today's behavior. GEN-UNIQUE refuses the
  same pairing for `unique` (`config/_generate_unique.py:114`).
- **Infeasible bounds.** Zero keeps today's meaning of "unset". With positive bounds, `P` distinct
  non-null parent keys and `N` child rows, feasibility is exactly `min * P <= N` and (max unset or
  `N <= max * P`) (pigeonhole). Equality is feasible on both sides.
  - At validate, only the max side is checked, against the parent table's `row_count` as an upper
    bound on `P`: `max * row_count < N` is refused for any parent column type. The min side needs the
    real `P`, which a nullable or repeated parent column can shrink, so it waits for the run. The
    comparison goes through `unique_capacity_ok(max * row_count, N)`, the existing
    domain-versus-rows rule. E1 moves its implementation from
    `generation/pool/_capacity.py:32` to new foundation leaf `decoy_engine/_capacity.py`.
    `generation/pool/_capacity.py` re-exports it for compatibility; `_generate_unique.py` and new
    `_generate_relational.py` import the foundation leaf. No config-to-generation import and no new
    `.importlinter` ignore is added. A test patches the foundation predicate and proves both config
    checks and the pool facade observe the patch.
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
  - `orphan_policy: warn` keeps the nulls and emits exactly one
    `warning.reference_parent_pool_empty` through public
    `generation.pool.emit_generation_warning` (`generation/pool/_events.py:52`). The record is
    `QualityWarning(code=<registered code>, provider="reference", column=<column name>,
    detail={"table": <table name>, "column": <column name>})`; its log summary is the registered
    fixed summary plus those names. Resident and streamed routes pass their existing `on_warning`
    callback. Pinned execution has no callback and still writes the value-free log record.
  - `preserve`, `remap` and no relationship keep today's nulls and Arrow bytes.
  - `relationships/_graph.py::normalize_orphan_policy_lookup(config)` extracts and normalizes every
    relationship child entry into the one
    `(parent table, parent columns, child table, child columns) -> OrphanPolicy` lookup.
    `check_orphan_fk_policy_completeness` consumes that helper rather than parsing the raw entries
    again. The helper is exported from `decoy_engine.relationships`; generation reaches it only
    through that public package API and matches
    `(reference_table, [reference_column], table, [column])` in the same lookup. The existing
    duplicate and multi-parent conflict checks remain central, so one child column cannot resolve
    to two policies. `run_generation` and `_generate_tables_from_config` build the lookup once and
    pass the matched policy to the draw. No relationship keeps the current null behavior.
  - `relationships/_graph.py` remains the one builder of relationship exceptions. Its invalid,
    duplicate and multi-parent conflict messages become the registered fixed summary plus parent
    and child table/column names only. They never interpolate a submitted policy token or the set of
    policies (`_graph.py:193-203`, `385-393`, `426-435` on the base). The existing reason codes and
    paths remain. AT-6r puts sentinel policy strings in each surface and proves they are absent from
    `str(exc)`, `__cause__` and `__context__`.
  - **Pinned mode.** Pinned compile supplies an empty profile relationship tuple
    (`generation/_pinned_compile.py:189`), while `generation/_pinned_execute.py:206` reaches the
    reference draw with per-column config only. Add optional
    `PinnedPlan.reference_policies: tuple[tuple[str, str, str], ...] = ()`, where each entry is
    `(table, column, policy)`. `to_dict` and `fingerprint` include the sorted list only when nonempty;
    `_reference_policies(data)` strictly requires three strings and a token from `OrphanPolicy`.
    An older plan with no field decodes to empty. Pinned compile fills it from the central normalized
    lookup; execute turns it into a map and passes the policy separately to the draw. It never enters
    `PinnedColumn.config_json` or `GenDeriveContext`, so a non-empty reference keeps its old values
    and seed while the plan fingerprint correctly changes when policy changes. A YAML column cannot
    set this artifact field.
  - **Gating step.** E1's first test proves a relationship that names generate tables validates and
    compiles. If it does not, E1 stops before the empty-parent part and reports to the orchestrator,
    with the choice between letting relationships name generate tables and a per-column
    `on_empty_parent` key.
- **Weights at run (GEN-STRICT F2).** `distribution: weighted` with a weights list whose length
  differs from `P` refuses with `runtime.reference_weights_length_mismatch` instead of falling back
  to uniform.
- **Fitted fan-out and exact precedence.** Positive `min_per_parent` or `max_per_parent` with
  `distribution: fitted` is refused. The static code is
  `config.generate_fitted_reference_bounds_conflict`; a direct runtime call that bypasses validation
  with a non-empty pool raises `runtime.generate_fitted_reference_bounds_conflict`. The order is:
  registry type/unknown/missing checks, generic `min > max` conflict, fitted-bounds conflict, then
  the null/bounds feasibility checks. A direct runtime bypass branches on fitted before the shared
  non-fitted checker: an empty pool enters the existing fitted allocator first; a non-empty pool
  with either positive bound gets the runtime conflict; otherwise fitted allocation continues.
  Thus a fitted empty pool reaches the existing `generation/statistical/_fanout.py:294` check and
  keeps
  `plan.reference_parent_pool_empty`, even when positive bounds or any `orphan_policy` are present.
  It emits no empty-parent warning. With a non-empty fitted pool and no positive bounds,
  `orphan_policy` has no effect and fitted allocation is unchanged. AT-6f pins every policy, both
  positive bound names, the compile-bypass runtime code and the empty-pool precedence.
- **One helper for every non-fitted route.** The feasibility, weights and empty-parent checks live
  in one function, `check_reference_pool(col, keys, n, policy, table, on_warning)`. `_reference`
  calls it before choosing any value. `ReferenceStream.__init__`
  (`generation/_column_streams.py:197`) calls it exactly once per column stream, not once per batch,
  so `warn` emits exactly once. Pinned execute reaches `_reference`, so it uses the same check.
  `ReferenceStream` runs in the shared streaming core for every eligible plan
  (`generation/_streamed.py:1`). Its uniform fallback at
  `generation/_column_streams.py:205` goes. A sentry inventories both direct callers and the pinned
  execute route to prevent an allowlisted helper bypass. The fitted branch has the explicit
  precedence above and is a named exemption in that guard, not a silent bypass.
- **Probe scale.** `downscale_config` (`execution/_probe_scale.py:100`) keeps the bounds but loosens
  them to the scaled sizes. With scaled `N'` and scaled parent `row_count` `P'`:
  - `P' = 0`: bounds are left as they are; the empty-parent rule decides.
  - Parent key is a `sequence` with no `null_probability` (so distinct keys equal `P'`):
    `min' = min(min or 0, N' // P')` and
    `max' = max(max or 0, (N' + P' - 1) // P')`.
  - Any other parent column (distinct keys unknown until run): `min' = 0` and `max' = N'`.

  Only a reference that already has a positive bound is rewritten. Both scaled fields are written so
  a positive-size probe remains on the bounded-reference decline route
  (`generation/_admission.py:212`) and never sees false infeasibility. The calculation uses exact
  integer ceiling division, not float `ceil`.
- **Reason phases.** Validate-time refusals use the `config` catalog (phase CONFIGURATION).
  Run-time refusals use the `runtime` catalog, whose allowed phases are MASK, GENERATE, FINALIZE and
  UNKNOWN; these occurrences are GENERATE. The warning uses the `warning` catalog at GENERATE. None
  uses `plan`, whose only phase is COMPILE (`reasons/_catalog.py:36`). Same-name codes in two catalogs
  have precedent (`orphan_policy_conflict` in `plan` and `runtime`). The existing fitted
  `plan.reference_parent_pool_empty` runtime use is retained only because the owner explicitly
  ratified its precedence; it is recorded in V2-LOG and not copied.

### E2: self-reference trees

- A `reference` column whose `reference_table` is its own table is a **self-reference**. The config
  graph check and the generate dependency graph (`generation/synthesize.py:226`) skip that one edge;
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
  `generation/_admission.py:229`), so it runs in memory. Pinned mode refuses it with
  `plan.pinned_relational_option_unsupported` at compile. The pinned execute preflight uses the
  same pure shape predicate but raises `runtime.pinned_relational_option_unsupported` at GENERATE
  for a deserialized or hand-built plan that bypassed compile.
- Probe: `downscale_config` clamps an explicit `root_count` to the scaled table row count when that
  count is positive. A zero-row table keeps the option because runtime ignores it. This prevents a
  valid full config from becoming invalid only in the probe.

### E3a: parent-anchored dates

- **`windowed_date` with `anchor_via`.** `anchor_via` names an earlier `reference` column in this
  table; `anchor` then names a date column in that reference's parent table. Each child date is the
  parent row's anchor plus the usual `min_days..max_days` window. A null FK gives a null date. A
  non-null FK that resolves to a present parent whose anchor is null is an error, not a null child.
- The parent key must be a `sequence` column with no `null_probability`, else
  `config.generate_parent_lookup_key_not_unique`. The predicate is one function in
  `config/_generate_relational.py` that accepts the caller's reason code; aggregates and
  GEN-INHERIT use it rather than restating the rule.
- The `anchor_via` column must reference another table. A self-reference there would read a parent
  row of the table being generated, which is not in `pools` yet; it refuses with
  `config.generate_parent_lookup_self_reference`. GEN-INHERIT reuses both generic codes.
- **One typed parent lookup.** New `generation/relational/_parent_lookup.py` exposes
  `lookup_parent_values(pools, *, parent_table, key_column, value_column, foreign_keys,
  child_table, child_column) -> ParentLookupResult`, where the result holds one aligned value per FK
  and the selected parent column's `pa.DataType`. It builds one
  `value_key(parent_key) -> parent row` map, so list and dict keys work. A null FK produces a null
  aligned value without lookup. A non-null FK missing from the map raises the registered fixed
  `runtime.parent_lookup_key_missing`; duplicate normalized parent keys raise
  `runtime.parent_lookup_key_not_unique`. Both errors name only table and column names and are
  raised outside any caught exception. A present parent with a null selected value remains a
  present-null result: `anchor_via` translates it to its date-specific refusal, while GEN-INHERIT
  copies the null with the preserved Arrow type. A choke-point sentry proves no other generation
  module builds a pool-column key-to-row map.
- **GEN-STRICT owns input existence and scope.** Extend `OptionSpec` at
  `config/_generate_options.py:66` with the `ref_kind` variants `column_via_reference` and
  `column_in_named_table`, a `ref_context_key` naming the companion option, and
  `ref_phase="inline" | "post_fill"` (default `inline`). Register
  `windowed_date.anchor` as `column_via_reference` with context `anchor_via`; absent context keeps
  the existing same-table-earlier rule. Register `anchor_via` itself as `column`. Register
  both `derived_aggregate.column` and `derived_aggregate.via` as
  `column_in_named_table` with context `from_table` and phase `post_fill`; absent context on
  `column` keeps the existing same-table-earlier inline rule. `GenerateInputRef` holds consumer
  table/column/option, source table/column, `must_be_earlier` and phase. Replace `_referenced` and
  `_check_forward_references` in `plan/checks/_generate_columns.py:332-359` with one
  registry-driven strict checker over `config.iter_generate_input_refs`. That public config
  iterator returns resolved source table, column, `must_be_earlier` and phase from `OptionSpec`;
  the strict checker alone checks table existence, column existence and ordering. Neither imports
  the private config module. The older windowed-date and aggregate checks keep only type-specific
  rules; they do not check target existence a second time. Generation order, reproducibility and
  aggregate read-before-fill checks consume the same iterator output without adding schema-validity
  checks. The ordinary topological order consumes only inline edges; the aggregate post-pass
  consumes post-fill edges after child generation, so a parent's aggregate over a child that
  references it does not create a false cycle. A sentry changes each cross-table descriptor back to
  `column` and both acceptance cells must fail.
- Consumers of those resolved edges are:
  - the existing `plan/checks/_generate_columns.py` strict-schema walk, replacing the target-name
    parts of `plan/_checks_windowed_date.py` and `plan/_checks_derived_aggregate.py`;
  - the streaming admission (`generation/_admission.py:235`) declines any column with `anchor_via`
    (`windowed_date_anchor_via`), so a same-named child column can never be used by mistake;
  - pinned mode (`generation/_pinned_compile.py:154`) refuses `anchor_via` with
    `plan.pinned_relational_option_unsupported`; pinned execute uses the runtime twin if a supplied
    plan bypassed compile, so `_pinned_execute.py` never reaches a column it cannot look up;
  - the reproducibility graph (`generation/_reproducibility.py:58`), which consumes those edges so a
    child is not reported reproducible when its parent anchor or lookup FK is not, without also
    reading a same-named child column.
- **Coded value-free date failures at the source.** `transforms/windowed_date.py` gives
  `WindowedDateError` a registered code and removes row positions, raw type names and submitted
  values from every message. Invalid grammar/type/timezone/NaT uses
  `runtime.windowed_date_anchor_invalid`; an anchor outside `[SUPPORTED_MIN, SUPPORTED_MAX]` uses
  `runtime.windowed_date_anchor_out_of_range`; an offset crossing either bound uses
  `runtime.windowed_date_offset_out_of_range`. Parent lookup adds
  `runtime.parent_lookup_key_missing` and a present null anchor adds
  `runtime.windowed_date_parent_anchor_null`. All messages are the registered fixed summary plus
  table/column names where available. Translations are raised after an `except` block, with no
  cause or context. Remove `WindowedDateError` from the reason-registry `UNCLASSIFIED` allowlist.
  Existing same-table date generation gets the same fixed codes for the shared invalid/range/offset
  classes, so E3a does not add a second date validator.
- **Seed proof.** AT-11 uses at least 64 child rows, four parents, a window at least 30 days wide and
  non-boundary anchors. It proves same-seed equality and that a fixed second seed changes at least
  one anchored child date while every output remains in its own parent's window. The fixture is
  rejected if either seed produces no non-null dates, so the proof cannot pass vacuously.
- **Docs.** Update `docs/strategies.md`, `docs/capability-matrix.md`, `docs/reasons.md` and add
  `changelog.d/gen-rel-e3a.md`.

### E3b: per-parent aggregates

- **Per-parent `derived_aggregate`.** On the parent table, `from_table` names a child table and `via`
  names the child's `reference` column back to this table; `column` is then a child column. Each
  parent row gets `op` over its own children (SQL `GROUP BY` with a `LEFT JOIN`).
  - `via.reference_table` must be this parent table and its `reference_column` must be an earlier
    `sequence` column here with no `null_probability`. Otherwise the mapping from a child key to one
    parent row is ambiguous and validation refuses with
    `config.generate_parent_lookup_key_not_unique`, through the shared predicate E3a and
    GEN-INHERIT consume.
  - Computed with one pandas `groupby` over the child, then reindexed to the parent keys.
  - Childless parents get 0 for `sum` and `count`, null for `mean`, `min` and `max`. NaN from pandas
    becomes `None` before the column is written.
  - The column is filled in a post-pass after every child table exists, and the parent Arrow table is
    rebuilt from the filled values before return. A later-filled column cannot be read before that
    pass. The `OptionSpec.ref_kind` iterator covers same-table `anchor`, `anchor_via`,
    `condition_on`, `group_by`, `order_by`, derived expressions, formula references,
    scalar or per-parent aggregates, and future option-registry entries with a reference kind.
    Any edge to a per-parent aggregate refuses with
    `config.generate_aggregate_read_before_fill`. A guard adds a new `ref_kind="column"` option and
    must fail until the edge registry handles it.
  - `null_probability` on a per-parent aggregate refuses with
    `config.generate_per_parent_null_conflict`: `_apply_null_probability`
    (`generation/synthesize.py:528`) would null the placeholders and the post-pass would overwrite
    them, so the option would be dropped without a word.
- The same OptionSpec resolver treats `column` as a same-table-earlier input without `from_table`
  and as a child-table input with it:
  - the strict-schema walk checks `column` against the child table; the older aggregate checker
    keeps only op, type and `via` direction rules;
  - the streaming admission already declines `derived_aggregate` (`generation/_admission.py:58`);
  - pinned compile refuses the per-parent form with `plan.pinned_relational_option_unsupported`, and
    pinned execute uses the runtime twin for a supplied plan that bypassed compile;
  - the reproducibility graph consumes the child's value and `via` reference edges.
- **Docs.** Update `docs/strategies.md`, `docs/capability-matrix.md`, `docs/reasons.md` and add
  `changelog.d/gen-rel-e3b.md`.

### GEN-INHERIT handoff frozen by E3a

GEN-INHERIT adds no validator, lookup or missing-key reason. It registers `inherit.via` as
`ref_kind="column"` and `inherit.column` as `ref_kind="column_via_reference"` with context `via` and
inline phase. That supersedes GEN-INHERIT's older provisional instruction to leave `column` without
a `ref_kind`, which was necessary only while GEN-STRICT understood sibling columns. Its generator
calls `_parent_lookup.py`, receives aligned values plus the parent `pa.DataType`, copies a
present-null parent value as null, and reuses `runtime.parent_lookup_key_missing` for a non-null FK
that is absent. It reuses `config.generate_parent_lookup_key_not_unique` and
`config.generate_parent_lookup_self_reference`. Its AT-6 same-named-child cell and AT-12 lookup
sentry therefore exercise GEN-REL's registry and choke point instead of adding parallel machinery.

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
| A list or dict parent key reaches a raw `Counter` or dict | Every repair key uses `value_key`; AT-4 runs both v2 and V1 callers with list and dict keys |
| Nulls applied after the repair break the bounds or a tree key | `null_probability` refused with bounds and on tree and `anchor_via` keys (AT-2b, AT-9) |
| The run-time check uses `row_count` instead of the real distinct count | The run check reads `parent_keys` (`generation/_synthesize_derived.py:95`), the same list the draw uses (AT-4) |
| One route enforces the empty-parent or weights rule and the other does not | One helper for both; route parity test (AT-6, AT-13) |
| Fitted fan-out silently ignores positive bounds or changes empty-pool policy | Validate and runtime refusals for bounds; fitted empty-pool check has frozen precedence (AT-6f) |
| Relationship conflict text echoes submitted policy tokens | Sanitize the central `_graph.py` builders; sentinel surface test (AT-6r) |
| A same-named child column silently stands in for a parent anchor | Streaming declines `anchor_via`; pinned refuses it (AT-10, AT-11) |
| GEN-STRICT still treats a cross-table anchor as a child sibling | Cross-table `OptionSpec.ref_kind` descriptors and one resolver; descriptor mutations (AT-11, AT-12) |
| A non-null FK is missing, or a present parent has a null anchor | Typed lookup distinguishes missing from present-null and raises fixed codes (AT-11c) |
| An anchor or shifted result is outside the supported range | Central coded `WindowedDateError`, with min and max boundary cells (AT-11c) |
| A child is reported reproducible though its parent input is not | Reproducibility graph includes cross-table inputs (AT-11b) |
| Self-reference skips more than the self-edge and lets a real two-table cycle through | AT-7 keeps `a -> b -> a` refused next to an accepted self-edge |
| A per-parent aggregate is read before it is filled and sees nulls | Static read-before-fill refusal (AT-12) |
| A refusal message leaks a key, policy token, row position or data value | Value-free message test over every new and edited code (AT-15) |
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
| AT-4 | A parent with `row_count: 4` but 3 real distinct keys and a child of 20 passes the conservative validate check at `max: 6`, then refuses at run; `max: 7` over 200 seeds never gives a parent more than 7 children. A very large max does not allocate proportional to max. Separate list-key and dict-key fixtures pass exact min/max on both v2 `_reference` and V1 `ColumnGenerator`, with original objects in output. The explicit final check still raises in a `python -O` subprocess mutant that corrupts one repaired count | current build (17 of 200 direct-repair seeds break max, H6, and raw `Counter`/dict keys raise on list/dict); row-count, raw-key, multiset-of-all-slots and Python-assert mutants |
| AT-5 | `downscale_config` keeps bounded references bounded with loosened values; the probe of an AT-3 passing config does not refuse and is declined by streaming admission like the full run; a parent `row_count: 0` does not divide by zero; a categorical parent gets `min' = 0, max' = N'`; a self-reference with `root_count` above scaled `N'` clamps and probes; exact integer ceiling works above float precision | a mutant that strips or keeps bounds unscaled; a mutant that applies the sequence formula to every parent; a mutant that leaves `root_count`; a float-ceiling mutant |
| AT-6 | Parent `row_count: 0`, child `N > 0`, and a matching relationship child entry at `orphan_policy: fail`: resident eligible core, streamed reference sink, forced legacy whole-table route and pinned compile plus execute all refuse with `runtime.reference_parent_pool_empty`. Pinned fingerprints differ when the policy differs. `warn` gives nulls and exactly one `QualityWarning`/log record with the frozen shape; pinned logs once with no callback. `preserve`, `remap` and no relationship give today's nulls silently. A relationship with two children selects the right child. The gating test first proves the generate relationship validates and compiles | current build (nulls under `fail`, H6); policy-fingerprint, first-child-only, emitter-bypass and route-call mutants |
| AT-6f | Positive `min_per_parent` and positive `max_per_parent`, separately, with `distribution: fitted` refuse with `config.generate_fitted_reference_bounds_conflict`; a non-empty direct runtime bypass gets the runtime twin. For an empty fitted pool, each of `fail`, `warn`, `preserve`, `remap` and no relationship gets the existing `plan.reference_parent_pool_empty`, even with a positive bound, with no warning. Non-empty fitted output without bounds is byte-identical | current build ignores bounds on fitted; mutants that run generic empty-parent policy first or emit a warning |
| AT-6r | Central relationship errors for an invalid policy, a duplicate key with different policies and a multi-parent child conflict retain their reason codes and paths but omit sentinel submitted policy tokens from `str(exc)`, `__cause__` and `__context__`; graph checks and generation both call the exported relationship normalizer | current `_graph.py:193-203`, `385-393`, `426-435`; mutant that reparses config in generation |
| AT-7 | `employees.manager_id -> employees.id` validates; `a.x -> b.y` with `b.y -> a.x` still refuses as a cycle | current build (self-edge refused, H5); a mutant that drops every cycle check |
| AT-8 | Self-reference over 200 rows and 10 seeds: rows below `root_count` are null, every other parent key belongs to an earlier row, every chain reaches a root | a mutant drawing from all rows |
| AT-9 | Self-reference with `max_per_parent: 2`: no key has more than 2 children over 10 seeds; `weighted`, `sequential`, `min_per_parent` and `null_probability` each refuse with `config.generate_self_reference_option_unsupported`; a non-sequence or nullable key refuses with `config.generate_self_reference_key_invalid`; `row_count: 0` with the default or an otherwise out-of-range `root_count` validates; `root_count: 0` on 10 rows refuses with `config.generate_option_range`; `root_count` on a non-self reference refuses with `config.generate_option_unknown` | a mutant ignoring max or failing to add each new row to the open set; current build |
| AT-10 | Pinned compile refuses a self-reference, an `anchor_via` column and a per-parent aggregate with `plan.pinned_relational_option_unsupported`. Pinned execute independently refuses a deserialized or hand-built plan carrying any of them with `runtime.pinned_relational_option_unsupported` at GENERATE. A tampered internal orphan-policy field of the wrong type or an unknown token also gets the runtime refusal before draw | a mutant that guards compile only, reuses the plan code at execute, or parses stored metadata laxly |
| AT-11 | `anchor_via` child date is within `[parent.birth + min_days, parent.birth + max_days]` for every row, on a child whose deliberately different same-named `birth` column would fail that range; the 64-row, four-parent, 30-day fixture gives identical output for the same seed and at least one changed non-null anchored date for a fixed second seed; both outputs stay in their own parent windows. The test fails if either output has no non-null dates. Streaming declines the column; a non-sequence parent key and self-reference refuse with the generic parent-lookup config codes | current build; same-named-child, constant-date and module-global-random mutants |
| AT-11c | Parent lookup and date failures are distinct: non-null FK absent from the map gives `runtime.parent_lookup_key_missing`; present parent with null anchor gives `runtime.windowed_date_parent_anchor_null`; anchors just below and above the supported range give `runtime.windowed_date_anchor_out_of_range`; offsets crossing minimum and maximum give `runtime.windowed_date_offset_out_of_range`. Invalid grammar/type gives `runtime.windowed_date_anchor_invalid`. List and dict lookup keys work, null FK yields null, and a duplicate normalized parent key gives `runtime.parent_lookup_key_not_unique`. GEN-INHERIT's planned caller is pinned to reuse the missing-key code and present-null result. Every exception is value-free and has no cause/context | current build's uncoded row-position error; mutants that use `dict[fk]`, conflate missing with null, or wrap inside `except` |
| AT-11b | A child whose `anchor_via` parent anchor is not reproducible is reported not reproducible. Existing configs without `anchor_via` keep the exact old reproducibility report | a mutant that keeps `_SIBLING_KEYS` same-table only; a refactor that changes old dependency edges |
| AT-12 | Per-parent `sum`, `count`, `mean`, `min`, `max` match a pandas `groupby` with a left join; childless parents get 0, 0, `None`, `None`, `None` (not NaN); the final returned parent Arrow table contains the filled values. A repeated or nullable parent key refuses with `config.generate_parent_lookup_key_not_unique`. The strict-schema OptionSpec resolver accepts `column` in `from_table` even when no same-named parent sibling exists, and refuses a missing child column. Each realistic resolved reader edge refuses an aggregate read before fill. `null_probability` on the aggregate refuses with `config.generate_per_parent_null_conflict` | current build; descriptor-as-sibling, childless-mean, NaN, placeholder-table and omitted-edge mutants |
| AT-13 | Weighted reference with 3 weights against 4 distinct parent keys refuses at run with `runtime.reference_weights_length_mismatch` on the resident eligible core, streamed sink, forced legacy route and pinned execute | current build (uniform fallback, H7); omission of any route call |
| AT-14 | Every new code is in the correct config, plan, runtime or warning catalog with the `since` the Frozen contracts rule gives it. `WindowedDateError` is removed from `UNCLASSIFIED`. Compile and execute use the plan and runtime twins of the pinned relational refusal. The registry version follows the rule in Frozen contracts | a mutant that raises an unregistered code, leaves the allowlist entry, or puts an execute refusal in `plan` |
| AT-15 | Every new or edited refusal and warning contains only its registered fixed summary plus table and column names. Sentinel keys, policy tokens, row positions, counts, bounds, weights and other configured or generated values are absent from `str(exc)`, logs and reason payloads. Every safe exception has `__cause__ is None` and `__context__ is None` | mutants that format a key, policy, row, count or bound, or raise inside an `except` block |
| AT-16 | Seed determinism: the same seed gives the same repair, tree, anchored dates and aggregates; fixed second seeds change at least one tree parent and at least one anchored date in sized non-vacuous fixtures | a mutant using module-global `random` or returning one legal date for every seed |

AT-1 runs in every slice. Each slice runs its applicable mutations on its final head: E1 restores the
with-replacement fill, drops the runtime feasibility check, removes each pool-check route call and
uses a Python `assert`, a raw parent value as a counter key, a second relationship parser, a direct
warning log call, generic empty-parent handling before fitted handling, and policy-token interpolation
in `_graph.py`; E2 draws tree parents from all rows and skips every cycle edge instead of the self-edge
only; E3a changes each cross-table `ref_kind` to `column`, inlines a parent lookup, conflates missing
with present-null, restores row positions in date errors, returns one legal date for both seeds and
lets streaming admit `anchor_via`; E3b fills childless `mean` with 0, returns the placeholder table
and removes each realistic resolved input edge. Each mutation must turn a named test red. A later
source edit voids the mutation result.

## Class table

| # | Class | Where it can bite here | Closing structure | Sentry or property test | Mutation |
|---|---|---|---|---|---|
| 1 | Wrong value | Per-parent counts above max; aggregate over the wrong children | Room-aware fill, explicit final postcondition, pigeonhole refusal; `groupby` over the FK | AT-3, AT-4, AT-12 | restore the with-replacement fill; fill `mean` with 0 |
| 2 | Cleanup | none: no files, handles or temp state are created | N/A, the new code is pure over in-memory lists | AT-1 shows no new artifact | none needed |
| 3 | State | Nulls applied after the repair; the post-pass reads a column not filled yet | `null_probability` refusals; static read-before-fill refusal; post-pass after the topo order completes | AT-2b, AT-12 | move the post-pass before the child |
| 4 | Route divergence | Empty-parent or weights rule on one route only; fitted precedence changes; streaming anchors to a same-named column | One `check_reference_pool` helper with a named fitted exemption; streaming declines `anchor_via` | AT-6, AT-6f, AT-11, AT-13 | remove one route; run generic empty handling first; admit `anchor_via` |
| 5 | Referential integrity | Child FK pointing at no parent; a self-reference loop; duplicate normalized parent key | One typed parent lookup; draw only from `parent_keys`; earlier-row-only tree | AT-8, AT-11c | direct `dict[fk]`; draw from all rows |
| 6 | Untrusted input | Bounds, `root_count` and weights from YAML | Option specs with `_int_at_least`; validate-time refusals | AT-2, AT-9 | remove the `root_count` lower bound |
| 7 | TOCTOU / check-then-use | Validate passes on `row_count`, run sees fewer distinct keys | Run-time recheck on the real `parent_keys` list | AT-3 (min side), AT-4 | drop the run check |
| 8 | Weak test | A seed-dependent test that passes by luck | AT-4 runs 200 seeds; each AT is red on the current build or a named mutant | the mutation list above | each listed mutation |
| 9 | Refactor drift | Moving the repair changes bytes beyond the fill fix; relationship parsing forks | Move lands with AT-1 green before fill changes; graph checks and generation call the relationship-owned normalizer | AT-1, AT-6r | reseed; reparse entries in generation |
| 10 | Frozen contract | Reason registry, generate option names, Arrow output | New codes only, additive options, registry version per the rule in Frozen contracts | AT-14, AT-1 | raise an unregistered code |

Recurring-defect catalog check: no regex is added; every new option and compiled orphan-policy field
uses exact type checks; AT-15 checks empty exception cause and context; no new wire version or scanner
is added; cross-package callers use public package exports and never another package's private
module; CI commands have no failure-swallowing form; raw YAML and compiled metadata keep separate
entry checks over one runtime core; mutations run on the final head; sentries identify helpers by
exact path and name and walk their callers; no cap, census value or allowlist is raised.

## Reuse inventory

- **Established solutions searched first.** Code search found the existing cardinality repair,
  option registry, parent-key extractor, reason catalogs and pandas aggregate implementation. The
  2026-11-11 online search was `random recursive tree later node chooses earlier node survey` and
  `pandas groupby aggregate official`. It verified Smythe and Mahmoud,
  [*A Survey of Recursive Trees*](https://statistics.columbian.gwu.edu/hosam-mahmoud), and the
  official pandas [groupby reference](https://pandas.pydata.org/docs/reference/groupby.html). E2 follows the standard
  recursive-tree construction, where each later label chooses an earlier label. E3b uses pandas'
  maintained split-apply-combine implementation. Custom code remains necessary for Decoy's seeded
  draws, option validation, route parity, value-free reasons and post-fill ordering.
- `parent_keys` (`generation/_synthesize_derived.py:95`): the one parent key list for the draw, the
  feasibility check and the weights check.
- `_apply_cardinality_bounds` (`generators/columns.py:552`): moved, with only the fill step changed.
- `value_key` (`config/_generate_unique.py:121`, exported by `config`): the one equality-normalizing
  key for parent pools, uniqueness, the moved repair and the parent lookup.
- `unique_capacity_ok` (`generation/pool/_capacity.py:32`): its implementation moves to the
  foundation leaf so config and generation share it without an import-layer exception.
- `GenDeriveContext.for_column(...).base_int("py")`: the per-column seed for every new draw.
- The option registry `TypeOptions`/`OptionSpec` (`config/_generate_options.py:66`) and GEN-STRICT's
  registry-driven reference resolver for all same-table and cross-table input edges.
- The normalized relationship-policy helper in `relationships/_graph.py`, exported by the package
  and consumed by graph construction and generation rather than copied.
- `_windowed_date_generate` (`generation/_grouped_windowed_generators.py:67`) for the window; only
  the anchor lookup is new.
- `AGGREGATE_OPS` (`transforms/derived_aggregate.py:61`) for the allowed ops; the per-parent path
  maps them onto one pandas `groupby`.
- `emit_generation_warning` (`generation/pool/_events.py:52`) for the `warn` policy.

## Enumerations

### Exit paths

- Validate refusals (`config` catalog): `config.generate_per_parent_bounds_conflict`,
  `config.generate_per_parent_null_conflict`, `config.generate_per_parent_infeasible`,
  `config.generate_fitted_reference_bounds_conflict`,
  `config.generate_self_reference_key_invalid`, `config.generate_self_reference_option_unsupported`,
  `config.generate_parent_lookup_key_not_unique`, `config.generate_parent_lookup_self_reference`,
  `config.generate_aggregate_read_before_fill`, and
  the existing `config.generate_option_range` and `config.generate_option_unknown` for `root_count`,
  raised through the config problem machinery called by the pipeline validator next to
  `config/_pipeline.py:218`.
- Compile refusals: `plan.pinned_relational_option_unsupported` from
  `generation/_pinned_compile.py:154`; existing `plan/_checks_windowed_date.py:23` and
  `plan/_checks_derived_aggregate.py:22` codes, with target existence moved to GEN-STRICT.
- Run refusals (`runtime` catalog): `runtime.generate_per_parent_infeasible`,
  `runtime.reference_parent_pool_empty`, `runtime.reference_weights_length_mismatch`,
  `runtime.reference_bounds_unsatisfied` (explicit postcondition),
  `runtime.generate_fitted_reference_bounds_conflict`, `runtime.parent_lookup_key_missing`,
  `runtime.parent_lookup_key_not_unique`, `runtime.windowed_date_anchor_invalid`,
  `runtime.windowed_date_anchor_out_of_range`, `runtime.windowed_date_offset_out_of_range` and
  `runtime.windowed_date_parent_anchor_null`; plus
  `runtime.pinned_relational_option_unsupported` from pinned execute preflight.
- Existing fitted run refusal: `plan.reference_parent_pool_empty`, frozen to win for an empty fitted
  parent pool under every orphan policy.
- Warning: `warning.reference_parent_pool_empty` under `orphan_policy: warn`.

### Routes

- In-memory generate (`generation/synthesize.py:84`): every slice.
- Streaming core (`generation/_streamed.py:1`, `generation/_column_streams.py:197`): E1 adds the
  shared pool checks; it declines repair, self-reference, `anchor_via` and `derived_aggregate`
  (`generation/_admission.py:212`, `generation/_admission.py:235`, `generation/_admission.py:58`).
- Pinned mode (`generation/_pinned_compile.py:154`): refuses the new shapes and carries the
  relationship policy for the empty-parent rule (`generation/_pinned_compile.py:189`,
  `generation/_pinned_execute.py:206`).
- Probe (`execution/_probe_scale.py:100`): loosens per-parent bounds and clamps self-reference
  `root_count`.

### FK shapes

- Single-column FK to another table: bounds, empty parent, weights (E1).
- Self-referencing FK: E2.
- Composite FKs: not generated today (`reference_column` is one column); unchanged.
- FK used as a lookup path: `anchor_via` (E3a) and `via` (E3b).

### Fields

- Existing: `reference_table`, `reference_column`, `distribution`, `weights`, `min_per_parent`,
  `max_per_parent` (`config/_generate_options.py:318`), `null_probability` on any column.
- New: `root_count` (reference), `anchor_via` (windowed_date), `from_table` and `via`
  (derived_aggregate). These exact keys are ratified; dotted anchors are refused as unknown syntax.
  `anchor` and `column` change meaning only when the new companion key is present.

### Output surfaces

- Generated Arrow tables: FK values, dates and aggregate values.
- Pinned plan bytes and fingerprint when a generate relationship policy becomes a pinned input.
- Reason records and the registry (`reasons/_registry.py:24`).
- Generation log records (`warn` policy).
- The reproducibility report (`generation/_reproducibility.py:58`).
- Probe output (bounds loosened).

## Frozen contracts

- Owner decisions, ratified 2026-11-11: `warn` keeps empty-parent nulls and emits one coded warning;
  `preserve` and `remap` keep nulls silently; infeasible positive bounds refuse; YAML uses
  `anchor_via`, `from_table`, `via` and no dotted anchor; bounded-repair output changes once on both
  v2 and V1 paths, with every re-pin named in E1's report.
- Reason registry: additive codes only, all with `since=UNRELEASED`. Slices do not change
  `REGISTRY_VERSION`; release stamping owns that change after the required REG-RELEASE prerequisite.
- Generate option names: additive. No existing key is renamed or removed.
- Output bytes: unchanged outside the named intentional-change cells (AT-1). Each changed fixture or
  snapshot is listed by path, config name, route and reason. Bounded-reference fill changes land
  once in E1 on the v2 and V1 `ColumnGenerator` paths.
  Pinned fingerprints intentionally change when a matching relationship policy becomes an input;
  non-empty Arrow bytes remain fixed.
- Pinned plan v1 gains optional `reference_policies`; absent reads as empty, present entries decode
  strictly, and the field enters the plan fingerprint but never the per-column seed fingerprint.
- `orphan_policy` Literal (`config/_relationships.py:21`): unchanged values; generate now honors
  `fail` and `warn`; fitted empty pools retain their existing refusal under every policy.
- Generate input descriptors: `OptionSpec.ref_kind`, `ref_context_key` and `ref_phase` are the
  single registry for same-table, cross-table and post-fill inputs. No parallel edge table may be
  added.
- Parent lookup: null FK returns null; missing non-null FK refuses with
  `runtime.parent_lookup_key_missing`; present-null selected value remains distinguishable; Arrow
  type is preserved. GEN-INHERIT consumes this exact contract.

## Placement

| Change | File | CODEMAP "Where Things Go" row | Existing neighbor |
|---|---|---|---|
| Shared capacity predicate implementation | new `src/decoy_engine/_capacity.py`; compatibility re-export in `generation/pool/_capacity.py` | Foundation | `src/decoy_engine/_reference_date.py` private leaf |
| Relational config checks only | new `src/decoy_engine/config/_generate_relational.py`; public engine-internal exports from `config/__init__.py` | Config models | `config/_generate_unique.py` (`Problem` shape) |
| One relationship-policy normalizer and safe errors | `relationships/_graph.py`, `relationships/__init__.py` | existing relationship package owner | `check_orphan_fk_policy_completeness` parser |
| New options, all input descriptors and their public iterator | `config/_generate_options.py`, `config/__init__.py` | Config models | `OptionSpec`, windowed_date and derived_aggregate entries |
| One strict-schema input resolver | `plan/checks/_generate_columns.py`; trim target-existence checks in `plan/_checks_windowed_date.py`, `plan/_checks_derived_aggregate.py` | Plan compile and checks | existing registry-driven forward check |
| Self-edge skip and relational validator call | `config/_pipeline.py` (`_reference_graph_valid`) | Config models | cycle DFS |
| Relational package surface | new `generation/relational/__init__.py` | Generation, amended to name `generation/relational/` | `generation/pool/` named sub-package precedent |
| Repair, pool check and explicit postcondition | new `generation/relational/_cardinality.py` | Generation relational sub-package | `_synthesize_derived._reference` |
| Self-reference tree draw | new `generation/relational/_self_reference.py` | Generation relational sub-package | reference dispatch in `synthesize.py` |
| Typed `anchor_via` and GEN-INHERIT lookup | new `generation/relational/_parent_lookup.py` | Generation relational sub-package | `_windowed_date_generate` |
| Per-parent aggregate post-pass | new `generation/relational/_aggregate.py` | Generation relational sub-package | `synthesize._generate_tables_from_config` |
| CODEMAP placement declaration | `CODEMAP.md` Generation row | N/A; this edit makes that row place the new sub-package | current Generation row |
| Runtime reference draw, self-edge skip and warning callback | `generation/_synthesize_derived.py`, `generation/synthesize.py` | Generation | `_reference`, `generation_order` |
| Streaming checks and `anchor_via` decline | `generation/_column_streams.py`, `generation/_admission.py` | Generation | `ReferenceStream`, `classify_windowed_date` |
| Pinned refusals and compiled policy | `generation/_pinned_plan.py`, `generation/_pinned_compile.py`, `generation/_pinned_execute.py` | Generation | `numeric_types` optional-field precedent; `_generate_column` call |
| Window generation and coded primitive failures | `generation/_grouped_windowed_generators.py`, `transforms/windowed_date.py` | Generation; Transforms | existing shared window helper |
| Resolved inputs and reproducibility | `generation/synthesize.py`, `generation/_reproducibility.py` | Generation | current generation-order and sibling scans |
| Probe loosening | `execution/_probe_scale.py` | Pipeline spine and routes | `downscale_config` |
| Reason codes | existing `reasons/config/_part2.py`, `reasons/runtime/_part2.py`, `reasons/plan/_part3.py`, `reasons/warning/_part1.py` | Reason codes | existing generate codes |
| Docs and release fragments | `docs/strategies.md`, `docs/capability-matrix.md`, `docs/reasons.md`, new `changelog.d/gen-rel-e<N>.md` | Plans/records docs; Changelog fragments | current generation docs and fragment README |
| Plan filename correction | rename `docs/plans/gen-rel.md` to `docs/plans/2026-11-11-gen-rel.md` in E1 and update GEN-INHERIT's link | Plans | CODEMAP row 255 date rule; current file is the source artifact |
| Config and relationship tests | new `tests/unit/config/test_generate_relational.py`; edit `tests/unit/relationships/test_orphan_fk_policy_dedup.py`, `test_multi_parent_fk.py` | Unit tests | existing relationship parser tests |
| Strict-schema tests | edit `tests/unit/plan/checks/test_generate_columns.py` | Unit tests | existing `ref_kind` forward-reference cells |
| Cardinality tests | new `tests/unit/generation/relational/test_cardinality.py` | Unit tests | mirrors `generation/relational/_cardinality.py` |
| Tree tests | new `tests/unit/generation/relational/test_self_reference.py` | Unit tests | mirrors `generation/relational/_self_reference.py` |
| Parent lookup tests | new `tests/unit/generation/relational/test_parent_lookup.py` | Unit tests | mirrors `generation/relational/_parent_lookup.py` |
| Aggregate tests | new `tests/unit/generation/relational/test_aggregate.py` | Unit tests | mirrors `generation/relational/_aggregate.py` |
| Route acceptance tests | new `tests/unit/generation/relational/test_routes.py` | Unit tests | nested beside the new relational source package |
| Pool choke-point sentry | new `tests/sentry/defects/test_reference_pool_choke_point.py` | Sentry test | recurring-defect sentries |
| Option-input sentry | new `tests/sentry/defects/test_generate_input_edges.py` | Sentry test | existing `test_generate_option_reads.py` registry walk |
| New foundation leaf registration | `.importlinter`, `tests/sentry/layout/test_import_layers.py` | Foundation; Sentry test | exhaustive root-module tier and snapshot |

Against `9878d17e`, `generation/` has 25 direct non-`__init__` Python modules, so all four new
generation modules go in the named `generation/relational/` sub-package. `config/` has 23 direct
non-`__init__` modules and gains only `_generate_relational.py`, reaching 24. `plan/` has 30 direct
non-`__init__` modules and gains none. `tests/unit/generation/` already has 32 direct Python modules,
so every new generation unit or route test goes under `tests/unit/generation/relational/`. The hard
source cap is 700 (`tests/sentry/test_module_size.py`). `generators/columns.py` is 658 lines and
shrinks when the repair moves. `generation/synthesize.py` is 582 lines;
`config/_generate_options.py` is 520; `plan/checks/_generate_columns.py` is 515; and
`relationships/_graph.py` is 467. Each new hand-written module stays below 600 lines. No cap,
fan-out ratchet, allowlist or import-layer ignore rises.

## Slices

| Order | Slice / branch | Source and durable files | Test files and red-first acceptance | Gate |
|---|---|---|---|---|
| 1 | E1 / `feat/gen-rel-e1` | `src/decoy_engine/_capacity.py`; `generation/pool/_capacity.py`; `config/_generate_relational.py`, `config/__init__.py`, `config/_pipeline.py`; `relationships/_graph.py`, `relationships/__init__.py`; `generation/relational/__init__.py`, `generation/relational/_cardinality.py`, `generation/_synthesize_derived.py`, `generation/_column_streams.py`, `generation/_streamed.py`, `generation/synthesize.py`, `generation/_pinned_plan.py`, `generation/_pinned_compile.py`, `generation/_pinned_execute.py`; `generators/columns.py`; `execution/_probe_scale.py`; `reasons/config/_part2.py`, `reasons/runtime/_part2.py`, `reasons/warning/_part1.py`; `.importlinter`; `CODEMAP.md`; `docs/strategies.md`, `docs/capability-matrix.md`, `docs/reasons.md`; `changelog.d/gen-rel-e1.md`; rename `docs/plans/gen-rel.md` to `docs/plans/2026-11-11-gen-rel.md` | new `tests/unit/config/test_generate_relational.py`, `tests/unit/generation/relational/test_cardinality.py`, `tests/unit/generation/relational/test_routes.py`, `tests/sentry/defects/test_reference_pool_choke_point.py`; edit `tests/unit/relationships/test_orphan_fk_policy_dedup.py`, `tests/unit/relationships/test_multi_parent_fk.py`, `tests/sentry/layout/test_import_layers.py`, `tests/unit/generation/pool/test_capacity_null_reusable.py`, `tests/unit/generation/test_pinned_generation.py`, `tests/unit/generation/test_pinned_with_generation.py`, `tests/unit/generation/test_fr0_streamed_entry.py`, `tests/unit/execution/test_probe_scale.py`, `tests/unit/execution/test_probe_scale_kills.py`. Red first: AT-1 to AT-6, AT-6f, AT-6r, AT-13 to AT-15 | Full suite once because reason manifests, snapshots and the plan rename are frozen-contract/moved-test triggers; no registry-version stamp |
| 2 | E2 / `feat/gen-rel-e2` | `config/_generate_options.py`, `config/_generate_relational.py`, `config/_pipeline.py`; `generation/relational/_self_reference.py`, `generation/synthesize.py`, `generation/_admission.py`, `generation/_pinned_compile.py`, `generation/_pinned_execute.py`; `execution/_probe_scale.py`; `reasons/config/_part2.py`, `reasons/plan/_part3.py`, `reasons/runtime/_part2.py`; `docs/strategies.md`, `docs/capability-matrix.md`, `docs/reasons.md`; `changelog.d/gen-rel-e2.md` | edit `tests/unit/config/test_generate_relational.py`, `tests/unit/generation/relational/test_routes.py`, `tests/unit/generation/test_generation_order.py`, `tests/unit/generation/test_pinned_generation.py`, `tests/unit/execution/test_probe_scale.py`; new `tests/unit/generation/relational/test_self_reference.py`. Red first: AT-7 to AT-9, self-reference AT-10, tree AT-16 | Full suite once for reason-manifest changes; no registry-version stamp |
| 3 | E3a / `feat/gen-rel-e3a` | `config/_generate_options.py`, `config/__init__.py`, `config/_generate_relational.py`; `plan/checks/_generate_columns.py`, `plan/_checks_windowed_date.py`; `generation/relational/_parent_lookup.py`, `generation/synthesize.py`, `generation/_grouped_windowed_generators.py`, `generation/_admission.py`, `generation/_pinned_compile.py`, `generation/_pinned_execute.py`, `generation/_reproducibility.py`; `transforms/windowed_date.py`; `reasons/config/_part2.py`, `reasons/runtime/_part2.py`; `docs/strategies.md`, `docs/capability-matrix.md`, `docs/reasons.md`; `changelog.d/gen-rel-e3a.md` | edit `tests/unit/config/test_generate_options.py`, `tests/unit/config/test_generate_relational.py`, `tests/unit/plan/checks/test_generate_columns.py`, `tests/unit/generation/relational/test_routes.py`, `tests/unit/generation/test_fr1_windowed_date.py`, `tests/unit/generation/test_fr0_reproducibility.py`, `tests/unit/generation/test_pinned_generation.py`, `tests/unit/transforms/test_windowed_date.py`; new `tests/unit/generation/relational/test_parent_lookup.py`, `tests/sentry/defects/test_generate_input_edges.py`. Red first: anchor AT-10, AT-11, AT-11b, AT-11c, date AT-14/15/16 | Full suite once for reason-manifest and frozen error-contract changes; no registry-version stamp |
| 4 | E3b / `feat/gen-rel-e3b` | `config/_generate_options.py`, `config/_generate_relational.py`; `plan/checks/_generate_columns.py`, `plan/_checks_derived_aggregate.py`; `generation/relational/_aggregate.py`, `generation/synthesize.py`, `generation/_pinned_compile.py`, `generation/_pinned_execute.py`, `generation/_reproducibility.py`; `reasons/config/_part2.py`; `docs/strategies.md`, `docs/capability-matrix.md`, `docs/reasons.md`; `changelog.d/gen-rel-e3b.md` | edit `tests/unit/config/test_generate_options.py`, `tests/unit/plan/checks/test_generate_columns.py`, `tests/unit/generation/relational/test_routes.py`, `tests/unit/transforms/test_derived_aggregate.py`, `tests/sentry/defects/test_generate_input_edges.py`; new `tests/unit/generation/relational/test_aggregate.py`. Red first: aggregate AT-10, AT-12, aggregate AT-16 | Full suite once for reason-manifest changes; no registry-version stamp |

Each slice keeps AT-1 green and starts from main after the previous one merges.

### Red-first execution per slice

For each slice, commit the named acceptance tests before source. Run the exact test node IDs with
`uv run pytest -q <named files and ids>` and record the expected failing IDs against that slice's
base. A test that unexpectedly passes is either tied to a named mutant and demonstrated red on that
mutant, or the slice stops. Then implement, run T0 after each edit and T1 on the final head. T1 is
the union of changed tests, every importer/symbol hit, the two new sentries, existing
`test_generate_option_reads.py`, import-layer, module-size, reason-registry, pinned, streamed and
snapshot tests. Run this for T1:

```bash
scripts/dev/gates.sh --steps ruff,format,mypy,sentry,layers,licenses,tests <selected-test-paths>
```

That script owns the exact ruff paths, mypy scope and import-linter environment. Record every named
mutation on the final source head; any later source edit invalidates the mutation evidence.

- **E1 red-first order:** (1) relationship validation/compile gate; (2) AT-6r central parser and
  safe-message cells; (3) AT-1 move-only byte witness; (4) AT-2 to AT-5 bounds, list/dict keys and
  capacity-layer witness; (5) AT-6 and AT-6f policies, fitted precedence and warning shape;
  (6) AT-13 to AT-15 route, reason and value-safety cells. Move the repair with AT-1 green before
  changing its fill. The slice report enumerates every re-pin before gate.
- **E2 red-first order:** AT-7 cycle pair, AT-8 tree invariant, AT-9 option matrix, then pinned and
  seed cells of AT-10/16. A two-table cycle must remain red beside the newly accepted self-edge.
- **E3a red-first order:** first the two strict-schema cross-table cells and their descriptor
  mutations; then typed lookup list/dict/null/missing/duplicate cells; then all five coded date
  failures at both supported-range ends; then the same-name anchor, route/refusal and non-vacuous
  two-seed fixture; finally reproducibility, reason registry and sentries.
- **E3b red-first order:** strict-schema accepts the child-table column and rejects a missing one;
  then every aggregate op and childless identity; then read-before-fill readers, placeholder-return,
  reproducibility and pinned cells.

## Engineering calls made

These follow standard behavior or ordering and are not owner questions:

- `root_count` defaults to 1, a single tree (the usual org-chart shape).
- `anchor_via`, per-parent aggregates and GEN-INHERIT use a non-null `sequence` parent key in this
  slice. Extending the shared predicate to other provably unique generated keys is a follow-up, even
  though GEN-UNIQUE is present on main.
- Childless parents: 0 for `sum` and `count`, `None` for `mean`, `min` and `max` (SQL `LEFT JOIN`
  with `COALESCE` on the additive ops only).

## V2-LOG

- The existing fitted empty-pool path raises `plan.reference_parent_pool_empty` during generation.
  The phase mismatch is pre-existing and the owner explicitly froze that code's precedence for
  fitted pools. GEN-REL does not copy it.
- The unchanged `PinnedPlan.from_dict` reader coerces several stored fields and does not reject an
  unknown shape. New orphan-policy metadata is decoded strictly before execute; wider reader cleanup
  remains a separate V2 pass.
- The parent-lookup choke-point sentry covers realistic dict/index construction over a `pools[...]`
  column. Hostile in-process indirection and provable completeness for every spelling remain V2-LOG
  under the standing sentry rule.

## Gate-ready completion check

The remote builder is done only when every slice has its recorded fail-before IDs, all named
acceptance cells green, final-head mutations red, T1 evidence, the one trigger-bound full-suite
baseline comparison, and an exact list of changed snapshots and fingerprints. Review checks that
`OptionSpec.ref_kind`, relationship-policy normalization, the capacity predicate, the warning
emitter, `value_key` and `_parent_lookup.py` remain their single named choke points. Any new parallel
table, validator, parser, logger or lookup is a change-created DESIGN failure.
