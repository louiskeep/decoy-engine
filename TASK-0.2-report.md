# Task 0.2 + 0.2b report: native planning boundary

Status: DONE

Phase 0, Tasks 0.2 (static strategy capabilities + resolved node requirements)
and 0.2b (NativeExecutionPlan compiler + public compatibility query). Pure
addition under `execution/native/`; no masking or generation behavior changed.

## Files created / changed

- `src/decoy_engine/execution/native/_capabilities.py` (new): `StrategyCapabilities`
  frozen dataclass + `capabilities_for(strategy)`.
- `src/decoy_engine/execution/native/_requirements.py` (new): `NodeRequirements`
  frozen dataclass + `requirements_for(node, *, plan, profile)`.
- `src/decoy_engine/execution/native/_plan.py` (new): `NativeExecutionPlan`,
  `NativePlanNode`, `NativeEligibility`, `compile_native_plan(...)`,
  `native_route_eligibility(config, *, table)`.
- `src/decoy_engine/execution/_runner.py` (changed): added the inert
  `WorkNode.requirements: NodeRequirements | None = None` field.
- Tests (new): `tests/native/test_capabilities.py` (18),
  `tests/native/test_requirements.py` (12), `tests/native/test_native_plan.py` (11).

## The capabilities model (Task 0.2)

`StrategyCapabilities` splits the orthogonal properties the old single
`locality` enum conflated. Each axis is independent, so a hash is described as
BOTH keyed AND row-local, and a shuffle as global + order-sensitive +
needing a durable global row number:

- `is_row_local`, `is_keyed`, `is_order_sensitive`, `is_global`,
  `needs_global_row_identity`, `output_type_is_static`
- `draw_family: str | None`, `key_source: "mask_key" | "generation_seed" | None`
- Phase-1 diagnostics: `row_error_modes`, `warning_codes`, `quality_obligations`,
  `quarantine_required`
- `notes` / `uncertain` (additive honesty fields, mirroring `DrawSite`)

`capabilities_for(strategy)` is total over the union of the live mask registry
(`SCALAR_HANDLERS`, 24 strategies), the live generation registry
(`GENERATE_TYPES`, 11 kinds), and the two work-node placeholder strategy names
(`<composite>`, `<group>`). It raises `KeyError` for an unclassified strategy,
so a newly added strategy fails loudly until it is entered.

Design note on name overlap: where a name spans mask and generation (e.g.
`faker`, `categorical`, `formula`), `capabilities_for` returns the MASK-context
(re-identification-relevant) entry; the three generation-only kinds
(`sequence`, `reference`, `statistical`) get dedicated entries. `draw_family` /
`key_source` come from the primary context's site. IMPLEMENTED vs DEFERRED: a
per-context split (a generate-kind node keyed distinctly from its same-named
mask strategy) is NOT implemented. `build_work_list` produces only the mask node
kinds, so `requirements_for` never sees a generation node today; the split is
deferred to the phase that streams generation, and `_resolve_strategy_name`
carries a comment saying so rather than a dead branch.

## How each field was sourced from the REAL strategy surface

No field is a hand-copied strategy list. Every real strategy's fields trace to
code as below. The one exception, stated plainly: the two node-kind PLACEHOLDER
rows (`<composite>` / `<group>`) are hand-authored, because a composite bundle
has no single draw site or handler to read; they exist so `capabilities_for` is
total over the work-node kinds.

- `draw_family` + `key_source` are read from the Task 0.1 inventory
  (`MASK_STRATEGY_TO_SITE` / `GEN_KIND_TO_SITE` -> `DrawSite.family` /
  `.entropy_root`). `is_keyed` is derived as `key_source == "mask_key"`, so it
  can never disagree with the inventory. A test asserts every mask strategy's
  `draw_family`/`key_source` equals its inventory site.
- `row_error_modes`: the distinct `RowError.trigger` values each handler
  appends to `ctx.row_errors`, read from the handlers' actual
  `ctx.row_errors.append(RowError(..., trigger=...))` sites. Exactly five
  strategies emit row errors: `bucketize` / `date_shift` / `top_code`
  (`format_error`), `code_set` (`mask_error`), `nested` (`format_error` plus the
  propagated child trigger, so `{format_error, mask_error}`). Every other
  strategy is `()`. A test asserts the error-capable set is exactly those five.
- `warning_codes`: the `QualityWarning.code` literals each handler returns
  through its `run()` tuple. `top_code` -> `top_code_generalized`;
  `geo_generalize` -> `geo_generalize_cascade`; `fpe` ->
  `{fpe_join_group_active, fpe_sub_minimum_domain,
  fpe_partial_plaintext_disclosure}`; `nested` -> `{nested_cell_json_parse_error,
  nested_jsonpath_path_overlap}`. `fpe`'s other coded conditions
  (`fpe_requires_namespace`, `fpe_unencryptable_value`, ...) are fatal
  `StrategyError` raises, not warnings, so they are excluded.
- `quality_obligations`: `faker` is the ONLY registry handler that builds and
  samples a bounded value pool (`PoolBuilder` + `PoolSampler` + `pool_cache`), so
  it is the sole static class-A pool-fidelity obligation (`("pool_quality",)`);
  every other handler's pool import is type-only. Its pool warnings ride the
  `PoolCache` side channel, not the `run()` return tuple, which is why they are
  a quality obligation rather than a `warning_code`.
- `quarantine_required`: True exactly when `row_error_modes` is non-empty, on
  the honest ground that a row-error fallback leaves the un-masked source value
  in the output (bucketize keeps `col`; date_shift returns the original date),
  so the row is unsafe without quarantine, which the streaming route lacks.

The classification pattern follows the engine's existing central strategy maps
(`TECHNIQUE_CLASS_BY_STRATEGY`): one audited table, read by every consumer.

## Resolved requirements (Task 0.2)

`NodeRequirements` is what a SPECIFIC compiled `WorkNode` needs, resolved from
the node config + profile: `required_input_columns`, `output_arrow_schema`
(None = indeterminate type = excluded from native), `lowering_id`,
`required_prepasses`, `required_state_tables`, `diagnostic_reducers`,
`fallback_policy`. `requirements_for(node, *, plan, profile)` keys on
`node.kind` for the composite / FK-group placeholders.

- The load-bearing example works both ways: a `date_shift` node WITH an explicit
  `date_format` in its config has no prepass and a determinate string schema;
  WITHOUT it, `requirements_for` reads the missing key and adds a `format_detect`
  prepass (matching the handler's `cfg.get("date_format") or _detect_format(col)`).
- `output_arrow_schema` is None for indeterminate-type strategies
  (`formula`, `derived`, `derived_aggregate`, `nested`, `joint_mask`),
  type-preserving for `passthrough`/`shuffle` (input dtype from the profile),
  and string for the masked/tokenized/generalized surfaces.
- `required_prepasses` derives from the resolved config + capabilities:
  `format_detect` for format-less `date_shift`, `global_row_number` for any
  `needs_global_row_identity` node, `whole_column_pass` for a non-row-local
  global node.
- `required_state_tables`: `code_set_corpus`, `reference_table` (joint_mask),
  `value_pool` (faker / composite).
- `diagnostic_reducers`: one per-code globalizer per warning code and per
  row-error trigger; empty for the zero-diagnostic admitted set, so Phase 1
  needs no reducers.
- `fallback_policy`: `native` when the node is static-type + row-local + not
  global + no global-row-identity; otherwise `python_only` (routes to the pinned
  pandas parity oracle). Provider `reject_large` is Task 0.5's separate concern.

## NativeExecutionPlan + compatibility query (Task 0.2b)

`compile_native_plan(config, profile, *, engine_version)` runs the existing
`compile_plan` + `build_work_list`, resolves each node's requirements, attaches
them to the work node via `dataclasses.replace` (inertly: nothing routes on the
field), and produces `NativeExecutionPlan(engine_version, nodes, work_nodes)`.
Each `NativePlanNode` carries the capabilities, the requirements, the input
projection, the per-node output schema, the determinism draw family + key
source, and the fallback policy (the backend-neutral description Part A.1
names). `output_schema_for(table)` merges the per-node schemas, returning None
if any node in the table is indeterminate. The parity-oracle route for a held
node is expressed by its `python_only` fallback policy.

`native_route_eligibility(config, *, table) -> NativeEligibility` is the public
query the platform's `classify_streaming_eligibility` calls instead of a copied
strategy set. It is config-only (its signature carries no profile, so it does
NOT recompile) but PROVIDER-AWARE: it resolves each mask column's (strategy,
provider) through the shared `_column_strategy_key` helper and accepts only when
the resolved capabilities are static-type + row-local + not global. Each miss is
a coded rejection (`output_type_indeterminate:...`,
`requires_global_execution:...`, `composite_provider_multi_column:...`,
`unclassified_strategy:...`, `generation_not_native_route:...`). A `formula`
column rejects with `output_type_indeterminate`; a hash-only table accepts.

### Review remediation: provider-registry-aware eligibility (IMPORTANT fix)

The first cut of `native_route_eligibility` read only `col["strategy"]`, so a
faker column backed by a COMPOSITE provider
(`{"strategy":"faker","provider":"composite_name_email"}`) was classified
accepted, while `compile_native_plan` correctly saw `WorkNode.kind ==
"composite"` and excluded it: the two public APIs disagreed on one input. Root
cause: the coverage never walked the live ProviderRegistry (same class of hole
as Task 0.1's providers_v2 gap).

Root-cause fix (shared rule, not a patch): `_provider_is_composite` /
`_column_strategy_key` apply the EXACT predicate `build_work_list` uses to set
`node.kind == "composite"` (a registry-bound provider whose capability
`backend_type == "composite"`). `native_route_eligibility` resolves through it,
so a composite-provider column now excludes with
`composite_provider_multi_column:...`, matching the WorkNode path by
construction. New coverage walks the LIVE registry: for every registered
provider (34), a faker column is accepted iff that provider is not composite,
and an end-to-end test compiles a real `composite_name_email` config and asserts
`native_route_eligibility.accepted == (all nodes native)` for both the composite
and the hash case.

Why this was not exploitable against the Phase 1 admitted set: faker always
carries a non-empty `quality_obligations` (`pool_quality`), so the Phase 1
predicate excludes it regardless. The bug mattered only for
`native_route_eligibility` as a STANDALONE native-route signal a later phase
would trust, which is now correct.

## How the coverage tests gate a new strategy

- `capabilities_for` is asserted total over live `SCALAR_HANDLERS` +
  `GENERATE_TYPES` (enumerated from the registries, never a copied constant). A
  new strategy with no entry raises `KeyError`, failing the totality test.
- The diagnostics tests assert the error/warn-capable strategies are exactly the
  handler-verified set, and that `draw_family`/`key_source` trace to the Task 0.1
  inventory. A regression that blanked a strategy's diagnostics, or drifted a
  draw-site classification, fails.
- `native_route_eligibility` is driven over the live mask registry as a drift
  sentinel: every strategy either accepts or yields a non-empty coded rejection,
  never raises. It is ALSO driven over the live ProviderRegistry (all 34
  providers), asserting it agrees with `compile_native_plan` on composite
  fan-out, so a new composite provider cannot silently diverge the two APIs.

## Strategies flagged uncertain (2)

- `windowed_date` (`uncertain=True`): carried from Task 0.1's flag. Its per-row
  seed keys on the GLOBAL row index, not the source value, so it is classified
  `is_row_local=False` + `needs_global_row_identity=True`. Correct reproduction
  under partitioning depends on Task 0.3 pinning the index to a global row
  number.
- `nested` (`uncertain=True`): delegates to a config-resolved child handler, so
  its effective diagnostic surface and pool obligation are the union of the
  direct JSON errors with the child's. Classified by its direct surface;
  excluded from the native route regardless by `output_type_is_static=False`.

## Verification

- `pytest tests/native/` -> 57 passed (12 from Task 0.1 + 18 caps + 12 req + 15
  plan, the plan set now including the provider-registry agreement tests).
- `pytest tests/native tests/unit/execution/test_runner.py
  tests/unit/execution/test_composite_routing.py` -> 86 passed (the runner +
  composite-routing tests prove the inert `WorkNode.requirements` field is
  byte-neutral).
- `ruff check` + `ruff format --check` + `mypy` clean on all new/changed files.
- No em-dashes or en-dashes.

## Review remediation summary (this round)

- IMPORTANT: `native_route_eligibility` is now provider-registry-aware and
  agrees with `compile_native_plan` by construction (shared composite predicate);
  new registry-walk + compile-agreement coverage added.
- MINOR 1: report corrected: the `<composite>`/`<group>` rows are stated as
  hand-authored (not inventory-derived), and the generation node-kind split is
  stated as deferred, not implemented.
- MINOR 2: `_resolve_strategy_name` carries a comment that generation-context
  resolution is deferred (no dead branch); `_fallback_policy` carries a comment
  that `required_prepasses` is intentionally not consulted until the
  prepass-consuming phase.
