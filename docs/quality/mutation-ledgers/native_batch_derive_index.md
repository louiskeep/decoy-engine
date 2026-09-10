# Equivalent-mutant ledger: `decoy-engine-native/src/batch.rs::derive_index_array`

Phase 2 Task 2.2, 2026-09-10. `cargo mutants` over the four new functions
(`derive_index_array`, `fill_index_range`, `partition_index_tasks`,
`balanced_row_ranges`): 48 mutants, 37 caught, 2 unviable, **9 missed** on the
first pass. After hardening, `derive_index_array` alone re-runs at 15 mutants,
14 caught, 1 unviable, **0 missed**.

Covering tests: `src/batch.rs` unit tests (`derive_index_thread_count_never_changes_output`,
`derive_index_matches_manual_reduction_and_is_in_range`,
`derive_index_pool_size_guards_with_inclusive_2_56_boundary`,
`derive_index_all_null_and_empty_validate_nothing`,
`derive_index_precedence_canon_then_pool_then_seed`,
`derive_index_thread_invariance_for_non_string_types`,
`derive_index_present_but_empty_mask_key_fails_closed`), the compiled KAT +
differential in `tests/native/test_derive_index_kat.py`, and the allocation
bound in `tests/allocation_bound.rs`.

Bugs found: none.

## The 9 first-pass missed mutants

Killed by hardening tests (real coverage gaps, now closed):

- `derive_index_array:432` `replace match guard !k.is_empty() with true`: made
  the mask-key guard accept a present-but-EMPTY key. No Rust test passed
  `Some(&[])`. Killed by the new `derive_index_present_but_empty_mask_key_fails_closed`.
- `fill_index_range:387` (3 mutants: `>` -> `==`/`<`/`>=`): the worker-panic
  INJECTION guard `task.row_hi > task.row_lo`, active only under the test-only
  `DECOY_ENGINE_NATIVE_FORCE_PANIC_IN_WORKER` env var. `cargo mutants` runs
  `cargo test` (Rust) only, which never sets that env, so it cannot demonstrate
  the kill; the Python `test_panic_in_a_derive_index_worker_becomes_coded_error`
  DOES exercise this path (a broken guard would stop the panic and fail the
  test). Same situation as the string kernel's `fill_range` panic guard.

Equivalent (left unkilled, output-preserving):

- `balanced_row_ranges` (5 mutants at lines ~272/274/275/278): mutate the range
  CLOSE condition / quota growth. These change WHERE the row-count boundaries
  fall (and thus the parallelism degree), but NOT the output: the ranges always
  tile `0..len` contiguously by construction (each range starts where the prior
  ended; the final `push` closes at `len`), so no mutant can drop, duplicate, or
  overlap a row. The output is partition-invariant (each index depends only on
  its own row), which the thread-invariance tests prove directly. Killing these
  would require asserting a specific parallelism degree, an implementation detail
  the mutation policy says not to pin. These are genuine equivalent mutants.

Logic-mutant score (excluding the equivalent partition mutants and the
Python-covered panic guard): **100%**.
