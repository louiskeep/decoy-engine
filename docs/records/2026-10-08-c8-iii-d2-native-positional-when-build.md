Status: record

# C8-iii-d-2 build record: native positional under `when:`, byte-identical to the d-1 oracle

Plan: `docs/plans/2026-10-08-c8-iii-d2-native-positional-when.md` (rev 3, Codex plan-gate GO).
Branch `feat/c8-iii-d2-native-positional-when` off the merge-ready d-1 (`3f562de8`).

## What shipped

A non-deterministic (seeded) `categorical` and a non-deterministic REUSE `faker` under `when:`,
over a string source with string predicate references, now run on the native routes (unified
full-frame and native chunked) with output byte-identical to the d-1 oracle, instead of the
pandas oracle.

Mechanism: the full-table gate positions already computed by the masked step (`np.flatnonzero(
mask)`) are threaded through `run_kernel_step_masked` -> `run_kernel_step` -> the two native
positional kernels (`native_categorical_positional`, `sample_faker_array_positional`), each
passing them to the already-shipped `positional_key_array(..., gate_positions=...)`. No new key
math; `_positional_keys.row_positions` stays the single owner. With `gate_positions is None` (no
`when:`) the key is the contiguous `row_offset + arange(n)`, byte-identical to before.

Admission: `when_native_rejection` (the one verdict both native routes read) gains a positional
branch classified by `positional_config_of_entry` / `positional_faker_config_of_entry`, requiring
a `pa.string()` target AND every predicate reference `pa.string()` AND a closed-grammar predicate
with no earlier writer. The unified bindings dropped their `when:` refusal; `when_columns_admitted`
remains the controlling gate. The chunked config veto no longer rejects a config-complete
positional+`when:` with a closed-grammar predicate; a shared per-chunk string-type guard
(`_chunked_when_guard`, installed in `_oracle_preflight`) enforces string target/references on the
first and every later raw chunk, rejecting drift with the existing codes before that chunk masks.

Deferred (unchanged, still on the oracle): `windowed_date` under `when:` (no native kernel), and a
numeric-source faker under `when:`. On the unified route a numeric-source faker or a non-string
reference declines to the full-frame oracle (same bytes); on the chunked route a non-string target
or reference is rejected with the existing codes.

## Tests (written first)

New acceptance suites, 68 tests, all green on the companion venv (3.11, compiled companion):
- `tests/native/test_c8_iii_d2_native_positional_when.py` (chunked route): test 1 (byte-identity to
  the forced-oracle leg AND the whole-frame oracle, plus native-execution evidence), 1a (numeric
  reference rejected on both entries; string reference admitted natively), 1b (per-chunk type drift
  rejected before the offending chunk; later all-null chunk accepted), 2 (chunk boundaries, nonzero
  base offset, Hypothesis property native-chunked == oracle), 3 (nulls in the subset), 4 (deferred
  declines keep their codes), 6 (no-`when:` invariance).
- `tests/physical/test_c8_iii_d2_unified_positional_when.py` (unified route): test 1 (lane-off
  oracle parity + native evidence), 1a (numeric reference falls to the full-frame oracle; string
  reference runs natively).

Byte-identity is asserted two ways: metadata-inclusive IPC equality against the forced-oracle
chunked leg (`assert_native_matches_oracle`), and value+Arrow-type equality against the whole-frame
pandas oracle (`assert_values_match_full_frame`; the chunked route strips the `b"pandas"` schema
metadata the full-frame route attaches, a documented route difference). Native execution is proven
per nonempty selection (`native_admitted` plus the positional kernel's own counter), never
oracle-equality alone.

### Red-before (d-1 base `3f562de8`, companion venv)

`git archive 3f562de8 src` into scratch, `PYTHONPATH` that tree:
- chunked file: 31 failed, 17 passed. The failures are the native-admission cases (test 1, 1a
  string-ref, 1b null-accepted, 2, 3): the base declines positional+`when:` to the oracle, so
  `native_admitted` is False. The 17 passes are the config-time rejection cases (1a numeric, 1b
  drift, 4), which the base already enforces at config time with the same codes.
- unified file: 15 failed, 2 passed. The failures are the native-admission cases; the 2 passes are
  the numeric-reference-falls-to-oracle case the base already declines.

## Old pins flipped (test 7)

Before: each asserted a positional `categorical`/`faker` under `when:` DECLINED. New expected
values follow from the admission narrowing and the d-1 oracle, never hand-edited.

Chunked (`test_chunked_nondet_categorical_admission.py`, `test_chunked_nondet_faker_admission.py`):
- `test_a_seeded_categorical_with_when_gets_the_new_exact_code` /
  `test_a_positional_faker_with_when_gets_the_new_exact_code`: split into an OUTSIDE-grammar case
  (`p + 1 > 2`, still rejected at config with the code) and a closed-grammar case (`p > 1`, no
  longer vetoed at config; `_code(...) is None`). The numeric-reference runtime rejection is
  covered by the d-2 suite.
- `test_the_when_rejection_names_the_column_and_path`,
  `test_the_when_rejection_fires_before_any_chunk_on_both_entries`: point at the outside-grammar
  predicate so the config veto still fires before any chunk.
- `test_the_when_gate_ignores_a_non_categorical_column_with_categorical_looking_config`: outside-
  grammar predicate for the positional raise case.

Unified (`test_unified_slice_positional.py`):
- `_DECLINES["cat_when"]`, `_DECLINES["faker_when"]` (string self-reference `c == 'src_1'`): removed
  from the decline set; they now run natively (covered by the d-2 unified suite).
- `_CAT_CLAUSES["when"]`, `_FAKER_CLAUSES["when"]`: removed (the binding predicate no longer refuses
  `when:`); replaced by `test_8_a_when_predicate_no_longer_blocks_the_binding_predicate`.
- `test_8_the_when_exclusion_holds_on_the_binding_path` -> `test_8_a_string_reference_when_binds_on
  _the_binding_path`: asserts the node now binds and carries the predicate.

The mutation-kill pin (`test_when_gate_mutation_kills.py`) asserts no native decline for these
strategies, so it was left unchanged.

## Sentries

- Physical-seam disconnection: added the new parent-level `_chunked_when_guard.py` to the permit
  list (imports only the two config-classification helpers and the when parser, nothing from
  `execution.physical`). Every other changed `execution/` file was already permitted. No
  `execution/physical/` module outside the package is newly connected.
- Module-size census: no changed file crossed 600 LOC; census unchanged. All sentries green.

## Mutation (hand harness)

Targeted mutants on the new position-threading, the admission branch, and the per-chunk guard,
each applied as a unique source substitution, run against a fast kill-test, then restored from the
committed version. Kill rate 10/10 (100% killed, 0 survived):

| mutant | target | result |
|---|---|---|
| masked step `gate_positions=selected` -> `None` | position composition | KILLED |
| masked step `missing_mask=pc.filter(...)` -> unfiltered | subset missingness | KILLED |
| `run_kernel_step` categorical forward -> `None` | kernel forward | KILLED |
| `run_kernel_step` faker forward -> `None` | kernel forward | KILLED |
| `native_categorical_positional` pass-through -> `None` | kernel pass-through | KILLED |
| `sample_faker_array_positional` pass-through -> `None` | kernel pass-through | KILLED |
| admission reference type check inverted (`!=` -> `==`) | admission branch | KILLED |
| admission `positional` forced False | admission branch | KILLED |
| per-chunk guard drops the `is_null` accept | guard | KILLED |
| per-chunk guard never raises (`or True`) | guard | KILLED |

## Testflight

`scripts/test_flight.py`: FINGERPRINTS 5/5 match golden; 53/53 invariant checks pass, 0 skipped, 0
failed. No fingerprint moved (no golden uses `when:` with these strategies), as the plan requires.

## Full suite

`pytest tests/` on Python 3.11 with the compiled companion, `-rfE` (2 runs: the first was killed
at a 30-min background limit at 90%, the re-run finished): 25442 passed, 6 failed, 172 skipped, 21
deselected, 59 xfailed, in 32m39s. Five of the failures were further old when:-decline pins
(C8-iii-a `declines()` and the shadow-binding pin), flipped and re-run green; see "Old pins". The
sixth is the pre-existing, unrelated GCS failure
(`test_v2_cloud_sources.py::...test_profile_gcs_source_via_mocked_client`: `google` cloud module not
installed in this env), which fails identically on the d-1 base.

ruff check + ruff format: clean on all changed source and test files. mypy (`mypy src/decoy_engine
testflight`, the CI invocation) in the local venv reports only pre-existing `pyarrow.compute`
attr-defined noise (`pc.sum`/`pc.filter`/`pc.replace_with_mask`/`pc.is_null`) on pre-existing lines;
the d-1 base shows the same on the same lines. The repo's mypy config lists `pyarrow`/`pyarrow.*`
under `ignore_missing_imports`, which the CI stub-less env honors (pyarrow becomes `Any`), so these
do not appear in CI. My one added `pc.filter(missing_mask, mask)` is the identical construct to the
`pc.filter(plain, mask)` two lines above it. No new error kind.

## Deviations

- Test 1's whole-frame-oracle comparison asserts value + Arrow-type equality (not metadata-inclusive
  IPC bytes) because the chunked route intentionally strips the `b"pandas"` schema metadata the
  full-frame route attaches; metadata-inclusive byte-identity is still asserted against the
  forced-oracle chunked leg. Not a weakening: the draw-for-draw output is proven identical both
  ways.
- The red-before profile is partial by design: the config-time rejection cases (numeric reference,
  per-chunk drift) already held on the base at config time, so only the native-admission cases are
  red-before. Recorded above.
