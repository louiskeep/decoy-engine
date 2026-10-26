The tractable work is narrow OOC admission plus reuse of existing kernels. Start with documentation corrections, then integrate the existing row-error bridge with its dtype guards. Add explicit-format date shifting and a bounded pool lifecycle for deterministic REUSE faker. Follow with OOC text-mask acceleration, an Arrow code-set map, and SEQ group-key acceleration. Seeded positional categorical can follow after the offset channel is established. Whole-dataset aggregation, dynamic expressions, nested dispatch, and stateful cross-row strategies remain declined. Every admitted configuration must preserve the oracle across batch splits. A Python batch bridge can reduce memory without improving compute speed. Measure both effects before claiming a performance gain.

# Part A: slice A, correct the reference documents

## Basis and scope

This is an initial implementation plan, not a review verdict or authorization to merge.

Baseline: main at `d613c8048f2e18db09c6135b60d909a6f9afd267`. The audit note is `/home/cam/obsidian-vault/10_Projects/decoy/Decoy-Strategy-Substrate-Matrix-2026-10-26.md`.

Paths below use these explicit abbreviations:

- `E/`: `src/decoy_engine/execution/`.
- `N/`: `src/decoy_engine/execution/native/`.
- `O/`: `src/decoy_engine/execution/out_of_core/`.
- `G/`: `src/decoy_engine/generation/`.
- `T/`: `src/decoy_engine/transforms/`.

Source inspection found a newer remote-tracking branch than the local branch. The local `claude/blissful-goldberg-mkwpdk` is `5fb89d15`. The available `origin/claude/blissful-goldberg-mkwpdk` is `e1f96acb`. Use the latter as the prerequisite reference. It contains the subsequent conversion and bucketize fixes. These are locally available refs, not a claim about the current remote server.

The plan artifact is low-risk documentation work. Behavioral slices are R2 because masking parity, diagnostics, and output publication are compatibility and privacy boundaries. Dennis reviews this plan before implementation. Dennis and a separate Codex pass gate each implementation slice before merge. Barry maintains the shipped documentation.

Rules consulted: `/home/cam/dev-rules/00-universal.md`, `risk-and-exceptions.md`, `README.md`, `development-loop.md`, `exploration.md`, `documentation.md`, `performance.md`, `architecture.md`, `delegation.md`, `workspace-and-vcs.md`, `reuse-first.md`, `scope-discipline.md`, and `verification.md`. Repository entry documents and the supplied AGENTS instructions were also consulted.

## Corrections to the supplied audit

The audit is useful, but several claims need correction before it becomes the implementation brief.

1. **An OOC decline does not always reroute to full-frame.** Auto routing can retain SEQ. Forced OOC rejects an incompatible plan. Neither outcome is an individual-row reroute. See `E/_pipeline_routing.py:507`, `:596`, and `:625`.
2. **The default router does not require five million rows for OOC.** Byte-estimate routing defaults on. Its compatible bounded path ignores that threshold. The threshold governs the older row-count branch. A confirmed probe can recover full-frame. See `E/_pipeline_routing.py:318`, `:338`, `:555`, and `:596`.
3. **Full-frame is an orchestration route, not a synonym for Python-only compute.** It first offers eligible work to the unified native slice. The reference oracle remains the pandas adapter. See `E/_pipeline_full_frame.py:106`.
4. **Faker is not deterministic-REUSE-only on existing native routes.** SEQ also admits positional REUSE. Chunked supports wider source types than SEQ. See `E/_sequential_native_ops.py:382`, `E/_operator_registry.py:97`, and `N/_dispatch.py:322`. This plan intentionally limits the new OOC faker port to deterministic REUSE.
5. **“Only deterministic REUSE is parity-able” is too broad as a mathematical claim.** The current masking handler already implements positional REUSE from global row positions. See `E/_strategies/_faker.py:108`. That is evidence of a future possibility, not authorization to widen this OOC slice.
6. **Seeded non-deterministic categorical already has a native chunked path.** Its matrix cell must distinguish seeded positional mode from unseeded mode. See `N/_dispatch.py:322`.
7. **The branch bridge fails closed on row errors.** It does not quarantine bad rows or reroute them individually. See `e1f96acb:O/_row_error_bridge.py`, `fail_closed_on_row_errors`, and `O/_runner.py:484` on that ref.
8. **The branch already adds more than a generic channel.** It admits guarded `bucketize` and `top_code`, plus a separate guarded `windowed_date` offset bridge. It does not add faker or date_shift. Do not duplicate this work.
9. **Clean data alone does not establish bucketize parity.** String-to-number inference can depend on the batch contents. The branch correctly declines string and large_string for bucketize. See `e1f96acb:O/_row_error_bridge.py:89`.
10. **The audit omits top_code from its mask table.** Main runs its Python handler on SEQ and the chunked oracle. OOC currently rejects it with `out_of_core_strategy_unsupported`. See `O/_compat.py:303`.
11. **OOC native parent hashing and orphan remapping are different operations.** The remap sites retain Python hash computation. See `O/_mask.py:140`.
12. **“Group_key is native only on chunked” excludes the unified full-frame slice.** It is absent from SEQ native operations, but has a registered native operator used by unified execution. See `E/_operator_registry.py:238`, `E/_pipeline_full_frame.py:106`, and `tests/physical/test_shadow_group_key.py`.
13. **The claim that OOC tokenize/vault is entirely full-frame-only is stale.** A restricted, fitting vault shape is admitted. Its Python accumulation remains unbounded in distinct triples. This plan does not widen it. See `E/_pipeline_routing.py:364` and `:556`.
14. **Universal byte-identical schema claims overstate existing contracts.** Chunked passthrough and fixed-schema streaming have explicit representation rules. Preserve those rules, but add no new parity exceptions. See `README.md` public API entry for `run_mask_chunked` and `O/_emit.py`.

The audit's A-2 platform recipe and external roadmap claims are not independently established by these engine files. Correct the engine references here. Leave platform recipe changes to their owner.

## Exact reference edits

Goal: remove inaccurate promises and distinguish route admission from native-kernel admission. This slice changes no runtime behavior.

**A.1. Substrate terminology.** After `docs/execution-backends.md:15`, insert:

> The ExecutionAdapter substrate is pandas-only. DECOY_SUBSTRATE accepts pandas; other values raise invalid_substrate. Execution routes are separate from this setting. Rust and Arrow kernels can run inside those routes.

Evidence: `E/_substrate.py:23`. Retain the separate meaning of Polars in subset joins.

**A.2. Route selection.** Replace `docs/execution-backends.md:46` through the introductory routing paragraph with:

> The router selects an execution route from job shape, eligibility, and memory signals. execution_mode can force a supported route. A caller does not select a masking backend through DECOY_SUBSTRATE. Default byte-estimate routing can select OOC below five million rows. The row threshold applies when byte-estimate routing is disabled. A confirmed probe can select full-frame.

Add a link to `E/_pipeline_routing.py`. Do not reproduce its stale “single-parent” explanatory comments as current policy.

**A.3. Decline vocabulary.** Replace `docs/execution-backends.md:33` through `:39` with:

> A route decline excludes the job from that route. Auto routing can choose sequential or full-frame, subject to memory admission. A forced incompatible route raises an error. A native-kernel decline can instead retain the current route and use its reference implementation. Backend evidence identifies that choice. The pandas adapter is the reference oracle; the full-frame route can also run the unified native slice.

Apply the same distinction to the “excluded” class at `docs/records/2026-10-16-d-audit-compute-matrix.md:22`. Replace the absolute full-frame fallback claim at `docs/execution-backends.md:147` with this route-local distinction. Remove the CLI bundle assertion from this engine reference unless it gains a separately checked CLI source.

Evidence: `E/_pipeline_routing.py:507`, `:596`; `O/_mask.py:140` through `:215`; `E/_sequential_native.py:212`; `E/_pipeline_full_frame.py:106`.

**A.4. Qualify every native table.** Above both route tables, insert:

> These cells identify available native implementations, not unconditional admission. Configuration, source dtype, companion availability, and runtime guards still apply. A Python fallback inside an admitted route is distinct from a route rejection.

Add the following shared qualification table to `docs/execution-backends.md`. Link it from the record immediately after its matrix.

| Strategy | Exact qualification to document |
| --- | --- |
| hash | SEQ/OOC native hashing admits string, large_string, bool, and signed/unsigned integers. Timestamp types decline there. Chunked also admits timezone-aware timestamps through its shared predicate, subject to other guards. Namespace requirements remain enforced. |
| fpe | Native payload requires exact string, namespace, valid FPE configuration, and no checksum option. SEQ keys additionally admit supported integer representations. Do not imply all integer payloads are native. |
| categorical | SEQ/OOC native selection requires deterministic mode, namespace, exact-string source, string categories, and a buildable weighted CDF. Seeded positional categorical is separately native on chunked. OOC rejects its non-deterministic mode. |
| bucket_perturb | OOC requires explicit date_format at route admission. Native execution further requires exact string, namespace, supported bucket and format, no timezone or special pandas format, and valid preserve_values. Runtime range guards still apply. |
| date_shift | Native SEQ/chunked requires exact string, namespace, explicit non-special timezone-free format, no group_by, integer day bounds within the accepted range, and valid preserve_values. Main OOC rejects the entire strategy. |
| faker | Providers are person_first_name and person_last_name on these native paths. SEQ admits exact string with deterministic REUSE or positional REUSE. Chunked additionally admits bool/integer deterministic REUSE with its fixed-schema producer guarantee and accepted conversion metadata. Positional bool/integer/float REUSE requires explicit pool_size. Other modes and providers use their existing Python path. Main OOC rejects faker. |
| text_redact / text_mask | Native paths have source/configuration guards. NER remains Python. OOC text_mask currently uses its dedicated loop, including batched NER inference. |
| code_set | OOC admits mask mode without chapter_preserve. Gen mode and chapter_preserve remain route declines. There is no named Arrow code-set kernel on main. |
| group_key | SEQ has no native admission. Native chunked requires an exact string/int64/bool sibling, an unmasked non-self sibling, and valid even length in [8,64]. Unified full-frame also has a native implementation. |
| redact / truncate / passthrough | Keep the named-Arrow/no-op classifications, but do not promise arbitrary dtype/schema identity beyond each route's existing contract. |

Evidence: `N/_requirements.py:95`; `O/_hash_native.py:49`; `E/_sequential_native_ops.py:225`; `O/_fpe_native.py:40`; `O/_categorical_native.py:57`; `N/_operator_config_rejections.py:80`, `:150`; `E/_sequential_native_ops.py:382`; `E/_operator_registry.py:97`; `N/_chunked_group_key_gate.py:73`.

Add a shared guard note: “Masked nullable integer columns under truncate or categorical can fail with null_bearing_int_unsupported. FK children resolved through relationships are exempt.” Evidence: `E/_guards.py:35`; `O/_null_int_guard.py:43`.

**A.5. OOC table.** At `docs/execution-backends.md:73` and record `:44`, split categorical into deterministic and non-deterministic rows. Qualify bucket_perturb with explicit format. Qualify code_set at backend `:77` and record `:48` with “mask mode without chapter_preserve.”

Replace “and the rest” at backend `:78` and record `:49` with explicit rows:

- faker: `out_of_core_faker_pool_unsupported`.
- date_shift and bucketize: `out_of_core_row_error_strategy_unsupported`.
- geo_generalize: `out_of_core_whole_column_aggregation_unsupported`.
- formula and derived: `out_of_core_dynamic_output_type_unsupported`.
- nested: `out_of_core_child_dispatch_unsupported`.
- grouped_series, windowed_date, derived_aggregate, group_key, joint_mask: `out_of_core_cross_row_strategy_unsupported`.
- shuffle and top_code: `out_of_core_strategy_unsupported`.
- non-deterministic categorical: `out_of_core_categorical_nondeterministic_unsupported`.

These edits describe main. B1 later changes the affected cells after its actual merge. Evidence: `O/_compat.py:18`, `:58`, `:95`, and `:272`.

**A.6. FK surface.** Change “As an FK key” in the OOC table to “Parent-key strategy.” Add:

> Parent-key masking and orphan remapping have separate compute paths. Native hash masks admitted parent batches. Hash remapping currently uses the Python reference. Other payload strategies are excluded as parent-key strategies, even when their payload implementation is native.

Evidence: `O/_compat.py:459`; `O/_mask.py:140`.

**A.7. A-9.** Replace `docs/execution-backends.md:93` exactly with:

> A-9 delivered the admitted composite and multi-parent FK shapes. It did not schedule OOC faker or date_shift. Composite scalar children, composite chains, and unsupported key dtypes retain the restrictions above.

Retain the existing scalar-child, chain, and mixed-parent-type restrictions at `:80` through `:92`.

**A.8. Missing OOC restrictions.** After that paragraph, add:

> OOC also declines when predicates, unsupported non-scalar work, self-referential relationships, and table-level cycles. The public supported-strategy set is only a coarse payload list. It does not establish job admission or native dtype support.

Cite `O/_compat.py:193`, `:212`, `:236`, `:412`, and its public-alias comment at `:125`.

**A.9. Text implementation.** Replace the universal “Rust spans, Python splice” wording at backend `:19`, `:57`, and record `:17`, `:33` with:

> Rust span detection, with native splice/FPE paths for admitted text_mask configurations and Python splice fallback.

Keep text_redact's own splice qualification distinct. Evidence: `N/_kernels_scalar.py:209` and `:293`; `N/_text_mask_native_splice.py`; `N/_text_mask_native_fpe.py`. Preserve the record's honest distinction between backend labels and compiled-consumption proof.

**A.10. Stale group_key statement.** Replace record `:60` with:

> The remaining shared SEQ handlers listed above have no SEQ native admission. group_key has native implementations on chunked and eligible unified full-frame execution.

Make backend `:125` consistent. Do not claim that a dedicated Python handler never invokes a compiled primitive internally.

**A.11. Faker exceptions.** Replace backend `:113` through `:119` with the faker qualification in A.4. Retain the pool-backed Python explanation. “Exact string only” must be scoped to SEQ, not all native legs.

**A.12. Generation.** Keep the Python/NumPy conclusion and the shelving of FR3. At backend `:133`, clarify:

> PoolSampler's compiled deterministic selection branch serves masking. Generation's pool call sets deterministic=False and REUSE. The streamed pooled selector also uses NumPy integers.

At backend `:140`, scope resident/streamed equality to admitted streamed generation shapes. Do not imply legacy whole-table statistical families all stream. Evidence: `G/_faker_pool.py:407`, `:437`; `G/pool/_sampler.py:259`; `G/_admission.py`; `docs/plans/2026-10-16-phase-f-program.md:140`.

**A.13. Capability matrix.** The 23 mask names, 12 generation names, and internal nested distinction match the inspected registries. No strategy rename or count change is justified. Preserve the provider backend identifiers, including `decoy_native`.

Add this text through `scripts/gen_capability_matrix.py:23` and its provider-section renderer, then regenerate `docs/capability-matrix.md`:

> This inventory lists strategies and provider capabilities, not route eligibility. See execution-backends.md for route and dtype restrictions. Provider backend names identify adapters. decoy_native identifies the engine's Python identifier providers, not the Rust companion. The Deterministic column describes provider capability, not deterministic pool-backed masking.

Evidence: `scripts/gen_capability_matrix.py:71`, `:102`, `:157`; `src/decoy_engine/providers_v2/_registry.py:180`; `G/_faker_pool.py:407`. Also state that generation enumeration combines V2 configuration and legacy ColumnGenerator keys. distribution is a legacy entry, not a streamed-generation promise. See `scripts/gen_capability_matrix.py:48` and `src/decoy_engine/generators/columns.py:116`. Never edit generated rows by hand.

## Slice A execution and acceptance

Modules: the three requested documents and the capability generator's prose templates only. Barry owns these edits.

Write the acceptance checklist before editing. Its new assertions must fail against main:

1. The rendered backend guide explicitly states pandas-only substrate and distinguishes route declines from kernel declines.
2. The rendered guide no longer promises A-9 for declined strategies.
3. Both compute matrices state conditional OOC code_set and categorical admission, and enumerate top_code.
4. The record no longer says all remaining SEQ handlers lack native admission everywhere.
5. Generated capability prose distinguishes provider backend labels from compiled compute.
6. The guide documents wider chunked faker types and the unified full-frame native slice.

These are document acceptance checks, not invented runtime tests. Run the existing capability drift test after regeneration. Run the existing D-audit suite to demonstrate that documentation changes did not alter its derived classifications. New prose assertions can be a scoped review checklist rather than permanent string-matching tests.

Failure modes: changing registry truth to match prose, importing unmerged branch behavior into main documentation, or replacing historical evidence with unrun claims.

Effort: about one day, low implementation risk. Benefit: an accurate contract for every later slice.

Gate: Dennis checks factual scope, Codex checks source citations, and Barry records the documentation pass. No behavioral claim is considered delivered by this slice.

# Part B: ordered implementation slices

## Shared acceptance contract

These requirements apply to every behavioral slice.

- Compare against the actual pandas oracle, with unified/native shortcuts disabled in the reference run. Forcing execution_mode=full_frame alone is insufficient.
- Compare ordered values, nulls, Arrow field types, relevant metadata, warnings, errors, corpus/pool evidence, and FK outcomes. Do not use values-only equality.
- Use several batch splits, including one-row batches, irregular splits, and splits immediately before nulls or invalid values.
- Include empty input, all-null input, duplicate values, non-ASCII text, nullable extension metadata, and unsupported dtypes where relevant.
- Prove that the requested route ran. For native compute, prove consumption of the real compiled result. A backend label or equal output alone is insufficient.
- Check both resident collection and transactional streamed output. On failure, publish no partial output and release spill files and run-local state.
- Keep unsupported configurations declined with a caller-visible reason. For auto routing, check the actual SEQ/full-frame/rejection outcome. For forced OOC, check refusal.
- Never add an accepted divergence, reduce equality strength, skip a fixture, or widen a tolerance to pass.
- Preserve the parent-key allowlist. Every new strategy in this plan is payload-only unless its slice explicitly says otherwise.

For each slice, add its named new acceptance tests first. Demonstrate failure on `d613c804` and on the immediate predecessor when the prerequisite has already merged. Existing regression tests need not fail on main. They remain mandatory guardrails, not substitutes for a new route or compute witness.

Performance evidence uses fixed synthetic FK data, stable configuration and seed, the pinned companion, and a fixed batch size. Start with a correctness tier, then a representative large tier that exercises spilling. Record warmup, at least three measured runs, median and spread, wall time, peak RSS, and spill bytes. Compare OOC against the route auto actually selected before the change, often SEQ. Also compare against full-frame where it safely fits. Record pool construction separately from selection. No speedup is assumed in this plan.

The remote builder runs the named slice tests and nearest affected regression suites in the foreground. CI or the gate owner runs broader suites and the required test flight. Each slice includes Dennis's adversarial pass, remediation, a separate Codex pass, and Barry's documentation sync. Record the gate against the exact final commit using the repository gate script. A new commit invalidates that receipt.

## Established solutions and implementation pattern

Reuse the OOC pattern already established by hash, FPE, categorical, text_redact, and bucket_perturb:

1. Static route admission in `O/_compat.py`.
2. Schema/configuration admission and one table-level kernel plan, such as `O/_categorical_native.py:57` and `:173`.
3. Batch dispatch through `O/_mask.py`.
4. Shared deterministic primitive, strict result validation, route-local reference handling, and measured backend evidence.
5. Runner and stream-driver integration with bounded state.

Hash provides a useful distinction between column masking and remap computation (`O/_mask.py:140`). Date and faker reuse the existing index kernel, not new cryptography. Text reuses existing span and splice implementations.

For code_set, use Arrow dictionary encoding plus indexed expansion. Arrow documents null-preserving dictionary indices and take semantics: [dictionary_encode](https://arrow.apache.org/docs/python/generated/pyarrow.compute.dictionary_encode.html), [take implementation](https://github.com/apache/arrow/blob/main/python/pyarrow/compute.py). Keep the existing Python selector for each distinct value initially. This avoids inventing a different HMAC or corpus-selection algorithm. No new dependency is needed.

## B1. Integrate the existing guarded row-error bridge

**One behavior added:** eligible own-column numeric masking can stay on OOC while preserving fail-closed row-error handling.

**Current disposition:** bucketize and top_code use shared Python handlers on SEQ and the chunked oracle. They have no native operator. Main OOC declines bucketize with `out_of_core_row_error_strategy_unsupported`, and top_code with `out_of_core_strategy_unsupported` (`O/_compat.py:58`, `:303`).

**Prerequisite choice:** integrate the reviewed equivalent of remote-tracking `e1f96acb`, not the older local branch. Do not rebuild its bridge. Rebase or reconcile it through the existing branch owner. Recheck the resulting SHA and gates.

The prerequisite has two important limits:

- `bucketize` admits only numeric types that pass the strategy-specific conversion check. Plain float64 is an important positive case. String and large_string remain declined.
- `top_code` can additionally admit plain string/large_string because its parser reads each cell independently. Metadata restoration and null widening still require checks.

Evidence: `e1f96acb:O/_row_error_bridge.py:66`, `:89`, `:142`; `e1f96acb:O/_compat.py`, `_row_error_dtype_rejection`. The decline is `out_of_core_row_error_dtype_unsupported`.

**Engineering:** retain schema-aware admission, registry transport, absolute source row offsets, and collection of RowErrorRecord values. Both runners must call fail_closed_on_row_errors before publishing. Never use an O(rows) accumulated error list. Preserve the branch's conservative missing-schema behavior at preflight.

**Quarantine boundary:** this bridge is an error channel, not an OOC quarantine implementation. For requests whose successful result requires quarantine, retain a compatible existing route or reject. Do not admit them merely because errors can now be collected. Compare dirty-data behavior under the same fail-closed policy. Whole-frame quarantine equivalence is not claimed.

The branch also contains the separate windowed_date offset work and NER changes. Track those dependencies and their gates explicitly. This plan neither duplicates nor broadens them.

**Modules:** branch changes in `O/_row_error_bridge.py`, `O/_compat.py`, `O/_mask.py`, `O/_runner.py`, `O/_stream_driver.py`, `O/_emit.py`, and schema-propagating preflight/routing callers.

**Acceptance first:** retain and run `tests/parity/test_out_of_core_row_error_bridge.py` from the newer ref. Add public-route cases that force OOC for plain-float64 bucketize and string top_code. Each must assert successful OOC execution and oracle-identical output across splits, so each fails on current main. Add a new bridge-level absolute-error-position test that cannot run without the bridge. Pair these with the branch's existing rejected dtype and metadata cases.

**Guardrails:** the string fixture `["18014398509481993", "0.5"]` must not run bucketize on OOC. Test null-driven integer widening, pandas extension metadata, missing schemas, lazy source preflight, first-batch nulls, and transactional abort.

**Failure modes:** confusing Arrow dtype stability with pandas coercion stability, using post-reorder offsets, silently retaining invalid raw data, or dropping source metadata.

**Effort/benefit:** one to two days of integration and gate work if the branch is ready. High reuse value, medium coverage benefit. The bridge retains Python compute.

**Gate:** Dennis plus Codex on the integrated exact commit. No dependent slice starts from the older unguarded bridge.

## B2. OOC date_shift for the existing native configuration subset

**One behavior added:** explicit-format, ungrouped date_shift payload runs on OOC.

**Current disposition:** native SEQ and chunked exist for the narrow configuration in `N/_operator_config_rejections.py:150`. Other shapes use Python. OOC declines all date_shift with `out_of_core_row_error_strategy_unsupported`. Parent-key use remains excluded.

**Tractability:** high for the existing subset. B1 supplies the missing row-error transport. Reuse `native_date_shift` from `N/_date_shift_ext.py:124`. It returns output plus batch-local error positions. It already shares the oracle's pandas parse, date arithmetic, and formatting.

**Engineering:** add an OOC date-shift table plan following `O/_bucket_native.py`. Reuse the existing configuration rejection function. Require exact string, namespace, explicit timezone-free non-special format, no group_by, valid preservation configuration, and accepted integer day bounds. Pass the resolved mask key. Convert kernel error positions to absolute RowErrorRecord values through B1. Preserve local error reasons from `N/_date_shift_ext.py:65`. A missing companion can use the same bounded reference computation only within this proven subset, with honest evidence.

Do not infer date formats from each batch. Do not add grouped date shifting. The latter reads pre-mask snapshots, not arbitrary live siblings (`E/_strategies/_date_shift.py:130`).

**Modules:** new `O/_date_shift_native.py`; `O/_compat.py`, `O/_mask.py`, both OOC runners, output typing, backend evidence, and the existing shared date helper if transport needs a small extension.

**Acceptance first:** new `tests/parity/test_ooc_date_shift_native.py`. Test admitted forced-OOC output and compiled-result consumption for several formats, inverted bounds, nulls, leap dates, duplicates, and preserve_values across splits. Each new positive test fails main's route gate. Add a bridge integration case with a bad date after a batch boundary. It must report the absolute position and abort output, rather than fail with “unsupported strategy.”

**Guardrails:** keep group_by, autodetect, timezone directives, mixed/ISO8601 special parser modes, large_string, invalid preservation settings, and out-of-range bounds declined. Check empty/all-null output typing and pandas date arithmetic boundaries.

**Failure modes:** changing parsing permissiveness, signing the shift with job_seed instead of mask_key, dropping parse errors, or reporting compiled execution for an all-null batch.

**Effort/benefit:** two to four days. High coverage benefit for date-heavy FK jobs. Reuses an existing kernel without new Rust.

**Gate:** Dennis plus Codex, including the existing SEQ and chunked date-shift parity suites.

## B3. Run-scoped OOC faker pool lifecycle

**One behavior added:** the OOC execution context can own and reuse a bounded provider pool across batches.

**Current disposition:** there is no OOC pool transport. Faker stays declined with `out_of_core_faker_pool_unsupported`. B1's bridge constructs a fresh PoolCache for each call, so that cache cannot serve faker (`e1f96acb:O/_row_error_bridge.py`, `bridge_own_column`).

**Tractability:** high, but memory accounting and identity reuse are essential. This infrastructure slice does not yet admit faker.

**Engineering:** construct a run-scoped cache and prepared pool descriptor. Reuse PoolBuilder, PoolCache, and `resolve_faker_pool_identity`. Pin provider, locale, build configuration, namespace, compiled pool size, and job seed. Pool construction uses job_seed. Deterministic selection uses mask_key. Do not size pools from a batch length. See `E/_strategies/_faker.py:67` and `G/pool/_identity.py:35`.

Reserve pool memory before construction under the OOC run budget. Reject oversized or unbounded pool plans with a new explicit code. Scope state to the run and provider registry. Avoid global caches and cross-job key contamination. Reuse across tables only when the full identity matches.

**Modules:** a small new `O/_faker_pool.py`; OOC runner/context construction, stream-driver arguments, budget integration, and teardown. Reuse `G/pool/` without changing its deterministic protocol.

**Acceptance first:** new `tests/unit/execution/test_ooc_faker_pool_context.py`. Tests construct the new context and demand one build across multiple simulated batches. Check identical identities share a pool, changed locale/provider/namespace/configuration do not, and a second run has independent state. Check reservation refusal before provider allocation. These tests fail main because the context and behavior do not exist.

**Guardrails:** oracle-identical ordered pool values, provider errors, failed construction cleanup, and no allocation proportional to source row count.

**Failure modes:** batch-sized pools, cache keys that omit provider configuration, unaccounted Python string/object memory, and reference lifetimes that survive abort.

**Effort/benefit:** two to three days. No immediate route coverage gain. It is the mandatory prerequisite for B4.

**Gate:** Dennis plus Codex. Retain faker's current route decline until B4 passes.

## B4. Deterministic REUSE faker payload on OOC

**One behavior added:** eligible person-name faker payload remains on OOC and selects from its prepared pool through the compiled index kernel.

**Current disposition:** SEQ admits exact-string deterministic or positional REUSE for person_first_name/person_last_name. Chunked has wider proven source domains. Other providers or cardinality modes use their Python paths. OOC rejects every faker configuration. Evidence: `E/_sequential_native_ops.py:382`; `E/_operator_registry.py:200`; `O/_compat.py:58`.

**Tractability:** high for deterministic REUSE with exact-string source and string pool output. Use B1's failure channel and B3's cache. Keep the initial provider allowlist unchanged. Address providers are not silently added.

**Engineering:** reuse `sample_faker_array`, already called by `E/_sequential_native_ops.py:401`. Admit only deterministic REUSE with namespace, supported provider, exact-string source, and bounded prepared pool. Thread the resolved keys and the same pool into every batch. Follow the categorical OOC table-plan pattern. Record actual compiled consumption.

Keep unsupported modes under an explicit faker shape rejection. Preserve `out_of_core_faker_pool_unsupported` for paths without the pool capability. Invalid provider or pool construction remains a job error, not a fabricated row error. No raw source value may become a successful fallback output.

**Modules:** `O/_faker_pool.py`, new `O/_faker_native.py`, `O/_compat.py`, `O/_mask.py`, runner/stream driver, output typing, and evidence.

**Acceptance first:** new `tests/parity/test_ooc_faker_native.py`. Force OOC for both providers and prove identical output for repeated values across batch boundaries. Include nulls, Unicode, explicit pool size, locale, distinct mask/job seeds, and equal pool identities across columns. Assert one build and real compiled-index consumption. These positive route tests fail main. Add a late provider/kernel failure test that proves no partial publication.

**Guardrails:** reject UNIQUE, MATCH, SCALE, positional REUSE, non-allowlisted providers, non-string source, non-string pool output, and absent deterministic namespace. Check preserved nulls and metadata. Check companion absence with an explicitly evidenced bounded reference path or a coded refusal, never silent whole-job materialization.

**Failure modes:** pool reseeding, wrong key selection, selecting from an unordered or deduplicated pool, cross-run cache leakage, and hidden O(rows) state.

**Effort/benefit:** three to five days after B3. High benefit for realistic person-name masking on large FK jobs. This does not solve native address faker coverage.

**Gate:** Dennis plus Codex, including existing faker pool, canonicalization, SEQ, and chunked regression suites.

## B5. OOC text_mask through the shared span implementation

**One behavior added:** eligible OOC text_mask uses the existing compiled span path instead of repeating Python detection.

**Current disposition:** SEQ and chunked use `native_text_mask`. OOC already admits text_mask but uses `mask_cell` for each non-null cell. It therefore has no whole-route decline for this gap. NER and unsupported native configurations retain Python computation. Evidence: `O/_mask_group_c.py:194`, `:261`; `N/_kernels_scalar.py:209`; `E/_sequential_native_text_mask.py:57`.

**Tractability:** high. This improves compute coverage, not strategy route admission.

**Engineering:** use the shared configuration gate and exact-string source gate. Call native_text_mask with mask_key, resolved detectors, token policy, date bounds, and sub-floor policy. Preserve the distinction between detectors=None and an empty detector tuple. Aggregate notices once per table, as OOC already does. Retain NER's existing batched Python branch and unsupported-cell fallback.

**Modules:** `O/_mask_group_c.py`, a small OOC text-mask plan/evidence helper, `O/_mask.py`, and shared text helpers only where needed for consumption evidence.

**Acceptance first:** new `tests/parity/test_ooc_text_mask_native.py`. Assert real compiled candidates affect output while Python detection is patched to reject recomputation for admitted cells. Compare redaction, passthrough, FPE, faker-span, and date-span configurations against the oracle across splits. Compare warning totals. These consumption tests fail main's Python loop.

**Guardrails:** catalog mismatch, missing companion, NER, Unicode span offsets, overlapping spans, no matches, all-null batches, FF1 sub-floor policies, and fail-closed error wording.

**Failure modes:** a cosmetic native label with Python recomputation, duplicated warnings, lost NER spans, or changed secret-key handling.

**Effort/benefit:** one to three days. Good compute benefit for detector-heavy text. No claimed gain for NER inference or text with no supported spans.

**Gate:** Dennis plus Codex. Preserve the existing text-mask and OOC group-C parity suites.

## B6. Named Arrow code_set map on OOC, then reuse on resident handlers

**One behavior added:** repeated code values are selected once per distinct batch value and expanded with Arrow.

**Current disposition:** code_set has dedicated Python compute on SEQ, OOC, and chunked oracle. OOC already admits mask mode without chapter_preserve. Other OOC shapes decline with `out_of_core_code_set_shape_unsupported`. Chunked has no registered native code_set operator and retains its oracle leg. See `O/_compat.py:314`, `O/_mask_group_c.py:171`, and `N/_dispatch.py:320`.

**Tractability:** high for exact-string mask input without chapter_preserve. The first version is an Arrow dict-map with Python selection per distinct value, not pure compiled cryptography.

**Engineering:** introduce a shared `kernel/_code_set.py` array helper. Dictionary-encode each batch, apply the unchanged `apply_code_set` to its distinct non-null strings, then expand using take. Pin one corpus record for the run. Preserve selection order, source exclusion, corpus-version evidence, and the existing mask-key semantics. Never substitute derive_index_batch merely because both algorithms use keyed hashes.

Use only a batch-local dictionary initially. A run-global input-value cache would grow with job cardinality. Reuse the pinned corpus indexes. Evidence: `T/code_set.py:233`, `:330`; `O/_mask_group_c.py:29`.

Ship the OOC consumer first. A follow-up commit can call the same helper from the shared handler on proven exact-string inputs, giving SEQ acceleration. Native-chunked registration is a separate independently gated extension, because its routing and output contracts differ.

**Modules:** new shared kernel; `O/_mask_group_c.py`; `E/_strategies/_code_set.py` only in the follow-up; corpus evidence transport and D-audit classification.

**Acceptance first:** new `tests/parity/test_ooc_code_set_arrow.py`. For repeated input codes, assert one scalar selection per distinct non-null input per batch and an Arrow expansion. Demand exact oracle output and corpus evidence across splits. The computation-count assertion fails main's cell loop. Use shipped and customer corpora, unknown input codes, source-exclusion cases, nulls, and Unicode.

For the SEQ follow-up, add a separate forced-SEQ consumption witness. For any later chunked operator, add an independently failing native-admission witness before registry changes.

**Guardrails:** corpus replacement during execution, version mismatch, singleton/empty corpus behavior, duplicate source values, and high-cardinality batches. Preserve gen/chapter declines.

**Failure modes:** changing the HMAC bytes, conflating source membership with dictionary index, losing nulls, or attributing a Python selector to Rust.

**Effort/benefit:** two to four days for OOC; one to two days per additional consumer. Strongest benefit on low-cardinality clinical codes. High-cardinality speedup is uncertain.

**Gate:** Dennis plus Codex for each consumer commit. The named kernel's existence does not automatically authorize every route.

## B7. Native group_key payload on SEQ

**One behavior added:** SEQ derives eligible group_key output through the existing raw-hex kernel.

**Current disposition:** SEQ runs `GroupKeyStrategyHandler` without a native seam. There is no current SEQ native-decline code for this absent operator. Chunked and eligible unified full-frame have native implementations. OOC rejects it with `out_of_core_cross_row_strategy_unsupported`. See `E/_strategies/_group_key.py:55`, `E/_sequential_native.py:181`, and `N/_group_key_kernel.py:86`.

**Tractability:** high for conservative payload admission. group_key reads a sibling in the same row. It is not a whole-dataset aggregate, despite its membership in the OOC cross-row set.

**Engineering:** add a SEQ native operation and a handler seam at the same point where the Python handler reads its sibling. Start with string, int64, and bool siblings, no self-anchor, no value-changing sibling node, and no FK-rewritten sibling. Keep key use reporting-only Python. Reuse the exact synthesized namespace `group_key/<target>`, mask_key, length, and prefix.

Preserve sibling metadata and null spelling. Do not rebuild a bare Arrow array that changes None to <NA>, or rounds large integers. The existing helper explains this requirement at `N/_group_key_kernel.py:49`. If the SEQ seam uses the live pandas sibling, share the proven stringification/derivation boundary instead of round-tripping through a different dtype.

**Modules:** `E/_strategies/_group_key.py`, a small SEQ operation helper, `E/_sequential_native.py`, native decision evidence, and shared group-key helper if needed.

**Acceptance first:** new `tests/parity/test_seq_group_key_native.py`. Force SEQ and prove raw-hex output consumption with string, nullable int64, and bool siblings. Include pandas-origin metadata, integers above 2**53, repeated/null siblings, non-default prefix/length, and different target columns. Each native-consumption assertion fails main.

**Guardrails:** masked/self/FK siblings, unsupported types, when subsets, missing companion, empty results, and FK-key reporting. Keep unsupported shapes on the existing Python handler with a specific new reason. Never add group_key to `_KEY_STRATEGIES`.

**Failure modes:** reading the wrong sibling version, deriving null instead of its oracle string, using plan.namespace, and preserving the wrong empty-output type.

**Effort/benefit:** two to three days. Medium compute benefit for group identifiers on FK jobs. It does not widen OOC.

**Gate:** Dennis plus Codex, including existing chunked and physical group-key parity fixtures.

## B8. Seeded positional categorical on OOC

**One behavior added:** a seeded, position-keyed categorical payload can use OOC's durable source offset.

**Current disposition:** SEQ uses its Python handler for non-deterministic categorical. Chunked has a seeded positional native path. OOC rejects it with `out_of_core_categorical_nondeterministic_unsupported`. See `E/_sequential_native_ops.py:309`, `N/_dispatch.py:322`, and `O/_compat.py:291`.

**Tractability:** plausible and bounded, but lower priority than the explicit requested gaps. It depends on B1's branch offset transport. This is not a port of unseeded RNG.

**Engineering:** reuse the existing chunked positional categorical primitive and selection namespace. Require explicit seed, supported string categories and CDF, proven source null behavior, no when predicate, and payload-only use. Thread the pre-join source row offset. Do not derive positions from output rows, which FK resolution can filter or reorder.

**Modules:** `O/_categorical_native.py`, `O/_compat.py`, `O/_mask.py`, both runners, and the existing positional categorical helper used by `tests/native/test_categorical_positional_kernel.py`.

**Acceptance first:** new `tests/parity/test_ooc_categorical_positional.py`. Force OOC for seeded uniform and weighted categorical, then compare whole-source oracle output across irregular splits. Include null rows before boundaries and FK reordering. Assert compiled consumption. These tests fail main's categorical rejection.

**Guardrails:** retain unseeded-mode decline, parent-key rejection, malformed CDF behavior, and unsupported source types. Keep the SEQ behavior unchanged in this slice.

**Failure modes:** resetting positions per batch, compressing positions around nulls, using mask_key instead of the existing positional entropy contract, and reordering before masking.

**Effort/benefit:** two to four days. Medium coverage benefit. Proceed only after the requested higher-priority slices and a representative workload justify it.

**Gate:** Dennis plus Codex. A failed parity proof leaves the existing decline intact.

# Explicitly out of scope / stays declined

- **geo_generalize on OOC:** whole-dataset k-anonymity counts determine each row's output. A batch-local approximation is incorrect. Keep `out_of_core_whole_column_aggregation_unsupported` (`O/_compat.py:95`).
- **formula and derived on OOC:** dynamic output types do not fit the fixed schema. Formula also has order-dependent RNG. Derived needs correctly ordered sibling context. Keep `out_of_core_dynamic_output_type_unsupported` (`O/_compat.py:103`).
- **nested on OOC:** child dispatch and JSON assembly need a separate bounded architecture. Keep `out_of_core_child_dispatch_unsupported` (`O/_compat.py:116`).
- **grouped_series, derived_aggregate, joint_mask:** group state, aggregates, or reference-row access require bounded relational lowering. Do not port them in this program.
- **OOC group_key:** same-row sibling access is theoretically tractable, but this plan adds only the requested SEQ native seam. Preserve its current OOC decline.
- **windowed_date:** main declines it. The prerequisite branch already owns a narrow offset-based port. Preserve its masked-anchor and FK-rewritten-anchor restrictions. Do not call the strategy inherently impossible or expand that branch's scope here.
- **shuffle:** whole-column permutation remains outside this program, with `out_of_core_strategy_unsupported`.
- **bucketize string/large_string and unstable metadata combinations:** remain declined even after B1. Numeric coercion can change with batch composition. “Clean strings” is not a proof.
- **date_shift autodetection and group_by:** retain declines. Whole-column format selection and pre-mask sibling snapshots are separate capabilities.
- **code_set gen and chapter_preserve:** offsets and row-error transport make future work more plausible, but do not automatically establish parity. Retain `out_of_core_code_set_shape_unsupported` in this program.
- **faker UNIQUE/MATCH/SCALE and positional OOC mode:** excluded from the new port. The first three require whole-column state. Positional REUSE is deferred by scope, not declared mathematically impossible.
- **Additional faker providers and broader source dtypes:** keep the allowlist narrow until each provider's pool identity, bounded construction, and output type are proved.
- **OOC parent-key expansion:** none of these payload ports changes the join/remap allowlist or composite-chain restrictions. Adding FPE keys requires its own orphan/remap and collision analysis.
- **Checksum FPE and other native dtype gaps:** retain current route-local Python fallbacks. This plan does not weaken canonicalization, timezone, dictionary, decimal, or float guards.
- **OOC quarantine:** the prerequisite only fails closed. A true quarantine route needs row lineage, FK consequences, bounded error storage, and atomic publication. It is a separate design.
- **Generation on Rust:** FR3 remains shelved. Existing generation uses Python/NumPy RNG; replacing it with masking HMAC selection changes values. No new evidence supports reviving it (`docs/plans/2026-10-16-phase-f-program.md:140`).
- **NER acceleration and large vault jobs:** retain their existing Python and memory restrictions. No new inference engine or spillable vault is proposed.

“Intractable” here means incompatible with the current batch-local execution contract and this program's scope. It does not claim that a separate multi-pass or relational architecture could never support these operations.

Evidence limits: this plan is based on source and branch inspection. No implementation, parity run, or performance measurement was performed. qmd recall failed because its SQLite initialization attempted a write in the read-only sandbox. No repository graph was available, so direct source inspection supplied the citations. The external audit note was not overwritten.
