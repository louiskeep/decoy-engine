Status: record

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, testing, observability-and-resilience, code-review.

# C5c-i build record: positional Faker over numeric and boolean sources

Plan: `docs/plans/2026-10-07-c5c-nonstring-faker.md` rev 3 (Codex plan gate GO). Branch `feat/c5c-nonstring-faker`.

## What changed

A non-deterministic `reuse` Faker column over int8-int64, uint8-uint64, bool, float32 or float64 now runs natively on both routes. Deterministic Faker stays string-only, the oracle is untouched, and the unified round-trip gate is unchanged.

- `native/_faker_null_mask.py` (new): `faker_missing_mask` converts one source column with the oracle's `to_pandas_fk_safe` and takes `isna()`. `FakerNullMasks` runs it per chunk inside the oracle leg's carry diagnosis, on the RAW chunk before `cast_null_columns` (the `_when_mask` pattern).
- `native/_operator_step.py`: the positional step takes `missing_mask`; `None` keeps Arrow validity.
- `native/_chunked_entry.py`, `_chunk_masking.py`: compute the masks and pass them to the step.
- `native/_dispatch.py`: the per-variant admitted-type check. The positional variant admits `POSITIONAL_FAKER_SOURCE_TYPES`; deterministic Faker keeps `string`/`large_string`.
- `_operator_registry.py`: `OperatorSpec.positional_resident_types` and the explicit-instance set `POSITIONAL_FAKER_SOURCE_TYPES`.
- `_unified_slice_resident_types.py`, `_unified_slice_admission.py`: the resident-type check uses the wider domain for a bound positional node only.
- `physical/_shadow_bindings.py`: `positional_faker_bindable` passes the numeric families to `_faker_pool_bindable`; the deterministic call does not.
- `physical/_shadow_coordinator.py`, `_shadow_operators.py`: each batch passes a single-column slice (schema metadata kept) so the same helper computes the mask.

## Judgment calls

- The mask conversion runs for every positional Faker chunk, strings included. A string-type shortcut would add a per-type rule the plan avoids, and the measured cost is small (below).
- On the unified route the mask is redundant for every admitted column: the unchanged round-trip gate already proves the pandas missingness equals Arrow validity there (a valid NaN fails the gate). The plan asked both routes to call the helper, so both do. It is the safeguard if that gate ever loosens, and mutation M15 below shows no admitted case can observe it today.
- A nested source (list, struct) fails in the profiler before any route decision on both legs, so the chunked decline test compares the error with the oracle's instead of asserting a reroute reason.
- Census: `_chunked_entry.py` 612 to 615 (dense entry). Physical-seam permitted list: one new entry, `native/_faker_null_mask.py`. The other `execution/` modules this build edits were already permitted.

## Test 6: changed decline tests

| File | Test | Change | Why |
|---|---|---|---|
| `tests/native/test_chunked_nondet_faker_auto_route.py` | `test_string_runs_the_native_leg_and_every_non_string_runs_the_oracle_leg` | Renamed `..._string_and_numeric_run_the_native_leg_and_every_other_source_runs_the_oracle_leg`. `int64` and `float64` now assert native admission; `null` still asserts the oracle leg and its reason. | int and float are admitted families under the positional variant. |
| `tests/native/test_chunked_nondet_faker_admission.py` | `test_a_non_string_source_runs_the_chunked_oracle_leg_and_equals_whole_frame` | Parameters `int64`, `float64` removed; `dictionary`, `null` kept with the same assertions. | Admitted families. Native values equal to whole-frame are covered by the new chunked tests. |
| same | `test_override_downgraded_by_a_non_string_source_fails_closed` | Source `int64` replaced by `dictionary`. | `int64` no longer downgrades; the test needs a source that still does. Assertions unchanged. |
| `tests/physical/test_unified_slice_positional.py` | `_DECLINES["faker_int_source"]` | Replaced by `faker_timestamp_source` (a timestamp source). | A null-free int64 now reaches the binder. |
| same | `_FAKER_CLAUSES["int_source"]` | Replaced by `timestamp_source`. | Same. |

Unchanged: the deterministic-Faker decline tests in `tests/native/test_dispatch_faker.py` (they use `deterministic=True`), every registry snapshot (the deterministic `unified_resident_types` is the same), and the categorical decline tests.

New tests: `tests/native/test_c5c_i_chunked_positional.py`, `tests/physical/test_c5c_i_unified_positional.py`, one addition to `tests/native/test_operator_registry.py`. They failed for the right reason before the code (the poisoned oracle leg ran, so the table had been declined) and pass after.

## Mutation (hand, plan test 8)

Each mutant was applied to one file, the new tests plus the touched old ones were run, and the file was restored.

| Mutant | Result |
|---|---|
| M1 mask from Arrow nulls instead of `isna` | killed |
| M2 chunked mask from the normalized chunk | killed (normalization trap) |
| M3 protected set dropped | survived, equivalent by semantics (corrected by dennis): the protected set changes only integer columns (`_fk_keys.py:533-536`), and nullable `Int64` and float64-with-NaN have identical `isna()`, so the mask is the same either way |
| M4 step ignores the mask | killed |
| M5 step inverts the mask | killed |
| M6 dispatch admits any type | killed |
| M7 dispatch admits numeric for deterministic | killed |
| M8 dispatch drops numeric admission | killed |
| M9 registry adds timestamp | killed |
| M10 registry drops uint64 | killed |
| M11 unified domain ignores the positional flag | survived, then killed after a unit test of that layer |
| M12 unified domain never wider | killed |
| M13 binder drops the numeric types | killed |
| M14 binder admits numeric for deterministic | survived, then killed after a unit test of that layer |
| M15 unified route passes no source slice | survived, equivalent: the gate makes Arrow validity equal pandas missingness for every admitted column |
| M16 unified mask inverted | killed |

## Perf (1M rows, positional Faker over int64 with 10% nulls, best of 3)

| Measurement | Time |
|---|---|
| mask helper, 1M int64 | 4.8 ms |
| mask helper, 200k string | 39 ms |
| chunked native (4 chunks), with the mask | 0.785 s |
| chunked native, Arrow validity (no conversion) | 0.785 s |
| unified lane, with the mask | 0.911 s |
| unified lane, Arrow validity | 0.918 s |
| lane off (oracle) | 0.913 s |

The conversion is noise next to the run. The unified end-to-end time is close to lane-off at this size; the 1M-row run is dominated by work both lanes share, so the table shows only that the extra conversion costs nothing measurable, not a lane speedup.

## Verification

Builder, final tree (3.11 with the companion, physical + native + unit/execution + parity + perf): 14661 passed, 12 skipped, 59 xfailed, 0 failed. tests/sentry: 2435 passed on 3.10 and 3.11. `scripts/test_flight.py`: 5 of 5 fingerprints match golden, exit 0. On 3.10: ruff, format and mypy clean.

## dennis gate and remediation

dennis GO (0 BLOCKER, 0 HIGH, 1 MEDIUM, 2 LOW). Its own differential probes all matched:
- Arrow-extension, NumPy and nullable float NaN;
- bool and int nulls, including uint64 extremes;
- mixed-metadata chunks;
- the normalization trap;
- mixed configs.

Fixed:
- **MEDIUM (string-source cost):** the pandas conversion ran on every positional chunk, strings included. That cost about 7.7% (1.617 s against 1.502 s on 2M strings in 200k chunks) on a path that was already native. For `string` and `large_string`, Arrow validity is exactly pandas missingness, so `faker_missing_mask` now returns `is_null` for them without converting, which restores main's path. A test pins that the fast mask equals the conversion's mask under object, `string` and Arrow-extension metadata.
- **LOW:** `date64`, `time64` and `decimal256` added to both declined tables, as plan test 4 listed.
- **LOW:** this Verification section filled in, the testflight result recorded, and M3's rationale corrected.
