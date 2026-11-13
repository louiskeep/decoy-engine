# GEN-INHERIT E2 build evidence

Status: built on `feat/gen-inherit-e2`. No census number, threshold, pin, baseline or allowlist
rose. No existing digest, golden or snapshot moved, and no expected output value was edited.

Spec: `docs/plans/2026-11-10-gen-inherit.md` on `origin/plan/gen-inherit`, slice row E2 (the
conditional categorical: `condition_on` plus `by:` on a `categorical` generate column). The plan
stays on the plan branch.

## Heads and environment

- Slice start: `feat/gen-rel-e3b` at `16b2fcde` (slice 4; it holds GEN-REL E1 to E3b and
  `origin/main` `c0961a17`). Main is not merged into this branch.
- Commits: `6f4a19ae` the `synthesize.py` move (refactor), `6dd3ed1b` the red acceptance tests
  (source as at `6f4a19ae`), `386d9a08` the #461 selector generalization (refactor), `6d0297cf`
  the feature. Final source head: `6d0297cf`. This record is added in a docs-only commit after
  it.
- Gates: Python 3.10.20 (temporary venv under the session scratch directory, synced with
  `uv sync --frozen --python 3.10 --extra dev --extra lint --extra vault --extra cloud` as CI
  does; removed after; the existing `.venv` was not changed), PyArrow 24.0.0, DuckDB 1.5.4.
  The `layers` step ran in `.venv-layers`.
- Move run, red-first run, sweeps and mutations: Python 3.11.18 (the `.venv`), PyArrow 24.0.0,
  DuckDB 1.5.4.

## Entry condition (plan Prerequisites)

Confirmed on `16b2fcde` before the first edit:

- `config/_generate_relational.py::iter_generate_input_refs` reads
  `GENERATE_OPTIONS.get(<type>)` and loops `for key, option in spec.keys.items()`, skipping only
  `option.ref_kind is None`; it holds no option name (the resolver's literal set is pinned by
  `tests/sentry/defects/test_generate_input_edges.py::test_the_resolver_holds_no_option_name_and_no_column_type_list`).
- The one strict checker, `plan/checks/_generate_columns.py::_check_input_edges`, iterates
  `iter_generate_input_refs(tables[table], col)` and refuses a not-ready edge with
  `plan.generate_column_forward_reference`. No hard-coded key list exists.
- The seeded guard: `test_generate_input_edges.py` seeds a hard-coded input-option table beside
  the resolver and a second resolver (`test_meta_an_option_name_in_the_resolver_or_a_table_beside_it_is_seen`,
  `test_meta_a_second_resolver_or_table_is_seen`) and requires the scan to see each; numeric
  typing's `READERS` is the one reader-safety policy (`test_numeric_reader_registry.py`).

So registering `categorical.condition_on` with `ref_kind="column"` in the registry is enough for
the forward-reference refusal, the reproducibility closure and numeric typing: the feature adds
no edge code (AT-2 forward-reference cell, the reproducibility cell and AT-11 show it).

## Commit 1: the `synthesize.py` move (`6f4a19ae`)

`generation/synthesize.py` was at exactly 600 lines. Per the owner ruling the first commit moves
code out of it, into existing modules, with no ceiling rise and no census entry. Two cohesive
blocks of function bodies moved; no top-level definition left `synthesize.py`:

- The per-table fidelity warn-gate block of `_generate_tables_from_config` (the
  "any statistical column" test, the lazy `_fidelity_gate` import and the
  `warn_on_low_fidelity` call) became `warn_on_table_fidelity` in
  `generation/_statistical_column.py`, the module that already carries the `statistical`
  branch and `warn_on_unseen_parents` out of `synthesize.py`.
- The `grouped_series` / `windowed_date` / `group_key` dispatch inside `_generate_column` became
  `grouped_windowed_values` in `generation/_grouped_windowed_generators.py`, the module of the
  three generators it dispatches to (still imported lazily).

`synthesize.py`: 600 to 581 lines at the move, 597 at the final head. Every name tests patch or
import through `synthesize` is still defined there (`_generate_column`, `_categorical`,
`_faker`, `_sequence`, `_topo_sort`, `_apply_null_probability`, `_get_default_faker`,
`generation_order`, and the bindings `_reference`, `_formula`, `_derived_generate`,
`_faker_pool`, `random`, `get_faker_providers`, `generate_derived_aggregate_column`,
`generate_tables`).

Why not the preferred shape (moving the `_categorical` draw into `generation/kinds/`): three
existing guards tie the categorical draw to `synthesize.py`.

1. `tests/fixtures/h1/definition_owners.json` lists `_categorical` and `_topo_sort` (and every
   other top-level definition of `synthesize.py`) under `stays`, which
   `tests/unit/test_wave_b_definition_owners.py` enforces ("names under `stays` never leave
   their file"). Moving any of them means editing that frozen ownership record.
2. The draw-site catalogue records `gen.categorical` at `generation/synthesize.py`
   (`execution/native/_draw_sites_gen.py`), the static scan
   `tests/native/test_draw_site_inventory_coverage.py::test_static_scan_finds_no_uncatalogued_draw_site`
   refuses an uncatalogued `random.Random(` / `.choices(` file, and
   `tests/native/test_draw_sites_catalogue_pinned.py` pins the catalogue's digest. Moving the
   draw needs an `execution/` edit (not pre-approved; seam sentry) and a re-pin.
3. The Phase F witness (`tests/parity/_f0_witness.py`) records categorical draws by patching
   `synthesize.random`.

So the RNG and the per-row draw stay at the catalogued site in `synthesize._categorical`, as the
plan's Placement row says ("`generation/synthesize.py` `_categorical` (+ about 15 lines)"), and
everything else of the conditional form (the tables, the parent fetch and its refusal) is in the
new `generation/kinds/_categorical.py`, which draws nothing and holds no draw token.

Green run of the move (Python 3.11, 4 workers, the move applied, before the red tests):
`tests/unit/generation tests/parity tests/native/test_determinism_goldens.py
tests/native/test_draw_site_inventory_coverage.py tests/native/test_draw_sites_catalogue_pinned.py
tests/unit/test_wave_b_definition_owners.py tests/unit/test_check_pure_move_scope.py
tests/unit/test_v2_generation.py tests/integration/golden tests/unit/transforms
tests/sentry/test_module_size.py tests/sentry/defects`: 10510 passed, 1261 skipped, 37 xfailed,
1 failed. The one failure,
`tests/native/test_draw_site_inventory_coverage.py::test_static_scan_finds_no_uncatalogued_draw_site`,
fails identically on `16b2fcde` (checked with the move stashed): it names
`generation/relational/_cardinality.py` and `generation/relational/_self_reference.py`, added by
GEN-REL slices 1 and 2. See "Pre-existing failure on the stack".

## Red first (commit `6dd3ed1b`, source as at `6f4a19ae`)

`.venv/bin/python -m pytest -q -p no:cacheprovider -p no:xdist
tests/unit/generation/test_conditional_categorical.py tests/unit/config/test_generate_options.py
tests/unit/generation/relational/test_aggregate_input.py --tb=line -rf`: 46 failed, 267 passed,
no collection error.

| File | Red |
| --- | --- |
| `tests/unit/generation/test_conditional_categorical.py` (new) | 17 |
| `tests/unit/config/test_generate_options.py` | 28 |
| `tests/unit/generation/relational/test_aggregate_input.py` | 1 |

Reasons:

- Absent API: `runtime.categorical_condition_column_unavailable` (`KeyError` from
  `reasons.get`); the registry has no `condition_on` / `by` on `categorical`
  (`test_condition_on_and_by_are_categorical_options`,
  `test_the_registered_inputs_are_exactly_these`).
- Wrong outcome, config: every `by` / `condition_on` cell is refused with
  `generate_option_unknown` at `("by",)` or `("condition_on",)` instead of its own code and
  nested location (the pairing, mapping, key, bool-key hint, nested-entry, weights and echo
  cells); `unique: true` with `condition_on` gives `generate_option_unknown`, not
  `generate_unique_conflict`; the aggregate-input cell is refused for the unknown keys.
- Wrong outcome, plan and run: plan compile ignores the keys, so a later or missing
  `condition_on` and mixed entry categories compile (`DID NOT RAISE PlanCompileError`); every
  row draws from the top-level mix (`'basic' in {'gold', 'silver'}` fails, the AT-10 rows are
  all `fb`, the north rows of the number cell are `{1, 2, 3}`, the uniform-entry cell draws the
  top-level categories); no fallback warning (`not enough values to unpack`); streamed
  admission admits the column (`'eligible'`); numeric typing keeps a sequence parent `int64`
  under a categorical reader, and a text entry beside number categories is typed `int64`; a
  missing conditioning column does not raise (`DID NOT RAISE GenerationError`, resident and
  pinned); a column conditioned on a `determinism: fresh` parent is not excluded from
  reproducibility; a pinned `windowed_date` anchored on a conditional column whose entry holds a
  non-absolute date compiles.

Green on the base by design (15 new cells), each tied to a mutation shown red below:

| Cell | Why green on the base | Mutation |
| --- | --- | --- |
| `test_at9_entries_equal_to_the_top_mix_draw_one_choices_stream[0..9]` (10) | entries equal to the top mix: the plain draw already gives `choices(cats, weights, k=n)` with the column's own seed | M3 (draw `k=n` once per parent value), M9 (reseed per parent value) |
| `test_at11_plain_categorical_types_are_unchanged[...]` (5) | plain typing is what the base does; AT-11's baseline cells | M7 (route every categorical through the conditional union) for the int, text, bool and float cells; M22 (a mixed int/float list typed `float64`) for the `[1, 2.5]` cell |

Test changes after the red commit (none weakened):

- `test_a_conditional_categorical_child_is_judged_by_every_category_it_draws` expected the
  refusal at the child column's position; the aggregate-input refusal is located at the
  aggregate column (`tables.0.generate_columns.2.column`), as every other cell of that file
  says. The location was corrected; the code is unchanged.

## Mutations (final source head `6d0297cf`, detached worktree, 22 of 22 red)

Each was applied once in a detached worktree of `6d0297cf` that imports its own `src`
(`PYTHONPATH=<worktree>/src`, checked by printing `decoy_engine.__file__`), its named tests run
(Python 3.11.18, the `.venv`), and the file restored; every anchor matched exactly once; the
worktree was clean at the end and is removed. Statuses are read from the harness's results JSON:
22 entries, 22 `RED`. No witness test starts a child process, so no mutation was applied in the
checkout.

| Id | Mutation | File | Red tests |
| --- | --- | --- | --- |
| M1 | skip the nested entry key check | `config/_generate_type_checks.py` | 3: `test_a_bad_entry_is_refused_at_its_nested_location[entry0-where0]`, `test_a_bad_entry_is_refused_at_its_nested_location[entry1-where1]`, `test_a_by_key_is_never_echoed` |
| M2 | drop the warn_on_unseen_parents call | `generation/synthesize.py` | 7: `test_at8_unseen_parents_fall_back_and_warn_once_without_values`, `test_at3b_default_snapshot_warns_once_with_the_independent_count`, `test_at3s_integer_parent_labels_count_the_same` ... |
| M3 | draw k=n once per parent value | `generation/synthesize.py` | 10: `test_at9_entries_equal_to_the_top_mix_draw_one_choices_stream[0]`, `test_at9_entries_equal_to_the_top_mix_draw_one_choices_stream[1]`, `test_at9_entries_equal_to_the_top_mix_draw_one_choices_stream[2]` ... |
| M4 | add condition_on and by to _EXCLUDED_FROM_FINGERPRINT | `generators/derivation.py` | 4: `test_at2b_full_snapshot_has_no_absent_pair_and_no_warning`, `test_at3b_default_snapshot_warns_once_with_the_independent_count`, `test_at7_the_sink_does_not_change_the_bytes` ... |
| M5 | key parents by repr | `generation/statistical/_sample.py` | 1: `test_at10_int_bool_null_and_text_parents_match_their_str_keys` |
| M6 | register the categorical.condition_on reader as numeric-safe | `generation/_numeric_typing.py` | 1: `test_at11_a_sequence_parent_is_typed_as_under_a_fitted_condition_on` |
| M7 | route every categorical through the conditional union | `config/_generate_type_checks.py` | 4: `test_at11_plain_categorical_types_are_unchanged[categories0-want0]`, `test_at11_plain_categorical_types_are_unchanged[categories1-want1]`, `test_at11_plain_categorical_types_are_unchanged[categories2-want2]` ... |
| M8 | a missing conditioning column falls back to an empty parent list | `generation/kinds/_categorical.py` | 2: `test_at15_resident_refuses_a_missing_conditioning_column`, `test_at15_pinned_refuses_a_missing_conditioning_column` |
| M9 | reseed per parent value | `generation/synthesize.py` | 10: `test_at9_entries_equal_to_the_top_mix_draw_one_choices_stream[0]`, `test_at9_entries_equal_to_the_top_mix_draw_one_choices_stream[1]`, `test_at9_entries_equal_to_the_top_mix_draw_one_choices_stream[2]` ... |
| M10 | drop data on the pinned call | `generation/_pinned_execute.py` | 2: `test_at7_resident_and_pinned_draw_the_same_rows_with_fallbacks`, `test_at8_pinned_draws_the_fallback_rows_without_a_warning` |
| M11 | quote the parent values in the log line | `generation/_statistical_column.py` | 1: `test_at8_unseen_parents_fall_back_and_warn_once_without_values` |
| M12 | change select_table's keying (non-text parents as JSON) | `generation/statistical/_sample.py` | 1: `test_at10_int_bool_null_and_text_parents_match_their_str_keys` |
| M13 | rename a warning detail key | `generation/_statistical_column.py` | 5: `test_at8_unseen_parents_fall_back_and_warn_once_without_values`, `test_at3b_default_snapshot_warns_once_with_the_independent_count`, `test_at8_every_route_carries_exactly_one_record[admitted-mixed]` ... |
| M14 | omit the runtime code from the fragment | `changelog.d/gen-inherit-e2.md` | 1: `test_every_live_pending_identity_is_named_by_a_fragment` |
| M15 | streaming admits the conditional form | `generation/_admission.py` | 2: `test_at7_resident_and_pinned_draw_the_same_rows_with_fallbacks`, `test_at7_streamed_admission_declines_the_conditional_form` |
| M16 | unique with condition_on is accepted | `config/_generate_unique.py` | 1: `test_unique_with_condition_on_is_a_conflict` |
| M17 | condition_on and by need not be given together | `config/_generate_options.py` | 2: `test_by_and_condition_on_are_given_together[by-condition_on]`, `test_by_and_condition_on_are_given_together[condition_on-by]` |
| M18 | the mixed-category check reads the top-level list only | `plan/checks/_generate_columns.py` | 1: `test_at2_mixed_category_types_across_entries_are_refused_once` |
| M19 | the pinned anchor check reads the top-level categories only | `generation/_pinned_compile.py` | 1: `test_a_pinned_date_anchored_on_a_conditional_column_checks_every_entry` |
| M20 | a by key that is not text is accepted | `config/_generate_type_checks.py` | 6: `test_a_key_that_is_not_text_is_refused_at_its_position_only[7731]`, `test_a_key_that_is_not_text_is_refused_at_its_position_only[1.5]`, `test_a_key_that_is_not_text_is_refused_at_its_position_only[True]` ... |
| M21 | conditional typing reads the top-level categories only | `config/_generate_type_checks.py` | 2: `test_at11_a_conditional_column_is_typed_by_every_category_it_can_draw`, `test_a_conditional_categorical_child_is_judged_by_every_category_it_draws` |
| M22 | a mixed int and float category list is typed float64 | `config/_generate_type_checks.py` | 1: `test_at11_plain_categorical_types_are_unchanged[categories4-None]` |

M1 to M12 are the slice's named mutations (plan AT-2, AT-8, AT-9, AT-10, AT-11, AT-15 and the
class table, in the order of the build brief); M13 and M14 are the class table's frozen-contract
mutations (rename a detail key; omit the runtime code from the manifest, which for this registry
regime is the fragment the minor-fixture sentry reads); M15 to M22 cover the streamed decline,
the unique conflict, the pairing rule, the mixed-category union, the pinned anchor union, the
key type rule, the conditional typing union and the AT-11 mixed baseline cell.

## What the slice does

- Registry (`config/_generate_options.py`): `categorical` takes `condition_on`
  (`OptionSpec(_str, ref_kind="column")`) and `by` (`OptionSpec(by_entries)`).
  `TypeOptions.together` (new, default empty) names keys given all or none; `categorical` sets
  `together=("condition_on", "by")`, and `option_problems` reports `missing` on the absent one.
  The JSON schema branch gets `dependentRequired` for the pair. A check may now return a third,
  fixed hint item that `problem_details` already appends after the summary.
- Checks (`config/_generate_type_checks.py`, where GEN-TYPES put the value checks): the
  `weights` rule (`_weight_ok`, now `weights_list`) moved here from `_generate_options.py`
  unchanged, so the `by` check reuses it without an import cycle. `by_entries`: a mapping of
  text keys (a non-text key is `generate_option_type` at `("by", <position>)` with a hint to
  quote it, saying `"True"` / `"False"`); each entry `weights`, or `categories` (a non-empty
  list) with optional `weights`; other keys `generate_option_unknown`, neither key or a non-mapping
  entry `generate_option_type`; entry weights by the `weights` rule against the entry's
  categories, else the top-level ones. `categorical_categories(col)`: the top-level list for a
  plain column, and the top-level list plus every entry's for a conditional one;
  `categorical_arrow_type` types a conditional column by that union and a plain one exactly as
  before.
- `config/_generate_unique.py::column_conflict_problems`: `unique: true` with `condition_on` is
  `generate_unique_conflict`.
- Plan check (`plan/checks/_generate_columns.py::_check_categories`, GEN-STRICT's one
  mixed-category check): runs over `categorical_categories(col)`. The forward-reference refusal
  needed no code.
- Draw (`generation/synthesize.py::_categorical`): a plain column is unchanged byte for byte.
  With `condition_on`, after the same seed and `random.Random`, it fetches the parent values
  (`kinds.condition_values`), builds the tables once (`kinds.conditional_tables`: categories and
  `itertools.accumulate` cumulative weights, None for an unweighted entry) and makes one
  `rng.choices(population, cum_weights=...)` call per row on the table
  `statistical.select_table` picks. `_generate_column` passes `generated` to it (resident and
  pinned both pass `data`).
- `generation/kinds/_categorical.py` (new, 72 lines): the tables and the parent fetch;
  `condition_values` raises `GenerationError(runtime.categorical_condition_column_unavailable)`
  when `generated` is None or lacks the column, before any row is selected; the message is the
  summary plus table, column and the option name. Raised outside any `except`.
- #461 generalization (`386d9a08`, its own commit): `_select_table` is now `select_table`, typed
  over any `(values, weights)` table; `fallback_counts(conditional, parent_values, n)` holds the
  counting loop over plain tables, and `conditional_fallback_counts(spec, ...)` calls it with the
  spec's tables. `select_table`, `fallback_counts`, `FallbackCounts` and `DISTINCT_PARENT_CAP` are
  exported from `generation.statistical`. #461's tests and statistical goldens pass unchanged.
- Warning (`generation/_statistical_column.py::warn_on_unseen_parents`): for a categorical column
  with `condition_on` it counts with `fallback_counts` over the same tables and emits through
  `emit_generation_warning` with `provider: "categorical"` and the unchanged detail keys. It is
  called only from the resident loop (pinned unchanged, no warning).
- Admission (`generation/_admission.py`): a categorical with `condition_on` is declined
  (`categorical_condition_on`) before `categorical_arrow_type` runs.
- Numeric typing (`generation/_numeric_typing.py`): `READERS["categorical.condition_on"] =
  Reader(False, "matched against the declared by keys as text")`. No option-key table.
- Pinned compile (`generation/_pinned_compile.py`): the `windowed_date` absolute-anchor check
  reads `categorical_categories(anchor)`, so an entry category cannot bypass it.
- Reason (`reasons/runtime/_part2.py`): `CATEGORICAL_CONDITION_COLUMN_UNAVAILABLE`,
  `since=UNRELEASED`, in the `GenerationError` catalog.
- `_EXCLUDED_FROM_FINGERPRINT` is unchanged (M4 shows the statistical goldens would move).

## Slice 4 interaction (aggregate input)

`numeric_as_text_read` reaches `categorical_arrow_type` through `generated_arrow_type`, so a
per-parent aggregate over a conditional categorical child is judged by every category it can
draw: number categories in every entry are accepted and aggregate to numbers; a text entry
beside number categories is refused with `config.generate_aggregate_input_not_numeric` at
validate (before the plan's mixed-category refusal). New cell
`test_a_conditional_categorical_child_is_judged_by_every_category_it_draws` in
`tests/unit/generation/relational/test_aggregate_input.py`; the existing verdict-consistency
cells pass unchanged.

## Plan notes and deviations

1. **Reason regime (plan and handoff disagree with the owner ruling).** The plan says E2 "takes
   the next registry minor at merge" and the fragment is `### Added`. Per REG-RELEASE the one new
   code is `since=UNRELEASED`; `REGISTRY_VERSION`, the minor fixtures, `consumer-contract.json`,
   the release pin and the docs version sentence are untouched; the one fragment
   `changelog.d/gen-inherit-e2.md` (`### Added (...)` per `changelog.d/README.md`) names the code
   as `runtime/categorical_condition_column_unavailable`.
2. **The draw stays in `synthesize.py`; the move took two other blocks.** See "Commit 1". The
   owner's preferred shape (move `_categorical` into a sub-package) conflicts with the frozen
   definition-owner record, the draw-site catalogue and its pinned digest (an `execution/` edit)
   and the Phase F witness. The plan's own Placement row keeps the draw in `_categorical`.
3. **Order of commits.** The handoff's "first commit = the red tests" is replaced by the owner
   ruling: the move first (green run recorded), then the red tests recorded on the post-move
   head, then the #461 generalization, then the feature.
4. **`by` locations use the entry's position, not its key.** The plan writes
   `("by", <key>, ...)`; a `by` key is a parent value, and the brief forbids values in
   messages. The bool-key rule already says "names the key position only"; every `by` location
   uses the position for consistency (a nested option name such as `bogus` is still named, as a
   top-level unknown key is).
5. **The pairing rule is `TypeOptions.together`.** The plan says "the conditional-required table
   GEN-TYPES adds, or the inline precedent"; `CONDITIONAL_REQUIRED` keys on fixed selector
   values and cannot say "given with". `together` is one generic field read by
   `option_problems` and the schema hook; the public catalog
   (`GenerateTypeOptions`) is unchanged and lists both keys as optional.
6. **Empty entry categories are refused.** The plan lists the entry checks without the empty
   case; `choices([])` would raise `IndexError` per row, so `categories: []` in an entry is
   `generate_option_type`. The top-level `categories: []` rule is unchanged.
7. **Streamed decline reason.** The plan quotes the matrix tuple `(False, "reads a sibling; no
   sibling-input seam yet")`; the matrix row for `categorical` stays admitted (a plain column
   streams), so the decline is a per-column reason in the classifier,
   `categorical_condition_on`, with that rationale as its comment, like
   `windowed_date_anchor_via`.
8. **`select_table` rename.** The plan names `_select_table`; it is now public as
   `select_table` because `generation/kinds` imports it through the `generation.statistical`
   package exports. The keying (`str(parent)`) is unchanged.
9. **Pinned anchor check.** Not in the plan's list: `_pinned_compile.py`'s absolute-anchor check
   read only the top-level categories, so an entry could carry a relative date into pinned mode.
   It now reads `categorical_categories`; cell
   `test_a_pinned_date_anchored_on_a_conditional_column_checks_every_entry`, mutation M19.
10. **Execution files.** E2 needs no `execution/` change: it adds no type and no draw site
    (`gen.categorical` covers it). The catalogue's `call_shape` text for `gen.categorical`
    ("single whole-column call") does not describe the conditional form's per-row call; see
    V2-LOG.

## Files outside the plan's E2 list

- `generation/_statistical_column.py` and `generation/_grouped_windowed_generators.py` (the
  move; `_statistical_column.py` is also the plan's warning row).
- `generation/kinds/__init__.py`, `generation/statistical/__init__.py`, `config/__init__.py`
  (package exports), `config/_generate_option_catalog.py` (the schema's `dependentRequired`),
  `generation/pool/_events.py` (docstring of the warning code), `generation/_pinned_compile.py`
  (note 9).
- `tests/sentry/defects/test_generate_option_reads.py`: adds
  `generation/kinds/_categorical.py` to `READER_FILES` (it reads `by`).
- `tests/unit/generation/relational/test_aggregate_input.py` (slice 4 interaction).
- `CODEMAP.md` (the `kinds/` row), `docs/strategies.md`, `docs/reasons.md`, this record.

`docs/capability-matrix.md` is unchanged: `scripts/gen_capability_matrix.py` writes the same
file (no new strategy). `config/` keeps 25 direct modules; `generation/` gains no direct module
(`_categorical.py` is in `generation/kinds/`, now 5 modules).

## New reason code

| Identity | Raised as | Phase |
| --- | --- | --- |
| `runtime.categorical_condition_column_unavailable` | `GenerationError` | generate |

Reused: `warning.generation_condition_parent_unseen` (detail keys unchanged, new provider value
`categorical`), `config.generate_option_type`, `config.generate_option_unknown`,
`config.generate_weights_invalid`, `config.generate_unique_conflict`,
`config.generate_aggregate_input_not_numeric`, `plan.generate_column_forward_reference`,
`plan.generate_categories_mixed`, `plan.pinned_windowed_anchor_not_absolute`.

## Pre-existing tests whose expectation changed

| Test | Before | Now | Why |
| --- | --- | --- | --- |
| `tests/unit/config/test_generate_options.py::test_the_registered_inputs_are_exactly_these` | no `categorical.condition_on` | `("column", None, "inline")` | the plan's registration |
| `tests/sentry/defects/test_generate_option_reads.py` | 21 reader files | 22, adding `generation/kinds/_categorical.py` | `by` is read there |

No masked-output test changed and no expected output value was edited.

## Line counts

Touched source files, final head (slice start in brackets): `generation/synthesize.py` 597
(600; 581 after the move), `generation/statistical/_sample.py` 559 (547),
`config/_generate_options.py` 549 (571), `reasons/runtime/_part2.py` 526 (520),
`plan/checks/_generate_columns.py` 513 (507), `config/_generate_type_checks.py` 411 (291),
`generation/_pinned_compile.py` 403 (399), `generation/_numeric_typing.py` 327 (326),
`generation/_admission.py` 295 (292), `config/_generate_unique.py` 291 (288),
`generation/_grouped_windowed_generators.py` 230 (208), `generation/_statistical_column.py` 217
(168), `config/__init__.py` 200 (198), `config/_generate_option_catalog.py` 125 (123),
`generation/kinds/__init__.py` 75 (66), `generation/kinds/_categorical.py` 72 (new),
`generation/statistical/__init__.py` 65 (54), `generation/pool/_events.py` 65 (64). At or above
550: `synthesize.py` 597, `_sample.py` 559. Every file is under the 600 goal; no census entry was
added or raised. New test file: `tests/unit/generation/test_conditional_categorical.py` 379.

## Gates

`GATES_WORKERS=4 scripts/dev/gates.sh --keep-going --baseline --steps
ruff,format,mypy,sentry,layers,licenses,tests <the T1 paths below>` on `6d0297cf` (the final
source head), Python 3.10.20 (temporary venv, `DECOY_VENV`), with `origin/main` fetched at
`dac26109`. Exit 0.

| Step | Branch (`6d0297cf`) |
| --- | --- |
| ruff | pass |
| format | pass |
| mypy | pass |
| sentry | 5339 passed, 0 failed, 2 skipped |
| layers | 15 passed, 0 failed, 0 skipped |
| licenses | 63 passed, 0 failed, 0 skipped |
| tests | 11269 passed, 0 failed, 201 skipped (parallel and serial passes) |

- Failure sets: branch 0, main 0, branch-only 0. Every branch step passed, so the gate script
  did not rerun `origin/main`. There is no branch failure.
- No existing digest, golden or snapshot moved, and no expected output value was edited: the
  statistical `condition_on` digests (`test_condition_fallback.py`), the determinism and
  draw-site goldens, the pinned draw-site catalogue, the Phase F oracle and witnesses, the
  GEN-REL near-neighbor digests and the GEN-UNIQUE old-input bytes are in the T1 set and pass
  unchanged.
- Checks on `6d0297cf` (same venv), by exit code:
  - `registry_renumber.py --check --main c0961a17`: 0 (`registry consistent`).
  - `registry_renumber.py --check --main origin/main`: 2 (`main-not-merged`; `origin/main` is
    `dac26109`, past `c0961a17`; main is merged only through slice 1).
  - `ratchet_check.py --base c0961a17`: 0.
  - `ratchet_check.py --base origin/main`: 1, the same two findings as slices 3 and 4
    (`f29-producer-inventory`
    `execution/out_of_core/_reorder_budget.py::require_disk::kw::'out_of_core_reorder_budget_too_small'`;
    `secret-material-census` `decoy_engine/vault.py::vault_writer_for_config::job_seed`), keys
    main removed from its own baselines; this branch touches neither file. The pattern is
    unchanged.
  - `check_fragment.py --base origin/main`: 0.
  - `reason_release_pin.py --check`: 0 (`reason release pin current`).
  - `gen_capability_matrix.py`: no change.
- Before the gate, a sweep (Python 3.11, 4 workers) of `tests/sentry` (without the import-layer
  test, which needs import-linter), `tests/unit/config`, `tests/unit/plan`,
  `tests/unit/generation`, `tests/unit/reasons`, the reason, public-API, definition-owner and
  v2-generation tests, `tests/parity`, `tests/integration/golden` and the draw-site scan on the
  uncommitted feature found the doc-table cell (`docs/strategies.md`), the fragment (not yet
  written), an ad-hoc code site (the code passed through a local; now the registry symbol at the
  raise) and the pre-existing draw-site scan failure.

## Pre-existing failure on the stack

`tests/native/test_draw_site_inventory_coverage.py::test_static_scan_finds_no_uncatalogued_draw_site`
fails on `16b2fcde` (and on this branch, unchanged): the static RNG scan finds
`generation/relational/_cardinality.py` and `generation/relational/_self_reference.py` (GEN-REL
slices 1 and 2) in neither the draw-site catalogue nor the scan's allowlist. It is not in slice
4's 80-path T1 list and not in this one; a full suite on any of the stacked heads will show it.
Fixing it needs either a catalogue entry in `execution/native/_draw_sites_gen.py` (an
`execution/` edit plus a re-pin of `tests/native/test_draw_sites_catalogue_pinned.py`) or an
allowlist entry, neither of which this slice may make. Not touched here.

## T1 paths (102: slice 4's 80 and 22 more)

```
tests/acceptance/test_f06_route_cells.py
tests/acceptance/test_f06_route_parity.py
tests/fileaccess/test_char_error_text.py
tests/fileaccess/test_x2_locators.py
tests/fileaccess/test_x3_classification.py
tests/generation
tests/integration/compat_corpus/test_compat_corpus.py
tests/integration/golden
tests/integration/golden/test_determinism_invariants.py
tests/integration/golden/test_fidelity_report_golden.py
tests/native/test_c8_iii_d2_native_positional_when.py
tests/native/test_capabilities.py
tests/native/test_chunked_categorical_admission.py
tests/native/test_chunked_entry_rev9_read.py
tests/native/test_chunked_entry_side_channels.py
tests/native/test_chunked_nondet_categorical_admission.py
tests/native/test_column_access_surfaces.py
tests/native/test_determinism_goldens.py
tests/native/test_determinism_protocol.py
tests/native/test_draw_site_providers_golden.py
tests/native/test_draw_sites_catalogue_pinned.py
tests/native/test_pooled_faker_draw_sites.py
tests/native/test_unconfigured_passthrough_read.py
tests/parity/test_numeric_typing_fk.py
tests/parity/test_numeric_typing_readers.py
tests/parity/test_phase_d_d1b2_categorical_disposition.py
tests/parity/test_phase_d_d2c_ii_cat_seq.py
tests/parity/test_phase_f_audit_matrix.py
tests/parity/test_phase_f_audit_no_legacy.py
tests/parity/test_phase_f_audit_vacuity.py
tests/parity/test_phase_f_audit_zero_companion.py
tests/parity/test_phase_f_f0_comparator.py
tests/parity/test_phase_f_f0_composite_repeatability.py
tests/parity/test_phase_f_f0_invariants.py
tests/parity/test_phase_f_f0_matrix.py
tests/parity/test_phase_f_f0_oracle.py
tests/parity/test_phase_f_f0_witness.py
tests/parity/test_phase_f_f2_mixed_admission.py
tests/parity/test_phase_f_f2_mixed_backend_witness.py
tests/parity/test_phase_f_f2_mixed_parity.py
tests/parity/test_phase_f_f2_mixed_publish.py
tests/parity/test_phase_f_f2_mixed_sweep.py
tests/parity/test_phase_f_fr0_mixed_stitch.py
tests/parity/test_phase_f_fr0_refreeze.py
tests/parity/test_phase_f_fr0_route_independence.py
tests/parity/test_phase_f_fr0_witness.py
tests/physical/test_characterization_synthesis.py
tests/physical/test_shadow_full_frame.py
tests/physical/test_shadow_generation.py
tests/physical/test_shadow_generation_faker.py
tests/physical/test_unified_slice_positional.py
tests/secret_providers/test_keyed_columns.py
tests/unit/config
tests/unit/determinism/test_derive_vectors.py
tests/unit/execution/test_c8_iii_c_plan_when.py
tests/unit/execution/test_categorical_weighted.py
tests/unit/execution/test_created_target_contract.py
tests/unit/execution/test_dgrn_windowed_date.py
tests/unit/execution/test_group_key_chunked.py
tests/unit/execution/test_multi_table_gates.py
tests/unit/execution/test_ooc_cross_row_decline_message.py
tests/unit/execution/test_pipeline_generation_contract.py
tests/unit/execution/test_probe.py
tests/unit/execution/test_probe_scale.py
tests/unit/execution/test_probe_scale_kills.py
tests/unit/execution/test_r3_failure_publication_trace.py
tests/unit/execution/test_r3_public_signature_freeze.py
tests/unit/execution/test_r3_route_executors.py
tests/unit/execution/test_r3_within_route_baseline.py
tests/unit/execution/test_technique_class.py
tests/unit/execution/test_text_mask_chunked.py
tests/unit/expressions
tests/unit/generation
tests/unit/generation/test_generate_determinism_ratchets.py
tests/unit/generation/unique/test_gen_unique_old_input_bytes.py
tests/unit/generators
tests/unit/plan
tests/unit/profiling/test_snapshot_cell_format.py
tests/unit/profiling/test_snapshot_values.py
tests/unit/profiling/test_snapshot_values_numeric_shape.py
tests/unit/providers_v2/test_distribution_behavior_metadata.py
tests/unit/quality/test_dp_flag_e2e.py
tests/unit/quality/test_retention_gate.py
tests/unit/quality/test_snapshot_fanout.py
tests/unit/reasons
tests/unit/relationships
tests/unit/run_result
tests/unit/scripts
tests/unit/strategy_catalog
tests/unit/test_check_pure_move_scope.py
tests/unit/test_de02_keyprovider.py
tests/unit/test_numpy_generic_timedelta.py
tests/unit/test_public_api.py
tests/unit/test_public_api_v2_choke_points.py
tests/unit/test_r3_10_generation_key_contract.py
tests/unit/test_reason_manifest_policy.py
tests/unit/test_reason_registry.py
tests/unit/test_reason_release_pin.py
tests/unit/test_registry_renumber.py
tests/unit/test_v2_generation.py
tests/unit/test_wave_b_definition_owners.py
tests/unit/transforms
```

Added to slice 4's list: the test files outside it that import or name a module this slice
changes (`synthesize`, `_statistical_column`, `kinds`, `statistical`, `_admission`, pinned,
`_numeric_typing`, `_generate_options`, `_generate_type_checks`, the option catalog and
`categorical_arrow_type`), found by a grep of `tests/` outside `tests/sentry`, plus
`tests/unit/test_check_pure_move_scope.py` (its module-shape cell parses `synthesize.py`).
Every test file the slice adds or changes is inside `tests/unit/config`,
`tests/unit/generation` or `tests/sentry`. The `sentry` step runs all of `tests/sentry`; the
`layers` step runs `tests/sentry/layout/test_import_layers.py`.
`tests/native/test_draw_site_inventory_coverage.py` is not in the list: its scan cell fails on
the slice start for files of GEN-REL slices 1 and 2 (above); its other 11 cells pass on this
head.

## Record commit

This record is added in a docs-only commit on top of `6d0297cf`. On that commit the `ruff`,
`format` and `sentry` gate steps were run again (results in the slice report). The `tests`,
`mypy`, `layers` and `licenses` steps were not rerun: the commit changes no Python file.

## Not run

- The full suite; `tests/unit/execution`, `tests/parity`, `tests/native`, `tests/physical`,
  `tests/integration` beyond the listed paths; `tests/perf`; `testflight`.
- The gate steps under Python 3.11 (the move run, red-first run, sweep and mutations used 3.11).
  A rerun of `origin/main` where no branch step failed. Gates against a branch holding the
  current `origin/main`.
- The full gate on the docs-only commit that adds this record (only `ruff`, `format` and
  `sentry` were rerun on it).

## V2-LOG

- Pinned mode raises no fallback warning for either provider (`statistical`, `categorical`), as
  the plan records; a pinned `on_warning` through `emit_generation_warning` is V2.
- A raw config that skips `PipelineConfig` validation reaches the run without the `by` checks: a
  non-text key never matches (rows fall back and warn), an entry whose weights do not pair with
  its categories raises `ValueError` from `random.choices` uncoded. Plan compile still refuses a
  forward or missing `condition_on` and mixed categories, and the run refuses a missing parent
  column with the new code.
- The draw-site catalogue's `gen.categorical` `call_shape` ("single whole-column call") and
  `partitionable=False` describe the plain column; the conditional form makes one call per row on
  the same stream (same draw count). Updating the text is an `execution/` edit and a catalogue
  re-pin.
- `generation/_bounds.py::categorical_payload_bound` reads the top-level categories only; it
  feeds the streaming route, which declines a conditional column, so no bound is used for one.
- `GenerateTypeOptions` (the public catalog) has no field for `together`; the catalog lists
  `condition_on` and `by` as optional, and the pairing is visible only in the JSON schema
  (`dependentRequired`) and the refusals.
- `generation/synthesize.py` is at 597 of 600 lines. GEN-INHERIT E1 adds an `inherit` dispatch;
  it will need another move first (the definition-owner record freezes every top-level
  definition of `synthesize.py`, so only function-body blocks can move without editing it).
- The pre-existing draw-site scan failure above.
