Status: plan (revision 2, BUILD-READY, author = Opus). Codex plan gate: round 1 REVISE folded; round 2 GO (0 findings). Built: see `docs/records/2026-10-06-c5b-iii-unified-positional-build.md`.
Rules consulted: 00-universal, development-loop, testing, architecture, code-review, scope-discipline, observability-and-resilience

# C5b-iii: position-keyed categorical and Faker on the unified full-frame route

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C. Follows C1b-ii (positional categorical, chunked) and C5b-ii (positional Faker, chunked, #212), which both left the unified route closed. This slice opens it.

Branch `feat/c5b-iii-unified-positional` off engine main `3e8d259e`. Risk R2: one route widened for two operator variants, parity-gated, no new kernel. The unified lane is on by default (`unified_slice_enabled=True`, `_pipeline.py:187`), so eligible single-table full-frame jobs with these columns switch from the pandas oracle to the unified lane. Outputs must stay byte-identical. This covers parquet, CSV and fixed-width single-table jobs.

## 1. Goal and scope

Admit to the unified route:
- **Positional categorical:** the seeded non-deterministic categorical. Its config passes `prepare_positional_categorical` (namespace, explicit all-string categories, buildable CDF, no `from_profile`) over a string resident source.
- **Positional Faker:** non-deterministic REUSE Faker with a C1-allowlisted provider (`person_first_name`, `person_last_name`), an explicit `pool_size`, a string resident source, not nested, not composite, and not in an FK relationship. These are the same conditions as C5b-ii's chunked stage A.

Out of scope:
- `when:`. The unified route still declines every `when` column (`_unified_slice_admission.py:308-314`), and positional operators stay out of any future unified `when` path.
- Multi-table, out-of-core and split routes, plus changes to the JC-5 requirement or to `prepare_categorical`. Those gates feed the chunked route and the eligibility report; changing them would change verdicts elsewhere.

## 2. Established facts (main `3e8d259e`; C5b-iii research)

**Declines today.**
- JC-5 resolves a non-native `fallback_policy` for both variants (`native/_requirements.py:525-535, 608-609`). `NodeRequirements` keeps no reason code.
- `execution_binding_for_slice_node` returns None for a non-native policy (`physical/_shadow_bindings.py:177`).
- Even past that check, categorical always binds `prepare_categorical(deterministic=True)` (`:237-239`), and Faker always resolves non-positional `FakerParams` (`native/_operator_params.py:189-190`).
- `_key_binding` refuses `namespace=None` (`:158`).
- `needs_index_kernel` is True only for `pool_binding` or a deterministic categorical (`physical/_plan.py:126-138`).
- `_bound_params` asserts determinism for categorical (`physical/_shadow_operators.py:192-199`).
- The chunked route admits both through a positional exception at the route layer (`native/_dispatch.py:277-279`, `chunked_positional_column`).

**Offsets.**
- The coordinator keeps a per-node `row_offset` starting at 0 (`physical/_shadow_coordinator.py:344`) and increments it after each batch (`:387`).
- `run_operator` passes neither `row_offset` nor `job_seed` to `run_kernel_step` (`_shadow_operators.py:300-309`), which already accepts both (`native/_operator_step.py:245-246`).
- The whole-frame oracle runs with `row_offset=0` (`_pandas_adapter.py:183, 242`). The handlers key on `ctx.row_offset + i`, so the global position is the cumulative batch offset, restarting at 0 per table.

**Keys.**
- Positional categorical keys on `mask_key` (`_strategies/_categorical.py:196-202`; step `_operator_step.py:340`).
- Positional Faker keys on `job_seed` (`_strategies/_faker.py:104`; step `:301`).
- Both live on `ShadowContext` (`physical/_shadow_context.py:132-133`, set at `:219-222`).

**Pools.**
- `_resolve_pool` reads the namespace from `binding.key_binding.namespace` for both identity and build (`_shadow_coordinator.py:556, 577`).
- `resolve_faker_pool_identity` accepts `None` (`generation/pool/_identity.py:26-34`). The oracle builds the pool with the configured `plan.namespace` (`_faker.py:80-86`).

**Evidence.**
- `assemble_node_evidence` raises when an operator with `positive_kernel_evidence=True` (Faker) never ran (`_unified_slice_evidence.py:110-117`). `_unified_slice.py:354` re-raises this as a hard failure.
- A zero-row table gives a positional Faker step `ran=False` (`_operator_step.py:295-296`). Positional categorical on zero rows gives `ran=False` with `positive_kernel_evidence=False`, so it reports `arrow_python`.

**Assembly.** Both operators are `tokenizing` (`_operator_registry.py:138, 152`). The oracle assigns lists of str or None, so empty is float64 and all-null is null. This matches tokenizing with no exception.

**Size.** `physical/_shadow_coordinator.py` is 599 lines.

## 3. Decisions

**3a. Admission at the unified binding layer.** In `execution_binding_for_slice_node`, before the `fallback_policy != "native"` return, a positional exception applies:
- **Categorical:** bind it when a shared predicate `positional_categorical_bindable(plan_slice, table, column, inputs)` holds. Codex round 1 asked for this, because the binder is shared with shadow compilation (`physical/_compiler.py:297-342`) and the mixed shadow harness reuses the coordinator without production's cheap/resident admission (`physical/_shadow_mixed.py:204-205`). It holds when all of:
  - the column is non-deterministic;
  - `prepare_positional_categorical` returns an artifact;
  - no `when:`;
  - no vault;
  - the resident source type is `pa.string()`;
  - the table is in no FK relationship (the same `_table_in_fk_relationship` guard Faker uses).
- **Faker:** when the node is Faker and passes a shared predicate `positional_faker_bindable(plan_slice, table, column, inputs)`, bind it. The predicate reuses C5b-ii's stage-A predicate (`native/_faker_positional_admission.py`) plus `_faker_pool_bindable`'s unified-specific checks: `when`, vault, resident string type, FK. Both predicates hold at the binding boundary itself, so they stand even where production admission is bypassed.

Every other case keeps today's decline. JC-5, `prepare_categorical`, `native_route_eligibility` and the chunked route are unchanged.

**3b. Parameters and bindings.**
- **Categorical:** `CategoricalParams(prepared=<positional PreparedCategorical>, namespace=plan namespace)`. `KeyBinding(key_source=<mask key source>, namespace=plan namespace)`.
- **Faker:**
  - Params: `FakerParams(namespace=<configured, may be None>, positional=True, selection_namespace=faker_selection_namespace(table, column, configured))`, built by making `_positional_faker_params` (`native/_operator_params.py:225-237`) the shared builder for both routes. `table` comes from the compiler's `table=` argument (`physical/_compiler.py:340-341`).
  - `KeyBinding(key_source="job_seed", namespace=selection_namespace)`: the binding names the key the draw actually uses, so the admission UTF-8 check (`_unified_slice_admission.py:527-530`) covers the selection namespace.
  - `PoolBinding(provider, plan_pool_size)`, as today.
  - `determinism_family` keeps its meaning: the site's FAMILY (`site.family`, e.g. `source_keyed_hmac`, `native/_capabilities.py:418-425`), not a site id (Codex round 1 LOW). The builder sets it from the variant's own draw site: `mask.faker_nondeterministic` for Faker (`native/_draw_sites_gen_pool.py:91-94`, entropy root `job_seed`), and the non-deterministic categorical site for categorical. It records the resulting family values. `KeyBinding.key_source`'s documented value set (`physical/_plan.py`) gains `job_seed`.
- **`needs_index_kernel`:** True for any `CategoricalParams` (positional or not) and for any `pool_binding`.
- **`_bound_params`:**
  - The categorical determinism assertion is replaced by a positional-aware check that the params are a `CategoricalParams` and the index kernel is loaded.
  - The Faker branch accepts positional params and asserts `ctx.job_seed` is non-empty for them.
  - The `categorical_deterministic` property stays, unchanged in meaning: false for positional.

**3c. Row offset and key threading.**
- `run_operator` gains `row_offset: int = 0` and forwards `row_offset=row_offset, job_seed=ctx.job_seed` to `run_kernel_step`.
- The coordinator passes its pre-increment `row_offset` at `:364-373`.
- Deterministic operators ignore both inputs, so their behavior is unchanged.
- The stale comments at `_operator_step.py:149, 289-290` are corrected.

**3d. Pools.**
- `_resolve_pool` takes the pool namespace from the bound params (`params.namespace`, the configured namespace, possibly None), not from `key_binding.namespace`. Pool identity and pool build then match the oracle for both variants.
- For deterministic Faker, `params.namespace` equals `key_binding.namespace`, so nothing changes.
- The non-string pool reroute (`FAKER_POOL_NON_STRING_OUTPUT`, `:591-598`) is unchanged.

**3e. Zero-row evidence.**
- `OperatorCallEvidence` gains `rows_seen: int`, summed per batch.
- `assemble_node_evidence` requires positive kernel evidence only when `rows_seen > 0`. A node that saw no rows reports `executed_backend` the way an idle chunked column does, consistent with the chunked `kernel_idle` semantics.
- Positional categorical's zero-row evidence then matches positional Faker's.
- Deterministic operators on zero rows: Faker calls the kernel even on zero rows (`sample_faker_array`), so its evidence is unchanged; for the others the builder records today's evidence as a baseline and keeps it identical.

**3f. Size.** If `_shadow_coordinator.py` passes 600 lines, add an exact census entry rather than splitting the module in this slice.

**3g. Docs.**
- CHANGELOG.
- Compatibility contract: the unified route now admits the positional variants.
- Capability notes in `native/_capabilities.py:220-226, 236-240` ("outside the native route" becomes "chunked and unified").
- Roadmap: C5b-iii shipped. Draw-site inventory: the unified native mirror for both sites.

## 4. Design notes

- **Admit where the route decides.** Like the chunked positional exception, the unified exception lives in the route's own binding step. The shared requirement gates keep meaning "the value-keyed native operator", so no other route or report changes.
- **One parameter builder per variant.** The unified binding and the chunked `resolve_params_by_column` build positional Faker params through the same function, so the default selection namespace cannot be spelled two ways.
- **The binding names the real key.** A positional Faker binding's key source is the job seed, so evidence and admission checks describe the draw that actually runs.

## 5. Acceptance tests (written first; red-before recorded)

1. **Unified parity, both variants, against an explicit lane-off whole-frame oracle** (Codex round 1). Each reference run passes `unified_slice_enabled=False` and asserts no unified activation; the existing shadow helper does not (`tests/physical/_shadow_helpers.py:241-263`). The lane-on run poisons the oracle (the C5a pattern, `tests/physical/test_unified_slice_faker.py:139-158`). Values, order, schema including metadata, warnings and row errors must all be equal.
   - Shapes:
     - nonzero rows across several batches (`batch_size_rows` smaller than the row count, ragged final batch);
     - mixed nulls placed before, at and across batch boundaries, so a later non-null draw proves it keeps its physical ordinal;
     - all-null, single row, zero rows.
   - Namespace: configured, None and "" (Faker); configured (categorical).
   - Weights: uniform and weighted (categorical).
   - Evidence on the lane-on run shows the unified route activated and the node bound to the native operator.
2. **Batch offset correctness.** The same source value at rows on either side of a batch boundary draws by global position. A whole-table result equals the concatenation of per-batch results computed with the right offsets. Mutants that drop `row_offset`, or use the post-increment offset, are killed.
3. **Keys** (fixtures use a secret provider with DISTINCT fixed `mask_key` and `job_seed`, because without one `mask_key == job_seed`, `keyprovider.py:164-178`. Categories and pool contents are non-degenerate, with enough non-null rows that a wrong key changes the output). Positional Faker uses `job_seed`: swapping in `mask_key` changes the output, as a required mutant. Positional categorical uses `mask_key`: swapping in `job_seed` changes the output, also a required mutant.
4. **Pool identity.** With namespace None, the unified pool equals the oracle's (identity and values). The C5b-ii sibling-collision case (a column whose configured namespace equals another's default selection namespace) passes on the unified route.
5. **Zero-row evidence.** A zero-row positional Faker table completes without `UnifiedSliceInvariantError`, and its evidence follows 3e. Deterministic operators' zero-row evidence equals the baseline recorded before the change. A NON-empty Faker node whose evidence says `compiled_kernel_executed=False` still raises, so the `rows_seen` exemption cannot erase the existing invariant.
6. **Declines unchanged.** Each of these still declines to the oracle with today's behavior:
   - deterministic-but-unadmitted configs;
   - non-allowlisted provider;
   - no `pool_size`;
   - UNIQUE;
   - `when:`;
   - FK;
   - non-string source;
   - vault.
   The chunked route and the `native_route_eligibility` report give the same verdicts as before (snapshot over a config corpus).
7. **Deterministic paths unchanged; intentional test migrations listed** (Codex round 1). The R1b cross-route kwargs baseline and the deterministic unified parity suites pass unedited. These existing tests pin today's decline and are MIGRATED to positive admission and execution assertions, never deleted or weakened:
   - `tests/physical/test_unified_slice_faker.py:519-531` `_DECLINE_CASES["non_deterministic"]` moves to an admitted case. The other decline cases stay.
   - `tests/physical/test_shadow_categorical.py:221-237` (positional binding absent) and `:265-283` (`run_operator` raises for positional) become positive binding and execution tests. The eligibility-query rejection at `:208-218` stays, since the eligibility report is unchanged.
   - `tests/native/test_chunked_nondet_faker_admission.py:682,697` (unified never binds; "positional" absent from `_shadow_operators`) and `tests/native/test_chunked_nondet_categorical_admission.py:359,366,371` ("full-frame stays closed") are updated to the new boundary.
   - `tests/unit/execution/test_c1b_i_route_regression.py:349-373` (`TestNativeStillDeclines`, a source-text pin) and `tests/physical/test_r1b_binding_markers.py:83,117` (`needs_index_kernel`) are updated.

   The builder lists every edit with file:line, old, new, and why the intent is preserved.
8. **Default-on lane.** `run_pipeline` on a single-table parquet job with each variant, default flags: the job takes the unified lane, outputs equal the forced-oracle run, and `unified_slice_activation` metrics are present.
9. **Mutation (by hand).**
   - Required mutants: the four in tests 2 and 3; the pool namespace read from `key_binding`; the `rows_seen > 0` guard removed; each binding-predicate condition removed one at a time. The predicate mutants are killed by DIRECT tests of `positional_categorical_bindable` and `positional_faker_bindable`, since upstream gates could otherwise hide them. The mixed/multi-table shadow path gets one check for the FK and `when` exclusions.
   - Record each killer.

Red-before: tests 1-5 and 8 fail on the base (the variants decline). Tests 6 and 7 are green-before.

Every new test also runs under the Python 3.10 mirror.

## 6. Risk, rollback, gates

| Risk | Mitigation |
|---|---|
| Wrong key per variant | 3b/3c, plus the required key mutants (test 3) |
| Offset off by a batch | Pre-increment offset, the boundary test and mutants (test 2) |
| Pool built on the wrong namespace | 3d, plus the sibling collision and None cases (test 4) |
| Zero-row hard failure | 3e, plus test 5 |
| Other routes' verdicts change | Admission only in the unified binding; verdict snapshot (test 6) |
| Default-on lane switches existing jobs | Byte-identical parity (tests 1 and 8) |
| Coordinator size | Exact census entry if needed |

Rollback: revert the merge commit. The variants decline to the oracle again.

Gates: Codex plan gate, Sonnet tests-first build, dennis, Codex final gate, ci-mirror, merge under the standing authority, then the post-merge suite (including `tests/perf`, with full `-rfE` output kept) and a main CI check.

## 7. Plan-gate history

- Rev 1: initial.
- Codex round 1, REVISE (4 MEDIUM, 1 LOW). Folded in rev 2:
  - Parity uses an explicit lane-off oracle with poisoning and mixed-null boundary cases.
  - Intentional test migrations are enumerated.
  - Positional categorical gets a full binding predicate (`when`, vault, string, FK) at the binding boundary.
  - Distinct mask and job keys and direct predicate tests make the mutants killable; a non-empty no-kernel evidence case still raises.
  - `determinism_family` keeps the family meaning; `KeyBinding.key_source` docs gain `job_seed`.

- Codex round 2: GO, 0 findings.
