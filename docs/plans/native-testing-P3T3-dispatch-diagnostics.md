Status: record

# P3-T3 dispatch faker branch + route diagnostics: route-integrity, masking-output, fail-closed gate

- **Plan:** `docs/plans/2026-08-30-phase3-c1-test-plan.md`, batch P3-T3 (section 4) and the method
  in section 3 (binding by reference from `docs/plans/2026-08-29-native-efficiency-test-plan.md`).
- **Scope:** `src/decoy_engine/execution/native/_dispatch.py`'s CHANGED lines only
  (`git diff origin/main..HEAD -- <file>`: the native faker branch -- pool lookup/build,
  deterministic selection seed, namespace/scale, the `native_pool` route tag, the pool_select
  counters, positional null restore, `pool_cache` reuse -- and the preflight string-type
  scope-lock guard), plus this batch's own new production addition (the chunk schema-drift
  sentry); and `src/decoy_engine/execution/native/_route_diagnostics.py` (206 LOC, graded in
  full, new in Phase 3).
- **Branch:** `feat/native-phase3`, worktree `.claude/worktrees/native-phase3-build`.
- **Harness reused:** `scripts/native-testing/python_mutation_pilot.py` (P3-T0), unchanged.
  Faker-only lane for `_route_diagnostics.py`; companion-required lane for `_dispatch.py`
  (`tests/parity/native/test_c1_faker_parity.py` needs the compiled crypto kernel for its hash
  column, `tests/native/test_native_dispatch.py` needs it for the kernel-path tests).
- **Bar:** kill every mutant that changes a selected value, a selection determinant, a route
  tag, a per-identity ledger event, the preflight-reroute verdict, the null-restore, the
  isolation, or the drift failure code; adjudicate everything else in writing.

## 1. Production change: the chunk schema-drift sentry

Empirically probed before writing any test (see section 6 for the probe): a later chunk with a
missing configured column was silently dropped from the output (no error, no signal), and a
later chunk with an extra unconfigured column raised a raw `KeyError` from inside
`_mask_chunk_native`'s column loop -- neither is a coded, fail-closed failure, and both were
already pinned as "current, defined behavior" by two pre-existing Phase 2 tests in
`tests/native/test_native_dispatch.py` (`test_second_chunk_introducing_an_unconfigured_column_...`,
`test_second_chunk_missing_a_configured_column_silently_drops_it`). A third shape -- a column
present in both chunks but with a changed Arrow type -- was neither guarded nor tested at all.

None of this is faker-specific: `_mask_chunk_native` iterates `chunk.schema.names` the same way
for every admitted strategy, so a passthrough/redact/truncate/hash column drifts exactly like a
faker column would. Preflight only inspects the FIRST chunk (the `covered == actual` check in
`run_native_or_oracle_chunked`, plus the string-type guard); nothing re-validates schema on any
chunk after the first, and the route returns a lazy per-chunk iterator, so "no partial output"
held only for preflight-detectable faults.

**Fix:** `NativeChunkSchemaDriftError` (a new `DecoyError` subclass, `.code =
"native_chunk_schema_drift"`, carries `.table` / `.chunk_index` / `.detail`) and
`_check_chunk_schema_drift(expected, chunk, ...)`, called once per chunk (including the first,
for uniformity) inside `_mask_native`'s `_masked()` generator, BEFORE `_mask_chunk_native` runs
for that chunk. `expected` is the admitted first chunk's own schema (already proven to match the
compiled plan's covered columns by the caller's preflight check), so the sentry is a pure
equality/type comparison against that baseline: a name-set mismatch reports both `missing` and
`extra` columns; a type mismatch on a column present in both reports `type_changed:{col}:{old}->
{new}`. Raised before the drifting chunk is yielded -- chunks already consumed by the caller are
unaffected, but there is no rollback of them, matching the plan's explicit instruction not to
promise one.

The two pre-existing Phase 2 tests were UPDATED (not deleted) to assert the new coded contract
instead of the old raw-`KeyError`/silent-drop behavior, since they explicitly pinned the old
behavior as "current, defined" rather than as a permanent design decision; a third test covers
the type-change shape, and a fourth confirms the sentry is a true no-op across five well-formed
chunks (`tests/native/test_native_dispatch.py`).

## 2. `_dispatch.py`: five fields (changed-lines scope) + every survivor bucketed

Two full pilot runs, both `--tests tests/native/test_dispatch_faker.py
tests/native/test_native_dispatch.py tests/parity/native/test_c1_faker_parity.py
tests/native/test_c1_bounded_state.py --timeout 30 --readjudicate-killed`:

- **Before fixes:** 586 mutants, 521 killed / 65 survived (88.91%).
- **After fixes:** 586 mutants, 533 killed / 53 survived (90.96%), 0 flaky-kills on the
  killed-bucket standalone re-adjudication. Exactly 12 fewer survivors than the first run, and
  every one of the 12 is a mutant this batch's fixes specifically targeted (verified by diffing
  the two survivor-name lists, not by count alone).

The file mixes CHANGED (Phase 3) and UNCHANGED (Phase 2, out of this batch's scope per the plan's
explicit instruction) code in the same functions, so mutmut's whole-file total mixes both. The
five fields below are stated for the CHANGED-lines scope, with every one of the 53 final
survivors bucketed by name so none is silently left out:

| Field | Count | Note |
|---|---:|---|
| (a) Branch coverage (whole file, for context) | 98% (219 stmts, 78 branches; 3 partial/missed lines, all discussed below) | `coverage run --branch`, companion-present |
| (b) Killed (changed-lines scope) | all changed-line mutants that are not equivalent/unreachable | see 2.1 |
| (c) Equivalent (reason below) | 5 | `_sample_faker_chunk` scale/mode mutants, section 2.3 |
| (d) Unreachable-by-contract | 21 | 4 defensive branches, section 2.4 |
| (e) Tool-excluded | 0 | none |
| Out of scope (unchanged Phase 2 lines) | 27 | not this batch's to kill, section 2.5 |

### 2.1 Killed: the 12 mutants this batch's fixes closed

All 12 are on lines this batch itself added or changed (new code: `NativeChunkSchemaDriftError`,
`_check_chunk_schema_drift`; or Phase 3 diff lines in `_resolve_faker_pools` and the preflight
string-type guard). Each was verified killed two ways: the corrected mutation tally (65 -> 53
survivors, exact name-list diff) and a direct check that the specific new/extended test fails red
against the mutated body (shown inline below for the two structural bugs; the P3-T2 record
established this "seed a mutation, show red" discipline and this batch follows it).

- **`xǁNativeChunkSchemaDriftErrorǁ__init____mutmut_1`** -- `super().__init__(None)`, dropping the
  exception's own message (kept `.table`/`.chunk_index`/`.detail`, which a prior looser version of
  the tests already asserted). Killed by adding `assert "schema drift" in str(exc_info.value)` /
  message-substring assertions to the three drift tests in `test_native_dispatch.py`.
- **`x__check_chunk_schema_drift__mutmut_12`** -- same message-drop pattern on the missing/extra
  raise site. Same fix.
- **`x__check_chunk_schema_drift__mutmut_25/26/27`** -- message dropped, `table=None`, and
  `chunk_index=None` on the type-changed raise site, none of which the original test asserted
  (it only checked `.code` and `.detail`). Fixed by adding `.table`, `.chunk_index`, and
  message-substring assertions to `test_second_chunk_column_type_change_fails_coded`.
- **`x__resolve_faker_pools__mutmut_8`** -- `if col_seed.strategy != "faker": continue` mutated to
  `break`. A non-faker column ordered BEFORE a faker column in `col_seed_by_name`'s iteration
  order would abort the whole pool-build loop, leaving the later faker column's pool never built
  -- but every existing config in the file (including the frozen C1 recipe itself, which declares
  FIRST/LAST/MAIDEN before the hash columns) puts the faker column first, so `continue` and
  `break` were indistinguishable in every prior test (nothing follows the non-faker column either
  way). Killed by `test_resolve_faker_pools_does_not_abort_on_a_non_faker_column_before_it`
  (a hand-built `col_seed_by_name` with a hash column BEFORE the faker column). Verified to fail
  red against the mutant directly:
  ```
  # with `continue` replaced by `break` in _resolve_faker_pools:
  AssertionError: assert set() == {'FIRST'}   # pool never built
  ```
- **`x__resolve_faker_pools__mutmut_31`** -- `pool = cached if isinstance(cached, ValuePool) else
  None` mutated to `pool = None` unconditionally. `pool_cache.get(identity)` still runs and still
  registers as a hit internally, so `cache.stats().hits`/`.misses` cannot tell "reused" apart from
  "value-identically rebuilt and discarded" -- and a rebuild IS value-identical (the build is
  deterministic), so no VALUE-based assertion can either. Killed by
  `test_resolve_faker_pools_reuses_the_exact_cached_object_on_a_hit`, which asserts Python object
  identity (`second_pools["c1"] is built_pool`) between the first build and a second call sharing
  the same warm cache.
- **`x__resolve_faker_pools__mutmut_37/38/43/44`** -- `locale=locale`/`config=build_config` in the
  `builder.build(...)` call forced to `None` or dropped (falling back to the function's own
  `None` defaults). Every existing test uses the default locale and an empty `provider_config`,
  so `locale=None` and `config=None` are indistinguishable from the correct values under every
  prior test -- not equivalent (a config with an explicit locale or extra provider kwarg IS a
  real, reachable production shape via `ColumnConfig.provider_config`), just untested. A real
  Faker provider method rejects an unrecognized extra kwarg (confirmed empirically: passing
  `{"unused_marker": "x"}` through the full route to `person_first_name` raises `AdapterError`),
  so the kill test spies on `PoolBuilder.build` via `monkeypatch` to capture the actual kwargs
  passed, sidestepping the need for a real accepted kwarg
  (`test_resolve_faker_pools_passes_locale_and_config_through_to_the_builder`).
- **`x_run_native_or_oracle_chunked__mutmut_61`** -- inside the preflight string-type guard,
  `if node.strategy != "faker": continue` mutated to `break`. The mirror of the
  `_resolve_faker_pools` bug: a non-faker node ordered BEFORE a bad-typed faker node in
  `decision.node_routes` would stop the type check before ever reaching the faker node, silently
  ADMITTING a table that should reroute -- and every existing test (including the C1 recipe) put
  the faker node first. Killed by
  `test_faker_source_type_reject_still_reroutes_when_a_kernel_column_precedes_it` (a passthrough
  column declared before the bad-typed faker column; passthrough, not hash, so no companion is
  needed). Verified to fail red against the mutant:
  ```
  # with `continue` replaced by `break` in the string-type guard loop:
  AssertionError: assert True is False   # table wrongly admitted native
  ```

### 2.2 The preflight-guard whole-table-reroute + masking-output value grading (already existed, extended)

`tests/parity/native/test_c1_faker_parity.py` already grades selected VALUES against the pinned
pandas oracle (never a native-produced golden) across batch sizes, row order, repeated-value
sources, and multiple null shapes (first/last/consecutive/scattered, including non-zero Arrow
offsets), which is most of item 1's bar. This batch added, in `tests/native/test_dispatch_faker.py`:

- `test_selected_values_match_pandas_oracle_over_repeated_and_null_source` (parametrized over 3
  batch sizes) and `test_null_positions_preserved_byte_for_byte_vs_oracle_null_dense_source`: a
  focused, dedicated repeated-key + null-dense source compared directly against a real
  `run_pipeline(substrate="pandas", execution_mode="full_frame", auto_chunk=False)` run of the
  SAME config/key, isolating the value-selection and positional-null-restore claims from the
  broader parity harness's own scope.
- `test_faker_output_column_is_arrow_string_type`: the output Arrow type determinant.
- `test_pool_cache_hit_selection_is_byte_identical_to_the_cold_build`: cache-hit vs cold-build
  selection identity through the FULL route (complementing the `_resolve_faker_pools`-level
  object-identity test above, which is more precise but bypasses the route).
- `test_faker_source_type_reject_reroutes_whole_table_including_hash_column`: the
  whole-table-reroute claim needs a multi-strategy config (a single-column config cannot
  distinguish "the bad column rerouted" from "the whole table rerouted"); mixes in an admitted
  hash column (companion-required) and asserts BOTH columns show `route == "oracle"`.
- `test_faker_source_type_reject_still_reroutes_when_a_kernel_column_precedes_it`: the ordering
  variant that killed mutmut_61 above.

### 2.3 Equivalent mutants (5)

- **`x__sample_faker_chunk__mutmut_11/12/16/21/29`** -- all five vary `scale` (forced to `None`,
  the `is not None`/`is None` comparison flipped, `mode=None` passed instead of the resolved
  `CardinalityMode`, or the `scale=scale` kwarg dropped from the `PoolSampler().sample(...)`
  call). Verified equivalent by reading `generation/pool/_sampler.py::PoolSampler.sample`
  directly: under `deterministic=True` (the ONLY value `_sample_faker_chunk` is ever called with
  -- it is explicitly "scoped to the ONE JC-5-admitted variant"), the method branches only on
  `mode is CardinalityMode.UNIQUE` (raise) vs. everything else (call `self._deterministic(...)`,
  which never reads `scale` at all). JC-5 admission (`faker_pool_precondition_met`,
  `_requirements.py`) rejects `cardinality_mode="unique"` for a deterministic faker column before
  it ever reaches this native route, so `mode` can never actually be `UNIQUE` here either -- every
  reachable call collapses to the identical `_deterministic(...)` invocation regardless of the
  `mode`/`scale` values passed. No test, however constructed, can distinguish these five mutants
  from correct code without first breaking the JC-5 precondition itself (a change to a DIFFERENT,
  already-graded unit, P3-T1).

### 2.4 Unreachable-by-contract (21)

All four defensive branches below are dead code by construction, not by omission: each is backed
by an invariant proven from a DIFFERENT module (`_requirements.py`, `keyprovider.py`), not
asserted from inside `_dispatch.py` itself, and each carries its own `# pragma: no cover`
predating this batch.

- **`x__sample_faker_chunk__mutmut_2/3/4/5/6/7`** (6) -- the `if mask_key is None: raise
  AssertionError(...)` branch. `keyprovider.require_mask_key(plan, key_provider) -> bytes` has no
  `None`-returning path in its signature or body (verified by reading it directly); the native
  route's `_mask_native` always resolves `mask_key` via `require_mask_key` before dispatching.
- **`x__resolve_faker_pools__mutmut_11/12/13`** (3) -- the `if provider is None: raise
  AssertionError(...)` branch. Admission (`faker_pool_precondition_met` + the oracle-side
  `FakerStrategyHandler.run`'s own `if not provider: raise ValueError(...)` parity check) never
  lets a providerless faker column reach this admitted-only path.
- **`x__mask_chunk_native__mutmut_85/86/87/88/89/90`** (6) -- the final `else: raise
  AssertionError(...)` for a strategy outside both `NATIVE_KERNEL_STRATEGIES` and
  `NATIVE_POOL_STRATEGIES`. `_static_route_decision` only admits a scalar node when its strategy
  is in one of those two allowlists (proven below); `_mask_chunk_native`'s `elif` chain
  exhaustively enumerates exactly that same set (passthrough/redact/truncate/hash/faker), so the
  final `else` cannot execute for any admitted table.
- **`x__static_route_decision__mutmut_41/43/50`** (3), plus their line-288 coverage gap in the
  branch-coverage table above -- `no_kernel`/`no_pool_path`/the `elif no_kernel and no_pool_path:`
  branch. Proven from `_requirements.py::_fallback_policy`: `fallback_policy == "native"` requires
  `kernel_reason is None`, and `kernel_reason` is computed by `native_pool_rejection` (for a
  `NATIVE_POOL_STRATEGIES` strategy) or `native_kernel_rejection` (everything else, which returns
  non-`None` -- i.e. rejects -- for any strategy NOT in `NATIVE_KERNEL_STRATEGIES`). So a scalar
  node with `fallback_policy == "native"` is guaranteed to have its strategy in
  `NATIVE_KERNEL_STRATEGIES` or `NATIVE_POOL_STRATEGIES`, meaning `no_kernel and no_pool_path` is
  always `False` for any node that reaches this `elif` at all (the `if node.fallback_policy !=
  "native":` branch above it already handles every other node). Forcing `no_kernel`/`no_pool_path`
  to `None` cannot change this: `None and X` is falsy exactly like `False and X`, and this `elif`
  condition is only ever consulted for its truthiness, never its identity -- so the mutation has
  zero observable effect in any reachable state, not just the ones this batch's tests happen to
  drive.

### 2.5 Out of scope: unchanged Phase 2 dispatch code (27)

Confirmed against `git diff origin/main..HEAD -- src/decoy_engine/execution/native/_dispatch.py`:
none of the lines below appear in that diff. Per the plan's explicit instruction ("Do NOT
re-grade unchanged Phase 2 dispatch code"), these are not this batch's to kill; citing exact
current line numbers so a future batch can confirm the claim without re-deriving it.

- **`_mask_chunk_native`'s redact branch (4)** -- `x__mask_chunk_native__mutmut_23/25/28/29`,
  `arrays[name] = native_redact(source, redact_with=cfg.get("redact_with", "REDACTED"))`
  (line 420), unchanged since Phase 2.
- **`_mask_chunk_native`'s truncate branch (2)** -- `x__mask_chunk_native__mutmut_44/46`, the
  `length=...`/`keep=_resolve_truncate_keep(cfg)` kwargs (lines 428-429), unchanged.
- **`_mask_native`'s pre-Phase-3 setup (11)** -- `x__mask_native__mutmut_4/6/10/17/18/22/23/43/46/
  47/48`: `first = next(chunks, None)` (536), `return iter(())` (538), `profile =
  first_chunk_profile(...)` (539), `plan = compile_plan(...)` (540), `table_seed = next(...)`
  (550), and its own defensive `AssertionError` message (553) -- all present before Phase 3's
  additions (`cache = ...` / `pool_by_column = _resolve_faker_pools(...)`), which start after
  these lines and are fully killed (section 2.1 has none of them).
- **`_resolve_truncate_keep` (2)** -- `x__resolve_truncate_keep__mutmut_10/12`, the whole function
  (line 328), untouched by the Phase 3 diff.
- **`_static_route_decision`'s pre-Phase-3 lines (3)** -- `x__static_route_decision__mutmut_14`
  (`plan = compile_native_plan(...)`, line 256), `_32` (the `label = ...` fallback string,
  line 270), `_73` (the `reroute_reason=None if native_admitted else "; ".join(reasons)` line,
  line 302) -- none of these three lines are in the Phase 3 diff (only the `no_kernel`/
  `no_pool_path`/`elif`/`node_routes` construction between them changed, covered in 2.4 and 2.1).
- **`plan_native_route` (1)** -- `x_plan_native_route__mutmut_5` (line 307) -- the whole function
  is absent from the diff; Task 3.1 did not touch it.
- **`run_native_or_oracle_chunked`'s pre-Phase-3 lines (7)** -- `x_run_native_or_oracle_chunked__
  mutmut_20/30/38/85/86/91/95`: the `empty_input` branch (611-615), `profile = ...` (623),
  `decision = plan_native_route(...)` (624), and the oracle-fallback `run_mask_pipeline_chunked(
  ...)` call (669) -- all present before Phase 3's additions (the preflight string-type guard
  block and the `pool_cache` kwarg threading), which are fully killed.

## 3. `_route_diagnostics.py`: five fields, zero unadjudicated survivors

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_route_diagnostics.py \
  --tests tests/native/test_c1_diagnostics.py tests/native/test_c1_bounded_state.py \
  --timeout 30 --readjudicate-killed
```

| Field | Count |
|---|---:|
| (a) Branch coverage | 100% (51 stmts, 8 branches) |
| (b) Killed | 45 |
| (c) Equivalent | 0 |
| (d) Unreachable-by-contract | 0 |
| (e) Tool-excluded | 0 |

First run: 45 mutants, 44 killed / 1 survived, 0 flaky-kills on the killed-bucket
re-adjudication. The one survivor, `xǁRouteDiagnosticsǁevidence__mutmut_1`, replaced
`pool_warnings=self.pool_warnings(owners=owners)` with `pool_warnings=None` inside
`RouteDiagnostics.evidence(...)`. The only existing `.evidence()`-consuming test
(`test_row_error_in_a_later_chunk_captured_and_surfaced_in_job_evidence`) never puts a pool, so
it asserts `.row_errors` but never `.pool_warnings` -- a real gap, not equivalent (a caller
reading job evidence for pool-quality warnings would see `None` instead of a tuple and crash on
`len()`/iteration). Fixed with
`test_evidence_snapshot_carries_pool_warnings_alongside_row_errors`
(`tests/native/test_c1_diagnostics.py`), which puts a pool, calls `.evidence(owners=...)`, and
asserts both halves of the snapshot. Verified to fail red against the mutant directly (monkeypatch
substitution, not a full mutmut re-run):

```
TypeError: object of type 'NoneType' has no len()
```

Second pilot run confirmed the fix: 45 mutants, 45 killed / 0 survived.

This module's per-invocation isolation (the length-prefix baseline) and its rebuild-churn
boundedness were both already covered by `test_c1_bounded_state.py`'s
`test_collector_view_stays_bounded_under_same_identity_rebuild_churn` (bounded by the count of
DISTINCT dominating pools -- 2 -- not the ~150 re-put count across 50 churn cycles) and
`test_pool_cache_evicts_at_byte_bound_under_many_distinct_identities` (isolation held across 30
invocations sharing one evicting cache); both are pre-existing, and the mutation run found zero
survivors on any line either test covers, so no new test was needed for isolation or boundedness.
Row-error fail-closed handling (`raise_if_row_errors`) was already covered by
`test_row_error_in_a_later_chunk_captured_and_surfaced_in_job_evidence` and
`test_no_row_errors_never_raises`; likewise zero survivors there.

## 4. Route ledger decision: NOT ADDED, with evidence

The plan's candidate gap 3 asks whether the runtime `NativeRouteEvidence`'s AGGREGATE counters
(`pool_select_calls`, `kernel_calls[strategy]`) are sufficient, or whether a per-(table, column,
chunk) mis-route can survive because only totals are checked -- in which case this batch would
need to add minimal per-identity event instrumentation to `_dispatch.py`.

**Decision: not needed.** The full 586-mutant sweep across the whole file (a comprehensive spray
of every literal/operator/conditional flip mutmut generates, not a hand-picked subset) found zero
survivors of the shape "the aggregate count stays correct but the wrong column/chunk gets
credited." The two REAL, demonstrated ordering bugs the sweep found
(`_resolve_faker_pools__mutmut_8`, `run_native_or_oracle_chunked__mutmut_61`, both section 2.1)
are column-ORDERING bugs at the pool-build / preflight-decision layer, not runtime per-identity
event mis-attribution, and both surface LOUDLY rather than silently:

- `_resolve_faker_pools__mutmut_8` (a skipped later faker column) does not silently under-count;
  it makes `pool_by_column[name]` missing, which `_mask_chunk_native`'s direct dict lookup turns
  into a hard `KeyError` the first time that column is masked -- a crash, not a miscounted
  success. The structural coupling (both functions key by column NAME, not by position or a
  separate index) means a column/identity mismatch cannot silently succeed with wrong counters;
  it aborts the whole invocation immediately.
- `run_native_or_oracle_chunked__mutmut_61` (a skipped later faker type-check) DOES silently admit
  a table it should reroute -- this is the one place in the whole sweep that comes close to a
  "silent mis-route." But it is caught at the PREFLIGHT DECISION layer (`evidence.native_admitted`
  / `evidence.reroute_reason`), which is already directly observable and already graded (section
  2.1's kill test), not something that needed a new RUNTIME per-chunk event to detect.

The "counters advancing before a chunk is yielded" concern is structurally closed without new
code: `_mask_chunk_native` fully processes every column of a chunk (including all counter
increments) before returning, and the wrapping generator's `yield` only happens after that full
return -- so a column-level exception mid-chunk propagates out of the whole
`run_native_or_oracle_chunked` call before the chunk is ever yielded, aborting the invocation.
`evidence()` is only ever consulted by callers AFTER the full chunk stream drains successfully
(the frozen gate's own `test_criterion4_exact_count_route_ledger` comment makes this explicit:
"'completed' is... evaluated AFTER the full chunk stream was consumed"), so a partially-advanced
counter from a failed chunk is never read as if it were evidence of a successful, published run.

Given zero demonstrated per-identity survivors across a full-file mutation sweep, adding
per-identity event instrumentation now would be adding code the tests do not need, which the
plan explicitly forbids ("Do not add instrumentation the tests do not demonstrably need").

## 5. Drift-sentry decision: ADDED, with evidence

See section 1 for the full account. Summary: empirically probed (not assumed) that a later-chunk
schema drift is silently corrupting or uncoded-crashing today for all three shapes (missing
column: silent drop; extra column: raw `KeyError`; type-changed column: silently accepted,
demonstrated separately). Two pre-existing Phase 2 tests explicitly pinned the first two as
"current, defined behavior," which the plan's own candidate gap 6 directs this batch to fix. Added
`NativeChunkSchemaDriftError` (`.code = "native_chunk_schema_drift"`, carries `.table` /
`.chunk_index` / `.detail`) and `_check_chunk_schema_drift`, wired into `_mask_native`'s
`_masked()` generator before each chunk (including the first) is masked. Updated the two
pre-existing tests to assert the new coded contract, added a third for the type-change shape and
a fourth confirming the sentry is a no-op across well-formed chunks. All four raise sites'
message content and structured fields (`.code`/`.table`/`.chunk_index`/`.detail`) are killed by
mutation (section 2.1). The frozen C1 gate (section 7) confirms the sentry never fires on the
recipe's own uniform-schema chunks.

## 6. Empirical probes (reproduced)

```
# Later-chunk drift, before the fix (three shapes):
$ .venv/bin/python -c "... run_native_or_oracle_chunked over [chunk_ok, chunk_missing_col] ..."
chunk 1: schema=['A'] rows=2   # "B" silently dropped, no error

$ .venv/bin/python -c "... [chunk_ok, chunk_extra_col] ..."
RAISED: KeyError 'C'           # uncoded crash

$ .venv/bin/python -c "... [chunk_ok, chunk_type_changed_col] ..."
chunk 1: schema=['A', 'B'] rows=2   # silently accepted, wrong-typed column processed as-is

# After the fix, same three inputs:
NativeChunkSchemaDriftError 't' chunk 1: schema drift ... (missing=['B'], extra=[])
NativeChunkSchemaDriftError 't' chunk 1: schema drift ... (missing=[], extra=['C'])
NativeChunkSchemaDriftError 't' chunk 1: column 'B' type changed string -> int64
```

## 7. Frozen C1 gate: still passes

```
PYTHONPATH=src .venv/bin/python -m pytest tests/parity/native/test_phase3_c1_gate.py -q
12 passed in 136.10s
```

The drift sentry runs on every chunk of the frozen recipe's own parity (10,000-row) and moderate
(250,000-row) tiers and never fires (both tiers use a uniform schema across every chunk), so
adding it does not regress any of the gate's four criteria.

## 8. Reproducible commands

```
# _dispatch.py mutation pilot (companion-required lane; ~15-20 min without --readjudicate-killed,
# ~40-60 min with it on this box -- run once, foreground, do not background it)
.venv/bin/python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_dispatch.py \
  --tests tests/native/test_dispatch_faker.py tests/native/test_native_dispatch.py \
          tests/parity/native/test_c1_faker_parity.py tests/native/test_c1_bounded_state.py \
  --timeout 30 --readjudicate-killed --clean-mutants-dir

# _route_diagnostics.py mutation pilot (faker-only lane; ~1 min)
.venv/bin/python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_route_diagnostics.py \
  --tests tests/native/test_c1_diagnostics.py tests/native/test_c1_bounded_state.py \
  --timeout 30 --readjudicate-killed --clean-mutants-dir

# Full companion-required regression + frozen gate
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/native/test_dispatch_faker.py tests/native/test_native_dispatch.py \
  tests/native/test_c1_bounded_state.py tests/native/test_c1_diagnostics.py \
  tests/parity/native/test_c1_faker_parity.py tests/parity/native/test_phase3_c1_gate.py

# Branch coverage
.venv/bin/python -m coverage run --branch -m pytest -q tests/native/test_dispatch_faker.py \
  tests/native/test_native_dispatch.py tests/parity/native/test_c1_faker_parity.py \
  tests/native/test_c1_bounded_state.py
.venv/bin/python -m coverage report --include=*/_dispatch.py -m

.venv/bin/python -m coverage run --branch -m pytest -q tests/native/test_c1_diagnostics.py \
  tests/native/test_c1_bounded_state.py
.venv/bin/python -m coverage report --include=*/_route_diagnostics.py -m

# ruff
.venv/bin/python -m ruff check src/decoy_engine/execution/native/_dispatch.py \
  tests/native/test_dispatch_faker.py tests/native/test_native_dispatch.py \
  tests/native/test_c1_diagnostics.py
.venv/bin/python -m ruff format src/decoy_engine/execution/native/_dispatch.py \
  tests/native/test_dispatch_faker.py tests/native/test_native_dispatch.py \
  tests/native/test_c1_diagnostics.py
```

mypy on `_dispatch.py` aborts on the same pre-existing numpy 2.5.0 stub syntax error the P3-T0/
P3-T1/P3-T2 records already carried forward (`Type statement is only supported in Python 3.12 and
greater`); not introduced or fixed here.

## 9. Acceptance against the plan's P3-T3 gate

- Masking output graded against the pandas oracle over repeated + null keys, never a
  native-produced golden: met (section 2.2, plus the pre-existing full parity harness).
- Preflight string-type guard: utf8/large_utf8 accept and non-string reject both tested, whole-
  table (not per-column) reroute proven with a multi-strategy and a column-ORDER-varied config:
  met (section 2.2).
- Route ledger independently observed vs. a Cartesian reconstruction: assessed; existing
  aggregate counters + the frozen gate's own ledger, PLUS this batch's two new ordering-bug kill
  tests, already kill every mis-route mutant the full sweep produced; instrumentation NOT added,
  with mutation evidence (section 4).
- Positional null-restore byte-for-byte vs. the oracle over a null-dense source: met (section
  2.2).
- `RouteDiagnostics` isolation under rebuild-churn, bounded by distinct dominating pools not
  re-put count, row-error fail-closed: met, zero unadjudicated survivors (section 3).
- Later-chunk drift fails CODED, not silently corrupt, for faker AND a non-faker strategy: met,
  production fix added (sections 1, 5).
- Zero unadjudicated survivors on the changed lines: met. Every one of the 65 -> 53 survivors is
  bucketed (killed, equivalent, unreachable-by-contract, or out-of-scope-unchanged-Phase-2) with
  a stated reason; none is a value/determinant/route-tag/ledger/verdict/null-restore/isolation/
  drift-code survivor left unadjudicated.
- Frozen C1 gate still passes after the drift-sentry production change: met (section 7).
