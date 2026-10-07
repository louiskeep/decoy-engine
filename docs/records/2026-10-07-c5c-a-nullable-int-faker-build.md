Status: record

# C5c-a nullable-int deterministic Faker: build record

Contract: `docs/plans/2026-10-07-c5c-a-nullable-int-faker.md` rev 2 (Codex plan gate GO). Branch `fix/c5c-a-nullable-int-faker` off engine main `228fe140`. The shared pandas frame is unchanged.

## What changed

- `execution/_exact_int_faker.py` (new, 118 lines): which columns qualify, registration and release, selected positions for a mask, the one-call gate context, and the sampling Series.
  - A column is registered when its node is a scalar deterministic Faker, its Arrow type is an integer, its frame column is float64, and it has at least one null and one value. No earlier node in the adapter's real dispatch order declared it as written.
  - The sampling Series is full length, explicitly `object`: Python `int` for valid rows, `None` for nulls, in the frame column's index.
  - A row-count or null-mask mismatch raises `GenerationError` code `exact_int_null_mask_mismatch`.
- `StrategyContext` gains `exact_int_sources` (a dict, filled per table or chunk) and `gate_positions`.
- `PandasExecutionAdapter.run` registers each table's columns after the work list is ordered. `run_sequential` registers at table load and releases at eviction. The chunked oracle goes through `run`, so each chunk's context holds only that chunk.
- `_when_gate.run_with_when_gate` computes positions once as `np.flatnonzero(mask.to_numpy(dtype=bool, na_value=False))`. It hands them to the handler through a `dataclasses.replace` copy of the context, only for a column that has an exact source. The same positions feed the row-error remap.
- `FakerStrategyHandler` keys the deterministic sampler from `sampling_source(...)`. The positional path is untouched.
- Re-pinned: the two draw-site `call_site` pointers into `_strategies/_faker.py` (`:101` to `:102`, `:118` to `:119`) in `_draw_sites_gen_pool.py`, `_determinism_protocol.py` and `docs/native/draw-site-inventory.md`; `_PINNED_RUN_CALLS` gains `register_exact_int_sources`; module census `_pandas_adapter.py` 676 to 681, `_sequential.py` 649 to 652; seam sentry permits `_exact_int_faker.py`.

## Judgment calls

- **The gate copies the context only for a column with an exact source.** The plan has the gate always pass positions through `dataclasses.replace`. Several existing test files hand the real gate a stand-in context (`SimpleNamespace`, `_Ctx`, `_FakeCtx`), which `replace` cannot copy. Doing it conditionally keeps every other column's context object identical to today and changes no existing assertion, including the gated identity tests. Plan test 8c's migration of `test_when_gate_mutation_kills.py` therefore turned out unnecessary. Its intent is covered by five new tests with a real `StrategyContext` (positions, shared sinks, unchanged context for other columns, NA mask, no positions without a gate).
- **All-null and empty columns are not registered.** They already mask, and the golden pins that their output is unchanged.
- **Row errors through a nullable-boolean mask now remap.** On main the remap raised `TypeError: boolean value of NA is ambiguous` for a gated handler that recorded a row error under a mask holding `<NA>`. It now uses the same safe positions. This changes only a job that failed before.
- **Numeric-output tests use a registry override.** No poolable default provider returns numbers (`random_int_range` is not poolable), so the tests override `address_zip` with an integer-pool adapter.
- **Not covered by a separate test:** generate-mask jobs. They reach the same adapter constructors and the same `register_exact_int_sources` call as every other `run`.

## Test migration list (plan 8c)

No existing test changed an assertion. Changes to existing tests:

- `tests/unit/execution/test_when_gate_mutation_kills.py`: five tests added; nothing edited.
- `tests/native/test_column_access_surfaces.py`: `register_exact_int_sources` added to `_PINNED_RUN_CALLS`, the inventory's declared way to admit a reviewed call.
- `tests/sentry/test_module_size.py`, `tests/sentry/test_physical_seam_disconnection.py`: census and permitted-file entries.

## Tests

New files: `test_c5c_a_nullable_int_faker.py`, `test_c5c_a_gate_and_writers.py`, `test_c5c_a_unchanged_jobs.py` (goldens in `c5c_a_unchanged_jobs.json`), helpers in `_c5c_a_support.py`.

- Written first. Against the tree before the fix, 34 failed and 20 passed (and 1 skipped writer): 30 for `float_canonicalization_unsupported`, 4 because the context fields did not exist. The 20 passing include all 11 unchanged-job goldens, which were captured from that tree, and the float-source test.
- Test 2 (jobs that work today stay byte-identical) is the golden file: null-free int, zero-row gate, predicate reading the Faker column, derived reader, group_by sibling, `Int64`-metadata source, all-null, empty, an earlier composite writer, redact and row-error remap under gates. Tables, schema, pandas metadata, warnings, row errors and quality metrics all match.
- Test 1 asserts the exact values at both kernel boundaries, with a spy on the compiled kernel proving it ran for int8 to int64 and uint8.
- Kept limits are pinned: partial gate with a string provider raises `ArrowTypeError`; numeric-output chunk drift raises `chunked_schema_mismatch` on concatenation.

## Counts (final committed tree)

| Suite | Python | Result |
|---|---|---|
| `ruff check .`, `ruff format --check .`, `mypy src` | 3.10 | clean (489 source files) |
| `tests/sentry` | 3.10 | 2435 passed, 1 skipped |
| `tests/sentry` | 3.11 | 2435 passed, 1 skipped |
| New files, `test_when_gate_mutation_kills.py`, `test_column_access_surfaces.py`, `test_c5b_i_metadata_inventory.py` | 3.10 | 268 passed, 8 skipped (compiled-kernel tests skip without the companion) |
| `tests/unit/execution` | 3.11 | 6510 passed, 5 skipped |
| `tests/unit/generation` | 3.11 | 529 passed, 9 skipped |
| `tests/native` | 3.11 | 5956 passed, 1 skipped |
| `tests/physical` | 3.11 | 1778 passed, 1 skipped |
| `tests/parity` | 3.11 | 352 passed, 6 skipped, 59 xfailed |
| `tests/integration` | 3.11 | 444 passed, 27 skipped |
| `tests/perf` | 3.11 | 15 passed, 10 deselected |
| `scripts/test_flight.py` (check only) | 3.11 | 53 of 53 checks, 5 of 5 fingerprints match golden |

An earlier full run found two failures, both fixed: the draw-site line pointers and the call inventory above.

## Mutation (plan test 9)

Hand mutants, each run against the three new files plus the gate and call-inventory tests on 3.11:

| Mutant | Result |
|---|---|
| Drop the earlier-writer check | killed |
| Ignore the deterministic flag when registering | killed |
| Drop the float64 check | killed |
| Never update the written set | killed |
| Written set holds only the first column | killed |
| Raw `to_numpy()` positions (NA unsafe) | killed |
| Gate never sets positions | killed |
| Positions reversed | killed |
| Row-error remap ignores positions | killed |
| Handler ignores positions | killed |
| No null-mask check | killed |
| Inferred dtype instead of `object` | killed |
| Handler never uses the exact Series | killed |
| Sequential never releases | killed |
| Sequential never registers | killed |
| Adapter never registers | killed |
| All-null columns admitted | killed |
| Handler passes `deterministic=True` always | survived; equivalent. The registry only holds deterministic nodes, and the non-deterministic modes that reach the sampler ignore source values. The guard stays as a second check. |

The first run of the helper-level mutants (ignore deterministic flag, drop float64 check, all-null admitted) survived. Output was identical under each, so `test_only_a_widened_nullable_integer_column_is_registered` was added and killed them.

## Not verified

- A generate-mask job end to end (see judgment calls).
- Memory behavior on a large table. `to_pylist()` builds one Python int per row of an exact-int column, only for columns that failed before.

## dennis gate and remediation

dennis GO (0 BLOCKER, 0 HIGH, 2 MEDIUM, 2 LOW).

What it confirmed:
- The 11 goldens reproduce on extracted main `src`.
- Its own main-vs-branch probes (14 whole-frame jobs, an FK child, an FK REMAP orphan, null-free FK) changed only jobs that fail on main.
- The row-error remap change is confined to jobs that raise on main.
- The conditional context copy, the writer order, the lifetimes and the census are all sound.

Fixed:
- **MEDIUM (goldens pin library versions).** The golden snapshots carried pyarrow's `creator` version and `pandas_version` inside the pandas schema metadata. CI installs unpinned, so a patch release would have turned main red. The comparison now strips those two keys on both sides and keeps the rest of the metadata.
- **MEDIUM (missing FK case, plan test 5).** New test: deterministic Faker on a nullable FK child column (`pid`) next to a nullable int column (`n`). `pid` resolves exactly as with a null-free `n`, and each non-null `n` value masks like its null-free copy. The FK column converts FK-safe and is never registered.
- **LOW:** `gated_context` is typed `StrategyContext`.
- **LOW, tracked not fixed:** `to_pylist()` makes one Python object per row, which costs several GB at 100M rows on the whole-frame route. Only jobs that failed before take this path. Revisit when C5c-ii opens the native route.

