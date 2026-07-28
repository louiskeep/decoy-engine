# Mutation grading: `execution/_mem_telemetry.py` -- peak-RSS calibration model

TQ isolated-substrate grade (branch `tq/isolated-substrate-grade`) by
`scripts/tq_mutate.py`. Like `_mem_estimate` / `_probe_scale`, this module was on
finding #15's "un-gradeable subprocess substrate" list but has NO DIRECT subprocess
call or import in its own code and grades in-process (the mutations execute in the
parent): a peak-RSS calibration model (a k-constant fit that
must never under-shoot the observed peak) plus the telemetry-record builders and a
schema fingerprint. Every mutant grades in-process with the existing tool -- no
standalone-per-mutant runner. Public API includes `recalibrate_k`,
`MemoryTelemetryStore.recalibrate`, `_percentile`, `_width_class`,
`schema_fingerprint`, `telemetry_record_from_governor_trip`,
`telemetry_record_from_isolated_run`, and `_assert_basis_matches_estimator`. Not
crypto/RI, so the bar is the measure-first substrate bar
max(baseline 72.99 + 15, 75) = **87.99%**; the re-grade clears it at 88.05%.

## Numbers

Baseline (existing `test_mem_telemetry.py` + `test_mem_calibration.py`): 281/385 =
72.99% LOGIC, 0 unresolved, 104 survivors. Full triage of the 104 (two kill files,
authored per-cluster): **58 LOGIC kills** + **2 proven equivalent** + **44 accepted
non-contract**. Re-grade with both kill files in the selection: **339/385 = 88.05%
LOGIC (tool-native, 0 unresolved)**; all 58 LOGIC targets absent from the survivor
set. The 44 accepted-non-contract are reachable-branch `ValueError` message prose
(code path already pinned by `match=` substrings) and bijective fingerprint-label
relabels; the 2 equivalent are byte-identical (a codec-name normalization and a
budget-dominated sentinel).

| Function | Survivors | Killed | Proven equiv | Accepted non-contract |
|---|---|---|---|---|
| `recalibrate_k` | 36 | 27 | 0 | 9 |
| `MemoryTelemetryStore.recalibrate` | 7 | 7 | 0 | 0 |
| `_assert_basis_matches_estimator` | 18 | 2 | 0 | 16 |
| `_percentile` | 9 | 9 | 0 | 0 |
| `_width_class` | 5 | 1 | 0 | 4 |
| `schema_fingerprint` | 8 | 2 | 1 | 5 |
| `telemetry_record_from_governor_trip` | 13 | 8 | 1 | 4 |
| `telemetry_record_from_isolated_run` | 8 | 2 | 0 | 6 |
| **total** | **104** | **58** | **2** | **44** |

Kill files: `tests/unit/execution/test_mem_telemetry_calib_kills.py` (15 tests,
`recalibrate_k` + `recalibrate`) and `tests/unit/execution/test_mem_telemetry_record_kills.py`
(19 tests, the six record/helper functions).

## Kills (58 via 34 tests)

### `recalibrate_k` (27) + `MemoryTelemetryStore.recalibrate` (7) -- calib kills file
Validation-boundary and gate mutants pin the exact verdict/value the mutation flips
(`lower_margin < 0`->`<=`, `resolved_floor > current_k`->`>=`, the margin multiplier
`(1+lower_margin)`, the strict `< current_k` lowering compare, the floor-clamp
direction, the `"hold"` literal), and the four `KRecalibration` return paths each
pin every field the `field=None` mutants blank out (path / current_k / floor_k /
percentile / sample_count). The `recalibrate` method forwards every keyword to the
delegate; each mutant drops or nulls one (`floor_k`, `schema_fingerprint`,
`min_samples_for_lower`, `percentile`, `lower_margin`), changing the suggestion or
suppressing a validation raise.

### `_percentile` (9) -- record kills file
The numpy-default linear-interpolation tail. The public `recalibrate_k` only ever
passes pct=1.0 (the exact max, short-circuiting interpolation), so these are killed
by testing the helper directly with sub-max pct where interpolation runs: the
neighbour index (`lo+1`, clamped to `len-1`), the `lo == hi` short-circuit (returns
the lower neighbour early at pct<1.0), and the interpolation arithmetic
(`frac = idx - lo`, `ordered[lo] + frac*(hi-lo)`).

### `_width_class` (1), `schema_fingerprint` (2) -- record kills file
`_width_class` 4: `<= boundary`->`<` (a width on a bucket boundary must stay in its
bucket; asserted via fingerprint distinctness). `schema_fingerprint` 16: the FK
position-pair append -> `None` (endpoint distinctness); 25: `hexdigest()[:16]`->`[:17]`
(fingerprint width == 16 hex).

### Basis-contract call args + record fields (11) -- record kills file
`telemetry_record_from_governor_trip` 8/9/10/11 + `telemetry_record_from_isolated_run`
13 + `_assert_basis_matches_estimator` 28/31: the basis-contract call args
(path / raw_bytes / tables / fk_cardinality), exercised on the sequential route
where the FK dedup term makes the working-set basis separable
(12,640,000 vs 12,000,000 for the three-table fixture); dropping any arg bypasses
the sequential guard or recomputes a mismatching basis. `telemetry_record_from_governor_trip`
29/35/44/45 + `telemetry_record_from_isolated_run` 47: record-field population
(`schema_fingerprint`, `outcome`).

## Proven equivalent (2)

- `schema_fingerprint` 24: `.encode("utf-8")` -> `.encode("UTF-8")`; Python codec-name
  normalization makes the encoded bytes identical.
- `telemetry_record_from_governor_trip` 23: `else 0` -> `else 1` for the
  no-observed-peak sentinel; `actual_peak_bytes = max(observed_bytes, budget_bytes)`
  dominates both for any budget >= 1, and a 0-byte governor budget is not a valid trip.

## Accepted non-contract (44): reachable-branch message prose + fingerprint relabels

Killable only by brittle full-message-equality / exact-hash assertions, which house
style declines. Each verified to survive standalone (rc 0).

- `_assert_basis_matches_estimator` 7, 9-23 (16): fragments of the sequential /
  tables-None basis-contract `ValueError` message. The branch is reachable (existing
  `test_sequential_record_requires_tables` triggers it) but the mutations touch only
  message prose; the `match="basis contract"` substring survives.
- `recalibrate_k` 4-10, 21, 22 (9): fragments of the percentile / floor_k validation
  `ValueError` messages, pinned by `match="percentile"` / `match="floor_k"` on their
  unmutated first lines.
- `_width_class` 2, 3, 5, 6 (4): bijective relabels of the "unpriceable" / overflow
  bucket labels inside the opaque fingerprint hash. A consistent relabel preserves the
  fingerprint's equivalence relation (same shape -> same fp, different shape ->
  different fp); only an exact-hash assertion distinguishes.
- `schema_fingerprint` 8-11, 14 (5): `role` labels and the "not present" fragment,
  used only in the invalid-FK-table `ValueError` message.
- `telemetry_record_from_governor_trip` 4-7 (4): not-memory-miss message prose.
- `telemetry_record_from_isolated_run` 3, 4, 6-9 (6): peak-None message prose.

## Candidate findings

None. No mutation exposed a wrong recalibrated k, a wrong percentile value, a wrong
width bucket, a wrong fingerprint field, a wrong telemetry-record field, or a wrong
basis / FK term.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `src/decoy_engine/execution/_mem_telemetry.py`
and the test selection to `tests/unit/execution/test_mem_telemetry_calib_kills.py`,
`tests/unit/execution/test_mem_telemetry_record_kills.py`,
`tests/unit/execution/test_mem_telemetry.py`, and
`tests/unit/execution/test_mem_calibration.py`; then `rm -rf mutants && python
scripts/tq_mutate.py --run --jobs 6`. `source_paths` stays at the package root.
