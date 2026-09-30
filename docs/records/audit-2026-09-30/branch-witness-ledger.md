# Branch-witness ledger

Status: record (input to docs/plans/2026-09-30-rust-coverage-evidence-audit.md)

Every routing or admission predicate outcome in the code that decides where a job runs, with its file:line, what route/backend results, and the minimal fixture that would witness it. The **Witness** column is left empty; a later run-stage pass fills it with a run id or "code-inferred" plus its reason.

Engine citations are file:line in this worktree (`decoy-engine` branch `docs/reality-2026-09-30`, HEAD on top of pinned commit `8dc559e5`). Platform citations are read from `git -C /home/cam/vscode/decoy-platform show origin/main:<path>` (the local platform checkout lags origin), with line numbers from that blob. CLI citations are from `/home/cam/vscode/decoy` as noted in that section.

All file:line citations below were read directly from the source in this pass; none are guessed.

## 1. Unified-slice admission (`execution/_unified_slice_admission.py`)

The full-frame native/Rust lane for the 8 scalar operators (passthrough, redact, truncate, hash, categorical, bucket_perturb, group_key, date_shift). Two stages: `cheap_admission` (no `execution.physical` import) then `resident_contract_admission` (compiled-plan-aware). Any decline in either stage falls through to the legacy pandas full-frame adapter.

### 1a. `cheap_admission` (starts L203) -- any `return None` = decline to legacy pandas

| ID | Predicate → outcome | file:line | Route/backend on decline | Minimal fixture | Witness |
|---|---|---|---|---|---|
| B001 | `route != "full_frame" or route_chunked` → decline | _unified_slice_admission.py:223 | legacy pandas / whatever route already chosen | any non-full_frame route reaching this call (defense-in-depth; not reachable via normal dispatch) | |
| B002 | `resolved_substrate != "pandas"` → decline | :225 | legacy pandas | unreachable today (pandas is the only substrate that resolves); defense-in-depth | |
| B003 | `source_loader is not None` (lazy relationship loading) → decline | :235 | legacy relationship-loading path | engine direct `run_pipeline(source_loader=...)` instead of resident `sources=` | |
| B004 | `fidelity_report or post_validation or vault_writer is not None` → decline | :252 | full pandas path (scan/vault seam) | `run_pipeline(fidelity_report=True)` or `post_validation=True` or `vault_writer=<writer>`, single hash-mask table, Parquet, <100k rows | |
| B005 | `config.get("validators") or config.get("quarantine") or config.get("run_storm")` → decline | :258 | legacy pandas (these gates only run there) | config with a `validators:` block on an otherwise-admissible single-table hash job | |
| B006 | `profile.relationships` → decline | :260 | falls to relationship routing layer, not the unified single-table lane | any 2-table FK config | |
| B007 | `len(table_kinds) != 1 or len(mask_tables) != 1` → decline | :264 | legacy pandas | a generate table alongside the mask table, or 2 mask tables | |
| B008 | source not `type=="file"` or `format not in {parquet,csv,fixed_width}` → decline | :268-273 | legacy pandas | source type `s3`/`gcs`, or a format outside the three | |
| B009 | `set(caller_sources) != {table}` (extra loaded source) → decline | :284 | legacy pandas | run_pipeline called with an extra resident table beyond the configured mask table | |
| B010 | source not a resident `pa.Table` (absent or LazySource placeholder) → decline | :292-293 | legacy pandas | TB-1 lazy-loaded relationship source, or missing source dict entry | |
| B011 | duplicate resident column names → decline | :298 | legacy pandas | Arrow table built with two columns sharing a name, passed directly to run_pipeline | |
| B012 | `table_cfg is None or table_cfg.get("transforms")` → decline | :308 | legacy pandas | config with a `transforms:` block on the mask table | |
| B013 | columns_cfg empty or non-dict entries → decline | :311-313 | legacy pandas | malformed columns list (API-level, not schema-reachable) | |
| B014 | column names null or duplicate → decline | :314-316 | legacy pandas | duplicate column names in config (schema-level, not reachable via validated config) | |
| B015 | `set(names) != set(source.column_names)` → decline | :317 | legacy pandas | config declares a column not present in the resident Arrow schema, or vice versa | |
| B016 | any column has `vault: true` → decline | :321 | legacy pandas | a column with `vault: true` | |
| B017 | any column has a `when:` gate → decline (**the plan's named `when`-gate decline**) | :323, helper `_has_when_gate` :415-417 | legacy pandas (`run_with_when_gate`, `_pandas_adapter.py:405`); unified lane has no row-gate concept | single hash-mask column with `when: "<predicate>"`, Parquet, <100k rows, engine direct entry point | |
| B018 | `source.validate(full=True)` raises `pa.ArrowInvalid` → decline | :331-333 | legacy pandas | deliberately malformed Arrow table (needs direct API misuse, not config-reachable) | |
| B019 | `to_pandas_fk_safe` raises (e.g. malformed `b"pandas"` schema metadata) → decline | :364-373 | legacy pandas | resident table with malformed pandas schema metadata | |
| B020 | `frame.index.name is not None or not frame.index.equals(pd.RangeIndex(len(frame)))` → decline | :374 | legacy pandas | resident Arrow table carrying pandas index metadata (`pa.Table.from_pandas(df.set_index(...))`) | |
| B021 | `list(frame.columns) != list(source.column_names)` (physical named index) → decline | :380 | legacy pandas | companion case to B020 | |
| B022 | per-column Arrow↔pandas round-trip type/value mismatch → decline | :396-410 | legacy pandas | column whose Arrow physical type disagrees with its pandas metadata (direct Arrow construction, not reachable via normal CSV/Parquet load) | |

Only after all 22 pass does `cheap_admission` return a `CheapCandidate`.

### 1b. `resident_contract_admission` (starts L468) -- compiled-plan-aware second gate; any `return None` = decline

| ID | Predicate → outcome | file:line | Route/backend on decline | Minimal fixture | Witness |
|---|---|---|---|---|---|
| B023 | `physical_table is None or physical_table.driver != DriverId.FULL_FRAME` → decline | :516 | legacy pandas | a non-full-frame driver reaching this call | |
| B024 | `not nodes` → decline | :519 | legacy pandas | table with no masked nodes | |
| B025 | duplicate node ids → decline | :522 | legacy pandas | compiler bug fixture (not config-reachable) | |
| B026 | `binding is None` → decline | :529-530 | legacy pandas | node with no compiled execution binding | |
| B027 | `node.kind != "scalar" or len(node.columns) != 1` → decline | :531 | legacy pandas (composite/bundle strategies never reach unified lane) | a bundled/composite strategy config | |
| B028 | `binding.operator_id not in ALLOWED_OPERATOR_IDS` → decline | :533 | legacy pandas | any strategy outside the 8 (faker, fpe, text_mask, bucketize, code_set, ...) | |
| B029 | `binding.required_prepasses` → decline | :535 | legacy pandas | date_shift without explicit format (needs whole-column format-detection prepass) | |
| B030 | `binding.diagnostic_obligations` not subset of the routed set (only date_shift's `format_error` is routed) → decline | :537-539 | legacy pandas | any operator emitting an obligation other than date_shift's format_error | |
| B031 | group_key sibling not admitted (`_group_key_sibling_admitted`, :420-465: sibling not nonempty string name, absent from schema, type outside `{string,int64,bool}`, input_schema shape mismatch, OR sibling masked by a non-passthrough node -- "order-dependence decline") → decline | :541-548 (helper :420-465) | legacy pandas | two columns: `group_key` keyed on `group_by: "other_col"`, and `other_col` has a non-passthrough strategy (e.g. hash) -- witnesses the order-dependence case specifically | |
| B032 | group_key `key_binding is None` → decline | :552-553 | legacy pandas | compiler-internal (not config-reachable) | |
| B033 | group_key namespace not UTF-8-encodable → decline | :555-557 | legacy pandas | namespace with a lone UTF-16 surrogate (not reachable via normal config) | |
| B034 | (non-group_key) `len(binding.input_schema) != 1 or names != [column]` → decline | :565 | legacy pandas | compiler-internal shape mismatch | |
| B035 | resident type outside `_ADMITTED_RESIDENT_TYPES[strategy]` (passthrough={string,int64,bool}; redact/truncate/categorical/bucket_perturb/date_shift={string}; hash={string,int64}) → decline | :571, domain table :154-171 | legacy pandas | **key gap-hunting fixture**: an int64 column bound to `redact` or `truncate` (compiler gates those on config only, never input type) | |
| B036 | companion-dependent op (hash/categorical/bucket_perturb/date_shift) `key_binding is None` → decline | :579-580 | legacy pandas | compiler-internal | |
| B037 | companion-dependent op namespace not UTF-8 → decline | :582-584 | legacy pandas | not config-reachable | |
| B038 | coverage not 1:1 across configured/physical/resident columns → decline | :594 | legacy pandas | duplicate column claimed by two nodes, or incomplete coverage (compiler-internal) | |
| B039 | **required native kernel unavailable** -- `native_kernel_availability()`; hash needs `crypto`, categorical/bucket_perturb/date_shift need `index`, group_key needs `raw_hex` (map :129-135) → decline WHOLE TABLE, per-operator not all-or-nothing | :600-603 | legacy pandas oracle | run any hash/categorical/bucket_perturb/group_key/date_shift job when `decoy_engine_native` is absent or ABI-mismatched -- **confirmed LIVE on this devbox**: preflight.json shows the companion absent, so this branch fires by default for every non-passthrough/redact/truncate strategy on this host today | |
| B040 | `reject_null_bearing_int` raises `ExecutionError` → decline | :604-607 | legacy pandas | int64 column with nulls bound to hash or truncate | |

Only after all 40 checks (B001-B040) pass is the table admitted to the native/Rust unified lane (`compiled_kernel_executed=True` territory).

## 2. Auto-chunk classification (`execution/_planner.py`, `classify_job` / `_chunked_rejection`)

`PLANNER_ROUTING_ENABLED` is a hard `False` constant (L90): only the `chunked` mode actually routes (via `run_pipeline(auto_chunk=True)`); the relationship modes here are detection-only, deferred to `_pipeline_routing.decide_execution_route` (section 3). Defaults: `AUTO_CHUNK_THRESHOLD_ROWS_DEFAULT = 100_000` (L97), `OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT = 5_000_000` (L135), `FULL_FRAME_REJECT_ROWS_DEFAULT = 7_500_000` (L146) -- all three are `run_pipeline` kwargs, overridable per call.

`_chunked_rejection` (starts L259): each appended reason is an independent decline of the `chunked` mode; no reason at all → chunked selected.

| ID | Predicate → outcome | file:line | Route/backend on decline | Minimal fixture | Witness |
|---|---|---|---|---|---|
| B041 | no mask-kind tables → decline chunked | :285-286 | stays full_frame-bound | generate-only job | |
| B042 | generate-kind table(s) present → decline chunked | :287-291 | full_frame | config with a generate-kind table alongside a mask table | |
| B043 | more than one mask table → decline chunked | :292-296 | full_frame | 2 mask tables | |
| B044 | resolved substrate != pandas → decline chunked | :297-302 | full_frame | non-pandas substrate override (defense-in-depth; pandas is the only substrate today) | |
| B045 | `config.get("relationships")` present → decline chunked (`chunked_relationships_unsupported`) | :309-313 | falls to relationship detection / `_pipeline_routing` | any FK config | |
| B046 | `check_chunked_compatibility` raises `PlanCompileError` (see section 2a for the 17 named codes) → decline chunked | :316-319 | full_frame | any per-strategy chunked-incompatible column (section 2a) | |
| B047 | non-scalar (composite bundle) work on the table → decline chunked | :322-330 | full_frame | a composite/bundle strategy on the mask table | |
| B048 | `fpe_join_group` set on an fpe column → decline chunked | :331-337, helper :415-427 | full_frame (the `fpe_join_group_active` QualityWarning can't ride the chunked stream) | fpe column with `provider_config.fpe_join_group: true` | |
| B049 | any column carries a `when:` predicate → decline chunked (`when_predicate_not_chunk_stable`) | :361-396 (`_whole_column_state_rejections`) | full_frame | column with `when:` set (only reachable via a raw dict passed directly to run_pipeline -- schema-validated configs cannot carry `when` today per B017's own comment) | |
| B050 | date_shift column without explicit `provider_config.date_format` → decline chunked (`date_shift_requires_explicit_format`) | :361-403 | full_frame | date_shift column with no date_format, vs. one WITH date_format (chunk-admissible) | |
| B051 | (runtime, `source_tables` given) extra loaded source frame → decline chunked | :448-456 (`_runtime_source_rejections`) | full_frame | an extra resident table beyond the configured one | |
| B052 | (runtime) no loaded source frame for the table → decline chunked | :457-460 | full_frame | source_tables missing the mask table's entry | |
| B053 | (runtime) source is a `LazySource` → decline chunked | :467-469 | full_frame | documented unreachable in production today (relationship jobs already rejected upstream) | |
| B054 | **(runtime) below auto-chunk threshold** -- `src.num_rows < auto_chunk_threshold_rows` (default 100,000) → decline chunked | :470-475 | full_frame | route-equivalent fixture: override `auto_chunk_threshold_rows` down (e.g. to 10) so a tiny fixture still crosses it -- route-equivalent only, not capacity-proven | |
| B055 | (runtime) non-chunk-stable dtype: integer column with nulls, or a type outside `{string, large_string, floating, boolean, temporal, null}` → decline chunked | :476-493 | full_frame | int64 column with nulls | |
| B056 | (runtime) `bucketize` source column non-numeric or has nulls → decline chunked (`bucketize_source_not_null_free_numeric`) | :499-520 | full_frame | bucketize column over a nullable or non-numeric source | |
| B057 | (runtime) unsafe group_key `group_by` dtype, via `_chunked_group_key.unsafe_group_key_group_by_columns` (not read this pass) → decline chunked | :521-534 | full_frame | group_key column whose group_by effective type is not provably chunk-safe | |
| B058 | (runtime) unsafe text_mask / code_set / bucket_perturb source column (non-string), via `_chunked_text_mask.py` / `_chunked_code_set.py` / `_chunked_bucket_perturb.py` (not read this pass) → decline chunked | :535-573 | full_frame | any of those three strategies over a non-string source | |

### 2a. `check_chunked_compatibility` (`execution/_chunked.py:250`) -- named `PlanCompileError` codes (per its own docstring, L253-273)

| ID | Code | Trigger | Witness |
|---|---|---|---|
| B059 | `chunked_table_unknown` | `table` not in config.tables | |
| B060 | `chunked_generate_unsupported` | table is generate-kind (`generate_columns` set) -- _chunked.py:284-293 | |
| B061 | `chunked_fk_orphan_policy_not_remap` | FK child edge with non-REMAP orphan policy (delegated to `gate_fk_child_edges`, not read this pass) | |
| B062 | `chunked_fk_composite_unsupported` | FK child edge with a composite key | |
| B063 | `chunked_fk_parent_strategy_not_self_mask_safe` | parent key strategy is not exactly `hash` | |
| B064 | `chunked_fk_child_namespace_missing` / `chunked_fk_child_namespace_mismatch` | child column has no explicit namespace, or it disagrees with the parent's | |
| B065 | `chunked_fk_child_strategy_missing` / `chunked_fk_child_strategy_mismatch` | child column has no explicit strategy, or it disagrees with the parent's | |
| B066 | `strategy_not_chunk_safe` | a non-FK column uses a strategy outside `CHUNK_SAFE_STRATEGIES` / `CHUNK_CONDITIONAL_STRATEGIES` -- _chunked.py:317-341 | |
| B067 | `chunked_strategy_conditions_unmet` (faker) | faker column missing `deterministic: true`, missing `namespace`, missing an explicit `pool_size` (top-level or provider_config), or `cardinality_mode` not in `{None, "reuse"}` -- `_conditional_admission_failures` :216-237 | |
| B068 | `chunked_strategy_conditions_unmet` (categorical) | categorical column using `from_profile` (chunked mode only sees the first chunk), or missing explicit `provider_config.categories` -- :238-246 | |
| B069 | `chunked_windowed_date_when_not_supported` / `chunked_text_mask_when_not_supported` / `chunked_code_set_when_not_supported` / `chunked_bucket_perturb_when_not_supported` | the respective strategy combined with a `when:` predicate -- :307-316, delegated to per-strategy gate modules | |
| B070 | `chunked_code_set_fk_key_unsupported` / `chunked_bucket_perturb_fk_key_unsupported` | code_set or bucket_perturb used as an FK key column (either orientation) -- :300-306 | |

## 3. Auto-chunk go/no-go (`execution/_pipeline_chunk_route.py`)

| ID | Predicate → outcome | file:line | Route/backend | Minimal fixture | Witness |
|---|---|---|---|---|---|
| B071 | `not (explain_plan or (auto_chunk and has_mask_table))` → no classification, forced full_frame, `route_chunked=False` | :55 | full_frame | engine direct `run_pipeline` with default `auto_chunk=False` and `explain_plan=False` (today's out-of-the-box default) | |
| B072 | `route_chunked = auto_chunk and decision.mode == "chunked"` | :69 | chunked (`run_mask_pipeline_chunked`) only when BOTH the knob is on AND every check in section 2/2a passed | `run_pipeline(auto_chunk=True)` on a job that survives all of B041-B070 | |

## 4. Relationship routing -- the live FK router (`execution/_pipeline_routing.py`, `decide_execution_route`)

This is layer 1, reached BEFORE the chunk layer (section 3); it early-returns before chunk classification for any relationship-bearing job.

### 4a. `_sequential_eligible` (L172) -- each False path is a distinct decline-to-sequential

| ID | Predicate → outcome | file:line | Witness |
|---|---|---|---|
| B073 | `not profile.relationships` → `(False, "no_relationships")` | :214 | |
| B074 | `has_generate_table` → `(False, "generate_plus_mask")` | :216 | |
| B075 | `validators` present → `(False, "validators_present")` | :218 | |
| B076 | `fidelity_report` → `(False, "fidelity_report_requested")` | :220 | |
| B077 | `post_validation` → `(False, "post_validation_requested")` | :222 | |
| B078 | `vault_writer is not None` → `(False, "vault_writer_requested")` | :224 | |
| B079 | `resolved_substrate != "pandas"` → `(False, "non_pandas_substrate_requested")` | :226 | |
| (else) | eligible=True, reason="pure_mask_fk" | :228 | |

### 4b. Self-FK vs cross-table cycle (`_has_cross_table_fk_cycle`, L143)

| ID | Predicate → outcome | file:line | Witness |
|---|---|---|---|
| B080 | DFS cycle check across DISTINCT tables only; self-edge (`edge.parent_table == edge.child_table`) is explicitly excluded from the successor graph -- **self-referential FK is NOT flagged a cycle here** | :155 | |
| B081 | cross-table cycle (A→B→A) IS flagged | :143-169 | |

Fixture for B080 (not flagged, sequential proceeds): one table with an FK column referencing its own primary key. Fixture for B081 (flagged, sequential barred): table A has FK to B, B has FK to A. Contrast with out-of-core's OWN edge check (section 6, B097), which rejects self-referential FK too -- sequential and out-of-core disagree on this one shape.

### 4c. `decide_execution_route` (L231) -- outcomes in evaluation order

| ID | Predicate → outcome | file:line | Route/backend | Minimal fixture | Witness |
|---|---|---|---|---|---|
| B082 | `execution_mode == "full_frame"` → `("full_frame", "override_full_frame")`, bypasses the reject-before-read entirely | :383-386 | full_frame, even for a huge job | `run_pipeline(execution_mode="full_frame")` on any FK job | |
| B083 | `execution_mode == "out_of_core"`, `not has_mask_table` → raise `ConfigError` | :391-395 | reject | forced out_of_core on a generate-only job | |
| B084 | `execution_mode == "out_of_core"`, `not eligible` → raise `ConfigError` | :396-400 | reject | forced out_of_core on a sequential-ineligible job | |
| B085 | `execution_mode == "out_of_core"`, `not out_of_core_compatible` → raise `ConfigError` | :401-407 | reject | forced out_of_core on a job failing section 6's compat gate | |
| B086 | `execution_mode == "out_of_core"` (else) → `("out_of_core", "override_out_of_core")` | :408 | out_of_core | forced out_of_core on an eligible+compatible job | |
| B087 | `execution_mode == "sequential"`, `not eligible` → raise `ConfigError` | :411-415 | reject | | |
| B088 | `execution_mode == "sequential"`, `cyclic` → raise `ConfigError` | :416-421 | reject | forced sequential on a cross-table-cycle FK graph | |
| B089 | `execution_mode == "sequential"`, `not has_mask_table` → raise `ConfigError` | :422-430 | reject | | |
| B090 | `execution_mode == "sequential"` (else) → `("sequential", route_reason)` | :431 | sequential | | |
| B091 | **"auto" mode, byte-estimate in scope**: `use_byte_estimate_routing and has_relationships and has_mask_table and not has_generate_table` (flag defaults **True**; excludes generate+mask FK jobs) | :439-444 | -- | | |
| B092 | in scope, `full_frame_fits_estimate is True` → `("full_frame", "byte_estimate_full_frame_fits")` | :451-452 | full_frame, backed by a real byte-level estimate (section 5) | a small FK job (estimate easily fits any reasonable budget) | |
| B093 | in scope, estimate not confirmed, **micro-probe confirms fit** (`use_probe_routing` default True, `probe_recovers_full_frame is True`) → `("full_frame", "probe_recovered_full_frame")` | :464-465 | full_frame, backed by a measured two-point probe | a job where the static estimate is conservative but the real measured peak fits (needs a resident mask table; see section 5c skip conditions) | |
| B094 | in scope, neither confirms, `eligible and not cyclic and out_of_core_compatible` → `("out_of_core", "byte_estimate_bounded_out_of_core")` | :471-472 | out_of_core | | |
| B095 | in scope, neither confirms, `eligible and not cyclic` (else) → `("sequential", route_reason)` | :473-474 | sequential | | |
| B096 | in scope, no bounded route applies (cyclic or otherwise ineligible) AND estimate doesn't confirm fit → raise `ExecutionError(code="fk_full_frame_oom_risk_rejected")` | :475-499 | reject | cross-table-cycle FK job too big for the byte estimate to admit full_frame | |
| B097 | **out of byte-estimate scope** (flag off, or has_generate_table): row-count routing, `out_of_core_ready` (eligible, not cyclic, has_mask_table, out_of_core_compatible, `largest_table_rows is not None`, `rows >= out_of_core_threshold_rows` default 5,000,000) → `("out_of_core", "out_of_core_large_fk")` | :374-381, :500-501 | out_of_core | route-equivalent: override `out_of_core_threshold_rows` down (e.g. to 10) so a tiny FK fixture still crosses it | |
| B098 | row-count routing, `eligible and not cyclic` (else) → `("sequential", route_reason)` | :502-503 | sequential | | |
| B099 | row-count routing, `has_relationships and rows >= full_frame_reject_rows` (default 7,500,000) AND `not largest_table_rows_exact` (CSV estimate) → raise `ExecutionError(code="fk_full_frame_oom_risk_rejected_estimated")` | :524-550 | reject | FK job with a CSV source at/above 7.5M **estimated** rows, no bounded route eligible | |
| B100 | same condition but `largest_table_rows_exact=True` (Parquet/fixed_width footer count) → raise `ExecutionError(code="fk_full_frame_oom_risk_rejected")` | :551-565 | reject | FK job with a Parquet source at/above 7.5M exact rows, no bounded route eligible | |
| B101 | fallthrough: no relationships, OR cyclic-but-eligible (`"cross_table_cycle"` reason), OR disqualified-from-sequential but under threshold → `("full_frame", full_frame_reason)` | :566 | full_frame | a cyclic pure-mask FK job under 7.5M rows | |

## 5. Byte-estimate + micro-probe signals (`execution/_pipeline_routing_signals.py`)

| ID | Predicate → outcome | file:line | Notes / minimal fixture | Witness |
|---|---|---|---|---|
| B102 | `out_of_core_admission` delegates to `check_out_of_core_compatibility` (section 6) | :46-67 | one decision surface shared with the runner's own preflight | |
| B103 | `largest_mask_table_rows`: max resident `num_rows` across mask-kind sources, or `None` if none resident | :70-89 | `None` forces the profile-based fallback (B104) -- the lazy `source_loader` path | |
| B104 | `largest_mask_table_rows_from_profile`: `TableProfile.row_count` -- exact for Parquet/fixed_width footer, `row_count_exact=False` for a CSV byte-size estimate -- **this is the "Parquet footer counts" signal** | :92-116 | source of the `largest_table_rows_exact` flag consumed at B099/B100 | |
| B105 | `_resolve_largest_mask_table_rows`: PER-TABLE reconciliation (not a single scalar max) -- resident table uses its own exact `num_rows` (with a `RuntimeWarning` at :184-193 if it disagrees with an exact profile count); lazy table uses the profile count with its `row_count_exact` flag; final signal is the MAX across tables | :119-199 | closes the "huge lazy table hides behind tiny resident one" hole (H1 fix). Fixture: 2-table FK job, parent resident+tiny, child lazy-loaded via `source_loader`+large per profile -- proves the max comes from the child's profile count | |
| B106 | `byte_estimate_full_frame_fits`: delegates to `_mem_estimate.fits` (not read this pass) against `budget_bytes` (cgroup/host memory detection via `resolve_budget`); tri-state True/False/None; **None (unpriceable, e.g. a variable-width string column with no resident sample) treated identically to False** | :264-312 | scoped OUT for `has_generate_table` jobs (:278-286, documented under-count risk) | |
| B107 | `resolve_probe_recovery` SKIP conditions (probe never runs, returns `None`): either flag off; `full_frame_fits_estimate` already True; no mask table; any mask table not resident / is a `LazySource`; OR raw-bytes estimate busts budget even under `MIN_PLAUSIBLE_K_FULL_FRAME` | :411-449 | | |
| B108 | otherwise the real subprocess micro-probe (`_probe.probe_peak_bytes`, not read this pass) runs and returns a real True/False/None via `probe_fits` | :469-487 | the actual two-point measured probe -- "probe on and off" knob surface the plan names | |
| B109 | `resolve_execution_route`: when route resolved to `out_of_core`, `enforce_ooc_disk_preflight` runs as ADVISORY ONLY (warns on tight disk estimate, never rejects/reroutes) | :588-599 | the runtime `check_temp_disk_budget` (not read this pass) is the actual enforcer | |

## 6. Out-of-core compatibility gate (`execution/out_of_core/_compat.py`, `check_out_of_core_compatibility`)

| ID | Code → trigger | file:line | Witness |
|---|---|---|---|
| B110 | `out_of_core_no_relationships` -- no relationship edges at all | :171-180 | |
| B111 | `out_of_core_multi_parent_child_unsupported` -- same child FK tuple has >1 parent edge | :182-197 | |
| B112 | `out_of_core_fk_arity_mismatch` -- parent/child FK column counts differ | :414-420 (`_check_edge`) | |
| B113 | `out_of_core_self_referential_fk_unsupported` -- `edge.parent_table == edge.child_table` (table-level dependency ordering can't express self-dependency); falls back to full-frame. **Contrast with B080**: the sequential router does NOT treat self-FK as a cycle, but out-of-core declines it outright | :421-437 | |
| B114 | `out_of_core_parent_seed_missing` -- parent key column not in the compiled plan | :438-446 | |
| B115 | `out_of_core_parent_strategy_unsupported` -- parent FK key strategy outside `_INITIAL_SUPPORTED_STRATEGIES` | :447-454 | |
| B116 | `out_of_core_parent_namespace_missing` -- parent key is `hash` with no namespace | :455-461 | |
| B117 | `out_of_core_relationship_cycle_unsupported` -- table-level FK cycle across ≥2 tables (even when column-level ordering is acyclic) | :202-219 | |
| B118 | `out_of_core_when_predicate_unsupported` -- any work node carries a `when` predicate (route masks every non-null row unconditionally) | :221-233 | |
| B119 | `out_of_core_composite_group_uncovered` -- a `composite_fk_group` node has a column not covered as a child FK column by any edge | :234-243 | |
| B120 | `out_of_core_non_scalar_work_unsupported` -- a work node of kind other than `scalar`/`composite_fk_group` | :245-251 | |
| B121 | `out_of_core_composite_fk_scalar_child_unsupported` -- a composite FK child column masked as an independent scalar strategy rather than a `composite_fk_group` (raw-value-leak divergence vs. the pandas oracle) | :253-280 | |
| B122 | `out_of_core_cross_row_strategy_unsupported` -- strategy in `_CROSS_ROW_STRATEGIES` | :281-287 | |
| B123 | deferred-group-B / deferred-group-C codes (per-strategy, table not read this pass for the exact code list) | :288-293 | |
| B124 | `_group_c_conditional_rejection` -- code_set/bucket_perturb ported only for specific config shapes; other shapes decline | :294-299 (helper :323-352, not read this pass) | |
| B125 | `out_of_core_categorical_nondeterministic_unsupported` -- non-deterministic categorical (unseeded RNG, no cross-route parity) | :300-311 | |
| B126 | `out_of_core_strategy_unsupported` -- strategy outside `_SUPPORTED_WORK_STRATEGIES` | :312-318 | |

Route on ANY rejection above: falls back to full-frame (if sequential-eligible, sequential; else full-frame, subject to section 4's reject-before-read).

## 7. Out-of-core sink-path: `batch_join` vs `reorder` (`execution/out_of_core/_route_policy.py`, `decide_route`)

| ID | Predicate → outcome | file:line | Notes | Witness |
|---|---|---|---|---|
| B127 | `sink is None or not incoming_edges or budget_bytes is None or temp_disk_budget_bytes is None or len(incoming_edges) > 2*merge_fan_in` → `use_reorder=False` (keeps byte-for-byte `_batch_join`) | :121-128 | high fan-in falls back rather than raising (plausible wide-schema shape, not misconfiguration) | |
| B128 | `parent_key_count < threshold_rows` (default `REORDER_PARENT_KEY_THRESHOLD = 2_000_000`, overridable via `out_of_core_reorder_threshold_rows`, `0` = force every eligible table) → `use_reorder=False` | :129-133 | decision key is the largest incoming edge's DEDUPED parent-key count (from the relation's own Parquet footer `num_rows`, not raw row count) | route-equivalent: override the threshold down (even to `0`) to force reorder at small scale | |
| B129 | per-edge width admission: `parent_relations[edge].max_sort_payload_row_bytes >= per_head_cap` for any edge → `use_reorder=False` (fail SAFE toward `_batch_join`, same posture as the fan-in fallback) | :154-157 | a slim sorter row wider than the sorter's per-merge-head cap would make reorder raise; falls back instead | |
| B130 | else → `use_reorder=True`, sized `ReorderCaps` | :158 | | |
| B131 | `resolve_reorder_threshold_rows`: non-int/bool or negative override → raise `ExecutionError(code="out_of_core_reorder_threshold_invalid")` | :68-91 | | |
| B132 | `validate_outgoing_parent_columns`: outgoing edge names a parent-key column the schema lacks → raise `ExecutionError(code="out_of_core_parent_column_missing")`, run route-independently so both routes fail identically | :161-181 | | |

## 8. Native operator config rejections (`execution/native/_operator_config_rejections.py`)

Both the compiler's config-gate and the config-only `native_route_eligibility` query call these SAME functions, so they can never disagree.

### 8a. `categorical_config_rejection`

| ID | Predicate → outcome | file:line | Witness |
|---|---|---|---|
| B133 | not `deterministic` (and not `allow_collisions`) → `categorical_not_deterministic` | :64-65 | |
| B134 | no `namespace` → `categorical_requires_namespace` | :66-67 | |
| B135 | `categories` not a nonempty list/tuple → `categorical_categories_not_nonempty_list` | :68-70 | |
| B136 | any category not a string → `categorical_categories_not_all_string` | :71-72 | |
| B137 | `weights` present but shape mismatch → `categorical_weights_shape` | :74-76 | |
| B138 | any weight non-numeric (or bool) → `categorical_weights_not_numeric` | :77-78 | |
| B139 | any weight negative → `categorical_weights_negative` | :79-80 | |
| B140 | weights fail `_build_cdf` (nonpositive total / below-resolution weight) → `categorical_weights_unbuildable_cdf` | :81-84 | |

### 8b. `bucket_perturb_config_rejection`

| ID | Predicate → outcome | file:line | Witness |
|---|---|---|---|
| B141 | no `namespace` → `bucket_perturb_requires_namespace` | :107-108 | |
| B142 | `bucket` outside `{week, month, quarter}` → `bucket_perturb_unsupported_bucket` | :109-111 | |
| B143 | no explicit `date_format` → `bucket_perturb_requires_date_format` (oracle default/autodetect is order-dependent, so native declines rather than risk it) | :112-114 | |
| B144 | `date_format` has a tz directive (`%z`/`%Z`) → `bucket_perturb_timezone_directive` (oracle drops tz, native kernel keeps tz-aware Timestamps) | :115-122 | |
| B145 | resolved source type != `pa.string()` (only checked when a profile or resident source is available) → `bucket_perturb_source_not_string` | :123-133 | |

### 8c. `date_shift_config_rejection`

| ID | Predicate → outcome | file:line | Witness |
|---|---|---|---|
| B146 | no `namespace` → `date_shift_requires_namespace` | :180-181 | |
| B147 | `group_by` set → `date_shift_group_by_not_native` (pre-mask sibling anchor stays on the oracle) | :182-183 | |
| B148 | no explicit `date_format` → `date_shift_requires_date_format` | :184-186 | |
| B149 | `date_format` is `"mixed"` or `"ISO8601"` → `date_shift_special_date_format` | :187-188 | |
| B150 | tz directive in format → `date_shift_timezone_directive` | :189-192 | |
| B151 | `min_days`/`max_days` not a real int (bool or non-int) → `date_shift_{key}_not_int` | :147-158, called :193-196 | |
| B152 | `min_days`/`max_days` exceeds `MAX_ABS_SHIFT_DAYS` → `date_shift_{key}_out_of_range` | :147-158 | |
| B153 | resolved source type != `pa.string()` → `date_shift_source_not_string` | :197-206 | |

### 8d. `group_key_config_rejection` / `group_key_sibling_type_admitted`

| ID | Predicate → outcome | file:line | Witness |
|---|---|---|---|
| B154 | `group_by` not a nonempty string → `group_key_requires_group_by` | :256-258 | |
| B155 | `length` not a real int → `group_key_length_not_int` | :259-261 | |
| B156 | `length` odd or outside `[8, 64]` → `group_key_length_out_of_range` | :262-263 | |
| B157 | resolved sibling type outside `{string, int64, bool}` (`_NATIVE_GROUP_KEY_SIBLING_TYPES`, deliberately narrower than the operator's full stringify-safe set, to match passthrough's own admitted-resident set) → `group_key_group_by_type_not_native` | :264-269, set at :221 | |

## 9. Native chunked-masking dispatch (`execution/native/_dispatch.py`, `_static_route_decision` + `plan_native_route`)

Distinct third lane: chunked (streaming) masking with native kernel execution, separate from both the unified full-frame lane (section 1) and the pandas-only chunked mode (section 2).

| ID | Predicate → outcome | file:line | Route/backend | Minimal fixture | Witness |
|---|---|---|---|---|---|
| B158 | table participates in any declared relationship (parent or child) → whole table reroutes to oracle (`fk_relationship_not_native_route`) | :165-174, :199-200 | oracle (chunked pandas) | any FK edge touching the table | |
| B159 | table has no mask nodes → `no_mask_nodes`, oracle | :204-205 | oracle | | |
| B160 | non-scalar node (`<group>`/`<composite>`) → `non_scalar_node:<kind>` veto, whole table to oracle | :217-219 | oracle | composite FK group or bundle strategy on the table | |
| B161 | strategy in `CHUNKED_ROUTE_VETOED_STRATEGIES` (categorical, bucket_perturb -- native on full-frame but explicitly vetoed here: eager per-chunk emit can't resolve their data-dependent output type) → whole table to oracle | :222-231 | oracle | a categorical or bucket_perturb column on an otherwise-native-eligible chunked table | |
| B162 | `node.fallback_policy != "native"` → `fallback_policy_not_native`, whole table to oracle | :234-235 | oracle | any strategy whose resolved fallback_policy isn't native (e.g. an ineligible faker column) | |
| B163 | strategy in neither `NATIVE_KERNEL_STRATEGIES` nor `NATIVE_POOL_STRATEGIES` (defense-in-depth; should be unreachable given B162) → `no_native_kernel_or_pool` | :232-244 | oracle | | |
| B164 | (first-chunk schema check) admitted-columns set != actual first-chunk schema → `uncovered_columns:...;missing_configured_columns:...`, downgrade to oracle | :311-330 | oracle | a chunk whose columns don't match the compiled plan's coverage | |
| B165 | faker node's resolved source type not string/large_string → `faker_source_type_not_string`, whole table downgraded (a non-string source can drift type across chunks, e.g. nullable Int64 → float64) | :332-351 | oracle | faker column over a numeric source | |
| B166 | any hash node + `load_compiled_crypto_kernel()` raises `CryptoExtensionUnavailableError` → `crypto_extension_unavailable`, whole table downgraded | :353-357 | oracle | hash-strategy chunked job when the native companion is absent (**live on this devbox** per preflight.json) | |
| B167 | any faker node + `load_compiled_index_kernel()` raises → `index_extension_unavailable`, whole table downgraded | :359-365 | oracle | faker-strategy chunked job when the native companion is absent (**live on this devbox**) | |
| B168 | (evidence) faker column executed via `sample_faker_array` → `evidence.pool_select_executed=True`, `pool_select_calls += 1` | `_chunk_masking.py:219-220` | native pool select | admitted faker column on the native chunked route, companion present | |
| B169 | (evidence) hash column executed via `native_keyed_hash` → `evidence.compiled_kernel_executed=True` (never falls back to a pure-Python reference internally -- a successful call IS the compiled kernel) | `_chunk_masking.py:193-203` | native compiled kernel | | |

## 10. Faker pool eligibility (generation side, `generation/_faker_pool.py`)

Generation's per-row `faker` path (`synthesize.py::_faker`) vs. the vectorized pool bridge. **This never touches the Rust/native companion** -- confirmed by grep: no `native` import or reference anywhere in `generation/*.py` beyond a docstring mentioning a hypothetical future "v2-native rewrite". The pool bridge is pure Python/NumPy.

| ID | Predicate → outcome | file:line | Witness |
|---|---|---|---|
| B170 | `pool_eligible`: `opted_out or n < N_THRESHOLD` (default 50,000) → False, stays per-row | :263-264 | |
| B171 | `faker_type in POOL_ELIGIBLE_FAKER_TYPES` (legacy global 10-type allowlist: first_name, last_name, name, prefix, suffix, city, state, country, job, company) → True regardless of locale | :265-266 | |
| B172 | else, locale resolved (`None`→`en_US`, `str` as-given, anything else -- notably a locale LIST -- declines) then `(faker_type, locale) in POOL_ELIGIBLE_LOCALE_PAIRS` (the 2026-09-18 widening set, 29 types × 5 certified locales) → pooled only in the certified locale | :267-273 | |
| B173 | `try_pool` → `build_and_sample` → `build_pool_values`: `custom_override_present or not exact_name_available` → pool build declines, falls through to per-row (`resolve_pool_provider`, checked under lock) | :276-314, :346-356 | |

`N_THRESHOLD = 50_000` (L230) is the size knob for route-equivalent-at-small-scale testing of this path.

## 11. Masking Faker pool (contrast with generation)

Masking's own `faker` strategy pooling (the `PoolBuilder`/`ProviderRegistry` V2 machinery, distinct from section 10's generation-local bridge) feeds the native chunked dispatch's `pool_select` evidence (section 9, B168) when the table is admitted to that lane; on the unified full-frame lane (section 1) `faker` is not in `ALLOWED_OPERATOR_IDS` at all (B028), so a full-frame faker column always stays on the legacy pandas adapter regardless of pooling.

## 12. Subset routing (`subset/_preflight.py`, `run_subset_preflight`)

Subsetting is Polars-only end to end; it never touches the Rust/native companion. Composition: `run_subset` first, writes Parquet, then those files feed the (unchanged) mask path above as `sources` -- so "subset then mask" is two fully independent routing passes, not a combined decision.

| ID | Code → trigger | file:line | Witness |
|---|---|---|---|
| B174 | `subset_unknown_table` -- a relationship or seed names a table with no `sources` entry | :88-100 | |
| B175 | `subset_requires_parquet` -- a referenced table's source format != `"parquet"` | :101-110 | |
| B176 (early-exit) | either of the above → preflight fails immediately, schemas not even read | :112-116 | |
| B177 | `subset_relationship_duplicate_column` -- duplicate column named in one edge-end's key tuple | :131-140 | |
| B178 | `subset_reserved_column` -- a key column is named `RI` (the engine's reserved row-index name) | :142-151 | |
| B179 | `subset_relationship_column_missing` -- a declared key column absent from the table's schema | :152-161 | |
| B180 | `subset_relationship_key_float_unsupported` -- parent or child key column is a float dtype | :172-183 | |
| B181 | `subset_relationship_type_mismatch` -- parent/child key dtypes incompatible (not both-integer, not equal) | :185-195 | |
| B182 (early-exit) | any of B177-B181 → preflight fails, the key-level orphan scan (B183) never runs | :199-204 | |
| B183 | `subset_source_orphans` -- under `orphan_policy="fail"`, a non-null child key has no match in the deduped parent key set (a `warn` policy instead appends to `warnings`, does not fail) | :206-256 | |

## 13. Generation routing (`execution/_pipeline_generate_mask.py`)

| ID | Finding | file:line | Witness |
|---|---|---|---|
| B184 | Generate-kind tables always run through `generation.synthesize.generate_tables` (Plan-only, filters by `generate_columns` presence internally) -- there is no routing DECISION here beyond `has_generate_table`; generation is never native, chunked, or out-of-core. It always runs before the mask step, and its outputs are merged into the mask adapter's `sources` so an FK parent that is itself a generate table feeds the mask side directly | :95-107, :122-129 | |
| B185 | `route_chunked` (from section 3) is passed through to gate whether the SUBSEQUENT mask step runs chunked (`run_mask_chunked`) or full-frame (`adapter.run`); the eligible chunked shape is exactly one mask table with no generate tables (section 2, B042), so a job that reaches here with `route_chunked=True` has, by construction, no generate table at all | :131-148 | |

## 14. Environment-dependent knobs for route-equivalent-at-small-scale fixtures

| Knob | Default | Where read | Effect |
|---|---|---|---|
| `auto_chunk_threshold_rows` | 100,000 | `_planner.py:97`, `run_pipeline` kwarg | B054 -- chunked route admission floor |
| `out_of_core_threshold_rows` | 5,000,000 | `_planner.py:135`, `run_pipeline` kwarg | B097 -- out_of_core auto-selection floor (row-count mode only) |
| `full_frame_reject_rows` | 7,500,000 | `_planner.py:146`, `run_pipeline` kwarg | B099/B100 -- reject-before-read ceiling |
| `out_of_core_reorder_threshold_rows` | 2,000,000 (`REORDER_PARENT_KEY_THRESHOLD`) | `_route_policy.py:49`, `0` forces every eligible table | B128 -- batch_join vs reorder |
| `use_byte_estimate_routing` | **True** (TB-5 default; several in-repo docstrings describing it as "defaults False" are stale relative to the actual parameter default at `_pipeline_routing.py:249`) | `decide_execution_route` kwarg | B091-B096 vs B097-B101 -- which routing regime is live |
| `use_probe_routing` | **True** | `decide_execution_route` kwarg, `_pipeline_routing.py:251` | B093/B107-B108 -- micro-probe recovery |

Overriding any row-count threshold down lets a small fixture cross it and exercise the branch: this is **route-equivalent only** (the code path taken matches what a real large job would take) and explicitly NOT capacity-proven (spill, cardinality, Arrow offset limits, row groups, and disk admission are untested at small scale), per the plan's own distinction.

## 15. Platform decisions (`/home/cam/vscode/decoy-platform`, read via `git show origin/main:<path>`, commit `0701a954`)

Line numbers below are from the `origin/main` blob content (`git show origin/main:<path> | cat -n`), not the stale local working tree (confirmed behind origin). All reads were `git show`; nothing in the platform repo was modified.

### 15a. FK admission pricing (`api/jobs/admission_fk.py`, 699 lines)

`is_fk_bounded_route_candidate` decides whether the 0.75x `FK_BOUNDED_ROUTE_DISCOUNT` applies to the job's price estimate; `out_of_core_admission_eligible` decides whether the small DuckDB-budget reservation applies instead of the sequential/full-frame one. Both are config+footer-only proxies for the engine's own runtime decisions (section 4/6), not the runtime decisions themselves.

| ID | Predicate → outcome | file:line | Result | Minimal fixture | Witness |
|---|---|---|---|---|---|
| B211 | `not isinstance(config, dict)` → `(False, "config not a mapping")` | admission_fk.py:252 | full multiplier (no FK discount) | pass a non-dict config directly to the helper (unit-level) | |
| B212 | no `relationships` → `(False, "no_relationships")` | :255-256 | standard multiplier | any job config with no `relationships:` block | |
| B213 | any table has `generate_columns` → `(False, "generate_plus_mask")` | :261-262 | not a discount candidate (matches the runtime's own generate+mask sequential disqualifier, section 4a B074) | relationship config with a `generate_columns` table | |
| B214 | top-level `validators` present → `(False, "validators_present")` | :263-264 | larger validators-phase pricing | relationship config with a `validators:` block | |
| B215 | any column has `vault: true` → `(False, "vault_writer_requested")` | :265-268 | larger vault-phase pricing | relationship config with a vault column | |
| B216 | **self-referential FK edge → `(False, "self_referential_fk_edge")`, but per the code's own comment (:270-278) this does NOT disqualify the job from `run_sequential` at runtime** -- a real divergence between the admission proxy and the engine's own runtime router (section 4b: sequential does NOT treat self-FK as a cycle) | :279-284 | excluded from the discount purely as a conservative pricing choice; still runs sequential at full price | an `employees.manager_id -> employees.id` self-FK edge | |
| B217 | falls through all checks → `(True, "pure_mask_fk")` | :286 | `fk_aware_multiplier` applies the 0.75x discount | N-table FK config, mask-only, no validators/vault, no self-FK | |
| B218 | any exception in the block → `(False, "fk_bounded_route_check_raised")` | :287-292 | fail-open to the larger estimate | malformed config that raises inside the helper | |
| B219 | `fk_aware_multiplier`: candidate AND `likely_out_of_core_eligible` → same discount, reason string documents (informational only) OOC-strategy eligibility | :336-342 | 0.75x discount, annotated | pure-mask FK candidate where every FK-adjacent column's strategy is OOC-supported | |
| B220 | `fk_aware_multiplier` top-level exception → `(None, None)` | :344-349 | caller uses the unmodified default multiplier | same malformed-config class as B218 | |
| B221 | `out_of_core_admission_eligible`: `not isinstance(config, dict)` → `(False, "config_not_a_mapping")` | :620-621 | priced at the larger sequential/resident phase | non-dict config | |
| B222 | `_out_of_core_structural_miss` reuses `is_fk_bounded_route_candidate`'s rejections (propagates B212-B218) | :381-387 | not OOC-eligible | same fixtures as B212-B218 | |
| B223 | any table has `transforms` → `"per_table_transforms_present"` | :390-391 | not OOC-eligible (raw-Parquet LazySource can't honor transforms) | FK-candidate config with a `transforms:` table | |
| B224 | `sources` missing/empty → `"no_sources"` | :393-395 | not OOC-eligible | FK-candidate config with empty `sources:` | |
| B225 | any source not `{"type":"file","format":"parquet","path":<truthy>}` → `"non_local_parquet_source"` | :396-403 | not OOC-eligible | CSV source, or `type: "s3"` | |
| B226 | any column has `when` → `"when_predicate_present"` | :405-410 | not OOC-eligible | all-Parquet FK config with a `when:` column | |
| B227 | any child FK key has >1 column → `"composite_fk_key"` | :418-421 | not OOC-eligible (conservative) | composite FK child (2+ columns) | |
| B228 | same `(table, columns)` child key targeted by >1 relationship → `"multi_parent_child"` | :424-425 | not OOC-eligible | one child column declared as FK to two different parents | |
| B229 | table-level FK graph cyclic (Kahn's algorithm, `ordered != len(tables)`) → `"cyclic_fk_graph"` | :426-427 (cycle check :431-464) | not OOC-eligible | A→B→C→A parent/child edges (excluding true self-edges, caught by B216) | |
| B230 | structurally clean → `None`, proceeds to size/strategy/dtype gates | :428 | -- | | |
| B231 | **the row-threshold check the plan explicitly calls out**: `_largest_mask_source_footer` unreadable → `"mask_source_footer_unreadable"`; else `rows < OUT_OF_CORE_ADMISSION_THRESHOLD_ROWS` → `"below_out_of_core_threshold (rows=...)"`. `OUT_OF_CORE_ADMISSION_THRESHOLD_ROWS` (:149) sources `decoy_engine.execution.OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT` (fallback `5_000_000` if the import fails, :152) -- **the same upstream engine constant** `v2_out_of_core._OUT_OF_CORE_THRESHOLD_ROWS` (B238) sources, so they agree by construction TODAY, but nothing enforces it at call time; overriding one for a small fixture without the other desyncs claim-time pricing from runtime routing -- **exactly the "override together and assert they agree" risk the plan names** | :625-630 (constant :149, import :146-152) | not OOC-eligible below threshold; footer-unreadable fails toward the larger reservation | a Parquet fixture at threshold-1 vs. threshold rows, run through claim | |
| B232 | strategy surface unknown (import failed) → `"out_of_core_strategy_surface_unknown"`; else any non-`from_parent` column strategy outside `OUT_OF_CORE_SUPPORTED_STRATEGIES` → `"strategy_{strategy}_not_out_of_core_supported"` | :475-488 | not OOC-eligible | a mask column using a strategy outside the engine's OOC-supported surface | |
| B233 | FK-key dtype unsupported/undeterminable -- five distinct reasons: `fk_key_sources_malformed`, `fk_key_source_missing`, `fk_key_source_pathless`, `fk_key_footer_unreadable`, `fk_key_dtype_undeterminable`, `fk_key_dtype_out_of_core_unsupported` | :557-579 (dtype table :492-522) | not OOC-eligible | FK-key column with uint64, tz-aware timestamp, or decimal physical dtype | |
| B234 | all gates pass → `(True, "out_of_core_admission_eligible")` | :637 | small DuckDB-budget reservation applies | pure-mask, all-Parquet, 2+-table FK config, no transforms/when/composite-FK/multi-parent/cycle, largest table ≥ threshold, every strategy + FK-key dtype OOC-supported | |
| B235 | whole-check exception → `(False, "out_of_core_admission_check_raised")` | :638-643 | fail-closed to the larger reservation | | |

### 15b. Runtime out-of-core dispatch (`api/jobs/v2_out_of_core.py`, 420 lines) -- distinct from admission pricing above

| ID | Predicate → outcome | file:line | Result | Minimal fixture | Witness |
|---|---|---|---|---|---|
| B236 | `not _should_use_sequential_relationship_path(config)` → `False` (OOC eligibility is a strict subset of sequential eligibility) | :178-179 | stays full_frame, never reaches the OOC-vs-sequential fork | config with no relationships, or a relationship config with `generate_columns` | |
| B237 | (runtime) any table has `transforms` → `False` | :184-185 | falls back to sequential (`v2_sequential.py`) | FK sequential-eligible config with a `transforms:` table | |
| B238 | (runtime) no sources → `False` | :188-189 | sequential fallback | empty `sources:` | |
| B239 | (runtime) any source not Parquet-file → `False` | :191-197 | sequential fallback | CSV-sourced FK config | |
| B240 | (runtime) footer read failure on a mask-kind source (missing path, or `pq.read_metadata` raises) → `False`, logs a warning | :209-221 | sequential fallback (fail-safe) | missing `path`, or a corrupt Parquet file | |
| B241 | **the runtime-side sibling of the admission threshold check**: `largest_rows >= _OUT_OF_CORE_THRESHOLD_ROWS`. Constant defined at :102, set directly to `OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT` (no try/except fallback, unlike B231's defensive import) -- the SAME upstream engine constant as B231; nothing enforces the two stay in sync if patched independently | :223 (constant :102) | ≥ threshold: proceed to try OOC (subject to the compat gate below); < threshold: sequential | Parquet fixture at threshold-1 vs. at/above threshold, run end to end through claim then dispatch to see admission reservation AND runtime route agree | |
| B242 | **engine compat-gate rejection post-compile**: `check_out_of_core_compatibility` (engine section 6) declines → `_OutOfCoreNotEligibleError` | :295-299 | caught by orchestrator, falls back to `run_sequential_relationship_job` | a config that passes the platform's cheap structural gate (B236-B241) but the engine's real compat gate declines post-compile (e.g. an unsupported strategy the config-only proxy can't see) | |
| B243 | DuckDB budget unresolved → `raise ExecutionError(code="out_of_core_budget_unresolved")` -- **hard failure, NOT a sequential fallback** | :315-324 | job hard-fails | simulated host-RAM/cgroup detection failure in `resolve_budget(None)` | |
| B244 | **runtime FK-key dtype rejection (dennis+Codex HIGH-1 fix)**: only `"out_of_core_fk_key_dtype_unsupported"` is translated to the same fallback signal as B242 (→ sequential); every OTHER `ExecutionError` code re-raises and hard-fails | :362-364 (fallback code set :143) | one code → sequential fallback; all others → hard fail | an FK-key column whose dtype passes the config+footer-only proxy (B233's inverse) but whose real mid-stream batch-join typing is value-dependent (e.g. "5.1M rows but not 4.9M" per the module's own docstring example) -- **flagged HIGH VALUE to witness**, a real prior bug this translation fixed | |
| B245 | all gates pass → out-of-core executes, table published via `ParquetTransactionalSink`, `node_execution_mode="out_of_core_fk"` | :365-420 | out_of_core | same fixture as B234, executed end to end | |

### 15c. Claim-time phase-1 streaming eligibility (`api/jobs/_phase1_eligibility.py`, 307 lines)

ALL-OR-NOTHING per config: a single table's rejection fails the whole config to `full_frame`.

| ID | Predicate → outcome | file:line | Witness |
|---|---|---|---|
| B246 | `not isinstance(config, dict)` → `["config_malformed"]` | :104-105 | |
| B247 | no tables → `["config_malformed"]` | :107-110 | |
| B248 | `config.get("relationships")` → `"relationships_not_supported"` -- this is exactly the shape that instead goes down the sequential/OOC branch (section 15b), so relationship configs and phase1-streaming configs are mutually exclusive by construction | :117-118 | |
| B249 | top-level `validators` → `"validators_not_supported"` | :119-120 | |
| B250 | table config malformed (not a dict, or `name` not a string) → `["table_config_malformed"]` | :147-148 | |
| B251 | table has `generate_columns` → `"generate_table_not_supported"` | :152-153 | |
| B252 | table has `transforms` → `"transforms_not_supported"` | :154-155 | |
| B253 | any column has `vault: true` → `"vault_not_supported_streaming"` | :160-162 | |
| B254 | **any column has `when`** → `"when_predicate_not_supported_streaming"` -- a Codex FINAL-gate fix-round-2 finding (#4), DELIBERATELY RE-ADDED after Task 1.2 originally dropped it on a wrong "row-local is chunk-safe" theory; now matches `admission_fk.py`'s identical `when_predicate_present` exclusion (B226) | :171-175 (docstring :163-170) | |
| B255 | `native_route_eligibility(config, table=...)` (engine's own config-blind query) rejects for any tracked reason → `"native_route_ineligible: {reason}"` (defense-in-depth, can only ADD rejections) | :203-206 | |
| B256 | **the admission rule, not a mechanical capability read**: strategy not in `_PHASE1_ALLOWED_STRATEGIES = {"hash", "redact", "truncate", "passthrough"}` → `"strategy_not_allowlisted_for_streaming"`. Deliberately narrower than what the engine's capability queries alone would admit -- the module docstring (:19-41) names `categorical` as the proof case: statically diagnostics-clean but excluded because its default config draws an unseeded whole-column RNG the capability query can't see without reading `provider_config`. **Highest-value branch to witness for "why isn't my job streaming."** | :216-220 (allowlist :72-74) | |
| B257 | `capabilities_for(strategy)` raises `KeyError` (unclassified strategy) → `"unclassified_strategy_excluded"` | :222-229 | |
| B258 | safety-check layer ON TOP of the allowlist: any of `row_error_modes` / `warning_codes` / `quality_obligations` / `quarantine_required` non-empty/True for an ALLOWLISTED strategy → its own coded exclusion, even though the strategy passed B256 | :243-250 | |
| B259 | computed `input_bytes` is `None` or below `settings.streaming_min_input_mb` → `"input_below_streaming_threshold"` | :265-269 | |
| B260 | all gates clean → `StreamingPlan(tables=...)`, naming every table (all-or-nothing) | :132-134 | |

### 15d. Claim-time consumption + adaptive-scheduler DispatchPlan route selection

Flag: `settings.adaptive_scheduler_lease_authority_enabled` (api/config.py:356, default **False**).

| ID | Predicate → outcome | file:line | Result | Witness |
|---|---|---|---|---|
| B261 | `classify_route_at_claim` (the ONE gate both claim paths call): `not settings.streaming_execution_enabled` → `RouteAtClaim("full_frame", None, ("streaming_execution_disabled",))` | admission.py:1050-1051 | full_frame regardless of phase1_eligibility | `STREAMING_EXECUTION_ENABLED=false` on an otherwise phase1-eligible job | |
| B262 | `classify_route_at_claim`: post-validation-enforce mode active → `RouteAtClaim("full_frame", None, ("post_validation_enforce_active",))` -- streaming never runs post-validation | admission.py:1053-1056 | full_frame | phase1-eligible job on an instance with post-validation enforce on | |
| B263 | `classify_route_at_claim` (else) → delegates to `phase1_eligibility` (section 15c); reasons → full_frame, else `RouteAtClaim("streaming", plan, ())` | admission.py:1058-1064 | | | |
| B264 | **flag OFF**: `_claim_next_job` (queue_worker.py) consumes `classify_route_and_disk_at_claim`'s result directly; `phase1_streaming_tables` set only when `route=="streaming"` with a non-None plan AND disk preflight passes | queue_worker.py:529-534,560-563 | sets `Job.phase1_streaming_tables` | submit a phase1-eligible job with the flag off | |
| B265 | **flag ON**: `_claim_next_job` bypasses its own legacy body ENTIRELY, delegating to `scheduler_claim_loop.claim_one_flag_on` → `scheduler_claim.claim_and_stamp` | queue_worker.py:459-462 | different code path entirely | toggle the flag and confirm which claim function runs (integration-level) | |
| B266 | `claim_and_stamp`: `phase1_streaming_tables` set when `Verdict.route == "streaming"` AND a `streaming` `PricedRoute` was built by `price_candidate_routes` | scheduler_claim.py:503-506 | | same phase1-eligible fixture, flag on, with a `BoxState` where streaming fits | |
| B267 | `price_candidate_routes`: always prices `full_frame`; ADDS a `streaming` candidate only `if claim_route.route == "streaming" and claim_route.reservation is not None` | scheduler_claim.py:348-380 | candidate list `decide()` chooses among | | |
| B268 | `_fits_now`: `committed_mem_mb + peak_mem_mb + mem_margin_mb <= alloc_mem_mb AND committed_cores + threads <= alloc_cores AND (alloc_disk_mb - committed_disk_mb - disk_floor_mb) >= peak_disk_mb` | scheduler_admission.py:103-108 | filters to `fitting` candidates | a `BoxState` where only one of full_frame/streaming fits given their priced `peak_mem_mb` | |
| B269 | concurrency gate: `not (box.cgroup_per_job or box.running_jobs == 0)` and nothing admits now but something fits alone → `DEFER`, "single-job conservative mode" | scheduler_admission.py:184,200-208 | DEFER, no route chosen | `BoxState(cgroup_per_job=False, running_jobs=1, ...)` | |
| B270 | **ADMIT, the actual route-selection predicate**: among `fitting` candidates, `max(fitting, key=throughput_rows_s)` wins | scheduler_admission.py:186-197 | `Verdict.route` = whichever route has the highest throughput among routes that fit NOW; full_frame wins when both fit (higher throughput constant), streaming wins only when full_frame's larger reservation doesn't fit | two `BoxState` fixtures: both fit (expect full_frame) vs. only streaming's smaller reservation fits (expect streaming) | |
| B271 | DEFER: nothing fits now but `_fits_alone` is true for some route → "waiting for capacity" | scheduler_admission.py:199,209-214 | DEFER | high `committed_mem_mb` such that nothing fits now but something would fit on an empty box | |
| B272 | REJECT, memory-bound: `min(peak_mem_mb) + mem_margin_mb > alloc_mem_mb` | scheduler_admission.py:216-219 | REJECT | `alloc_mem_mb` smaller than even streaming's floor | |
| B273 | REJECT, core-bound: `min(threads) > alloc_cores` | scheduler_admission.py:220-221 | REJECT | | |
| B274 | REJECT, disk-spill-bound (else) | scheduler_admission.py:222-223 | REJECT | | |
| B275 | `claim_priced`: ADMIT → `DispatchPlanBindingRow.route = chosen.route`, `allowed_fallbacks = fallbacks_within_reservation(chosen, routes)` (every other priced route whose peak_mem/peak_disk are `<=` chosen's) | host_lease_authority.py:490-494,517-527 | route frozen for this lease | | |
| B276 | real job launch's `DispatchPlan.route` is a straight passthrough of the binding (`route=binding.route`) -- no second decision point between claim and launch. **Contrast**: `preflight_dispatch.py`'s `_preflight_plan` builds a degenerate `DispatchPlan(route="", ...)` for the preflight subprocess only | cgroup_supervisor_job_launch.py:95; preflight_dispatch.py:194-218 | | | |
| B277 | **the last layer**: `force_route` -- `plan.route not in ALL_ROUTES` → `DispatchError`; `plan.route in available` → runs as planned; else first `plan.allowed_fallbacks` entry in `available` → `is_fallback=True`; else `DispatchError`, fail-closed reject. `available_routes` computes `{full_frame}` always, `+streaming` iff `claim_streaming_plan is not None`, `+sequential` iff sequential-eligible, `+out_of_core` iff sequential-eligible AND OOC-eligible AND not overridden -- this is where the config predicates (sections 15b/15c) actually gate the FORCED route, independent of what `decide()` priced | dispatch_route.py:113-140 (available_routes :70-101) | 3 outcomes: planned route runs; a priced-ladder fallback substitutes (**high-value to witness**); or hard reject | planned-route case: a claim priced `streaming` still phase1-eligible at dispatch. Fallback case: an OOC-priced job that declines post-compile (B242/B244) with `sequential` in `allowed_fallbacks`. Reject case: a planned route that becomes unavailable between claim and dispatch with no laddered fallback | |
| B278 | mid-run OOC→sequential reroute must ALSO be on the priced ladder: `assert_runtime_fallback_allowed` -- `fallback_route not in plan.allowed_fallbacks` → `DispatchError` (never a silent, unpriced reroute) | dispatch_route.py:143-157; call site v2_orchestrator.py:384-387 | fail-closed guard distinct from B277's initial pick | force a claim where sequential's peak_mem/peak_disk are NOT `<=` the chosen OOC route's, then trigger a runtime OOC decline | |

### 15e. RESOLVED: what code `phase1_streaming_tables` actually dispatches to (plan's open question, check 5)

Confirmed by reading the platform code directly (not inferred): the call chain is

`v2_orchestrator.run_v2_pipeline_job` (line 298, `_run_streaming` branch) → `v2_stream_coordinator.run_claim_time_streaming_route` (v2_stream_coordinator.py:256) → `api.jobs.v2_runner._run_v2_pipeline_streaming` (imported v2_stream_coordinator.py:158, defined v2_runner.py:314) → **`decoy_engine.execution.run_mask_pipeline_chunked`** (imported v2_runner.py:375).

This is a live, currently-imported engine function, not the deleted standalone native route. Engine PR #166 deleted `native_route_enabled` and the standalone "native route" flag/branch that the engine's `run_pipeline` auto-router used to expose (superseded by `unified_slice_enabled`), which is consulted only by the **full-frame** `run_pipeline(...)` call sites (v2_runner.py:298-311, v2_preview.py) -- a separate code path from phase1-streaming entirely. So there is no dangling reference in the phase1-streaming consumption path; the two "native"/"streaming" concepts are easy to conflate by name but are architecturally distinct, and the ledger should keep them distinct.

## 16. CLI mode switches (`/home/cam/vscode/decoy`, `src/decoy/cli/run.py` + `src/decoy/_native_gate.py`)

Read at commit `b8274b0194748d5a60262b78b5969a8c85aeeac7` (local working tree, confirmed identical to `origin/main` by SHA comparison and a clean `git status`). All --native/--no-native dispatch logic lives in `_native_gate.py`, which `run.py` imports and delegates to; there is no separate `_dispatch.py`. Four engine entry points total: `run_pipeline(...)` (default and `--native`, identical call, differing only in pre/post gating), `run_pipeline(..., unified_slice_enabled=False)` (`--no-native`, same function with one forced kwarg), and `run_mask_pipeline_chunked(...)` (`--chunked`, a distinct function called once per mask-kind table).

| ID | Predicate → outcome | file:line | Route/backend | Minimal fixture | Witness |
|---|---|---|---|---|---|
| B186 | `native and no_native` → raise `NativeGateError(code="native_flag_conflict")`, EXIT_USAGE | run.py:576-580 | reject before dispatch | `decoy run pipeline.yaml --native --no-native` on any config | |
| B187 | `native_intent` ternary: `"require" if native else "disable" if no_native else "default"` | run.py:581-583 | -- | | |
| B188 | **default mode**, `chunked` false → `else:` branch → `run_pipeline(config_dict, sources, ..., **run_pipeline_kwargs(gate_result))` with `gate_result.unified_slice_enabled` normally True → kwargs `{}` (no override; engine's own default applies) | run.py:605,615,618-626; _native_gate.py:479-506 | `run_pipeline`, engine's own default routing | `decoy run pipeline.yaml` on a single-table `redact` config, CSV | |
| B189 | default-intent gate: `vault_active or not candidate or not static_native_eligibility(config_dict)` → `PreGateResult(unified_slice_enabled=True)` (plain pass-through, no probe) | _native_gate.py:382-383 | unified slice stays at engine default | any config with a live vault writer, or not native-shape-eligible | |
| B190 | default-intent gate, config IS native-shape-eligible (`static_native_eligibility`: exactly 1 table, no relationships/validators/quarantine/run_storm/transforms, every strategy in `_NATIVE_STRATEGIES`, ≥1 companion-dependent strategy, no vault/when columns, no non-deterministic categorical, source format exactly parquet -- _native_gate.py:208-298) AND companion probe reports present-but-broken (`status.ok is False`, `status.reason != "absent"`) → raise `NativeGateError(code=f"native_companion_{status.reason}")` **even with no --native flag at all** | _native_gate.py:399-408 | reject before dispatch | plain `decoy run pipeline.yaml` (no native flags) on a native-eligible single-table parquet/hash config, with a present-but-ABI-mismatched or KAT-corrupt companion installed | |
| B191 | default-intent gate, eligible but companion simply absent (`status.reason == "absent"`) → informational hint printed, proceeds on the Python fallback (no reject) | _native_gate.py:391-398; run.py:591-595 | Python fallback (legacy pandas or whatever the engine default resolves to) | native-eligible config with no companion installed (**live on this devbox** per preflight.json) | |
| B192 | `is_unified_slice_candidate(chunked, any_generate) = not chunked and not any_generate` | _native_gate.py:182-189 | -- | | |
| B193 | `--native`, `not candidate` (i.e. `--native --chunked`, or `--native` on a config with any generate table) → raise `NativeGateError(code="native_require_not_a_candidate")` | _native_gate.py:323-331 | reject before dispatch | `decoy run pipeline.yaml --native --chunked` on any minimal config | |
| B194 | `--native`, `vault_active` (a live `--vault` writer with declared vault columns) → raise `NativeGateError(code="native_require_vault_active")` | _native_gate.py:332-342 | reject | `--native --vault <path>` on a config with a `vault: true` column | |
| B195 | `--native`, installed engine's companion-status probe attribute missing (too old) → raise `NativeGateError(code="native_require_engine_too_old")` | _native_gate.py:343-352 | reject | `--native` against an old `decoy-engine` install lacking `native_companion_status` | |
| B196 | `--native`, `not status.ok` (absent/abi-mismatch/kat-corrupt/load-error) → raise `NativeGateError(code=f"native_require_{status.reason}")` | _native_gate.py:353-358 | reject | `--native` with no companion installed (**live on this devbox**) | |
| B197 | `--native`, all gates pass → `PreGateResult(unified_slice_enabled=True)`, `run_pipeline_kwargs` returns `{}` (same `run_pipeline` call as default) | _native_gate.py:359,479-506 | `run_pipeline`, pre-gated on a healthy companion | `--native` on a native-eligible parquet/hash config with a healthy companion | |
| B198 | `--native` **post-run**: `route.state != "native"` → raise `NativeGateError(code="native_require_no_evidence")`, runs AFTER `run_pipeline` returns but BEFORE `_write_mask_outputs` -- **no output is written** | _native_gate.py:462-476; run.py:636 (before 637) | reject after execution, before write | `--native` on a config that is gate-admitted but whose actual run produces zero `compiled_kernel_executed=True` nodes | |
| B199 | `--native` post-run: `route.state == "native"` → success, `_write_mask_outputs` proceeds | _native_gate.py:467-476; run.py:637 | native | | |
| B200 | `classify_route` (used for both post-run verification and reporting): reads `quality_metrics["execution"]["execution_mode"]` + `unified_slice_activation.nodes[*].compiled_kernel_executed`; `state="native"` only if ≥1 node has it truthy | _native_gate.py:439-459 | -- (this is the CLI-side mirror of the plan's own "compiled_kernel_executed=True" claim rule) | | |
| B201 | **`--no-native`**, `intent="disable"` gate: computes a best-effort informational `warn_message` only (companion present-but-broken), never a hard reject beyond the shared flag-conflict check → `PreGateResult(unified_slice_enabled=False, warn_message=...)` | _native_gate.py:361-375 | forces legacy pandas | `--no-native` on any config | |
| B202 | `--no-native` → `run_pipeline_kwargs` returns `{"unified_slice_enabled": False}` if the installed engine's `run_pipeline` signature has that parameter (capability-detected via `inspect.signature`), else `{}` | _native_gate.py:479-506 | `run_pipeline(..., unified_slice_enabled=False)` -- same function as default/`--native`, one forced kwarg | `decoy run pipeline.yaml --no-native` | |
| B203 | `--no-native` post-run: `intent != "require"` → no-op, no assertion | _native_gate.py:467-468 | | | |
| B204 | `--chunked`, `any_generate` (any table has `generate_columns`) → raise `_ChunkedGenerateError`, EXIT_USAGE | run.py:538-543 | reject before dispatch | `--chunked` on a config with a generate-kind table | |
| B205 | `--chunked` (else) → `_run_chunked_mask` → per mask-kind table, `run_mask_pipeline_chunked(config_dict, chunk_iter, table=name, engine_version=..., adapter=..., vault_writer=...)` -- **a distinct engine entry point**, never `run_pipeline` | run.py:605-608,1039-1105 | chunked pandas/native-chunked stream (engine-side, section 9) | `decoy run pipeline.yaml --chunked --chunk-size 100000` | |
| B206 | `--chunked --native` together → rejected via B193 (`candidate=False` because `chunked=True`) | _native_gate.py:182-189,323-331 | reject | cross-reference B193 | |
| B207 | `--chunked --no-native` together → legal, no hard reject (disable-intent gate never checks `candidate`) | _native_gate.py:361-375 | chunked, legacy-pandas-forced | `decoy run pipeline.yaml --chunked --no-native` | |
| B208 | `--chunked` reporting: `classify_route` always returns `applicable=False, state=None, label=f"{resolved_substrate or 'pandas'} chunked stream"` regardless of what actually executed -- **a chunked run is never labeled "native" in CLI reporting**, even if the engine-side native chunked dispatch (section 9) ran compiled kernels underneath | _native_gate.py:429-434 | -- | an important CLI-vs-engine reporting gap: the engine's own `pool_select_executed`/`compiled_kernel_executed` evidence (section 9) is invisible at this CLI layer for `--chunked` runs | |
| B209 | `--substrate` / `DECOY_SUBSTRATE` env var consulted ONLY inside the chunked path (pandas vs. legacy polars adapter); on a non-chunked run it is ignored with a stderr warning | run.py:244-257,364-369,1077-1078 | | `--substrate polars` has an effect only combined with `--chunked` | |
| B210 | **CLI has no size/row-count-based auto-chunk routing at all** -- confirmed negative finding (grepped `auto_chunk`/`chunk_threshold` across `src/`, zero matches); `--chunked` is purely an explicit user flag, `--chunk-size` only sets the per-chunk row count once chunking is already chosen | (repo-wide grep, no single citation) | -- | any invocation without `--chunked`, regardless of input size, never auto-routes to the chunked engine entry point | |

Engine entry points, summarized: **default** → `run_pipeline(...)` no override kwarg; **`--native`** → identical `run_pipeline(...)` call, pre-gated on a healthy companion and post-verified for `compiled_kernel_executed` evidence (refuses to write output on a miss); **`--no-native`** → identical `run_pipeline(..., unified_slice_enabled=False)`; **`--chunked`** → `run_mask_pipeline_chunked(...)`, called once per mask-kind table, a wholly separate function.

## Open questions for the run stage

1. ~~Platform phase-1 streaming path after engine #166.~~ **RESOLVED, see section 15e.** Engine commit `d5539240` (PR #166) deleted the standalone native-route lane, which had zero production ownership even before deletion (its own plan doc says the platform repo had zero references to it). The platform's `phase1_streaming_tables` path was never calling that lane; it dispatches today to the live `decoy_engine.execution.run_mask_pipeline_chunked`, a separate, still-live primitive PR #166 did not touch. No dangling reference found.
2. **`_chunked_group_key.unsafe_group_key_group_by_columns`, `_chunked_text_mask.unsafe_text_mask_source_columns`, `_chunked_code_set.unsafe_code_set_source_columns`, `_chunked_bucket_perturb.unsafe_bucket_perturb_source_columns`** (referenced at B057/B058) were not read directly this pass; their exact per-column predicates are cited only by name and call site, not by their own file:line. A follow-up pass should open these four files.
3. **`_mem_estimate.py` (`fits`, `raw_data_bytes`) and `_probe.py` (`probe_peak_bytes`, `probe_fits`, `MIN_PLAUSIBLE_K_FULL_FRAME`)** were not read directly; B106-B108 cite their call sites and documented contracts from `_pipeline_routing_signals.py`'s docstrings, not their own internal branches. A follow-up pass should open these two files for the byte-estimate/probe math itself.
4. **`out_of_core/_compat.py`'s `_DEFERRED_GROUP_B` / `_DEFERRED_GROUP_C` / `_GROUP_C_CONDITIONAL` dicts and `_group_c_conditional_rejection`** (B123-B124) were seen only as call sites, not their per-strategy code/reason contents (the dicts and the ~30-line helper starting at line 323 were not read). A follow-up pass should read lines 1-160 and 320-408 of that file in full.
5. **`_chunked.py`'s `gate_fk_child_edges`** (backing B061-B065) was not opened directly; the codes are taken from `check_chunked_compatibility`'s own docstring, not verified against the implementation.
6. **Platform admission_fk.py / v2_out_of_core.py / DispatchPlan** line numbers in section 15 are from the `origin/main` git blob as fetched at preflight time (commit recorded in `docs/records/audit-2026-09-30/preflight.json`); if origin/main advances before the run stage, re-fetch and re-cite rather than trusting these line numbers.

## Correction: native companion "absent" in the preflight

The preflight reported the native companion absent. That is an artifact of this worktree, not a product fact: the worktree has no virtualenv of its own, and the environment the preflight borrowed does not have `decoy_engine_native` installed. The Track A worktree's environment on the same host reports `present=True, ok=True, abi decoy-native-abi-2, version 0.1.0`. B039's decline therefore applies only where the companion is missing. The run stage must use a dedicated environment with the companion built from the pinned engine tree, and must record `native_companion_status()` in every run.

Also noted for the map: the CLI's `classify_route` (B200) reports a job as `native` when at least one node has `compiled_kernel_executed` truthy, so a mixed job can be reported as native by the CLI. The map records per-node backends and does not use that label.
