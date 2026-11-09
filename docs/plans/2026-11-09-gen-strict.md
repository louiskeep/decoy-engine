Status: plan

# GEN-STRICT: generate columns are checked per type at validate (roadmap row 24k)

Owner: Cam. Author: claudeworkerbee (Opus). Date: 2026-11-09. Scope: engine + CLI, V1 (owner decision
2026-11-08). Revision 7.

Rules consulted: `dev-rules/README.md`, `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`
(PLAN: choke point, Placement, Design notes, Established solutions), `feature-dev.md`, `architecture.md`,
`reuse-first.md`, `testing.md` (test depth, escalation triggers), `cross-cutting-concerns.md`,
`api-and-compatibility.md`, `code-review.md`, `workspace-and-vcs.md`, `scope-discipline.md`,
`verification.md`, `documentation.md`, `catalogs/recurring-defects.md`; the plan contract
(`30_Resources/Decoy-Plan-Contract-and-Defect-Catalog.md`), `LOCAL-BUILDER-BRIEF.md`; the standing rules
develop-correctly-registries-and-sizing and best-practice-becomes-a-sentry; the V1 bar (function break, data
loss or a non-admin value leak blocks; a second copy of a central mechanism, or a new module in a flat folder
over 25, always blocks); engine `CLAUDE.md` (cite the established method; validation never mutates; land the
assertion test first; modules aim for 600 lines, hard max 700, census above 600); engine `CODEMAP.md:219`
(new plan checks go in `plan/checks/`, the target subpackage); engine `docs/reasons.md` (additions raise the
registry minor, one fixture per minor); the value-free error contract (engine #412: `ConfigProblem`,
`ConfigRuleError`, `parse_pipeline_config`, `tests/sentry/defects/test_validation_sites.py` and the
shrink-only `scripts/dev/ratchet_check.py`). Code read at engine `main` `046887e9` and CLI `main` `d73328c4`.

Source: CLI repo `docs/records/2026-11-08-cli-qa-generation.md` High findings H3 and H4, and the roadmap row:

- H3, accepted silently at exit 0: `faker_type: not_a_provider` gives random words; `weigths: [0.99, 0.01]` is
  ignored (the schema says `additionalProperties: true`); `weights: [-1, 2]` makes every value the second
  category; `locale: xx_YY` gives English; `faker_kwargs: {nope: 1}` is ignored; `null_probability: 2` is
  accepted; `step: 0` makes every id 1; two columns with one name drop one; a `formula` with a syntax error, a
  missing reference, no `references:` list or the `{a} * 2` form gives a blank column.
- H4, `validate` OK and `run` fails with raw library text and no column name: weights summing to zero,
  mixed-type categories, `group_by` equal to `order_by`, a `derived` reference to a column declared later.
  `row_count: 5000000000` passes validate, then ends with an empty `error: ` and exit 3.
- Row 24k: all of the above refused at `decoy validate` with exit 1, naming the column; the over-capacity
  `row_count` fails at validate and run with exit 5.

Terms: an **option** is any key on a generate column other than `name` and `type`. The **option registry** is
the one table, per generator type, of the options that type reads and what a valid value is. A **floor** is a
byte count the run must allocate at least, whatever the values turn out to be.

## 0. Review dispositions

Revision 2 folds in Dennis round 1 (GO-WITH-FIXES, 0 blocker, 5 high, 6 medium, 4 low).

| Finding | Disposition |
|---|---|
| H1 `_categorical_type` is a streaming-eligibility rule, not a validity rule: `[1, 2.5]` and `["a", None]` run today on the legacy route (`pa.table` infers, `synthesize.py:175`) | Accepted. The predicate is not moved. The validity rule is Arrow's own inference: the plan check calls `pa.array(categories)`, the same inference `pa.table` applies at run, and refuses only when it raises. It runs in `plan/checks/` (pyarrow stays out of `config/`, which also closes L2). AT-2 has accept cells for `[1, 2.5]` and `["a", None]` and a property: every list admission accepts is accepted |
| H2 calling each `from_dict` at validate raises the engine `ValidationError` the CLI does not catch, carries `provider_config.*` paths and value-quoting messages, and repeats checks `_checks_derived_aggregate.py:106-116` and `_checks_windowed_date.py:108-118` already own | Accepted. E2 step zero tabulates every `from_dict` rule against the check that already owns it; only uncovered rules run. Each call is wrapped: the wrapper records the original code inside `except`, then raises a `PlanCompileError` with that code (or `plan.generate_option_invalid` when the original has none), the generate-column path and a fixed message, after the `except` block (catalog #3: no value-bearing `__context__`). AT-7 canary cells cover each translated rule |
| H3 the floor compared to `resolve_budget(None)`, which is 25% of memory (`_budget.py:137`, `:383`), refuses runs that fit; a bounded sink streams (`_streamed.py:361`), so output is not resident there | Accepted. The floor is compared with `detect_effective_memory_bytes()` (`_budget.py:339`), the whole ceiling, and applies only when the output is resident (`sink is None` or not a bounded sink). AT-9 cells for both |
| H4 second copy of generate-column pricing beside `_mem_estimate_schema.py:211-275`, and a second CLI capacity entry beside `estimate_job_capacity` (`capacity.py:208`) | Accepted. No new SDK export. `estimate_job_capacity` gains the generate floor and returns its existing `CapacityEstimate` (`out_of_core/_capacity_eval.py:131`) with verdict `INSUFFICIENT` and code `generation_budget_exceeded`; the CLI preflight already calls it. Per-column floor bytes come from `ColumnAdmission.arrow_type`, the type the run builds; `_mem_estimate_schema` stays an advisory upper estimate for the out-of-core route, and folding its `_GENERATE_NUMERIC_DTYPE` onto the admission type is F5 |
| H5 the faker kwargs signature read is inline in `_make_reflected_provider` (`faker_setup.py:416-448`); "extract if inline" left a second copy possible | Accepted. Extraction is mandatory in E2: one function in `faker_setup.py` used by the reflected provider and the plan check. `faker_setup.py` (668) is in E2's file list and must not grow (the extraction is net 0 or smaller) |
| M1 two call sites, not three (`compile_plan` `:211-224`; `run_config_only_checks` `:539-551`); the wrapper left three ref checks behind | Accepted. The wrapper owns all five ref checks (derived, derived_aggregate, grouped_series, windowed_date, group_key) at both sites; `_compile.py` drops eight call lines and their comments. AT-7 runs through `run_config_only_checks` and `compile_plan` |
| M2 `locale` can be a list (`faker_setup.py:640`, `_column_streams.py:156-162`) | Accepted. str or non-empty list of str; each must be in `AVAILABLE_LOCALES` |
| M3 comprehension variables; `len({a, b})` | Accepted. Names bound by a comprehension target are excluded. The `{a}` rule refuses only a one-element set display holding a bare sibling column name that is an operand of an arithmetic `BinOp` or the whole expression |
| M4 AT-8 can not see E1 codes through `compile_plan` (it takes the dump, `execution/_pipeline.py:161`); AT-9(a) oracle; AT-1 location form | Accepted. AT-8 asserts config codes at `parse_pipeline_config` and the CLI gate, plan codes at `run_config_only_checks` and `compile_plan`. AT-9(a) compares with measured allocation (tracemalloc plus `pa.total_allocated_bytes()`). Locations use the `_validation.py:202-209` form `tables[0].generate_columns[0].weigths` |
| M5 AT-6 scan missed `generation/statistical/_spec.py` (`col_cfg`) | Accepted; added with `col_cfg` |
| M6 the empty `error: ` (an untexted `MemoryError`) remains for runs that pass the floor | Accepted. C1 maps a bare `MemoryError` in `run.py` to fixed text and exit 5 |
| L1 plan load does not re-validate (`_serialize.py:429-446` uses `json.loads`) | Fixed in 5.1 |
| L2 pyarrow in `config/` | Closed by H1 |
| L3 a `sequence` parent with `null_probability > 0` yields fewer distinct keys (`synthesize.py:600-605`) | Accepted. The length check applies only when the parent has no `null_probability` or it is 0 |
| L4 each schema `then` must list `name`, `type`, `pooled` and the common keys | Accepted; stated in section 3 and AT-5 |
| Q6 E3 T1 | Added `tests/integration` and the determinism goldens |

Revision 3 folds in Dennis round 2 (GO-WITH-FIXES, 0 blocker, 1 high, 2 medium, 5 low).

| Finding | Disposition |
|---|---|
| N1 `detect_effective_memory_bytes()` raises `out_of_core_memory_detection_failed` on a host with no sysconf and no `/proc/meminfo` (`_budget.py:202-229`), so every resident generate run would fail there; `estimate_job_capacity` must return `UNKNOWN` for an unpriceable ceiling | Accepted. `run_generation` catches that code and skips the floor (the floor only adds a refusal, so skipping fails open to today's behavior); `estimate_job_capacity` returns `UNKNOWN`. AT-9(h) |
| N2 the five ref checks also cover masking columns (`plan/_checks.py:633-641` and siblings) | Accepted. The wrapper calls the five once on the whole config before any per-table loop, with or without generate tables. AT-7 mask-only cell |
| N3 the engine `ValidationError.code` is not a reason code (`errors.py:71-90`) and `compile_expr` raises with none (`_lark_parser.py:113-179`); `from_dict` runs all its rules, so "not re-run" can not hold | Accepted. The original code passes through only when `reasons.is_registered` (`reasons/_registry.py:130`) knows it, else `plan.generate_option_invalid`. Owned rules refuse first because step 1 runs before step 3; AT-7 has a bad `op` and a bad `distribution` cell returning the owner's code and path |
| N4 CLI `_check_capacity` returns before `estimate_job_capacity` when a source is not a file or is missing; `preflight.py:39-40` is docstring text; the `INSUFFICIENT` text says "full-frame recovery" | Accepted. Guarantee 5 and 5.2 state the gap (run still exits 5 through the run handler); C1 picks the message by `code` and updates the docstring |
| N5 `estimate_job_capacity(budget_bytes=...)` is an explicit ceiling override | Accepted. The floor compares with it when given |
| N6 `list_generate_faker_providers` builds a Faker per call (`faker_setup.py:636`) | Accepted. The wrapper caches the provider list per locale for the call; `locale` is checked before `faker_type` |
| N7 F5 missing from section 14 | Added |
| N8 stale round 1 text; three location forms | Fixed in section 13. Location forms are stated per surface in Guarantee 1 |

Revision 4 folds in Codex round 1 (NO-GO, 4 V1, 3 V2-LOG).

| Finding | Disposition |
|---|---|
| V1-1 a plan declined to the legacy route ignores the sink and materializes every table (`_streamed.py:339`, `:487`), so the bounded-sink exemption let it skip the floor | Accepted. The floor is skipped only when `admission.eligible and is_bounded_sink(sink)`. AT-9(e) splits into an eligible streaming cell (floor not called) and a legacy cell with a bounded sink (floor called) |
| V1-2 the five existing ref checks interpolate invalid `op`, `distribution` and `generator` values into messages the CLI prints (`_checks_derived_aggregate.py:106`, `_checks_windowed_date.py:110`, `_checks_grouped_series.py:117`) | Accepted. Step 1 runs inside the same translation as step 3: the wrapper keeps the registered code and path and replaces the message with fixed text naming table, column and key, raised after the `except` block. Mask-column messages become value-free too; existing tests that assert the old text are updated in E2. AT-7 canaries for all three |
| V1-3 a boolean "is a reference" flag compares a `derived.expression` such as `"a + 1"` literally with column names | Accepted. `OptionSpec.ref_kind` is one of `column`, `column_list` or `expression` (the registry stays free of heavy imports); the plan check extracts `expression` refs with `compile_expr` plus `_get_column_refs` (`expressions/_lark_parser.py:74`, `transforms/derived.py:188`), as `check_derived_column_refs` does (`plan/_checks.py:608`). AT-7 cells: earlier refs, several refs, a literal-only expression |
| V1-4 new test modules land in flat folders over 25 (engine `tests/sentry/` 58, `tests/unit/plan/` 26; CLI `tests/e2e/` 81) | Accepted. The AT-6 sentry goes to `tests/sentry/defects/` (CODEMAP `:235`, the recurring-defect class folder) and is added to `tests/sentry/test_sentry_manifest.py`. The plan-check tests go to `tests/unit/plan/checks/`, mirroring the source subpackage. CLI tests go to a new named folder `tests/e2e/generate_config/` with a CLI CODEMAP row, as `cli_fmt/` and `private_file/` did. C1 splits into C1 and C2 to stay under ten files |
| V2 AT-6 readers missing | Taken: `_grouped_windowed_generators.py`, `_referenced_formula.py`, `_faker_pool.py`, `_window_admission.py`, `_pinned_compile.py` added |
| V2 AT-9(a) summing two independent peaks overstates | Taken: each part of the floor is compared with its own measure (legacy list slots with the `tracemalloc` peak, Arrow buffers with the `pa.total_allocated_bytes()` peak) |
| V2 E3 changes a public capacity result | Taken: E3 runs the full engine suite (frozen contract trigger) |

Revision 5 folds in Codex round 2 (NO-GO: round 1 fixes confirmed in the body; 3 new V1, 2 V2-LOG).

| Finding | Disposition |
|---|---|
| R2-1 a formula `references` entry naming no sibling (`formula: "ghost + 1"`, `references: ["ghost"]`) passes every check and the post-pass emits an all-null column (`_formula.py:138`) | Accepted. Step 5 refuses every `references[i]` that is not a sibling generate column, at the indexed path, with the new code `plan.generate_formula_reference_unknown` (any position: the post-pass fills formulas after every other column). AT-7 and AT-C1 cells |
| R2-2 `statistical.condition_on` must name a column generated earlier (`_statistical_column.py:61`) and was not in the reference set | Accepted. `condition_on` gets `ref_kind="column"` and joins the step-zero run-time list; AT-7 cells for earlier (accepted), missing and later (refused) through both sites |
| R2-3 `bit_width / 8` yields `0.125` for a boolean column, a float in the integer `needed_bytes` contract (`_capacity_eval.py:135`, `:148`) | Accepted. Each fixed-width column floor is `(row_count * bit_width + 7) // 8`, an integer; AT-9(i) asserts an integer `needed_bytes` for a boolean categorical table |
| R2 V2-LOG AT-6 relies on a fixed list of mapping variable names | Logged; the list is the reader census of section 3 and a new reader adds itself there |
| R2 V2-LOG E2p files not enumerated | Logged; E2p is created only if the in-place updates push E2 past ten files, and then lists them |

Revision 6 folds in Codex round 3 (NO-GO, 1 V1; the round 2 fixes confirmed).

| Finding | Disposition |
|---|---|
| R3-1 the post-pass fills referenced formulas in one declared-order pass (`_referenced_formula.py:30-35`), so a formula that references itself, or a later formula column that itself carries `references`, reads a `None` placeholder; "any position" let both validate | Accepted. Step 5 refuses an actual formula read of the column itself or of a reference-bearing formula column declared at or after it, with `plan.generate_column_forward_reference` at the matching `...references.<i>`. Any other sibling is accepted at any position (non-formula columns and formulas without `references` are final before the post-pass). Revision 7 narrows "reference" to an actual expression read so unused declarations remain compatible. AT-7 cells: used self/later dependencies (refused), unused declarations and earlier/later-final dependencies (accepted with output pinned) |

Revision 7 folds in the Codex revision-6 self-review (GO-WITH-FIXES, 1 V1; R3-1 otherwise confirmed).

| Finding | Disposition |
|---|---|
| V1 R4-1 revision 6 refused every self or later-reference-bearing entry in `references`, even when the formula never reads that name; today the post-pass evaluates such a formula correctly because the placeholder is unused, so the new rule would reject a valid config and break Guarantee 6 | Accepted. Step 5 intersects `references` with the formula's unbound `ast.Name` reads and applies the dependency-order refusal only to that intersection. Unknown sibling declarations are still refused. AT-7 accepts unused self and later-reference-bearing declarations and pins their generated output, while retaining refusals for actual self and later-reference-bearing reads |

## 1. FRAME

### Problem

`GenerateColumnConfig` (`config/_tables.py:238-370`) declares `name`, `type` and `pooled` and carries every
other key through `extra="allow"` (`:255`). Generators read options with `col.get("<key>", default)`, so a
misspelt key, a wrong type or an out-of-range value is either ignored, replaced by a default, or crashes deep in
a library:

| Input | Where it goes wrong today |
|---|---|
| unknown key (`weigths`) | carried by `extra="allow"`; nothing reads it |
| `faker_type` not a provider | falls back to `word` (`synthesize.py:463`, `_column_streams.py:171`) |
| `locale` unknown | falls back to `en_US` (`internal/faker_setup.py:657-668`) |
| `faker_kwargs` key unknown | filtered out (`faker_setup.py:416-448`) |
| `null_probability` outside 0 to 1 | not validated; read at `synthesize.py:488`, `_column_streams.py:289` |
| `step: 0` | not validated (`synthesize.py:333`) |
| negative or all-zero weights | `random.choices` raises or skews (`synthesize.py:395`, `_column_streams.py:139`; reference `:626`) |
| mixed-type categories | admission declines (`_admission.py:93-111`), then `pa.table` fails (`synthesize.py:175`) |
| reference `distribution: bogus` | random (`synthesize.py:626-630`) |
| reference `weighted` with no `weights` | uniform (`synthesize.py:621-625`) |
| duplicate generate column names | admission declines (`_admission.py:253`), then `synthesize.py:149` overwrites |
| `group_by` equal to `order_by` | pandas raises at `transforms/grouped_series.py:199`, `:226`, `:266` |
| `derived` names a later column | `check_derived_column_refs` (`plan/_checks.py:581`) checks existence only; `KeyError` at `transforms/derived.py:296`, `:317` |
| bad `formula` | every row error is swallowed to `None` (`generators/_formula.py:138-144`, `:179`, `:242-250`) |
| huge `row_count` | no bound; the pipeline calls `generate_tables` with no budget (`execution/_pipeline_generate_mask.py:128`, `_pipeline_mixed_route.py:74`) |

The only per-type checks are two `ConfigRuleError` validators for required keys (`_reference_params_required`
`:296-306`, `_type_params_present` `:308-368`).

### Guarantee (what V1 promises)

1. Every option on a generate column is either known for its type or refused at config parse, with its
   location and a registered reason code. Nothing is ignored. Location forms by surface: the engine
   `ConfigProblem` uses `tables[0].generate_columns[2].weigths` (`safe_output/_validation.py:202-209`); a
   `PlanCompileError` path uses names, `tables.<table>.generate_columns.<column>.<key>`; the CLI prints its
   own dotted index path for config errors (`gate/_layers.py:151-152`), unchanged.
2. Every option value that can be checked without the environment or other columns is checked at config parse:
   type, range, enum, list shape and the categorical and weight rules run would otherwise trip.
3. Every refusal that needs the Faker registry, the locale list, Arrow type inference or sibling columns runs
   in `run_config_only_checks` and in `compile_plan` through one wrapper, so `decoy validate` and `decoy run`
   refuse the same configs, naming table and column.
4. Each refusal message is value-free by construction: it carries the registry summary, the column name and the
   option key, never a category, weight, formula text or other option value.
5. A config whose resident generated tables cannot fit the machine's effective memory, by a floor that holds
   whatever the values are, is refused before any provider runs: at `decoy validate` (preflight) and at
   `decoy run`, both with exit 5. One gap: the CLI preflight returns before the engine estimate when a
   source is not a file or a source file is missing (`_check_capacity`), so such a mixed config gets exit 5
   from the run handler at `decoy run`, not at validate.
6. A config that validates today and is valid under the registry produces byte-identical config dumps, seed
   fingerprints and generated output.

### Non-goals (V1)

- Row-time formula failures that no static check can see (division by zero on some rows) keep today's
  behavior: the row becomes `None` and one warning is logged. Recorded as F1.
- H5, H6 and H7 beyond what row 24k names: per-parent aggregates, `min_per_parent` larger than possible, and
  the fitted fan-out (row 24h) are other rows. GEN-STRICT only adds the H7 cheap parts that the row's "bad
  values refused" covers: an unknown `distribution` value and `weighted` with no `weights`.
- A reference `weights` list whose length differs from a parent pool whose size is only known at run (any
  parent column other than `sequence`). The runtime fallback to uniform stays for those; F2.
- Masking columns (`columns:` with `strategy:`). Their options already sit under `provider_config` with their
  own checks.
- A capacity estimate that predicts every out-of-memory run. The floor refuses only what cannot fit; a run that
  passes the floor can still run out of memory and exit 3. F3 is the bounded generation route.
- New generate types or options (row 24q) and new providers (row 24p). The registry is where they will land.

### Failure modes

| # | Failure | Guard |
|---|---|---|
| FM1 | A key the generators read is missing from the registry, so a valid config is refused | sentry AT-6: every literal `.get("<key>")` on a generate column in the reader files is in the registry for that type; compat corpus sweep AT-3 |
| FM2 | A registry key no generator reads, so a typo-shaped key is accepted | AT-6 runs both directions: registry minus reads must be empty |
| FM3 | The new validator stamps a default or coerces a value into the dump, changing seeds and bytes | the validator reads `model_extra` and never writes it; AT-3 asserts dump and fingerprint identity on the corpus and the determinism goldens |
| FM4 | A refusal message echoes a category, weight or formula | registered `PydanticCustomError` codes with fixed text; AT-1 and AT-2 canary cells; the sentry already scans `ConfigRuleError` sites, and the two old sites are removed |
| FM5 | `validate` and `run` disagree (one wrapper called at one site only) | one wrapper `check_generate_columns`, replacing the five existing generate ref-check calls in `compile_plan` and in `run_config_only_checks`; AT-8 |
| FM6 | The faker check refuses a custom provider the run would accept | the check uses `list_generate_faker_providers(locale)` (`faker_setup.py:609-637`), the same lookup run uses, plus the provider snapshot; AT-7 custom-provider cell |
| FM7 | The capacity floor refuses a run that would fit | the floor counts only buffers the resident output must hold, is compared with the whole effective memory, and is skipped when output streams to a bounded sink (section 3); AT-9 property: floor <= measured allocation |
| FM10 | A translated `from_dict` error carries the original value-bearing message as `__context__` | the wrapper raises after the `except` block with a fixed message; AT-7 canary asserts `exc.__context__ is None` |
| FM8 | A forward-reference refusal for a type whose run tolerates forward references, or for an unused formula declaration that never reads the placeholder | E2 step zero: a run-time red test per reference key; only keys that fail at run today are refused; formula dependency order is checked only for names the expression actually reads (section 3) |
| FM9 | The two removed `ConfigRuleError` validators leave stale `permitted::` keys, or a new site adds one | E1 deletes the 14 keys (shrink is allowed); new sites raise `PydanticCustomError`, never `ConfigRuleError`; the ratchet fails on growth |

## 2. Established solutions

- **Discriminated unions with `extra="forbid"`** (pydantic, JSON Schema `oneOf` with a `const` tag) are the
  usual way to give each variant its own closed key set. A probe on the VM (pydantic 2.13.4) confirmed it
  reports `extra_forbidden` at `('cols', 0, 'faker', 'weigths')` and `union_tag_invalid` for a bad type. It is
  not used here, because every declared field is dumped, and the seed fingerprint
  (`generators/derivation.py`, `_EXCLUDED_FROM_FINGERPRINT` `:55-63`) hashes every dumped key except five. Any
  new declared field changes output bytes for every existing config (FM3). The registry plus one
  `model_validator` gives the same closed key set without changing the dump.
- **Raising several located errors from one validator**: `ValidationError.from_exception_data` with one
  `InitErrorDetails` per problem, each with a `PydanticCustomError` type. The VM probe showed the location
  nests under the column, as `('cols', 0, 'weigths')`, inside a `mode="after"` model validator. The engine
  already raises registered `PydanticCustomError` codes this way (`config/_tables.py:202`).
- **JSON Schema conditional subschemas** (`allOf` of `if`/`then`, draft 2020-12) give per-type
  `additionalProperties: false` for editors and `decoy schema` without changing the model.
- **Faker locales**: `faker.config.AVAILABLE_LOCALES` is Faker's own list; the providers list already exists
  (`list_generate_faker_providers`).
- **Formula parsing**: simpleeval, which runs formulas (`expressions/_safe_eval.py`), parses with Python's
  `ast`. The static check parses with `ast.parse(mode="eval")` and walks `ast.Name` nodes, the same grammar.
- **Memory admission by floor**: the generation ledger already follows DuckDB's buffer manager and Spark's
  `MemoryManager` (reserve before allocating, refuse rather than over-commit; `_budget_ledger.py:1-12`). The
  floor check is the same pattern applied once, before the run, with a lower bound instead of an upper bound.

## 3. Design notes

### Choke points

| Concern | Choke point today | After this plan |
|---|---|---|
| per-column option keys and static values | `GenerateColumnConfig` validators `_reference_params_required`, `_type_params_present` (`_tables.py:296-368`) | one validator `_options_valid` driven by `config/_generate_options.py` |
| duplicate generate column names | none at config; admission declines (`_admission.py:253`), window admission refuses (`_window_admission.py:390-397`) | `TableConfig` validator refuses; the two runtime guards stay as backstops |
| categorical values the run can type | `pa.table` inference at run (`synthesize.py:175`); `_admission._categorical_type` (`_admission.py:93-111`) only picks the route | the plan check calls `pa.array(categories)`, the same inference; the admission predicate is unchanged |
| environment and sibling checks | `compile_plan` (`plan/_compile.py:211-224`) and `run_config_only_checks` (`:539-551`) each call five generate ref checks | one wrapper `check_generate_columns` in `plan/checks/_generate_columns.py` replaces the five calls at both sites and calls them |
| faker kwargs filter | inline signature read in `_make_reflected_provider` (`internal/faker_setup.py:416-448`) | extracted to one named function in the same file, used by the provider and the plan check |
| grouped_series parse rules | `GroupedSeriesConfig.from_dict` (`transforms/grouped_series.py:91`) | gains `group_by != order_by`; the wrapper calls it, translating errors |
| memory before generation | none (`run_generation` `_streamed.py:301-330` is unbounded when `budget_bytes` is `None`); CLI preflight's engine capacity entry `estimate_job_capacity` (`execution/capacity.py:208`) covers the out-of-core route only | `generation_floor_bytes` in `_bounds.py`, checked at the top of `run_generation` and inside `estimate_job_capacity` |

### The option registry (`config/_generate_options.py`)

One frozen mapping `GENERATE_OPTIONS: Mapping[str, TypeOptions]`, keyed by every member of `GENERATE_TYPES`
(`_tables.py:377`). `TypeOptions` holds:

- `keys`: a mapping from option key to an `OptionSpec`: a static check function, and `ref_kind`, which says
  whether the value names columns and how (`None`, `column`, `column_list` or `expression`), for E2. The registry holds no pyarrow or Faker import; anything needing them is a plan
  check.
- `required`: the keys today's validators require (taken one for one from `:296-368`).

Common options, allowed on every type: `null_probability` (number, 0 to 1, not bool), `determinism` (`stable` or
`fresh`; only `fresh` acts, `derivation.py:236`). Engine-internal keys allowed on every type: exactly
`_legacy_column_name_seed` until GEN-C1 removes it (section 11). `name`, `type` and `pooled` are declared fields,
never extras, so the validator does not see them; `pooled` keeps its existing faker-only rule, moved into the
registry pass unchanged.

Per-type keys, from the run-time reads at `046887e9`:

| Type | Options (required in bold) | Static checks |
|---|---|---|
| faker | **`faker_type`**, `locale`, `faker_kwargs` | `faker_type` str; `locale` str or non-empty list of str; `faker_kwargs` a mapping with str keys |
| sequence | **`start`**, `step`, `prefix`, `suffix`, `pad_length` | `start`, `step` int (not bool); `step != 0`; `pad_length` int >= 0; `prefix`, `suffix` str |
| categorical | **`categories`**, `weights` | categories a non-empty list; weights as below, length equal to categories (element typing is plan check 2b) |
| formula | **`formula`**, `references` | `formula` str that parses (`ast.parse(mode="eval")`); `references` a list of str (names and the `{a}` form are plan check 5) |
| reference | **`reference_table`**, **`reference_column`**, `distribution`, `weights`, `min_per_parent`, `max_per_parent` | `distribution` in {`random`, `weighted`, `sequential`}; `weighted` requires `weights`; weights as below; min and max int >= 0 |
| statistical | **`snapshot_file`**, `source_column`, `high_cardinality`, `allow_real_categories`, `other_mode`, `condition_on` | types only; meaning is checked by `check_statistical_columns` as today; `condition_on` has `ref_kind="column"` (step 4) |
| derived | **`expression`**, `bounds`, `null_propagation` | types only; grammar by `from_dict` in E2 |
| derived_aggregate | `op`, `column` | types only; `from_dict` in E2 |
| grouped_series | **`group_by`**, **`order_by`**, `generator`, `step`, `max_step`, `start` | types only; `from_dict` in E2 |
| windowed_date | **`anchor`**, **`max_days`**, `min_days`, `distribution` | types only; `from_dict` in E2 |
| group_key | **`group_by`**, `length`, `prefix` | types only; `from_dict` in E2 |

Weights rule (categorical and reference): a list of int or float (not bool), each >= 0 and finite, sum > 0.

The E1 builder confirms this table against the AT-6 sentry before writing the validator; the sentry, not this
table, is the authority (FM1, FM2).

### The validator

`GenerateColumnConfig._options_valid` (`mode="after"`) replaces `_reference_params_required` and
`_type_params_present`. It collects every problem for the column, then raises one
`ValidationError.from_exception_data` with an `InitErrorDetails` per problem:

- unknown key: `PydanticCustomError("config.generate_option_unknown", <summary>)`, loc `(key,)`;
- missing required key: pydantic's own `missing` type, loc `(key,)` (already value-free in `_FIXED`,
  `safe_output/_validation.py`);
- bad value: one of the codes in section 11, loc `(key,)` or `(key, index)` for a list element.

It reads `self.model_extra` and returns `self` unchanged (validation never mutates). Today an absent
`start: null` counts as missing (`extras.get("start") is None`); the registry keeps that.

`TableConfig` gains a validator: two `generate_columns` with one name raise
`config.generate_column_duplicate` at `("generate_columns", j, "name")` for the second.

The JSON schema hook (`json_schema_extra` on `GenerateColumnConfig`) is generated from the registry: an
`allOf` of `if {type: const}` then `{properties, required, additionalProperties: false}`. Each `then` lists
`name`, `type`, `pooled`, the common options and the internal keys beside the type's own, or
`additionalProperties: false` would reject them.

### Plan-time checks (`plan/checks/_generate_columns.py`)

`check_generate_columns(config, *, provider_snapshot=None)` runs:

1. Once, on the whole config, before any per-table loop and whether or not there are generate tables: the
   five existing ref checks: `check_derived_column_refs`, `check_derived_aggregate_refs`,
   `check_grouped_series_refs`, `check_windowed_date_refs`, `check_group_key_refs`. They also cover masking
   columns (`plan/_checks.py:633-641` and siblings), so a mask-only config keeps every refusal it has today.
   Their messages interpolate the invalid value (`_checks_derived_aggregate.py:106`,
   `_checks_windowed_date.py:110`, `_checks_grouped_series.py:117`), so the wrapper runs them inside the
   same translation as step 3: it keeps the registered code and the path, replaces the message with fixed
   text naming table, column and key, and raises after the `except` block.

Then, per generate table, per column in declared order:

2. faker: first `locale` (or each list entry) in `faker.config.AVAILABLE_LOCALES`
   (`plan.generate_locale_unknown`), so a bad locale never reaches `make_faker`'s fallback warning; then
   `faker_type` in `list_generate_faker_providers(locale)` or the provider snapshot
   (`plan.generate_faker_type_unknown`), with the provider list cached per locale for the call (each call
   builds a Faker, `faker_setup.py:636`); then each `faker_kwargs` key accepted by the provider, through the function E2
   extracts from `faker_setup.py:416-448` (`plan.generate_faker_kwargs_unknown`).
   2b. categorical: `pa.array(categories)` must not raise (`plan.generate_categories_mixed`). This is the
   inference `pa.table` applies at run, so `[1, 2.5]` and `["a", None]` stay valid and `["a", 1]` is refused.
3. transform-backed types: the wrapper calls `DerivedConfig.from_dict`, `DerivedAggregateConfig.from_dict`,
   `GroupedSeriesConfig.from_dict`, `WindowedDateConfig.from_dict` or `GroupKeyConfig.from_dict` (exact
   class names confirmed by the builder). `from_dict` runs all its rules, so rules an existing check owns
   (for example `_checks_derived_aggregate.py:106-116` owns `op`, `_checks_windowed_date.py:108-118` owns
   `distribution`) keep their owner's code and path because step 1 runs first and refuses before step 3 is
   reached. Step zero of E2 tabulates the rules and their owners for the AT-7 cells. The wrapper translates
   any `PlanCompileError`, engine `ValidationError`, `ValueError` or `TypeError`: inside `except` it records
   only the original `code` attribute, and only when `reasons.is_registered` (`reasons/_registry.py:130`)
   knows it (the engine `ValidationError.code` comes from `validation_result.CODES`, `errors.py:71-90`, and
   `compile_expr` raises with none, `_lark_parser.py:113-179`). After the block it raises `PlanCompileError`
   with that code or `plan.generate_option_invalid`, the generate-column path and a fixed message, so no
   value-quoting text survives as message or `__context__` (FM10). `GroupedSeriesConfig.from_dict` gains
   `group_by != order_by` (`plan.grouped_series_group_by_equals_order_by`), so generation and validate share
   the rule.
4. forward references: for each option with a `ref_kind`, the referenced names are taken from the value
   (`column`: the string; `column_list`: each entry; `expression`: `compile_expr` plus `_get_column_refs`,
   `expressions/_lark_parser.py:74`, `transforms/derived.py:188`, as `check_derived_column_refs` does at
   `plan/_checks.py:608`), and each must be a column declared earlier in `generate_columns`
   (`plan.generate_column_forward_reference`). Step zero of E2 writes a
   red run-time test per reference key (`derived.expression`, `derived_aggregate.column`,
   `grouped_series.group_by`/`order_by`, `windowed_date.anchor`, `group_key.group_by`,
   `statistical.condition_on`, which raises `STATISTICAL_CONDITION_COLUMN_UNAVAILABLE` at run when the
   parent comes later, `_statistical_column.py:61`); only keys whose
   forward reference fails at run today are marked (FM8). Formula `references` are filled in a post-pass
   (`_formula.py:101-111`), so they are not forward-checked.
5. formula names: every `ast.Name` in `formula` that is not bound by a comprehension target is in
   `references`, in `FORMULA_SCOPE_NAMES`, or is `i` or `index` (`plan.generate_formula_name_unknown`); a
   name that is a sibling column but not listed in `references` gets
   `plan.generate_formula_reference_missing`; every `references[i]` must name a sibling generate column, else
   `plan.generate_formula_reference_unknown` at `...references.<i>` (today the post-pass logs and emits an
   all-null column, `_formula.py:138`). Let `formula_reads` be the unbound `ast.Name` set already computed
   above, including names that also happen to be in `FORMULA_SCOPE_NAMES`, `i` or `index` when they are
   explicitly present in `references` (the run lets a declared sibling overwrite those scope bindings).
   The post-pass fills reference-bearing formulas in one declared-order pass
   (`_referenced_formula.py:30-35`), so for each entry that is also in `formula_reads`, a reference to the
   column itself, or to a reference-bearing formula column declared at or after it, is refused with
   `plan.generate_column_forward_reference` at the same path. An unused `references` entry is not a data
   dependency and remains legal; rejecting it would refuse a config whose output is valid today. Any other
   sibling is final before the post-pass and is accepted at any position. A one-element set display holding a bare sibling column name,
   used as an operand of an arithmetic `BinOp` or as the whole expression (the `{a} * 2` form), gets
   `plan.generate_formula_template_syntax`; `len({a, b})` and `x in {1, 2}` stay valid. `FORMULA_SCOPE_NAMES` is a new constant in
   `generators/_formula.py` beside `_formula_scope` (`:257`); a test pins it equal to the scope's keys.
6. reference weights against a `sequence` parent: when `distribution: weighted` names a parent column of type
   `sequence`, `len(weights)` must equal the parent's `row_count`
   (`plan.reference_weights_length_mismatch`), unless the parent column sets a `null_probability` above 0,
   which removes keys at run (`synthesize.py:600-605`). Other parents keep the run-time fallback (F2).

Each refusal is a `PlanCompileError` with `path=tables.<t>.generate_columns.<c>.<key>` and a fixed message that
names the column and key only.

### Capacity floor

`generation_floor_bytes(config, admission)` in `generation/_bounds.py` sums, per generate table,
`row_count * per_row_floor` over its columns. The per-column figure comes from `classify_plan`
(`_admission.py:149`), the routing the run itself uses:

- an admitted column with a fixed-width Arrow type: `(row_count * arrow_type.bit_width + 7) // 8` for the
  whole column (integer ceil division, so a boolean column stays an integer byte count);
- an admitted column with a variable-width type: 4 (one int32 offset per row);
- a column routed to the legacy whole-table path: 8 (one pointer per row in the Python list that
  `synthesize` builds for every column before `pa.table`, `synthesize.py:149`, `:175`).

The pipeline holds every generated table resident (`_pipeline_generate_mask.py:122`,
`generate_outputs: dict[str, pa.Table]`, built with `sink=None`), so on that path the sum is a lower bound
on what any route must allocate. Only an admission-eligible plan streaming to a bounded sink
(`_streamed.py:361`, `is_bounded_sink`) holds nothing resident; a plan declined to the legacy route
materializes every table whatever the sink (`_streamed.py:339`, `:487`), so the floor applies to it.

The floor is compared with `detect_effective_memory_bytes()` (`out_of_core/_budget.py:339`: the cgroup limit,
else host RAM), not with `resolve_budget`, whose default is a 25% planning fraction (`_budget.py:137`, `:383`)
that would refuse runs that fit today. Over it, the run raises the existing `GenerationBudgetExceeded`
(`_budget_ledger.py:40`, code `generation_budget_exceeded`) with byte counts and the table name only.

Two callers, one function:

- `run_generation` (`_streamed.py:323`), after `classify_plan` and before any pool or provider, when
  `budget_bytes` is `None`, unless `admission.eligible and is_bounded_sink(sink)`. With a budget set, the ledger's upper-bound
  reservations already refuse first. If memory detection raises `out_of_core_memory_detection_failed`
  (`_budget.py:202-229`, a host with no sysconf and no `/proc/meminfo`), the floor is skipped: it only adds a
  refusal, so skipping keeps today's behavior.
- `estimate_job_capacity` (`execution/capacity.py:208`), the engine capacity entry the CLI preflight already
  calls. For a config with generate tables it checks the floor first, against its `budget_bytes` override
  when one is given and the detected memory otherwise, and returns its existing `CapacityEstimate`
  (`out_of_core/_capacity_eval.py:131`) with verdict `INSUFFICIENT`, code `generation_budget_exceeded` and
  `needed_bytes` / `available_bytes`, before the source profile. When detection fails it returns `UNKNOWN`,
  as its contract says for an unpriceable ceiling. Otherwise behavior is unchanged. No new public function
  is added.

`CAPACITY_REASON_CODES: frozenset[str]` in `decoy_engine.errors` lists `generation_budget_exceeded`,
`out_of_core_insufficient_memory` and `out_of_core_fanin_exceeds_budget`; the CLI stops hardcoding its own set
(`gate/_run.py:31`).

## 4. Behavior

| Input | Today | After |
|---|---|---|
| `weigths: [...]` on categorical | exit 0, ignored | validate exit 1, `config.generate_option_unknown` at `tables[0].generate_columns[2].weigths` |
| `faker_type: not_a_provider` | random words | validate exit 1, `plan.generate_faker_type_unknown` |
| `weights: [-1, 2]` | skewed output | validate exit 1, `config.generate_weights_invalid` at `...weights.0` |
| `weights: [0, 0]` | run exit 3, raw text | validate exit 1, `config.generate_weights_invalid` |
| `locale: xx_YY` | English | validate exit 1, `plan.generate_locale_unknown` |
| `faker_kwargs: {nope: 1}` | ignored | validate exit 1, `plan.generate_faker_kwargs_unknown` |
| `null_probability: 2` | accepted | validate exit 1, `config.generate_option_range` |
| `step: 0` | every id 1 | validate exit 1, `config.generate_option_range` |
| duplicate generate column name | one dropped | validate exit 1, `config.generate_column_duplicate` |
| formula `"a +"` | blank column | validate exit 1, `config.generate_formula_invalid` |
| formula `{a} * 2` | blank column | validate exit 1, `plan.generate_formula_template_syntax` |
| formula names a sibling with no `references` | blank column | validate exit 1, `plan.generate_formula_reference_missing` |
| formula `references: [ghost]`, no such sibling | blank column | validate exit 1, `plan.generate_formula_reference_unknown` |
| formula actually reads itself or a later reference-bearing formula | reads that column's `None` placeholder in the single post-pass | validate exit 1, `plan.generate_column_forward_reference` |
| formula declares but does not read itself or a later reference-bearing formula | runs; the placeholder is unused | runs, byte-identical |
| `statistical` `condition_on` a later column | run exit 3 | validate exit 1, `plan.generate_column_forward_reference` |
| mixed-type categories (`["a", 1]`) | run exit 3, raw text | validate exit 1, `plan.generate_categories_mixed` |
| `categories: [1, 2.5]` or `["a", null]` | runs | runs, unchanged |
| `group_by == order_by` | run exit 3, raw text | validate exit 1, `plan.grouped_series_group_by_equals_order_by` |
| `derived` names a later column | run exit 3, raw text | validate exit 1, `plan.generate_column_forward_reference` |
| `distribution: bogus` | random | validate exit 1, `config.generate_option_value_unknown` |
| `distribution: weighted`, no weights | uniform | validate exit 1, `missing` at `...weights` |
| `row_count: 5000000000` | run exit 3, empty `error: ` | validate exit 5, run exit 5, `generation_budget_exceeded` |
| any config valid today and under the registry | runs | runs, byte-identical |

## 5. Enumerations (from the code at engine `046887e9`, CLI `d73328c4`)

### 5.1 Routes that read generate options

| # | Route | Entry | Covered by |
|---|---|---|---|
| A | config parse | `PipelineConfig.model_validate` (engine `parse_pipeline_config` `config/_parse.py:14-27`; CLI gate `_layers.py:148`; `run` through `check_before_run` `run.py:670`) | E1 validator |
| B | plan load | `_generation_plan_from_dict` (`plan/_serialize.py:429-446`) loads the stored config with `json.loads` and does not re-validate | not re-checked: a stored Plan comes from `compile_plan`, whose callers pass a validated dump (`execution/_pipeline.py:161`), so it passed E1 and E2 when compiled |
| C | config-only checks | `run_config_only_checks` (`plan/_compile.py:484`) from CLI gate `_layers.py:182` | E2 wrapper |
| D | compile | `compile_plan` (`_compile.py:211-224`); `run_config_only_checks` is route C (`:539-551`) | E2 wrapper |
| E | legacy whole-table generation | `_streamed._run_legacy` to `synthesize` | E3 floor at `run_generation`; reads unchanged |
| F | streamed core | `classify_plan` to `_column_streams` | E3 floor; reads unchanged |
| G | windowed `generate_rows` | `_window_admission.py:390-397` | E1 validator on its config; duplicate guard stays |
| H | probe run | `execution/_probe_scale.py:118-120` scales `row_count` | the floor uses the scaled count, so a probe is not refused for the full size |
| I | V1 `ColumnGenerator` (`generators/columns.py`) | no production caller; tests only | untouched |

### 5.2 Exit paths

| Path | Code | Change |
|---|---|---|
| engine config parse | `pydantic.ValidationError`, wrapped as `PipelineConfigError` by `parse_pipeline_config` | new registered codes; the two `ConfigRuleError` sites are removed |
| engine plan checks | `PlanCompileError` | the plan codes in section 11, plus translated `from_dict` codes |
| engine generation | `GenerationBudgetExceeded` (`generation_budget_exceeded`) | now raised by the floor when no budget is set |
| CLI `validate` config layer | 1 (`_layers.py:149-163`, `:183-188`) | more findings, same mapping |
| CLI `validate` preflight | 5 (`gate_exit_code` `_layers.py:338-348`, `capacity_insufficient`) | `_check_capacity` (`:289-291`) already calls `estimate_job_capacity` and maps any `INSUFFICIENT` to `capacity_insufficient`; C1 picks the finding text by `code` (the current text speaks of full-frame recovery) and updates the `preflight.py:39-40` docstring. A config with a non-file or missing source returns before the estimate, so it reaches exit 5 only at run |
| CLI `run` pre-run gate | 1 or 5 (`gate/_run.py:69-99`) | same gate, same codes |
| CLI `run` engine error | 5 when the code is in the capacity set (`run.py:966-975`, `:988-989`), else 3 | the set comes from the engine; the handler also catches `GenerationBudgetError` subclasses by `.code`; a bare `MemoryError` gets fixed text and exit 5 instead of an empty `error: ` |
| success | 0 | unchanged |

### 5.3 FK shapes

- **Reference to a `sequence` parent**: weights length checked against `row_count` (step 6).
- **Reference to any other parent column**: weights shape checked (non-negative, sum > 0); length against the
  pool stays a run-time fallback (F2).
- **Reference to a mask-table parent** (mixed mode, `_pipeline.py:174`): unchanged; no weights length check.
- **Chains, two FKs in one child, self-reference, composite**: unchanged; `_reference_graph_valid`
  (`config/_pipeline.py:174`) still owns the graph.

### 5.4 Fields

- Per-type options: every key in the section 3 table, for its type only.
- Common options on every type: `null_probability`, `determinism`.
- Engine-internal: `_legacy_column_name_seed` (until GEN-C1 removes it).
- Declared fields, not checked by the registry: `name`, `type`, `pooled`.
- No field is added to the dump; the registry is metadata, not model fields.

### 5.5 Output surfaces

| Surface | Change |
|---|---|
| `decoy validate` findings | new codes, located at `tables.<t>.generate_columns.<c>.<key>` |
| `decoy run` errors | the same refusals before any output is written; capacity exit 5 |
| `decoy schema` | generate columns gain per-type `additionalProperties: false` |
| engine `docs/strategies.md` generation sections, CLI `docs/cli-reference.md` | the closed option lists, the formula `references` rule, the `{a}` form removed from any example that shows it |
| generated output | unchanged for every valid config |

## 6. Class table

| # | Class | Applies here | Structure that closes it | Sentry or property test | Mutation that turns it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output | refusal text could echo a category, weight or formula | registered `PydanticCustomError` codes with fixed summaries; `PlanCompileError` messages built from column and key names only; `from_dict` errors translated after the `except` block | AT-1, AT-2, AT-7 canary cells (a category `"CANARY-7731"` never appears in `str(exc)` or `errors()`); `test_validation_sites.py` | M1: put `{value!r}` in the weights message (canary cell red); M1b: raise the translated `from_dict` error inside the `except` block (AT-7 `__context__` cell red) |
| 2 | Cleanup missing on an exit path | N/A: the checks open no file or handle; the floor allocates nothing | N/A | N/A | N/A |
| 3 | State and ordering | validator order (required keys before value checks); referenced-formula declared-order dependencies; floor before any provider call | one validator collects all problems; actual formula reads are checked against post-pass order; floor at the top of `run_generation` | AT-2 multi-error cell; AT-7 used/unused dependency cells; AT-9 provider spy | M3: call the floor after the pools are built (AT-9 spy red); M3b: compare the floor with `resolve_budget` (AT-9(c) red); M3c: order-check every declared formula reference instead of actual reads (AT-7 unused-reference cells red) |
| 4 | Route parity and silent fallback | validate passes what run refuses; a fallback kept silently | one wrapper at both sites; registry enums for `distribution`, `determinism` | AT-8 parity (each H4 repro raises at `run_config_only_checks` and at `compile_plan` with one code) | M4: drop the wrapper call from `run_config_only_checks` (AT-8 red) |
| 5 | Referential integrity | forward references; weights length vs a `sequence` parent | order check from registry ref keys; `row_count` comparison | AT-7 | M5: compare against all columns instead of earlier ones (forward-ref cell red) |
| 6 | Untrusted producer | a hand-written config with wrong types, bools for ints, NaN weights | exact-type checks (`type(v) is int`, not bool), `math.isfinite` | AT-2 type cells | M6: `isinstance(step, int)` (bool cell red) |
| 7 | Check then use on a path (TOCTOU) | N/A: option checks read the config dict only; the floor reads `row_count`, which the run reads from the same dict | N/A | N/A | N/A |
| 8 | Weak tests and overclaimed evidence | a test that only checks "raises" passes for the wrong code or location | every cell asserts code and location; AT-8 asserts run fails today on each repro before the fix (red first) | AT-1, AT-2, AT-8 | M8: raise one generic code for all problems (code assertions red) |
| 9 | Behavior-preserving refactor drift | dump, fingerprint, generated bytes; the extracted faker kwargs filter | validator never writes; the filter is extracted verbatim and the reflected provider calls it | AT-3 corpus and goldens; existing `tests/native/test_determinism_goldens.py`, `tests/parity`, the faker kwargs tests | M9: have the validator set `self.__pydantic_extra__["step"] = 1` when absent (AT-3 red) |
| 10 | Frozen contract drift | reason registry minor; `decoy schema` output; removed `permitted::` keys; CLI capacity mapping | registry minor plus fixture; schema golden re-pinned in E1; baseline shrink | `test_reason_minor_fixtures.py`, AT-5, ratchet | M10: add a code without the fixture (registry test red) |

## 7. Acceptance tests (red first)

Engine E1 (`tests/unit/config/test_generate_options.py`, new):

- **AT-1** Unknown keys, parametrized over all 11 types: a valid minimal column plus `weigths: 1` raises
  `pydantic.ValidationError` with one error, type `config.generate_option_unknown`, loc ending in
  `("generate_columns", 0, "weigths")`. Two unknown keys give two errors. Through `parse_pipeline_config`, the
  `ConfigProblem` has the registry summary as text and the location `tables[0].generate_columns[0].weigths`
  (the `_validation.py:202-209` form).
- **AT-2** Static values, one cell per row of the section 3 table and the section 4 table: wrong type, bool for
  int, out of range, NaN and inf weights, negative weights, zero-sum weights, weights length mismatch, empty
  categories, `step: 0`, `pad_length: -1`, `null_probability` in {-0.1, 1.5, True, "0.5"},
  `determinism: sometimes`, `distribution: bogus`, `weighted` without weights, formula `"a +"`, `locale: 5`,
  `faker_kwargs` not a mapping. Each asserts code and loc. Canary cells: categories and weights that contain
  `"CANARY-7731"` or `7731.5` never appear in `str(exc)`, `exc.errors()` or the `ConfigProblem` text, and the
  raised template is fixed text (the CLI prints the raw `msg`, `_layers.py:148-163`). Boundary cells pass:
  `null_probability` 0 and 1, weights `[0, 1]`, `step: -1`, `locale: [en_US, fr_FR]`.
- **AT-3** Byte identity: every generate config in `tests/integration/compat_corpus`, `tests/fixtures` and
  `examples/` still validates, and `model_dump()` and `strategy_config_fingerprint` per column equal the
  values computed at `main` (fixture recorded in E1 before the validator lands). The determinism goldens and
  parity suites pass unchanged.
- **AT-4** Duplicate generate column names raise `config.generate_column_duplicate` at
  `("tables", 0, "generate_columns", 1, "name")`; one name in two tables is fine.
- **AT-5** Schema: `PipelineConfig.model_json_schema()` validates (with `jsonschema`, already a dev
  dependency, confirmed in E1) every AT-3 corpus config and rejects the AT-1 `weigths` column for each type.
- **AT-6** Registry sentry (`tests/sentry/defects/test_generate_option_reads.py`, new, listed in
  `tests/sentry/test_sentry_manifest.py`): an AST scan of the reader files
  (`generation/synthesize.py`, `generation/_column_streams.py`, `generation/_bounds.py`,
  `generation/_admission.py`, `generation/_reproducibility.py`, `generation/_window_admission.py`,
  `generation/_pinned_compile.py`, `generation/_faker_pool.py`, `generation/_grouped_windowed_generators.py`,
  `generation/_referenced_formula.py`, `generators/derivation.py`, `generators/_formula.py`,
  `internal/faker_setup.py`, `generation/statistical/_spec.py`, and each transform
  `from_dict`) collects every literal key read with `.get("<k>")` or `["<k>"]` on a generate column variable,
  per type branch where the branch is syntactic and otherwise as "any type". Every collected key is in the
  registry for its type, and every registry key is collected for some reader. The variable names the scan
  follows (`col`, `cfg`, `column_config`, `col_cfg`) are listed in the test; the test fails if a reader file
  has no match at all, so a rename cannot empty it.

Engine E2 (`tests/unit/plan/checks/test_generate_columns.py`, new, in a new `tests/unit/plan/checks/`
package mirroring the source subpackage; `tests/unit/plan/` already has 26 modules):

- **AT-7** Each plan check, through `run_config_only_checks` and `compile_plan`:
  `faker_type` unknown (and a custom provider in the snapshot accepted); `locale` unknown, alone and inside a
  list (`en_US` and `fr_FR` accepted); `faker_kwargs` key the provider does not accept (a real one accepted);
  categorical `["a", 1]` refused, `[1, 2.5]` and `["a", None]` accepted, and a property that every
  categories list `_categorical_type` admits is accepted; forward reference per marked key; `group_by ==
  order_by`; each translated `from_dict` rule (code, generate-column path, fixed message,
  `exc.__context__ is None`); formula unknown name; formula sibling not in `references`; a `references`
  entry naming no sibling refused at `...references.<i>`; an actual self-read and an actual read of a later
  reference-bearing formula refused with `plan.generate_column_forward_reference`; an unused self
  declaration (`name: a`, `formula: "1"`, `references: [a]`) accepted and producing `1`; an unused later
  declaration (an `a` formula `"1"` declaring later reference-bearing `b`) accepted and producing `1`; an earlier
  reference-bearing formula and a later plain column accepted and producing the expected values;
  `statistical.condition_on` earlier accepted, missing and later refused; `{a} * 2` refused,
  `len({a, b})`, `x in {1, 2}` and `[v for v in range(3)]` accepted; a mask-only config with a missing
  `strategy: derived` ref still refused at both sites; a bad `derived_aggregate.op` and a bad
  `windowed_date.distribution` refused with the owning check's code and path; weights length vs a `sequence` parent
  (equal accepted, a parent with `null_probability > 0` and other parent types accepted at any length). Every
  refusal asserts code and `path`; canary cells as in AT-2.
- **AT-8** Validate refuses what run refuses: for each H4 repro, the refusal comes from the layer that owns
  it, with the code in section 4 and a provider spy showing generation never starts. Zero-sum weights: at
  `parse_pipeline_config` (config codes are only visible there, since `compile_plan` takes the dump,
  `execution/_pipeline.py:161`). Mixed categories, `group_by == order_by` and forward `derived`: at
  `run_config_only_checks` and at `compile_plan`, same code. Red first: written before E1 and E2, it fails on
  `main` because every call passes. A second cell per repro runs `run_pipeline` on the dump and asserts it
  still fails at run on `main` behavior (the repro is real, not a config the run would accept).

Engine E3 (`tests/unit/generation/test_generation_floor.py`, new):

- **AT-9** Floor: (a) property (Hypothesis): for generated tables of up to 2,000 rows over every generate
  type, on both routes, each part of the floor is at most its own measure: the legacy list-slot part at most
  the `tracemalloc` peak, the Arrow part at most the `pa.total_allocated_bytes()` peak; (b) `row_count: 5_000_000_000` on a sequence
  column with `detect_effective_memory_bytes` patched to 1 GiB raises `GenerationBudgetExceeded` in under a
  second, with a provider spy showing no call and the message holding no value; (c) a table needing 60% of
  the patched memory runs (the floor is not compared with the 25% planning budget); (d) with `budget_bytes`
  set, the floor is not called (ledger path unchanged); (e) an admission-eligible plan with a bounded sink does not call the floor, and a plan declined to the
  legacy route with a bounded sink does call it;
  (f) the probe route uses the scaled `row_count`; (g) `estimate_job_capacity` on the 5e9 config returns
  `INSUFFICIENT` with code `generation_budget_exceeded` without profiling a source, honors an explicit
  `budget_bytes`, and on a mask-only config returns what it returns on `main`; (h) with memory detection
  patched to raise `out_of_core_memory_detection_failed`, `run_generation` runs as on `main` and
  `estimate_job_capacity` returns `UNKNOWN`; (i) a boolean categorical table refused by the floor gives an
  `int` `needed_bytes` equal to `ceil(rows / 8)` plus the other columns.
- **AT-10** `decoy_engine.errors.CAPACITY_REASON_CODES` equals the three codes and each is registered.

CLI C1 and C2 (`tests/e2e/generate_config/test_validate_generate.py` and
`tests/e2e/generate_config/test_generate_capacity.py`, new; a named folder because `tests/e2e/` has 81 flat
modules, with a CLI CODEMAP row beside `cli_fmt/` and `private_file/`):

- **AT-C1** Each H3 repro: `decoy validate` exits 1, the finding names `table.column.key` and the code, stdout
  and stderr hold no option value (canary).
- **AT-C2** Each H4 repro: `decoy validate` exits 1 with the code; `decoy run` exits 1 at the gate, never 3.
- **AT-C3** `row_count: 5000000000`: `decoy validate` exits 5 and `decoy run` exits 5 at its pre-run gate
  (`check_before_run`, `run.py:670`), before any output file exists. The machine-memory reading is patched in
  the test so the result does not depend on the runner.
- **AT-C4** `decoy schema` output marks generate columns `additionalProperties: false` per type.
- **AT-C5** The run handler maps an engine `GenerationBudgetExceeded` to 5 and JSON `error_kind: capacity`
  (`run.py:1089-1091`), using `CAPACITY_REASON_CODES`; a bare `MemoryError` raised by a patched engine call
  exits 5 with fixed, non-empty text.

## 8. Owner questions

Taken with the recommended option (V1 rule); the owner may overrule in the result:

1. `determinism` accepts `stable` as an explicit spelling of the default, beside `fresh`. Only `fresh` acts
   today, and `stable` is excluded from the fingerprint, so it changes no bytes.
2. Reference `weights` length is checked at validate only against a `sequence` parent; other parents keep the
   run-time uniform fallback (F2). Making that fallback an error would refuse probe runs, which scale parent
   rows (`_probe_scale.py:118-120`).
3. The capacity check is a floor against the machine's whole effective memory, so it refuses only what
   cannot fit. Some runs that pass it will still run out of memory; C1 turns those into exit 5 with fixed
   text, and F3 makes them refuse earlier.
4. `categories: [a, a, b]` (a repeated category) stays legal and gives `a` twice the weight; the docs say so.

## 9. Reuse inventory

| Need | Reused | Where |
|---|---|---|
| value-free located config errors | registered `PydanticCustomError` codes, `ConfigProblem`, `validation_problems` | `config/_tables.py:202`, `safe_output/_validation.py:249`, `config/_parse.py:14-27` |
| required-key rules | today's validators, moved into the registry one for one | `_tables.py:296-368` |
| categorical typing | Arrow's own inference, the one `pa.table` applies at run | `pa.array`; `synthesize.py:175` (the admission predicate `_admission.py:93-111` stays the routing rule, unchanged) |
| faker provider list | `list_generate_faker_providers` | `internal/faker_setup.py:609-637` |
| faker kwargs filter | the signature read run already uses | `faker_setup.py:416-448`, extracted in E2 to one named function both callers use |
| locale list | `faker.config.AVAILABLE_LOCALES` | Faker |
| transform parse rules | each type's `from_dict` | `transforms/derived.py`, `transforms/grouped_series.py:91`, and siblings |
| derived ref extraction | `compile_expr`, `_get_column_refs` | `expressions/_lark_parser.py:74`, `transforms/derived.py:188` |
| existing ref checks | the five generate ref checks | `plan/_checks.py:581`, `plan/_checks_derived_aggregate.py`, `plan/_checks_grouped_series.py:23`, `plan/_checks_windowed_date.py`, `plan/_checks_group_key.py` |
| existing transform rule checks | `op`, `distribution` and siblings | `_checks_derived_aggregate.py:106-116`, `_checks_windowed_date.py:108-118` |
| memory ceiling | `detect_effective_memory_bytes` | `execution/out_of_core/_budget.py:339` |
| engine capacity entry and verdict | `estimate_job_capacity`, `CapacityEstimate` | `execution/capacity.py:208`, `out_of_core/_capacity_eval.py:131` |
| capacity error | `GenerationBudgetExceeded`, code `generation_budget_exceeded` | `generation/_budget_ledger.py:40`, `reasons/runtime/_part2.py:253` |
| column Arrow types | `classify_plan`, `ColumnAdmission.arrow_type` | `generation/_admission.py:61-67`, `:149` |
| CLI capacity mapping | `gate_exit_code`, `CAPACITY_CODES` handling | CLI `gate/_layers.py:338-348`, `gate/_run.py:31`, `run.py:966-989` |

## 10. Placement

| Item | Where | Size | Why there |
|---|---|---|---|
| option registry, `TypeOptions`, `OptionSpec`, static checks | engine `config/_generate_options.py` (new; no pyarrow or Faker import) | about 300 | the config layer owns option shape; `config/` has 20 direct modules (21 after), under the 25 limit |
| `_options_valid`, duplicate-name validator, schema hook; two old validators removed | `config/_tables.py` | 452 to about 400 | the model the options belong to |
| `check_generate_columns` | `plan/checks/_generate_columns.py` (new), `plan/checks/__init__.py` (new) | about 280 and 10 | CODEMAP's target for new plan checks (`CODEMAP.md:219`); `plan/` has 31 flat modules, so no new flat module there, and `plan/_checks.py` (712) is over the hard max and shrink-only |
| one wrapper call replaces five calls and their comments at each site | `plan/_compile.py` | 680 to about 655 (census entry lowered to the exact count) | `compile_plan` and `run_config_only_checks` |
| kwargs filter extracted | `internal/faker_setup.py` | 668, net 0 or smaller (census entry unchanged or lowered) | where the filter lives |
| `group_by != order_by` | `transforms/grouped_series.py` | +8 (293 to about 301) | the type's parse rule, shared by generation |
| `FORMULA_SCOPE_NAMES` | `generators/_formula.py` | +12 (331 to about 343) | beside the scope it names |
| floor | `generation/_bounds.py` | +45 (256 to about 301) | the bounds module |
| floor call | `generation/_streamed.py` | +6 (537 to about 543) | `run_generation`, the one entry both public calls reach |
| floor in the estimate | `execution/capacity.py` | +15 (507 to about 522) | the engine capacity entry the CLI already calls |
| `CAPACITY_REASON_CODES` | `errors.py` | +8 (536 to about 544) | public errors facade |
| reason codes | `reasons/config/_part2.py` (12 lines today, about +40), `reasons/plan/_part3.py` (61, about +40) | | the parts with room |
| engine tests | `tests/unit/config/test_generate_options.py` (10 modules there), `tests/sentry/defects/test_generate_option_reads.py` (2), `tests/unit/plan/checks/test_generate_columns.py` (new package; `tests/unit/plan/` has 26), `tests/unit/generation/test_generation_floor.py` (23, 24 after) | about 380, 180, 340, 180 | CODEMAP `:235` puts recurring-defect sentries in `defects/`; no test lands in a flat folder over 25 |
| CLI tests | CLI `tests/e2e/generate_config/` (new named folder), CLI `CODEMAP.md` row | about 260 and 1 row | CLI `tests/e2e/` has 81 flat modules; new contract tests go in named folders (CLI CODEMAP `:59-60`) |
| CLI preflight and run mapping | CLI `preflight.py`, `gate/_run.py`, `run.py` | about +25, -3, +15 | where capacity is decided today |

## 11. Frozen contracts touched

- **Reason registry**: new codes.
  - config: `config.generate_option_unknown`, `config.generate_option_type`, `config.generate_option_range`,
    `config.generate_option_value_unknown`, `config.generate_weights_invalid`,
    `config.generate_formula_invalid`, `config.generate_column_duplicate`;
  - plan: `plan.generate_faker_type_unknown`, `plan.generate_locale_unknown`,
    `plan.generate_faker_kwargs_unknown`, `plan.generate_categories_mixed`,
    `plan.generate_column_forward_reference`, `plan.grouped_series_group_by_equals_order_by`,
    `plan.generate_formula_name_unknown`, `plan.generate_formula_reference_missing`,
    `plan.generate_formula_reference_unknown`,
    `plan.generate_formula_template_syntax`, `plan.reference_weights_length_mismatch`,
    `plan.generate_option_invalid`.

  They take the next free minor when E0 lands. Provisional minors on other branches: 1.10.0 (QA-FIT-REDACT,
  QA-FK-FANOUT, CONFIG-EXPLICIT E1), 1.11.0 (CONFIG-EXPLICIT E2a), 1.12.0 (E2b-r); this plan writes 1.13.0
  provisionally and `scripts/dev/registry_renumber.py` renumbers at merge. Fixture
  `tests/fixtures/f29/reason-manifest-v1.NN-gen-strict.json`; `consumer-contract.json` moves with it.
  Escalation trigger: E0 runs the full engine suite.
- **Value-free baseline**: E1 removes the 14 `GenerateColumnConfig` `permitted::` keys from
  `tests/sentry/baselines/validation-name-messages.json` (`:74-115`). Shrink only.
- **`decoy schema` output**: generate columns become closed per type. Additive for valid configs; editors
  that relied on `additionalProperties: true` to accept typos now flag them, which is the point.
- **Config acceptance**: configs with unknown or bad options that were silently accepted are now refused.
  Pre-GA hard change (owner 2026-11-08); CHANGELOG entry in E1 and E2.
- **Seed and output bytes**: unchanged (AT-3).
- **Engine API**: `CAPACITY_REASON_CODES` is a new public name. `estimate_job_capacity` keeps its signature
  and result type; for configs with generate tables it can now return `INSUFFICIENT` with a third code.
- **CLI pin**: C1 bumps `decoy-engine` (`pyproject.toml:111`; escalation trigger: dependency lock, full CLI
  suite).

## 12. Slices

| Slice | Repo | Files (at most 10) | Lines | Tests |
|---|---|---|---|---|
| E0 | engine | `reasons/config/_part2.py`, `reasons/plan/_part3.py`, `reasons/_registry.py` (version), fixture `reason-manifest-v1.NN-gen-strict.json`, `tests/fixtures/f29/consumer-contract.json`, `docs/reasons.md`, `tests/unit/test_reason_registry.py` (7) | about 120 | registry tests; full engine suite (registry trigger) |
| E1 | engine | `config/_generate_options.py` (new), `config/_tables.py`, `tests/unit/config/test_generate_options.py`, `tests/sentry/defects/test_generate_option_reads.py`, `tests/sentry/test_sentry_manifest.py`, `tests/sentry/baselines/validation-name-messages.json`, the AT-3 identity fixture, the schema golden if one exists, `CHANGELOG.md` (9) | about 600 (half tests) | AT-1 to AT-6 first; T1 = `tests/unit/config`, `tests/sentry`, `tests/integration/compat_corpus`, `tests/native/test_determinism_goldens.py`, `tests/parity`, `tests/unit/generation`; full engine suite (shared baseline, serialization trigger) |
| E2 | engine | `plan/checks/__init__.py`, `plan/checks/_generate_columns.py`, `plan/_compile.py`, `internal/faker_setup.py`, `transforms/grouped_series.py`, `generators/_formula.py`, `tests/unit/plan/checks/__init__.py`, `tests/unit/plan/checks/test_generate_columns.py`, `tests/sentry/test_module_size.py`, `CHANGELOG.md` (10; existing plan tests that assert the old value-bearing messages of the five ref checks are updated in place, and if that pushes past 10 files they move to a prep slice E2p before E2) | about 750 (half tests) | AT-7, AT-8 first; T1 = `tests/unit/plan`, `tests/unit/transforms`, `tests/unit/generation`, `tests/sentry`, `tests/integration/compat_corpus` |
| E3 | engine | `generation/_bounds.py`, `generation/_streamed.py`, `execution/capacity.py`, `errors.py`, `tests/unit/generation/test_generation_floor.py`, `tests/sentry/test_public_import_boundary.py` baseline if it lists facade names, `CHANGELOG.md` (7) | about 280 | AT-9, AT-10 first; T1 = `tests/unit/generation`, `tests/unit/execution`, `tests/sentry`, `tests/physical`, `tests/integration`, `tests/native/test_determinism_goldens.py`; full engine suite (frozen contract: `estimate_job_capacity` result) |
| D | engine | `docs/strategies.md` (generation sections), `CODEMAP.md` (`plan/checks/` no longer a target) (2) | about 80 | docs link check |
| R | engine | release request to claudeorchestrator after E0 to D merge | | |
| C1 | CLI | `pyproject.toml`, `uv.lock`, `tests/e2e/generate_config/__init__.py`, `tests/e2e/generate_config/test_validate_generate.py`, `CODEMAP.md`, `CHANGELOG.md` (6) | about 220 | AT-C1, AT-C2, AT-C4; full CLI suite (pin bump) |
| C2 | CLI | `src/decoy/cli/preflight.py`, `src/decoy/cli/gate/_run.py`, `src/decoy/cli/run.py`, `tests/e2e/generate_config/test_generate_capacity.py`, `docs/cli-reference.md`, `CHANGELOG.md` (6) | about 200 | AT-C3, AT-C5 first; T1 = `tests/e2e/generate_config`, `tests/e2e/test_run_capacity.py`, `tests/unit/test_gate_*.py`, `tests/e2e/test_run_validate_always.py` |

Order and position against other work:

- **GEN-C1** (`fix/gen-seed-by-name`, head `1b3e4d1c`) removes `_legacy_column_name_seed` and stamps `_table`
  through `with_table` at generation time, after validation. E1 lands after GEN-C1 merges, so the registry's
  internal-key list is empty from the start. If E1 has to go first, it lists `_legacy_column_name_seed` and
  GEN-C1 drops it in the same commit that drops the read (AT-6 forces this).
- **GEN-C2** (`fix/gen-numeric-typing`, head `fcec3660`) adds no option keys but edits generation typing near
  `transforms/grouped_series.py`. E2 lands after GEN-C2 to avoid a textual conflict.
- **CONFIG-EXPLICIT E1** (`a9e8bc54`) and **E2a** (`85e1df76`) do not touch `GenerateColumnConfig`; their
  declaration gate skips generate tables. Row 24k depends on row 24b, which is folded into CONFIG-EXPLICIT E2
  (E2b-r, E2b-s). E0 and E1 are independent of it. E2 lands after CONFIG-EXPLICIT E2b so the
  `run_config_only_checks` edits in `_compile.py` do not collide and the CLI gate is already the 24b gate.
- **QA-FK-FANOUT** (row 24h) adds `distribution: fitted`, `snapshot_file` and `source_column` to reference
  columns. Whichever of E1 and that plan's E2b lands second adds the three keys and the enum value to the
  registry; AT-6 fails until it does.
- **Rows 24p and 24q** extend the faker provider authority and add types and options; both add registry rows.
- Within this plan: E0, then E1, then E2, then E3 (E3 is independent of E1 and E2 and may go earlier), then D,
  R, C1, C2.

## 13. Pre-mortem

- **A real config in the wild carries a key the generators never read** (a comment-like `description`, a
  platform UI key). It is refused after E1. Pre-GA, owner rule: no users depend on the soft path (the same call
  as F6, `_tables.py:316-322`). E1 sweeps every shipped example, template and fixture (AT-3), and CLI C1 sweeps
  `decoy init` templates.
- **The sentry's AST scan misses a read through a helper** (`_get(col, "k")`). Then a key could be in the
  registry with no collected read (FM2 direction fails loudly) or read and not in the registry (a valid
  config refused in testing by AT-3 or AT-7). The scan fails on a reader file with zero matches, and the
  builder adds any helper name to the scanned call list.
- **A Faker upgrade renames a provider.** `faker_type` validation follows the installed Faker, as run does;
  a config naming a removed provider fails at validate instead of silently writing words.
- **The floor is wrong for a type** (for example, a fixed-width type that Arrow stores dictionary-encoded).
  AT-9(a) is a property over every type against measured peak allocation; a counterexample fails the build,
  not a user.
- **Two validators raise for one column** (`pooled` rule and an unknown key). Both are reported in one error
  list; nothing is lost.
- **`ValidationError.from_exception_data` changes shape in a pydantic upgrade.** AT-1 pins the location and
  type through `parse_pipeline_config`; `uv.lock` pins pydantic.
- **The `{a}` form is documented somewhere.** D and C1 grep the docs and examples for `{` inside `formula:` and
  fix each; AT-7 keeps `x in {1, 2}` and `len({a, b})` legal.

## 14. Follow-ups (recorded, not in V1)

- F1: row-time formula errors become a run failure (or a counted warning in the run summary) instead of
  `None` plus a log line.
- F2: reference `weights` length against a non-`sequence` parent pool becomes a run-time refusal, with the
  probe route passing its scaling factor.
- F3: the pipeline passes a resolved `budget_bytes` to `generate_tables`, so the generation ledger's upper
  bounds apply on every run and exit 5 replaces the remaining out-of-memory exit 3 cases.
- F4: `min_per_parent > max_per_parent` and impossible minimums (QA H6) refused at validate.
- F5: `_mem_estimate_schema._GENERATE_NUMERIC_DTYPE` (`execution/_mem_estimate_schema.py:244`) derives from
  the admission Arrow type, so generate-column widths have one source.
- F6 (V2-LOG): AT-6's AST census recognizes a fixed list of generate-column mapping variable names. A new
  reader or spelling must add itself to that list; deliberate or novel indirection remains outside the V1
  completeness claim.
- F7 (V2-LOG): if the conditional E2p prep slice is needed, enumerate its exact existing-test files before
  build. This is slice bookkeeping only and does not change the E2 behavior or verdict.
- F8 (V2-LOG): engine `origin/main` advanced from the plan's `44d6afe2` snapshot to `046887e9` with the
  scan-set slice, moving CODEMAP lines and registry version 1.10.0 but not the GEN-STRICT readers. Citations
  are refreshed here; E0 still re-reads and renumbers the reason registry on its final merge base.
