Status: plan (revision 1, author = Opus). Codex plan gate: pending.
Rules consulted: 00-universal, development-loop, testing, architecture, code-review, scope-discipline, api-and-compatibility

# C5b-ii: non-deterministic REUSE Faker on the chunked native route

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C. Decision: Cam 2026-10-05 (position-keyed like C1b; job_seed key; namespace defaults from table and column; REUSE only; one pre-GA output break, already shipped in C5b-i, engine #205).

Branch: `feat/c5b-ii-chunked-nondet-faker`, stacked on `feat/r1b-merged-dispatch` (R1b: one kernel step per operator). It is rebased onto main after R1b merges and before this slice's final gate. Risk R2: one route opened, parity-gated, no new kernel.

Template: C1b-ii (`docs/plans/2026-10-04-c1b-ii-chunked-nondet-categorical.md`, rev 5), which did the same for the position-keyed categorical. Its two dennis lessons apply here directly: a non-string source must take the chunked-oracle leg rather than fail closed, and the auto-router needs an end-to-end test.

## 1. Goal and scope

C5b-i made non-deterministic REUSE Faker position-keyed on the oracle: row `g` takes `pool.values[derive_index(job_seed, sel_ns, encode_int(g), pool.size)]`, where `sel_ns` is the configured namespace or `faker-nd/{len(table)}:{table}/{len(column)}:{column}`. Every route except whole-frame still vetoes it (`_chunked.py:217-222` says "deferred to C5b-ii").

C5b-ii admits it to the chunked route. A string source runs natively, a non-string source runs on the chunked-oracle leg, and native chunked equals oracle chunked equals whole-frame on values, Arrow field types, column order, warnings, errors and route evidence.

Out of scope (vetoes stay, each with an honest reason):
- **Unified full-frame route.** No position-keyed operator runs there yet: the coordinator keeps a per-batch table offset (`physical/_shadow_coordinator.py:344,387`) but never passes it to operators, and C1b-ii kept categorical off the route too. Opening it for both positional categorical and positional Faker is a separate slice, **C5b-iii**, added to the roadmap by this slice's DOCUMENT step. The determinism assertion in `_shadow_operators` stays.
- **Multi-table split and out-of-core.** These are the C1b-iii and C1b-iv analogues. Non-det Faker is already split-eligible (whole-frame per table, `row_offset` 0) and stays so. Out-of-core keeps `out_of_core_faker_pool_unsupported`.
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
- the namespace is OPTIONAL (None or "" means the default selection namespace).

`PositionalFakerConfig` carries the configured namespace (for pool identity) and nothing derived from the table. A REUSE non-det Faker that fails stage A keeps a `chunked_strategy_conditions_unmet` failure whose text names the missing input (pool_size), with the "deferred to C5b-ii" wording removed. The provider allowlist is NOT a stage-A condition: a provider outside it takes the chunked-oracle leg at first-chunk admission, exactly as deterministic Faker does.

**3b. The four consumers read stage A.**
- (1) Compat veto: `_conditional_admission_failures` skips the determinism and namespace failures for a stage-A-admissible entry.
- (2) `_static_route_decision`: the positional exception extends to Faker (`node.strategy == "faker"` and stage A holds), so its non-native `fallback_policy` does not block the chunked native route.
- (3) `plan_column_backends`: the planned backend is `RUST_COMPANION` for a stage-A Faker, as for positional categorical.
- (4) Preparation: pools are resolved by the existing `_resolve_faker_pools` (job_seed, configured namespace).

`_requirements.py`, `prepare_categorical`, `_shadow_bindings` and `_shadow_operators` are NOT touched: the full-frame route stays closed.

**3c. Stage B, leg selection at the first chunk.** A string source runs native. A non-string source, a provider outside the C1 allowlist, or non-string pool output takes the existing real-type downgrade to the chunked-oracle leg with the existing codes (`faker_source_type_not_string`, `faker_provider_not_native`, `faker_provider_output_not_string`), never a hard error. The positional draw ignores source values, so a native run on non-string sources is possible, but it is deferred: the oracle leg's output type for non-string sources has to be characterized first, and C1b-ii made the same call.

**3d. `when:` is rejected on the chunked route.** New `reject_nondeterministic_faker_when(table_cfg, table=)` raises `PlanCompileError(code="chunked_faker_nondeterministic_when_not_supported")` for a stage-A Faker with a non-empty `when:`. It is called next to `categorical_gate.reject_nondeterministic_when` (`_chunked.py:302`). Reason: `when:` hands the oracle only matching rows, so its ordinal is the match index, not the physical position. Columns that already fail stage A keep their existing code.

**3e. Parameters and the shared step (built on R1b).**
- `FakerParams` gains `positional: bool = False` and `selection_namespace: str | None = None`. For a positional column the resolver sets `positional=True` and `selection_namespace = faker_selection_namespace(table, column, configured_namespace)`, the SAME function the oracle calls (moved or re-exported so `native/` does not import `_strategies/`; the builder picks one and records it). `resolve_params_by_column` gains a `table` argument for this. The deterministic path is unchanged: `positional=False`, `namespace` as today.
- `run_kernel_step` gains `job_seed: bytes | None = None`. For positional Faker it calls a new `sample_faker_array_positional(source, pool=, row_offset=, job_seed=, namespace=selection_namespace, index_kernel=, native_threads=)`. That function builds the dense uint64 keys with `positional_key_array(row_offset, n, code="faker_position_out_of_domain")`, calls `derive_index_batch(keys, mask_key=job_seed, namespace=, pool_size=)`, gathers `pool.values`, and restores nulls from the source. It asserts `job_seed is not None` and never reads `mask_key`.
- A positional zero-row source returns a typed empty array without calling the kernel and `ran=False`, mirroring positional categorical. Any non-empty source (all-null included) runs the kernel and reports `ran=True`.
- The chunked adapter passes `job_seed` (already in scope at `_chunked_entry.py:363`) and `row_offset`. Evidence: a run sets `pool_select_executed` and `pool_select_calls += 1` as deterministic Faker does; an idle zero-row chunk adds the column to `kernel_idle` and is uncounted.
- The unified adapter never builds positional `FakerParams` (its binding requires the native fallback policy). The step asserts this: positional Faker with `job_seed is None` is an `AssertionError`.

**3f. Output type.** The chunked schema rule pins positional Faker to the same output type as deterministic Faker on both legs. The builder confirms the existing pin covers it (Faker is in the string-output set) and adds it only if it does not.

**3g. Docs.**
- CHANGELOG under [Unreleased].
- `docs/determinism.md` and `docs/strategies.md`: the chunked route now runs non-det REUSE Faker natively.
- Compatibility contract: the chunked route admits it, with the `when:` rejection and the pool_size requirement.
- `_chunked.py` module docstring: the faker conditions at `:40-51`.
- Out-of-core and split prose unchanged.
- Roadmap (platform repo): C5b-ii shipped; C5b-iii (unified positional route for categorical and Faker) added.

## 4. Design notes

- The selection namespace is resolved once per table into `FakerParams`, from the same function the oracle uses, so the two routes cannot spell the default differently. Pool identity keeps using the configured namespace, which is the property C5b-i guaranteed.
- `job_seed` is a separate step input rather than passed through the `mask_key` parameter. A step that silently swapped keys would be hard to audit, and the C5b-i mutation list includes "mask_key used instead of job_seed".
- The positional sampler is a sibling of `sample_faker_array`, not a flag inside it, because the null-mask invariant differs (dense keys versus source-keyed).
- Open-closed: after R1b this slice adds one params field pair, one step branch, one sampler, one stage-A module and one `when:` gate. Neither adapter's dispatch gains an operator branch.

## 5. Acceptance tests (written first; red-before recorded)

Parity means native chunked == oracle chunked == whole-frame on values, column order, Arrow field types, warnings, errors and route evidence. Schema-level metadata is excluded, as in C1b-ii.

1. **Parity matrix.**
   - Namespace: configured, and None (default).
   - Chunk shapes: zero-row, all-null non-empty, single-row, ragged, null block then values.
   - Sizes {1, 7, 50_000}; threads {1, 4}.
   - The same source value in two chunks gets different values by global position.
   - Two non-det Faker columns with the same provider and pool identity but different column names differ row-wise (the bug C5b fixed).
2. **Global offset and uint64.** A nonzero `base_row_offset` matches whole-frame at those positions. Frozen KATs at g in {0, 2**63-1, 2**63, 2**64-1}. A chunk past 2**64-1 raises `faker_position_out_of_domain` on both legs.
3. **Key and namespace KATs (frozen literals).**
   - The native output equals scalar `pool.values[derive_index(job_seed, sel_ns, encode_int(g), size)]`.
   - Replacing `job_seed` with `mask_key` changes the output (shown with a mask_key that differs from job_seed).
   - The default namespace equals `faker_selection_namespace(table, column, None)` and differs between two tables with the same column name.
   - The pool built natively equals the oracle's pool (identity and values) for a configured and a None namespace.
4. **Stage-A agreement.** The four consumers return the same verdict for: admissible with namespace; admissible without; missing pool_size; UNIQUE; deterministic; nested; composite.
5. **Fails closed at the veto.** Missing pool_size gives `chunked_strategy_conditions_unmet` with no "deferred to C5b-ii" text, and the oracle route is not taken. UNIQUE, MATCH and SCALE stay rejected as before.
6. **Leg selection does not crash (C1b-ii lesson).** Each of these takes the chunked-oracle leg reproducibly and equals whole-frame, with the existing downgrade code in evidence:
   - an int64 source;
   - a float64 source;
   - a dictionary source;
   - an entirely null-typed source;
   - a provider outside the C1 allowlist.
7. **`when:` rejected.** Gives the new code. Deterministic Faker with `when:` behaves as today.
8. **FK.** A table touched by a declared relationship stays off native and runs chunked-oracle reproducibly. A Faker FK key is rejected by the existing FK gate (existing codes).
9. **Evidence.**
   - Admitted with at least one non-empty chunk: `pool_select_executed`, `pool_select_calls` per non-empty chunk, backend `rust_companion`.
   - Zero-row chunk: idle, uncounted.
   - All-null non-empty chunk: ran.
   - Companion absent: oracle downgrade with equal output.
10. **Auto-router end to end (C1b-ii BLOCKER lesson).** `run_pipeline` on real sources with a low `auto_chunk_threshold_rows` for string, int64 and float64 sources, each with namespace configured and None. The job auto-routes chunked and equals the forced whole-frame run.
11. **Closed routes stay closed.**
    - Unified: the binding returns None for non-det Faker, and the `_shadow_operators` determinism path is untouched.
    - Out-of-core: `out_of_core_faker_pool_unsupported`.
    - Multi-table split: unchanged eligibility and output.
12. **Step contract.**
    - `ran` is False for positional zero-row and True otherwise.
    - The step asserts on positional with `job_seed=None`.
    - The deterministic Faker step path is byte-unchanged; the R1b cross-route baseline stays green.
13. **Changed-unit coverage and mutation.**
    - Units: the stage-A predicate, the veto changes, the `when:` gate, `sample_faker_array_positional`, the step branch, the resolver fields.
    - Required mutants: mask_key for job_seed; `row_offset` dropped; configured namespace used for selection when None; pool built on the selection namespace; nulls not restored; `ran` on zero rows.
    - Every mutant must be killed. Record the results.

Red-before: tests 1, 2, 4-7, 9, 10 and 12 fail on the base (the chunked route vetoes the column or the code does not exist). Test 3's pool-identity case and test 11 are green-before by design.

Every new test also runs under the Python 3.10 mirror.

## 6. Risk, rollback, gates

| Risk | Mitigation |
|---|---|
| Default namespace spelled differently from the oracle | One function, tests 3 and 10 with namespace None |
| Pool identity changes when namespace is None | Test 3 pool equality; pools keep the configured namespace |
| Wrong key (mask_key) | Separate `job_seed` input, test 3 KAT, required mutant |
| Non-string or off-allowlist source crashes the auto-router | Leg selection reuses the existing downgrade; tests 6 and 10 |
| Evidence overclaims on empty chunks | Idle path, test 9 |
| `_chunked.py` size (619 of max 700) | The new stage-A module and `when:` gate live outside it; census exact |
| R1b changes at its gate | Rebase onto merged R1b before the final gate; rerun the full targeted set |

Rollback: revert the merge commit. The veto returns, and non-det Faker runs whole-frame as after C5b-i.

Gates: Codex plan gate, Sonnet tests-first build, dennis, Codex final gate, ci-mirror, merge under the standing authority after R1b, the post-merge suite including `tests/perf`, and a main CI check.

## 7. Plan-gate history

- Rev 1: initial.
