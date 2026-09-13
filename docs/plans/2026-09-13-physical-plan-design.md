Status: record

# Physical Plan Design (Task 4.1) — v3

> Task 4.1 APPROVED by Cam 2026-09-13 on the ARCHITECTURE basis (see §12). The two-level
> driver/operator model, the driver taxonomy, invariants, generation/publication handling, companion
> gate, and the mapping to existing structures are the frozen Task 4.1 contract. The exhaustive
> compiler input set and the code-for-code rejection catalog are FINALIZED in Task 4.3 (the compiler
> build, where they are enumerated in code and verified by Task 4.4 shadow mode), per §12 — not by
> hand on paper here. Reviews: dennis GO (round 3, 0 blocker/high/medium); Codex GO on the model
> across rounds, NO-GO only on paper-exhaustiveness of the input set + catalog, which §12 relocates.

Reviewed design slice for Task 4.1 of
[2026-09-09-execution-consolidation-and-native-throughput.md](2026-09-09-execution-consolidation-and-native-throughput.md):
one backend-selection contract before any execution code moves, so Phase 4 can wrap today's executors
(4.2), build the compiler (4.3), shadow it (4.4), and migrate routes one slice at a time (4.5-4.7).

Revision history: v1 double NO-GO (topology collapse); v2 confirmed the two-level model but NO-GO'd on
completeness. v3 keeps the v2 model (both reviewers confirmed it) and closes the v2 gaps: it represents
the generation stage, makes the compiler input set total, fixes the companion-gate and publication
invariants to match current behavior, adds the provider-classification axis, and completes the frozen
rejection-code catalog.

Scope: representation-only. No execution, output, determinism, or publication-contract change. The exit
gate is that the physical plan can represent every current production route exactly, so Task 4.4 shadow
mode can assert code-for-code equivalence. Right-sized to single-org (one host, one job at a time by
default, ~100M-row ceiling). No distributed execution, no engine scheduler, no cryptographic change.

## 1. Problem: fragmented routing, an explain/live split, and two stages

Three facts shape the design:

- Backend choice is spread across independent mechanisms with their own vocabularies:
  `_pipeline_routing.decide_execution_route` (Layer 1), `_pipeline_chunk_route.decide_chunk_route`
  (Layer 2), the streaming native lane (`_native_route` + `_native_route_preflight`), the chunked
  native/oracle dispatch (`native/_dispatch`), and the not-yet-wired queries
  `native/_plan.native_route_eligibility` + `native/_phase3_eligibility`.
- `_planner.py` already has `ExecutionPlan{mode, rejections, reason}` recording the winning mode plus,
  in `rejections`, each *faster* mode that lost and why (`EXECUTION_MODES`, fastest-first: polars_native >
  chunked > sequential_relationship > out_of_core_relationship > pandas_fallback). But
  `PLANNER_ROUTING_ENABLED = False`: it is an EXPLAIN/chunked-admissibility surface, not the live router.
- A job has TWO stages. `run_pipeline` first runs the separate `generate_tables()` synthesis executor for
  generate-kind tables, then merges those resident outputs into the sources the masking stage reads
  (`_pipeline.py`). Generation is not one of the masking routes and is explicitly excluded from
  `NativePlanNode`/`NodeRequirements`.

The physical plan is the single immutable per-job artifact that represents both stages, the chosen driver
per table with its reasons, and the rejected faster alternatives, reconciling the explain surface with the
live routing.

## 2. The two-level model

- A **table driver** owns a whole table's execution: source residency, scheduling, publication path, and
  table-atomic fallback. Five masking drivers plus one synthesis driver (section 4).
- A **node operator** is one column's/group's lowering — its real `lowering_id = "{kind}:{strategy}"` and
  the concrete kernel that runs it, hosted by a driver. It never chooses a driver, publishes files, or
  defines behavior. The same strategy can have different operator implementations across drivers: e.g.
  `hash` runs the compiled Rust kernel under `native_chunk`, and the SHARED `decoy_engine.kernel.hash_array`
  Arrow kernel under both `full_frame` (fed a pandas Series) and `out_of_core` (fed the Arrow batches
  DuckDB streams) — one shared kernel, two feed shapes, not three distinct implementations.

Invariant 1 (section 6) is "one table *driver* per table," not "one implementation for every node" — which
is what lets the out-of-core driver host DuckDB relational joins alongside per-column masking, as the code
does today.

## 3. Physical plan schema

Frozen, per-job, compiled once at preflight, immutable after. Grows out of the inert
`NativeExecutionPlan` (`execution/native/_plan.py:87`) and subsumes the `ExecutionPlan` explain record.

The compiler is a pure function of the COMPLETE routing input set, not just `(Plan, profile)`:
- the validated pipeline `config` and the logical `Plan`;
- the source profile AND source representation (resident `pa.Table` vs `LazySource`), plus a
  `source_loader` presence flag;
- the run knobs that affect routing: `execution_mode`, `substrate`, `fallback_to_pandas`, `sink` type
  (transactional / legacy-callable / none), `native_route_enabled`, `auto_chunk`, `fidelity_report`,
  `vault_writer` presence, `chunk_size_rows`, `auto_chunk_threshold_rows`, `out_of_core_threshold_rows`,
  `full_frame_reject_rows`, `out_of_core_budget_bytes`, `out_of_core_reorder_threshold_rows`,
  `use_byte_estimate_routing`, `use_probe_routing`, `fpe_chunk_count`, `max_workers`;
- MEASURED source facts consulted during routing: largest-table row count (exact or byte-estimate),
  byte-fit estimate, probe recovery, and — for the OOC inner driver — sink presence, the deduplicated
  parent-key relation row count (from generated-relation Parquet metadata), and the measured maximum
  sort-payload width.

```
PhysicalPlan
  engine_version: str
  plan_hash: str                       # over the FULL routing input set above (incl. every route-affecting
                                        # knob and the HostBudget fields that can differ and are stored here)
  host_budget: HostBudget              # resolved per-job allocation (section 8)
  synthesis: SynthesisStage | None     # the generate_tables() stage (section 4); None for pure-mask jobs
  tables: tuple[PhysicalTable, ...]    # masking tables, dependency order
  relationships: tuple[PlanRelationship, ...]   # carried verbatim from Plan (lossless)
  job_diagnostics: JobDiagnosticObligations

SynthesisStage
  tables: tuple[SynthesisTable, ...]   # generate-kind tables, produced BEFORE masking, resident output,
                                        # merged into the mask stage's sources
  # operators map to Plan.generation (GenerationPlan), NOT NativePlanNode/NodeRequirements

PhysicalTable                          # a masking table
  table: str
  driver: DriverId                     # one of the five masking drivers (section 4)
  driver_reason: str                   # coded reason for driver
  rejected_alternatives: RejectedAlternatives   # see below
  inner_driver: {"batch_join","reorder"} | None # OOC only (+ ReorderCaps); None otherwise
  substrate: {"pandas","polars"}       # non-pandas admits only the full_frame polars-capable driver
  relationship_role: {"independent","parent","child"}
  ordering: OrderingNode | None
  residency: {"resident","evict_per_table","chunked","stream_per_batch"}
  publication: PublicationMode         # per-driver, UNCHANGED (section 7)
  nodes: tuple[PhysicalNode, ...]
  prepasses / state_tables / resource_estimate

PhysicalNode
  node_id: str                         # stable: table + column(s) + lowering_id
  columns / kind ("mask"|"group") / strategy
  operator: OperatorId                 # (driver, lowering_id, [compiled entry point + required ABI])
  operator_reason: str                 # coded reason for this operator OR a route-rejection code
  capabilities: StrategyCapabilities   # from capabilities_for(strategy), unchanged
  input_projection / input_schema / output_schema   # output None => output_type_indeterminate => oracle-only
  determinism: DeterminismBinding      # draw_family, entropy_root, key_source, namespace, partitionable, version
  required_prepasses / required_state_tables
  diagnostic_obligations: tuple[DiagnosticObligation, ...]  # backend-qualified (section 7)
  fallback_policy: {"native","python_only"}     # from NodeRequirements (section 6, inv 5)
  provider_class: {"pool_native","python_only","reject_large"} | None   # faker/provider axis (section 6, inv 5)
  resource_estimate: ResourceEstimate

RejectedAlternatives                   # reconciles the explain record with the live gates
  # An ordered list of (lane, coded_reason) for every lane FASTER than the chosen driver, using the
  # EXECUTION_MODES ordering. Populated from ExecutionPlan.rejections (planner prose reasons, translated to
  # stable codes) AND the losing live-gate codes (native-lane declines, OOC-compat rejects, chunk gates).
  # A lane not attempted because an earlier gate already excluded it is recorded as unattempted with the
  # excluding code, not silently dropped. Precedence: the LIVE gate result is authoritative; the planner
  # prose reason is carried as its explain annotation, never as the routing decision.
```

Every field derives from an existing structure (section 9), so shadow mode (4.4) can prove equivalence.

## 4. Drivers (residency + publication + hosted operators)

Each driver is a capability declaration. The compiler never assigns a table to a driver that cannot host
every one of its nodes (invariant 1); on any miss the table falls back as a unit.

| Driver | Entry | Selected by | Residency | Publication (UNCHANGED) | Hosts operators for |
|---|---|---|---|---|---|
| `synthesis` | `generate_tables()` | `has_generate_table` (runs before masking) | resident | resident dict; output merged into mask sources | generation kinds (from `GenerationPlan`) |
| `full_frame` | `PandasExecutionAdapter.run` | Layer-1 `full_frame`; Layer-2 fall-through; the polars adapter target | resident | resident dict; a provided sink is silently ignored (accepted today) | every strategy (oracle / parity reference) |
| `sequential` | `run_sequential` | Layer-1 `sequential` (`_sequential_eligible` + FK + not-cyclic + not-OOC-large) | evict_per_table | optional sink via whole-table `write`+`commit`, incl. legacy immediate/non-transactional callable sinks; else resident | every strategy (pandas, table-by-table) |
| `chunked` | `run_mask_pipeline_chunked` / `run_native_or_oracle_chunked` | Layer-2 `classify_job` mode==chunked + auto_chunk | chunked | resident dict via `run_mask_chunked`; the direct chunked entrypoint is an iterator publisher; a provided sink is silently ignored | chunk-safe strategies; native variant adds `native_chunk` operators |
| `native_stream` | `try_native_route` | `native_route_enabled` + `static_candidacy` + preflight | stream_per_batch | optional `sink.write_batches`+`commit`; else resident | passthrough, redact, truncate |
| `out_of_core` | `run_fk_out_of_core` | Layer-1 `out_of_core_ready` (OOC-compatible FK job; size via byte-estimate OR the ≥threshold rule — see note) | stream_per_batch (DuckDB, bounded) | optional `sink.write_batches`+`commit`; else resident | DuckDB relational ops + its OWN per-column kernels (section 5) |

Note (Codex): OOC selection is NOT threshold-only. Default byte-estimate routing (`use_byte_estimate_routing`)
can pick OOC without consulting `out_of_core_threshold_rows`; the row-threshold is one of several inputs.

`full_frame` hosts two substrate variants: the pandas adapter (oracle) and, for `substrate="polars"`,
`PolarsExecutionAdapter` — polars-native where `_is_fully_polars_native` holds, else the pandas-oracle
fallback when `fallback_to_pandas` is true, else `polars_substrate_strategy_unmigrated` (section 8).

## 5. Node operators and the out-of-core catalog

Operators keyed `(driver, lowering_id)`. Distinguish two OOC surfaces:

- The always-admitted set `{hash, redact, truncate, passthrough}` runs the SHARED `decoy_engine.kernel`
  Arrow kernels, hosted by the OOC driver (fed the batches DuckDB streams) — the same kernels
  `full_frame`/`sequential` use, not OOC-specific ports. This set is also the ONLY admitted parent-key
  (FK join/remap) strategy set.
- The Group B/C payload kernels ARE OOC-specific ports (`out_of_core/_mask_group_b.py`, `_mask_group_c.py`),
  reusing the same underlying primitives (FF1 encrypt, STORM detectors, `derive_index`) as the full-frame
  handlers for byte-parity:
  - Group B payload: `{fpe, text_redact, categorical}` — `categorical` admitted only when deterministic.
  - Group C payload: `{text_mask}` always; `{code_set}` mask-mode-without-`chapter_preserve` only;
    `{bucket_perturb}` only with an explicit `date_format`.
  - OOC rejects (table falls back), each with a coded reason (Appendix A): faker; bucketize / date_shift
    (row-error strategies); geo_generalize (whole-column aggregation); formula / derived (dynamic output
    type); nested (child dispatch); the cross-row set {grouped_series, windowed_date, derived_aggregate,
    group_key, joint_mask}.
- OOC inner driver: `batch_join` (default) or `reorder`, chosen per table by `_route_policy.decide_route`
  from sink presence, fan-in (≤ 2×merge_fan_in), memory/disk budgets, deduplicated parent-key count (≥
  `REORDER_PARENT_KEY_THRESHOLD`, default 2M, overridable), and payload width. Modeled as
  `PhysicalTable.inner_driver` + `ReorderCaps`.
- Other-driver operators: `native_stream` = `{passthrough, redact, truncate}`; `native_chunk` (chunked
  driver's native variant) = compiled `{passthrough, redact, truncate, hash}` + `{faker}` via
  `derive_index_batch` (JC-5 deterministic-reuse variant only); `full_frame`/`sequential` = every strategy
  (the oracle set); `synthesis` = the generation kinds. `bounded_python` operators (Faker pool build, ML,
  approved hard-tail) are hosted inside whichever driver runs the table, never a table driver themselves,
  never arbitrary Python on a large job.

## 6. Invariants

Compile-time, before any output is staged. A violation is a coded rejection, never a silent runtime
degrade.

1. One table driver + atomic fallback. Every node on a table executes under one driver; a node whose
   operator is not hosted by that driver forces the whole table to a driver that hosts every node
   (ultimately `full_frame`/oracle). Table-atomic, existing fail-closed behavior; NOT homogeneous node
   implementation (OOC legitimately mixes DuckDB relational ops with per-column kernels).
2. Global / order-sensitive isolation. A table with any `is_global`, `is_order_sensitive`, or
   `needs_global_row_identity` node cannot use `native_stream`; it uses `full_frame`/`sequential`, or
   `out_of_core` for FK ordering.
3. FK route as a unit — not threshold-only. Parent-key and child columns never split across drivers; which
   driver an FK job takes follows the live `decide_execution_route` inputs (eligibility, byte-estimate fit,
   probe recovery, OOC compatibility + size), recorded as `driver_reason` + `rejected_alternatives`.
4. Determinism partitionability. A node runs on a partitioned/streamed driver (`native_stream`,
   `native_chunk`, `out_of_core` batch masking) only if its draw site is `partitionable=True`; shuffle,
   grouped_series, etc. stay whole-frame.
5. Companion + provider gating.
   - The ENGINE-INTERNAL companion gate is two per-companion loaders: `load_compiled_crypto_kernel` (any
     `hash` node) and `load_compiled_index_kernel` (any admitted `faker` node), each raising
     `CryptoExtensionUnavailableError`, invoked in `native/_dispatch.py`, surfacing
     `crypto_extension_unavailable` / `index_extension_unavailable`. On absent OR ABI-mismatch
     (`_EXPECTED_ABI_VERSION = "decoy-native-abi-2"`) the WHOLE TABLE downgrades to the oracle driver.
     (The platform-facing `decoy_engine.native_companion_status()` probe added in 3.2a is a separate
     startup-gate layer, not the engine's per-table route gating; the physical plan keys on the loaders.)
   - `fallback_policy` (from `NodeRequirements`, values `{native, python_only}`) means native-READY vs
     Python-executed; it never means "swap in a Python operator at runtime."
   - `provider_class` (from `native/_provider_class.py`, values `{pool_native, python_only, reject_large}`)
     is the faker/custom-provider axis; `reject_large` raises a coded rejection on a large job. This axis
     is distinct from `fallback_policy`; a PhysicalNode carries both. The source `FallbackPolicy` literal
     (`_requirements.py`) carries a vestigial `reject_large` value that `_fallback_policy()` never emits;
     it must NOT be carried into this frozen field, and `reject_large` belongs only to `provider_class`.
6. Static output type. `output_type_is_static=False` (e.g. `derived`) excludes the node from typed batch
   drivers → `full_frame`/oracle.
7. Bounded state declared. A node needing state (`code_set_corpus`, `reference_table`, `value_pool`)
   declares a `StateTable`; a driver that cannot host that state class cannot be selected for it.
8. Source/sink compatibility — WITHOUT changing current sink behavior. A driver is selected only if it can
   consume the job's source shape. Publication compatibility is represented, NOT enforced as a reject: a
   provided sink that the winning driver ignores today (full_frame, chunked) is represented as
   "sink-ignored," exactly as `run_pipeline` behaves now — the invariant never reroutes or rejects such a
   job. A `substrate != "pandas"` job admits only the full_frame polars-capable driver.
9. Requirements ⊆ capabilities. Each node's prepasses, state, and diagnostic obligations must be a subset
   of what its driver+operator provide; else the table falls back.
10. Complete coverage, per stage. Every configured MASK column maps to exactly one masking `PhysicalNode`
    (backed by `ColumnSeed`); every configured GENERATE column maps to exactly one `synthesis` operator
    (backed by `GenerationPlan`). No configured column is unrepresented; no node covers an unconfigured
    column. (Generation is represented by the synthesis stage, not by `NativePlanNode`/`NodeRequirements`,
    which exclude generation.)

## 7. Diagnostics, resources, publication

- Diagnostics are backend-qualified: each obligation records which drivers satisfy it. An obligation a
  driver is known NOT to satisfy is a DECLARED divergence, not a fail-closed trigger — e.g. the OOC route
  does not emit `fpe_join_group_active` (which full-frame emits), a currently-accepted divergence; shadow
  mode treats declared divergences as expected. A native route is credited only via a non-zero native-call
  counter. Reports never contain raw values, keys, derived material, or sensitive failure detail.
- Route evidence: one per-node record (`node_id`, driver, operator, `operator_reason`, native-call
  counters) that the adapters' existing shapes reduce into during 4.2.
- Publication is UNCHANGED and per-driver, and the physical plan represents the full current surface, not
  a single contract: `full_frame`/`chunked` return resident dicts and silently ignore a provided sink; the
  direct chunked entrypoint is an iterator publisher; `sequential` uses whole-table `write`+`commit` and
  also supports legacy immediate/non-transactional callable sinks; `native_stream`/`out_of_core` use
  `write_batches`+`commit` (transactional Parquet, `os.replace` atomic commit, best-effort abort). The
  design only NAMES each driver's existing mode; it adds no sink to a resident driver and changes no
  contract.

## 8. HostBudget and substrate

- `HostBudget` is a resolved per-job allocation: `memory_bytes`, `temp_disk_bytes`, `batch_size_rows`,
  `duckdb_threads`, `native_threads`, `python_workers`. Naming + resolution only; host-wide concurrent-job
  admission stays a platform/governor responsibility (no engine scheduler). Because the resolved budget is
  stored in the artifact and can differ between jobs, and because some fields affect routing (e.g. the OOC
  reorder threshold/caps), EVERY route-affecting budget field participates in `plan_hash`; the hash cannot
  claim two physical plans byte-identical while excluding stored budget fields that differ. Budget *values*
  and platform-vs-engine precedence remain the open Task 1.3 carry-forward.
- Substrate: a `substrate="polars"` job bypasses the native/OOC/chunk drivers (each self-excludes
  non-pandas) and is the `full_frame` driver hosting `PolarsExecutionAdapter`: polars-native where
  `_is_fully_polars_native` holds, else pandas-oracle fallback when `fallback_to_pandas` is true, else
  `polars_substrate_strategy_unmigrated`. Represented explicitly (a full_frame table with
  `substrate="polars"`). Polars stays frozen (Task 6.1 decides its status); the plan neither removes nor
  expands it.

## 9. Mapping to existing structures (Task 4.1 step 5)

| Physical plan | Source today |
|---|---|
| `PhysicalPlan.plan_hash` | function of the full routing input set (section 3) + stored route-affecting budget |
| `PhysicalPlan.synthesis` | `Plan.generation` (`GenerationPlan`) + the `generate_tables()` executor |
| `PhysicalPlan.relationships` | `Plan.relationships` (verbatim; lossless) |
| `PhysicalTable.driver/driver_reason` | live `resolve_execution_route` + `decide_chunk_route` + native-lane admission |
| `PhysicalTable.rejected_alternatives` | `ExecutionPlan.rejections` (planner prose → stable codes) + losing live-gate codes |
| `PhysicalTable.inner_driver` | `out_of_core/_route_policy.decide_route` |
| `PhysicalTable.residency/publication/substrate` | the driver's fixed behavior (section 4) + resolved substrate |
| `PhysicalNode.*` (mask) | `NativePlanNode` + `NodeRequirements` + `capabilities_for` + `ColumnSeed` + `DrawSite` |
| `PhysicalNode.fallback_policy` | `NodeRequirements` (`native`/`python_only`) |
| `PhysicalNode.provider_class` | `native/_provider_class.classify_provider` |
| synthesis operators | `Plan.generation` (`GenerationPlan`); NOT `NativePlanNode` (which excludes generation) |
| `HostBudget` | existing thread/OOM-router thresholds + OOC caps, gathered + resolved |

## 10. Appendix A — frozen rejection-code catalog

The complete set the compiler (4.3) must emit and shadow mode (4.4) must match, grouped by source. Codes
with `:<detail>` carry a parameterized suffix.

- Planner explain reasons (`_planner.py`, stored in `ExecutionPlan.rejections`, prose translated to stable
  codes): polars_native (`no_mask_work` / `substrate_is:<s>` / `fk_resolution` /
  `non_polars_native_work:<strategies>`); chunked (`no_mask_tables` / `generate_tables_present` /
  `masks_one_table_per_run` / `substrate_is:<s>` / `chunked_relationships_unsupported` / re-raised
  `check_chunked_compatibility` code / `non_scalar_composite` / `fpe_join_group` /
  `when_predicate_not_chunk_stable` / `date_shift_requires_explicit_format` / runtime-source reasons); AND
  the two relationship modes' deferral/no-relationship reasons (`sequential_relationship`,
  `out_of_core_relationship`) — both must have stable codes.
- Layer-1 routing (`_pipeline_routing.py`): raised `fk_full_frame_oom_risk_rejected[_estimated]`;
  `route_reason` tokens {no_relationships, generate_plus_mask, validators_present,
  fidelity_report_requested, vault_writer_requested, non_pandas_substrate_requested, pure_mask_fk,
  override_full_frame, override_out_of_core, byte_estimate_full_frame_fits, probe_recovered_full_frame,
  byte_estimate_bounded_out_of_core, out_of_core_large_fk, cross_table_cycle}. NOTE: the forced-mode
  (`execution_mode=` mismatch) failures are currently UNCODED `ConfigError`s — Task 4.1 requires assigning
  them stable codes (e.g. `forced_mode_out_of_core_ineligible`, `forced_mode_sequential_ineligible`,
  `forced_mode_no_mask_table`) or an explicit documented exclusion from the shadow catalog.
- Native stream (`_native_route.py`): execution_mode_not_auto, non_pandas_substrate:<s>,
  generation_table_present, multi_table_job, fk_relationship_present, source_loader_present, non_lazy_source,
  fidelity_report_requested, validators_present, quarantine_configured, unsupported_sink,
  no_columns_configured, invalid_column_config, vault_column:<name>, unsupported_strategy:<name>:<strategy>,
  zero_row_source, unsupported_projection:missing=..:extra=.., non_utf8_column:<name>:<type>.
- Native preflight (`_native_route_preflight.py`): native_preflight_schema_drift:<drift>,
  native_preflight_reroute:<col>:<strategy>:<family>:<state>, native_preflight_strategy_unresolved,
  native_source_snapshot_digest_mismatch, native_chunk_schema_drift, columns_changed:..., type_changed:....
- Native ledger (`_native_route_exec.py`): native_route_ledger_invalid, native_chunk_schema_drift.
- Chunked native dispatch (`native/_dispatch.py`): fk_relationship_not_native_route, no_mask_nodes,
  non_scalar_node:<kind>:<label>, fallback_policy_not_native:<col>:<policy>,
  no_native_kernel_or_pool:<col>:<strategy>, uncovered_columns:..;missing_configured_columns:..,
  faker_source_type_not_string:<col>:<type>, crypto_extension_unavailable, index_extension_unavailable,
  empty_input.
- Native eligibility (`native/_plan.py`): generation_not_native_route:generate_columns,
  missing_strategy:<name>, composite_provider_multi_column:<name>:<provider>,
  unclassified_strategy:<name>:<resolved>, output_type_indeterminate:<name>:<strategy>,
  requires_global_execution:<name>:<strategy>.
- Native requirements (`native/_requirements.py`): no_native_kernel:<name>:<strategy>,
  no_native_pool_path:<name>:<strategy>, faker_not_deterministic_reuse_variant:<name>,
  mixed_object_not_native:<name>, hash_input_type_not_native:<name>:<type>, redact_with_not_string:<name>,
  re-raised truncate_*_invalid:<name>.
- Provider classification + Phase-3 faker admission (`native/_provider_class.py`,
  `native/_phase3_eligibility.py`; not-yet-wired, like native_route_eligibility, but represented per the
  faker operator in §5): provider classes {pool_native, python_only, reject_large}; codes
  allow_collisions_mode_conflict:<name>:<mode>, faker_not_deterministic:<name>,
  faker_cardinality_not_partition_independent:<name>:<mode>, faker_namespace_required:<name>,
  faker_pool_size_required:<name>, provider_reject_large:<name>:<provider>,
  provider_not_pool_native:<name>:<provider>, provider_not_in_c1_allowlist:<name>:<provider>,
  faker_config_shape_unsupported:<name>:<shape> (shape in {vault, when}).
- Out-of-core compat (`out_of_core/_compat.py`): out_of_core_no_relationships,
  out_of_core_multi_parent_child_unsupported, out_of_core_fk_arity_mismatch,
  out_of_core_self_referential_fk_unsupported, out_of_core_parent_seed_missing,
  out_of_core_parent_strategy_unsupported, out_of_core_parent_namespace_missing,
  out_of_core_relationship_cycle_unsupported, out_of_core_when_predicate_unsupported,
  out_of_core_composite_group_uncovered, out_of_core_non_scalar_work_unsupported,
  out_of_core_composite_fk_scalar_child_unsupported, out_of_core_cross_row_strategy_unsupported,
  out_of_core_faker_pool_unsupported, out_of_core_row_error_strategy_unsupported,
  out_of_core_whole_column_aggregation_unsupported, out_of_core_dynamic_output_type_unsupported,
  out_of_core_child_dispatch_unsupported, out_of_core_code_set_shape_unsupported,
  out_of_core_bucket_perturb_autodetect_unsupported, out_of_core_categorical_nondeterministic_unsupported,
  out_of_core_strategy_unsupported.
- Out-of-core route policy (`out_of_core/_route_policy.py`): out_of_core_reorder_threshold_invalid,
  out_of_core_parent_column_missing, out_of_core_relationship_cycle.
- OOC runtime kernel codes: out_of_core_source_schema_required, hash_requires_namespace,
  out_of_core_fpe_column_required, out_of_core_parent_rows_unresolved, out_of_core_memory_detection_failed,
  out_of_core_fanin_exceeds_budget, out_of_core_source_missing, out_of_core_code_set_row_error_unreachable.
- Substrate (`_substrate.py`): invalid_substrate, invalid_execution_knob.
- Polars adapter (`polars/_polars_adapter.py`): polars_substrate_strategy_unmigrated,
  unsupported_strategy.
- Chunked compat (`_chunked.py check_chunked_compatibility`) — FROZEN full enumeration (not a
  parenthetical): chunked_table_unknown, chunked_generate_unsupported, strategy_not_chunk_safe,
  chunked_strategy_conditions_unmet, chunked_group_key_when_not_supported,
  chunked_group_key_group_by_dtype_unsupported, chunked_row_offset_out_of_domain,
  chunked_fk_orphan_policy_not_remap, chunked_fk_composite_unsupported,
  chunked_fk_parent_strategy_not_self_mask_safe, chunked_fk_child_namespace_missing,
  chunked_fk_child_namespace_mismatch, chunked_fk_child_strategy_missing, chunked_fk_child_strategy_mismatch,
  chunked_windowed_date_when_not_supported, chunked_text_mask_when_not_supported,
  chunked_code_set_when_not_supported, chunked_code_set_fk_key_unsupported,
  chunked_bucket_perturb_when_not_supported, chunked_bucket_perturb_fk_key_unsupported,
  chunked_schema_mismatch, plus the runtime-source dtype codes
  (chunked_{group_key_group_by,text_mask,code_set,bucket_perturb}_source_dtype_unsupported,
  bucketize_source_not_null_free_numeric).

Task 4.3 emits exactly one stable code per decision from this catalog; 4.4 shadow mode asserts code-for-code
equivalence. Any code discovered during 4.3 that is not here is a catalog defect to fix, not a silent add.

## 11. Exit gate, non-goals, resolved review points

Exit gate: independent architecture + compatibility review (dennis + cross-model Codex) return GO, then
Cam approves before Task 4.2 begins.

Non-goals for 4.1: no adapters/compiler/coordinator/shadow harness; no new backend/driver beyond naming the
existing synthesis stage; no Polars decision (Task 6.1); no FPE/determinism/publication change.

Resolved from v2 review: generation represented as the synthesis stage (Codex HIGH); compiler input set
made total (Codex BLOCKER); `rejected_alternatives` semantics specified (Codex BLOCKER); invariant 5
describes the real two-loader companion gate + adds the provider_class axis, `reject_large` re-keyed off
`fallback_policy` (dennis HIGH-1/HIGH-2, Codex BLOCKER); invariant 8 no longer changes publication behavior
and §7 represents the full publication surface incl. sink-ignored/iterator/legacy-callable (Codex HIGH);
catalog completed incl. planner relationship reasons, polars `unsupported_strategy`, the frozen chunked
enumeration, phase-3/provider codes, and the uncoded-ConfigError note (Codex BLOCKER); the `hash` example
corrected to the shared kernel and §5 OOC "own kernels" narrowed to Group B/C (dennis MEDIUM-1); HostBudget
hash rule fixed; OOC "≥ threshold" corrected to byte-estimate-or-threshold; polars `fallback_to_pandas`
included.

## 12. Task 4.1 approval basis and Task 4.3 completion punch-list

Cam approved Task 4.1 on the ARCHITECTURE basis: the design's model, driver taxonomy, invariants,
mapping, and stage/publication handling are correct and buildable (dennis GO; Codex confirmed the model
across all rounds). The exit-gate bar of an EXHAUSTIVE compiler input set and a code-for-code-complete
rejection catalog is deliberately relocated to Task 4.3, where the physical-plan compiler enumerates both
in code and Task 4.4 shadow mode verifies code-for-code against the live routes. Freezing the full
enumeration by hand on paper is error-prone (rounds 1-3 each surfaced one more input/code); producing it
in the compiler and proving it in shadow mode is the reliable place. This section is the binding
punch-list Task 4.3 MUST discharge and Task 4.4 MUST verify; the catalog in §10 and the input set in §3
are the near-complete starting point, not a closed set.

Task 4.3 MUST add to the compiler input set (Codex round 3, verified against code):
- `registry: ProviderRegistry` — affects provider classification, work-list construction, and routing
  (`_pipeline.py:135`, `_runner.py:61`).
- Exact sink TYPE, not a coarse category — native streaming requires `type(sink) is
  ParquetTransactionalSink`; another transactional implementation routes differently
  (`_native_route.py:176`).
- Resident-source measured facts: schema, null counts, extra/missing source frames, and per-column types,
  which gate chunk admission (`_planner.py:496` runtime-source gates).
- The native companion probe OUTCOME (present / absent / ABI-mismatch, per loader) as a declared compiler
  input / captured fact — it selects native vs whole-table downgrade (`native/_dispatch.py:269`).

Task 4.3 MUST complete the frozen catalog (§10) with the codes Codex round 3 found still missing or
uncoded:
- Assign stable codes to the Layer-1 forced-mode `ConfigError`s (currently uncoded) or explicitly exclude
  them from the shadow catalog with a recorded reason.
- Name stable codes for the planner "runtime-source reasons" and the relationship deferral reasons.
- Add the remaining live `chunked_*` codes the §10 enumeration still omits (enumerate directly from
  `_chunked.py` at build time).
- Add `pool_size_location_conflict:<name>` from `resolve_pool_size()` (`_phase3_eligibility.py:185`).

Task 4.4 gate: shadow mode asserts the compiler's emitted codes and routing decisions match the live
routes code-for-code across the acceptance corpus; any code the compiler emits that is absent from the
frozen catalog is a catalog defect to fix, and any input the compiler consumes that is absent from §3 is
an input-set defect to fix. Passing 4.4 is what closes this punch-list.

## Source documents

- [2026-09-09-execution-consolidation-and-native-throughput.md](2026-09-09-execution-consolidation-and-native-throughput.md)
- `execution/_planner.py`, `_pipeline.py`, `_pipeline_routing.py`, `_pipeline_routing_signals.py`, `_pipeline_chunk_route.py`, `_pipeline_route_exec.py`
- `execution/_pandas_adapter.py`, `_sequential.py`, `_chunked.py`, `_substrate.py`, `polars/_polars_adapter.py`, `_transactional_sink.py`
- `execution/_native_route.py`, `_native_route_preflight.py`, `_native_route_exec.py`, `native/_dispatch.py`, `native/_plan.py`, `native/_requirements.py`, `native/_capabilities.py`, `native/_determinism_protocol.py`, `native/_crypto_ext.py`, `native/_provider_class.py`, `native/_phase3_eligibility.py`, `kernel/__init__.py`
- `execution/out_of_core/_compat.py`, `_route_policy.py`, `_mask.py`, `_mask_group_b.py`, `_mask_group_c.py`, `_runner.py`
- `generation/_plan_entry.py` (`generate_tables`), `plan/_types.py` (`Plan`, `GenerationPlan`, `ColumnSeed`)
