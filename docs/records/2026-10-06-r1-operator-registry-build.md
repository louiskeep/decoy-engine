Status: record (R1 build, behavior-preserving refactor; branch `feat/r1-operator-registry`, off engine main `01c560da`)

# R1 build record: one descriptor per masking operator

Plan: `docs/plans/2026-10-06-r1-operator-registry.md` rev 2 (Codex plan gate GO). Risk R2. Not merged; awaiting dennis and the Codex final gate.

## What changed

- New leaf module `src/decoy_engine/execution/_operator_registry.py`: a frozen `OperatorSpec` per slice operator, `OPERATORS` (read-only, keyed by strategy) and `operator_spec` (raises `KeyError`). It imports only the stdlib and pyarrow and owns the backend vocabulary.
- Seventeen per-operator tables became one-line derivations that keep their names, types and values: `ALLOWED_OPERATOR_IDS`, `BACKEND_BY_OPERATOR_ID` (still a `MappingProxyType`), the six `*_OPERATOR_ID` constants, `_COMPANION_DEPENDENT_OPERATOR_IDS`, `_OPERATOR_REQUIRED_KERNEL`, `_ROUTED_DIAGNOSTIC_OBLIGATIONS` (from the capability reducers, sparse), `_POSITIVE_KERNEL_EVIDENCE_OPERATOR_IDS`, `SLICE_STRATEGIES`, `OPERATOR_ID_BY_STRATEGY`, `_ADMITTED_RESIDENT_TYPES` (8 keys, no group_key), `NATIVE_KERNEL_STRATEGIES`, `NATIVE_POOL_STRATEGIES`, `_INDEX_KERNEL_STRATEGIES`, `_COMPANION_STRATEGIES`, `_TOKENIZING_STRATEGIES`, `_NULL_ON_EMPTY_STRATEGIES`, `_NATIVE_GROUP_KEY_SIBLING_TYPES`, `C1_PROVIDER_ALLOWLIST`. The nine op-id literals in `physical/_shadow_operators.py` read the registry.
- `_planned_backend` keeps its node-kind, fallback-policy, veto and unknown-strategy guards; only the strategy-to-backend step reads the registry.
- `native/_phase3_eligibility.py` and `tests/native/test_phase3_eligibility.py` deleted; the standalone phase3 tests in the four `test_chunked_*_admission.py` files deleted (7 functions: 2 categorical, 2 date_shift, 2 bucket_perturb, 1 group_key). Comment-only references cleaned.
- `string_source_type_rejection(strategy, column, schema)` replaces the three string-only gates.
- New guard test for the forced-oracle stand-in; three stale support-module docstrings fixed.

## Fowler steps (one commit each, see `git log`)

1. Introduce Parameter Object (`OperatorSpec`) and Move Function (backend vocabulary into the registry).
2. Replace Magic Literal in the unified-slice tables.
3. Replace Magic Literal in the route tables and `_shadow_operators`.
4. Remove Dead Code (phase3 predicate), Move Function (`C1_PROVIDER_ALLOWLIST`), Consolidate Duplicate Conditional Fragments (string gate), plus the remaining derivations that depended on them.

## Behavior-preservation evidence

- Baseline: `tests/native/test_operator_tables_snapshot.py` (literal values copied from main `01c560da`, plus container-type assertions) was written first and run green on the unmodified code (10 passed), committed alone as `e10df40a`.
- Red-before (commit `1f048f75`, run against the unmodified source): `test_operator_registry.py` and `test_string_source_type_gate.py` failed at collection (module and function missing); in `test_operator_registry_sentry.py` the single-source sentry, the leaf-module sentry and the phase3 grep gate failed; the admission import probe and the `native_route_eligibility` import check were green. The stand-in guard pins current behavior and is green by design (it is a tripwire, shown killable by mutant M8).
- Module-load comparison: the set of `decoy_engine.*` modules loaded by `import decoy_engine` and by `import decoy_engine.execution._unified_slice_admission` is identical to main except `+_operator_registry`, `-native._phase3_eligibility` and `-native._provider_class`. The last follows from deleting the phase3 module (it was the only import-time importer; every other user imports it lazily). No `execution.physical` module loads (the fresh-import probe and the admission probe pass).
- Suites after the refactor: `tests/sentry tests/native tests/physical tests/unit/execution tests/parity/native`: 15159 passed, 7 skipped, 59 xfailed, 2 failed. The 2 failures were `test_real_type_gate_accepts_string_and_names_every_other_type` in the bucket_perturb and date_shift admission files, which import the old per-strategy gate names. Plan 3f allows thin aliases when a test imports them, so `bucket_perturb_source_type_rejection` and `date_shift_source_type_rejection` stay as one-line wrappers (the categorical one was not imported by any test and is removed). After that, a targeted re-run of the four chunked admission files, the new tests and `tests/sentry` gave 2497 passed, 1 skipped. The full 20-minute run was not repeated after the alias change; the alias is the only source delta since.
- Edits to existing tests: phase3 removals (3e); `tests/sentry/test_module_size.py` census `_requirements.py` 646 to 641 (the sentry's own ratchet instruction); `tests/sentry/test_physical_seam_disconnection.py` permitted-diff list gains the three new or newly touched non-physical files and keeps the deleted file's path so the deletion stays permitted; two docstring/comment fixes in `test_requirements_jc5.py` and `tests/physical/test_shadow_bindings.py`; the snapshot test's `C1_PROVIDER_ALLOWLIST` import path moved with the constant (its literal is unchanged).

## Hand mutation (one at a time, each reverted)

| # | Mutant | Result |
|---|---|---|
| M1 | Faker `planned_backend` to `RUST_COMPANION` | killed (`test_backend_by_operator_id`) |
| M2 | categorical `positive_kernel_evidence=True` | killed (`test_positive_kernel_evidence_operator_ids`) |
| M3 | drop bucket_perturb from `OPERATORS` | killed (import-time `KeyError` in the admission module; every operator is referenced by name, so any drop fails loudly) |
| M4 | date_shift `required_kernel="crypto"` | killed (`test_companion_dependent_and_required_kernel`) |
| M5 | string gate prefix `_source_not_string` | killed (`test_string_gate_reason_is_byte_identical`) |
| M6 | operator-id literal back in `_shadow_operators.py` | killed (single-source sentry) |
| M8 | stand-in categories made all-string (native-admissible) | killed (stand-in guard) |

6 of 6 plan mutants killed, plus the stand-in tripwire. Measured coverage and a mutmut run on the registry were not run; the evidence here is literal snapshot equality plus these hand mutants.

## Judgment calls

- `C1_PROVIDER_ALLOWLIST` is defined once in `native/_real_type_admission.py` from the Faker descriptor and imported by `_shadow_bindings` (plan 3e allowed either importer choice). The registry field is optional, so the derivation uses `or frozenset()`: a missing allowlist admits no provider (fail closed). The same pattern is used for `_NATIVE_GROUP_KEY_SIBLING_TYPES`.
- `_STRING_SOURCE_STRATEGIES` (categorical, bucket_perturb, date_shift) lives next to the gate, not in the registry, because the unified route's domains differ and 3d keeps route-specific facts out.
- The hash-to-crypto and group_key-to-raw_hex kernel-loader branches in `native/_dispatch.py` are left as written: they select a loader function per strategy, which is dispatch (R1b), not a table.
- The phase3 grep gate checks identifiers and the module in an import. It cannot forbid the substring `_phase3_eligibility` repo-wide because the seam sentry's permitted-diff list must keep the deleted file's path.
- Intermediate commits are not each fully test-green (the sentries and tests change together with the final commit).

## Follow-ups (not done, out of scope)

- Whether categorical belongs in `_POSITIVE_KERNEL_EVIDENCE_OPERATOR_IDS` (plan section 1).
- R1b: merging the two per-operator dispatch chains.

## dennis gate (round 1): NO-GO, remediated by the plan author

- **HIGH-1:** the seam sentry was red because `native/_pool_quality.py` (comment-only edit) was not permitted, and the earlier "tests/sentry green" claim was wrong. Permitted with a reason.
- **HIGH-2 (a plan error):** plan 3b derived `_ROUTED_DIAGNOSTIC_OBLIGATIONS` from the capability reducers. That table is coordinator POLICY (what the unified coordinator actually routes), and deriving it from the same function that produces each binding's obligations made the `obligations <= routed` gate always pass. A future operator with declared diagnostics would have been auto-admitted, with its warnings dropped or row errors unquarantined.
  - Fix: a new descriptor field, `OperatorSpec.routed_diagnostics` (date_shift = `{"reduce_row_error:format_error"}`, all others empty). The table reads it, and admission no longer imports `_diagnostic_reducers`.
  - New tests:
    - every slice operator's declared capability reducers must be a subset of its `routed_diagnostics` (a future mismatch fails the suite and forces a decision);
    - the table equals the field derivation, and admission does not reference `_diagnostic_reducers`.
  - An earlier subprocess probe version of the second test was found vacuous (the admission module is imported before any patch can apply) and replaced.
- **MEDIUM-1:** `_NATIVE_GROUP_KEY_SIBLING_TYPES` lost the group_key stringify-safe intersection. It is now `group_key_sibling_types(passthrough set)`, which filters through `group_by_type_is_safe`, with a test that float64 and decimal stay excluded even if passthrough widens.
- **MEDIUM-2:** the single-source sentry derives the operator ids from `OPERATORS` (the snapshot test still pins the literals).
- **LOW-1:** the `_STRING_SOURCE_STRATEGIES` rationale was corrected.
- **LOW-2:** `_shadow_bindings` reads the Faker allowlist from the registry.
- **Red-before:** all three new tests fail on the pre-fix commit `bc13aeca`.
- **After the fix:** `tests/sentry tests/native tests/physical tests/unit/execution tests/parity/native`: 15165 passed, 7 skipped, 59 xfailed, 0 failed. `tests/sentry` on Python 3.10: 2286 passed, 1 skipped. ruff and mypy are clean.
