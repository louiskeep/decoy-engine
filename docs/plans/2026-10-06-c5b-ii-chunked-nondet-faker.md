Status: plan (revision 4, author = Opus). Codex plan gate: rounds 1-3 REVISE folded; round 4 authorized by the owner (2026-10-06) and pending.
Rules consulted: 00-universal, development-loop, testing, architecture, code-review, scope-discipline, api-and-compatibility

# C5b-ii: non-deterministic REUSE Faker on the chunked native route

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C. Decision: Cam 2026-10-05 (position-keyed like C1b; job_seed key; namespace defaults from table and column; REUSE only; one pre-GA output break, already shipped in C5b-i, engine #205).

Branch: `feat/c5b-ii-chunked-nondet-faker`, stacked on `feat/r1b-merged-dispatch` (R1b: one kernel step per operator). It is rebased onto main after R1b merges and before this slice's final gate. Risk R2: one route opened, parity-gated, no new kernel.

Template: C1b-ii (`docs/plans/2026-10-04-c1b-ii-chunked-nondet-categorical.md`, rev 5), which did the same for the position-keyed categorical. Its two dennis lessons apply here directly: a non-string source must take the chunked-oracle leg rather than fail closed, and the auto-router needs an end-to-end test.

## 1. Goal and scope

C5b-i made non-deterministic REUSE Faker position-keyed on the oracle: row `g` takes `pool.values[derive_index(job_seed, sel_ns, encode_int(g), pool.size)]`, where `sel_ns` is the configured namespace or `faker-nd/{len(table)}:{table}/{len(column)}:{column}`. Every route except whole-frame still vetoes it (`_chunked.py:217-222` says "deferred to C5b-ii").

C5b-ii admits it to the chunked route for the C1 provider allowlist (string-output providers). A string source runs natively and a non-string source runs on the chunked-oracle leg. Per-chunk output is `string` on both legs. Assembled output equals whole-frame on values, column order, warnings and errors, and on Arrow field types except for the one documented degenerate case in §3f. Route evidence is asserted per route, not compared across native and oracle runs.

Out of scope (vetoes stay, each with an honest reason):
- **Unified full-frame route.** No position-keyed operator runs there yet: the coordinator keeps a per-batch table offset (`physical/_shadow_coordinator.py:344,387`) but never passes it to operators, and C1b-ii kept categorical off the route too. Opening it for both positional categorical and positional Faker is a separate slice, **C5b-iii**, added to the roadmap by this slice's DOCUMENT step. The determinism assertion in `_shadow_operators` stays.
- **Out-of-core.** Keeps `out_of_core_faker_pool_unsupported`.

Explicitly IN scope (Codex round 1, HIGH 2): **multi-table split per-table routing.** Non-det Faker is not in the split-deferred set (`_pipeline_multi_table.py:70`), and the split reclassifies each table through the single-table planner (`_pipeline_multi_table.py:191-205`), which reads the same compatibility veto (`_planner.py:327-332`). Lifting the veto therefore lets an above-threshold Faker table inside a split run chunked instead of whole-frame. This is accepted and validated rather than fenced off: each table restarts at `row_offset` 0 under both treatments, and the per-table default namespace is the same, so chunked equals whole-frame per table under the same parity contract. Test 10b pins it end to end.
- UNIQUE, MATCH and SCALE cardinality modes (whole-column draws), nested and composite Faker (non-scalar, already offending on the chunked route), and any Faker inside a FK remap or orphan path.

## 2. Established facts (main `d44465c9` plus R1b; research probe 2026-10-06)

**Oracle (C5b-i):**
- Positional gate `not plan.deterministic and mode is REUSE` (`_strategies/_faker.py:60`).
- Selection namespace `resolve_selection_namespace` (`_faker_positional.py:49-62`). `None` and `""` both mean unset. The column is `ctx.nested_outer_column or column`. An empty `ctx.current_table` raises `faker_positional_table_unknown`.
- The pool is built with the CONFIGURED `plan.namespace` on `job_seed`, so pool identity is unchanged (`_faker.py:77-96`).
- `row_offset = ctx.row_offset`. Nulls are restored from the source and still consume their ordinal (`_faker.py:98-110`).
- Out-of-domain code `faker_position_out_of_domain` (`_faker_positional.py:28`).
- `current_table` is stamped by the pandas adapter for every node (`_pandas_adapter.py:365`); the chunked oracle runs through the same adapter with the global `row_offset` per chunk (C1b-ii fact, `_chunked_oracle.py:170,340`).

**Kernel:** `derive_index_batch` over a dense `pa.uint64()` key column `[row_offset, row_offset+n)` reproduces `derive_index(key, ns, encode_int(g), size)` byte for byte (C1b-ii; `native_categorical_positional`, `_categorical_ext.py:147-178`). `sample_faker_array` is source-keyed and asserts that the index null mask equals the source null mask (`_operator_step.py:122-128` on R1b), so it cannot take dense keys.

**Where it is declined today:**

| Gate | Code |
|---|---|
| Chunked compat veto `_conditional_admission_failures` (`_chunked.py:217-241`) | `chunked_strategy_conditions_unmet` ("requires deterministic: true (position-keyed; chunked implementation deferred to C5b-ii)"; also namespace and explicit pool_size) |
| JC-5 `faker_pool_precondition_met` (`native/_requirements.py:195-218`) | `faker_not_deterministic_reuse_variant`; fallback policy non-native |
| Chunked static route (`native/_dispatch.py:299-300`) | `fallback_policy_not_native:{col}:{policy}` |
| First-chunk checks | `faker_source_type_not_string` (`_dispatch.py:445-462`), `faker_provider_not_native` (C1 allowlist, `_real_type_admission.py:131-132`), `faker_provider_output_not_string` (pool values, `_chunked_entry.py:363-371`) |

**C1b-ii structure to mirror:** a config-only stage-A predicate (`native/_categorical_positional.py`) read by four consumers: the compat veto, `_static_route_decision`'s positional exception (`_dispatch.py:292-298`), evidence `plan_column_backends` (`_chunked_evidence.py:104-109`), and preparation. Stage B (source dtype) only picks the leg.

**FK:** a chunked FK parent key must be `hash` and the child must declare the same strategy (`_chunked_fk.py:253-275`), so a Faker FK key is already rejected. Native preflight reroutes any table touched by a declared relationship to the oracle (`_dispatch.py:244-245`).

## 3. Decisions

**3a. Stage A, config only.** New `native/_faker_positional_admission.py` with `positional_faker_config_of_entry(col_entry) -> PositionalFakerConfig | None` and `positional_faker_config_for_column(config, table, column)`, mirroring `_categorical_positional.py`. Admissible:
- `strategy == "faker"`, not deterministic, `cardinality_mode` absent or `reuse`;
- not nested, not composite;
- an explicit `pool_size` (top-level or provider_config; the chunked capacity declaration rule stays);
- the provider in the C1 allowlist (`C1_PROVIDER_ALLOWLIST`, string-output providers; Codex round 2 HIGH). A non-det REUSE Faker with any other provider stays vetoed and runs whole-frame exactly as today. Its failure text names the provider and says only allowlisted providers run chunked. This keeps numeric- or date-output providers, whose oracle chunks can infer int in one chunk and float in the next and fail assembly with `chunked_schema_mismatch` (`_strategies/_faker.py:109`, `_pipeline_auto_chunk.py:143`), off the chunked route entirely;
- the namespace is OPTIONAL (None or "" means the default selection namespace).

`PositionalFakerConfig` carries the configured namespace (for pool identity) and nothing derived from the table. A REUSE non-det Faker that fails stage A keeps a `chunked_strategy_conditions_unmet` failure whose text names the missing input (pool_size), with the "deferred to C5b-ii" wording removed. Unlike deterministic Faker, the provider allowlist IS a stage-A condition here: before this slice non-det Faker always ran whole-frame, so admitting a provider whose output types are not chunk-stable would turn working jobs into assembly failures.

**3b. The four consumers read stage A.**
- (1) Compat veto: `_conditional_admission_failures` skips the determinism and namespace failures for a stage-A-admissible entry.
- (2) `_static_route_decision`: the positional exception extends to Faker (`node.strategy == "faker"` and stage A holds), so its non-native `fallback_policy` does not block the chunked native route.
- (3) `plan_column_backends`: the planned backend is `RUST_COMPANION` for a stage-A Faker, as for positional categorical.
- (4) Preparation: pools are resolved by the existing `_resolve_faker_pools` (job_seed, configured namespace).

`_requirements.py`, `prepare_categorical`, `_shadow_bindings` and `_shadow_operators` are NOT touched: the full-frame route stays closed.

**3c. Stage B, leg selection at the first chunk.** A string source runs native. A non-string source takes the existing real-type downgrade to the chunked-oracle leg (`faker_source_type_not_string`), never a hard error; the output is the pinned `string` either way (§3f). One exotic case fails closed instead: an allowlisted provider NAME whose registered implementation returns non-string pool values (a custom provider registered under a built-in name). Every stage-A positional Faker pool is validated EAGERLY on BOTH dispatcher legs, before any masking, normalization or sink write, regardless of the table's native admission (Codex round 3 HIGH: the existing check at `_chunked_entry.py:363-371` runs only while `decision.native_admitted` holds, and the oracle leg only warms pools, `_chunked_entry.py:112`, `_chunked.py:485`). The pool is resolved once with the configured namespace and `job_seed` and cached for the leg that runs. A non-string value raises a coded `chunked_faker_nondeterministic_pool_not_string` naming the column and provider. Deterministic Faker's existing downgrade behavior is unchanged. The error tells the user to disable auto-chunking or use a different provider name. Downgrading would have the string pin silently change the column's type relative to whole-frame. This case is a new failure for an auto-chunked job that ran whole-frame before. It is accepted pre-GA and recorded in the CHANGELOG. The positional draw ignores source values, so a native run on non-string sources is possible, but it is deferred: the oracle leg's output type for non-string sources has to be characterized first, and C1b-ii made the same call.

**3d. `when:` is rejected on the chunked route.** New `reject_nondeterministic_faker_when(table_cfg, table=)` raises `PlanCompileError(code="chunked_faker_nondeterministic_when_not_supported")` for a stage-A Faker with a non-empty `when:`. It is called next to `categorical_gate.reject_nondeterministic_when` (`_chunked.py:302`). Reason: `when:` hands the oracle only matching rows, so its ordinal is the match index, not the physical position. Columns that already fail stage A keep their existing code.

**3e. Parameters and the shared step (built on R1b).**
- `FakerParams` gains `positional: bool = False` and `selection_namespace: str | None = None`. For a positional column the resolver sets `positional=True` and `selection_namespace = faker_selection_namespace(table, column, configured_namespace)`, the SAME function the oracle calls (moved or re-exported so `native/` does not import `_strategies/`; the builder picks one and records it). `resolve_params_by_column` gains a `table` argument for this. The deterministic path is unchanged: `positional=False`, `namespace` as today.
- `run_kernel_step` gains `job_seed: bytes | None = None`. For positional Faker it calls a new `sample_faker_array_positional(source, pool=, row_offset=, job_seed=, namespace=selection_namespace, index_kernel=, native_threads=)`. That function builds the dense uint64 keys with `positional_key_array(row_offset, n, code="faker_position_out_of_domain")`, calls `derive_index_batch(keys, mask_key=job_seed, namespace=, pool_size=)`, gathers `pool.values`, and restores nulls from the source. It asserts `job_seed is not None` and never reads `mask_key`.
- A positional zero-row source returns a typed empty array without calling the kernel and `ran=False`, mirroring positional categorical. Any non-empty source (all-null included) runs the kernel and reports `ran=True`.
- The chunked adapter passes `job_seed` (already in scope at `_chunked_entry.py:363`) and `row_offset`. Evidence: a run sets `pool_select_executed` and `pool_select_calls += 1` as deterministic Faker does; an idle zero-row chunk adds the column to `kernel_idle` and is uncounted.
- The unified adapter never builds positional `FakerParams` (its binding requires the native fallback policy). The step asserts this: positional Faker with `job_seed is None` is an `AssertionError`.

**3f. Output type: a string pin for admitted positional Faker (Codex rounds 1 and 2).** Faker is not in `_STRING_OUTPUT_STRATEGIES` (`native/_chunked_schema_rule.py:49`). A new classifier `faker_positional_pinned_columns(configured)` returns the stage-A-admissible columns (no `when:`, which is rejected anyway). `build_schema_rule` adds it to `string_columns` next to `date_shift_pinned_columns` and `group_key_pinned_columns`. Both schema-rule construction sites (the dispatcher entry and the streamed sink, `_pipeline_auto_chunk.py:314`) build through `build_schema_rule`, so both legs and the sink pin identically, independent of companion availability. Because stage A admits only string-output providers and the exotic override fails closed (§3c), the pin never casts a non-string value.

The resulting type contract, fixed here rather than discovered at build:

| Shape | Per-chunk type, native leg | Per-chunk type, oracle leg | Assembled chunked | Whole-frame |
|---|---|---|---|---|
| Any chunk with at least one non-null value | `string` | `string` (pinned) | `string` | `string` |
| Zero-row chunk | `string` | `string` (pinned) | (contributes nothing) | n/a |
| All-null non-empty chunk | `string` | `string` (pinned) | `string` if any other chunk has values | `string` |
| Whole column empty or entirely null | `string` | `string` (pinned) | `string` | pandas inference (not `string`) |

The last row is the one exception. It is the same documented route-dependent type C1 and C1b-ii accepted for pinned columns, and the CHANGELOG and compatibility contract say so.

**3g. Docs.**
- CHANGELOG under [Unreleased].
- `docs/determinism.md` and `docs/strategies.md`: the chunked route now runs non-det REUSE Faker natively.
- Compatibility contract: the chunked route admits it, with the `when:` rejection and the pool_size requirement.
- `_chunked.py` module docstring: the faker conditions at `:40-51`.
- Out-of-core and split prose unchanged.
- Roadmap (platform repo): C5b-ii shipped; C5b-iii (unified positional route for categorical and Faker) added.
- Determinism (Codex round 1, LOW): `docs/native/draw-site-inventory.md:197-198` names the native mirror. The existing `mask.faker_nondeterministic` site (`_draw_sites_gen_pool.py:90-114`) registers the native implementation. The strategy-to-site mapping stays `mask.faker`.
- R1b step: the namespace assertion in `_operator_step.py:211` applies to deterministic Faker only.

## 4. Design notes

- The selection namespace is resolved once per table into `FakerParams`, from the same function the oracle uses, so the two routes cannot spell the default differently. Pool identity keeps using the configured namespace, which is the property C5b-i guaranteed.
- `job_seed` is a separate step input rather than passed through the `mask_key` parameter. A step that silently swapped keys would be hard to audit, and the C5b-i mutation list includes "mask_key used instead of job_seed".
- The positional sampler is a sibling of `sample_faker_array`, not a flag inside it, because the null-mask invariant differs (dense keys versus source-keyed).
- Open-closed: after R1b this slice adds one params field pair, one step branch, one sampler, one stage-A module and one `when:` gate. Neither adapter's dispatch gains an operator branch.

## 5. Acceptance tests (written first; red-before recorded)

Parity means native chunked == oracle chunked == whole-frame on values, column order, warnings and errors, with Arrow field types per the §3f contract. Route evidence is asserted per route (native: rust_companion and pool_select; oracle: the downgrade code). Schema-level metadata is excluded, as in C1b-ii.

1. **Parity matrix.**
   - Namespace: configured, and None (default).
   - Chunk shapes: zero-row, all-null non-empty, single-row, ragged, null block then values.
   - Sizes {1, 7, 50_000}; threads {1, 4}.
   - Namespace None AND `""` are both parametrized (both mean unset).
   - Fixtures are frozen and discriminating: a pool large enough, and seeds chosen and recorded, so that the asserted differences actually occur. REUSE permits collisions, so no assertion says "always differs" in general.
   - With the DEFAULT namespace, two columns with the same provider and pool differ row-wise on the frozen fixture (the bug C5b fixed). With the SAME explicit namespace and pool, two differently named columns are EQUAL row-wise, by design (`_faker_positional.py:38-40`).
2. **Global offset and uint64.** A nonzero `base_row_offset` matches whole-frame at those positions. Frozen KATs at g in {0, 2**63-1, 2**63, 2**64-1}. A chunk past 2**64-1 raises the EXISTING public `chunked_row_offset_out_of_domain` on both chunked legs (the generic guard runs first, `_chunked_entry.py:265`, `_chunked_oracle.py:338`, `_chunked_dgrn.py:118-123`). `faker_position_out_of_domain` is tested at the sampler and handler boundary directly. No other strategy's error contract changes.
3. **Key and namespace KATs (frozen literals).**
   - The native output equals scalar `pool.values[derive_index(job_seed, sel_ns, encode_int(g), size)]`.
   - Replacing `job_seed` with `mask_key` changes the output (shown with a mask_key that differs from job_seed).
   - The default namespace equals `faker_selection_namespace(table, column, None)` and differs between two tables with the same column name.
   - The pool built natively equals the oracle's pool (identity and values) for a configured and a None namespace.
4. **Stage-A agreement.** The four consumers return the same verdict for: admissible with namespace; admissible without; missing pool_size; UNIQUE; deterministic; nested; composite.
5. **Fails closed at the veto.** Missing pool_size gives `chunked_strategy_conditions_unmet` with no "deferred to C5b-ii" text, and the oracle route is not taken. UNIQUE, MATCH and SCALE stay rejected as before.
6. **Leg selection does not crash (C1b-ii lesson).** Each of these takes the chunked-oracle leg reproducibly and equals whole-frame on values, with field types per the §3f contract and the existing downgrade code in evidence:
   - an int64 source;
   - a float64 source;
   - a dictionary source;
   - an entirely null-typed source;
   - (off-allowlist providers are covered by 6c, and the overridden allowlisted provider by 6d.)
6b. **Type contract.** The §3f table, asserted literally:
   - per-chunk types on both legs: native and oracle, the latter forced by a non-string source and by companion absence;
   - assembled chunked output against whole-frame for every row, including the documented exception row;
   - the streamed-sink path (`_pipeline_auto_chunk`) produces the same assembled types.
6c. **Non-allowlisted providers stay whole-frame (Codex round 2 HIGH).** Each of these is vetoed for the chunked route, the auto-router keeps it whole-frame, the run succeeds, and the output equals today's:
   - a non-det REUSE Faker with a numeric-output provider over a string source with mixed null and non-null rows across what would be chunk boundaries (the reported int-then-float case);
   - the same with a date-output provider.
6d. **Override fails closed on every path.** A custom provider registered under an allowlisted name that returns integers (the registry-override mechanism of `tests/native/test_chunked_entry_gate_findings.py:61`) gives `chunked_faker_nondeterministic_pool_not_string` with zero output writes in each of these cases:
   - native admitted;
   - downgraded by a non-string source;
   - downgraded by companion absence;
   - downgraded by ANOTHER column's rejection;
   - through the streamed sink.
   Required mutant: restoring the `native_admitted` guard around the validation must be killed.
7. **`when:` rejected.** Gives the new code. Deterministic Faker with `when:` behaves as today.
8. **FK (Codex round 2 MEDIUM), both orientations tested separately.**
   - Child side: a positional Faker column as a CHILD FK key is rejected by the existing child-edge gate (`_chunked_fk.py:253-275`, existing codes).
   - Parent side, accepted: the gate checks only the executed table's child edges (`_chunked_fk.py:398`), so a parent-only chunked run may mask its parent key with positional Faker. Native preflight downgrades the table to the chunked-oracle leg (`_dispatch.py:246`). This is accepted because it is what whole-frame does for the same single-table job: whole-frame with no child table in the job also masks the parent key without remapping anything. The test asserts the parent-only chunked run equals the whole-frame run of the same config, and that the route is the chunked-oracle leg.
   - Any table touched by a declared relationship stays off native.
9. **Evidence.**
   - Admitted with at least one non-empty chunk: `pool_select_executed`, `pool_select_calls` per non-empty chunk, backend `rust_companion`.
   - Zero-row chunk: idle, uncounted.
   - All-null non-empty chunk: ran.
   - Companion absent: oracle downgrade with equal output.
10b. **Multi-table split end to end (Codex round 1, HIGH 2).** A two-table job with low `auto_chunk_threshold_rows`: table A with a non-det Faker column (namespace None) above threshold, table B an eligible sibling.
   - Assert each table's actual route: A chunked, B per its existing treatment.
   - Assert each table's output equals the forced whole-frame run.
   - Assert A's default namespace uses table A.
   - Assert the row offset restarts at 0 per table.
   - Repeat with A below threshold (whole-frame) to show the split's full-frame group is unchanged.
10. **Auto-router end to end (C1b-ii BLOCKER lesson).** `run_pipeline` on real sources with a low `auto_chunk_threshold_rows` for string, int64 and float64 sources, each with namespace configured and None. The job auto-routes chunked and equals the forced whole-frame run.
11. **Closed routes stay closed.**
    - Unified: the binding returns None for non-det Faker, and the `_shadow_operators` determinism path is untouched.
    - Out-of-core: `out_of_core_faker_pool_unsupported`.
    - Multi-table split: job-level eligibility unchanged (per-table routing is covered by 10b).
12. **Step contract.**
    - `ran` is False for positional zero-row and True otherwise.
    - The step asserts on positional with `job_seed=None`.
    - The deterministic Faker step path is byte-unchanged; the R1b cross-route baseline stays green.
13. **Changed-unit coverage and mutation.**
    - Units: the stage-A predicate, the veto changes, the `when:` gate, `sample_faker_array_positional`, the step branch, the resolver fields.
    - Required mutants: mask_key for job_seed; `row_offset` dropped; configured namespace used for selection when None; pool built on the selection namespace (the fixture gives the two namespaces different pool contents, so this mutant is killable); nulls not restored; `ran` on zero rows.
    - Every mutant must be killed. Record the results.

Red-before: tests 1, 2, 4-7, 9, 10, 10b and 12 fail on the base (the chunked route vetoes the column or the code does not exist). Test 3's pool-identity case, test 6c and test 11 are green-before by design.

Every new test also runs under the Python 3.10 mirror.

## 6. Risk, rollback, gates

| Risk | Mitigation |
|---|---|
| Default namespace spelled differently from the oracle | One function, tests 3 and 10 with namespace None |
| Pool identity changes when namespace is None | Test 3 pool equality; pools keep the configured namespace |
| Wrong key (mask_key) | Separate `job_seed` input, test 3 KAT, required mutant |
| Non-string or off-allowlist source crashes the auto-router | Leg selection reuses the existing downgrade; tests 6 and 10 |
| Evidence overclaims on empty chunks | Idle path, test 9 |
| Split per-table routing changes for above-threshold Faker tables | Accepted in scope (§1); test 10b pins route and output per table |
| Output types drift between legs or chunks | Stage A admits only string-output providers; string pin on both legs and the sink; override fails closed; tests 6b-6d |
| `_chunked.py` size (619 of max 700) | The new stage-A module and `when:` gate live outside it; census exact |
| R1b changes at its gate | Rebase onto merged R1b before the final gate; rerun the full targeted set |

Rollback: revert the merge commit. The veto returns, and non-det Faker runs whole-frame as after C5b-i.

Gates: Codex plan gate, Sonnet tests-first build, dennis, Codex final gate, ci-mirror, merge under the standing authority after R1b, the post-merge suite including `tests/perf`, and a main CI check.

## 7. Plan-gate history

- Rev 1: initial.
- Codex round 1, REVISE (2 HIGH, 2 MEDIUM, 1 LOW). Folded in rev 2:
  - H1: no output-type pin; positional Faker uses deterministic Faker's existing policy; degenerate-type baseline 6b; the overridden allowlisted provider case added to test 6.
  - H2: split per-table chunking is accepted and validated end to end (test 10b) instead of being claimed unchanged.
  - M3: the overflow test uses the existing generic chunked code; the Faker code is tested at the sampler.
  - M4: REUSE-aware assertions with frozen discriminating fixtures; `""` parametrized; explicit-namespace equality pinned.
  - L5: draw-site inventory and native mirror registration added to DOCUMENT.
- Codex round 2, REVISE (1 HIGH, 2 MEDIUM). Folded in rev 3:
  - H: stage A admits only C1-allowlisted (string-output) providers, so other providers stay whole-frame as before (test 6c); an overridden allowlisted provider fails closed (test 6d).
  - M: the type contract is fixed in §3f as a table, enforced by a string pin through `build_schema_rule` on both legs and the sink; one documented degenerate exception; route evidence asserted per route.
  - M: FK both orientations are tested separately; parent-only positional masking is accepted as equal to whole-frame.
- Codex round 3, REVISE (1 HIGH). Folded in rev 4: the non-string-pool check is eager on both dispatcher legs, before any write, with test 6d covering every downgrade path and a guard-restoring mutant. Round 4 needs the owner's go-ahead (three-round cap).
