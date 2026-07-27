# Mutation grading: `execution/_chunked.py` -- substrate bar 77.91%

TQ substrate sweep (branch `tq/substrate-sweep`), graded by `scripts/tq_mutate.py`
with default survived-bucket re-adjudication (finding #16 RESOLVED + gated by
dennis and Codex). This is the FULL-TRIAGE grade: every one of the module's
surviving mutants is individually adjudicated -- killed, proven equivalent, or
accepted non-contract (killable but deliberately not killed) -- with no
killable-and-undocumented residual. `_chunked.py` is the chunked mask-execution route.
`run_mask_pipeline_chunked` masks ONE table chunk-by-chunk for out-of-memory
inputs; the contract is byte parity with the full-frame `run_pipeline` path.
`check_chunked_compatibility` is the compile-time admission gate;
`_conditional_admission_failures` enumerates unmet faker/categorical conditions;
`_warm_faker_pools` builds each admitted faker pool once into the shared pool
cache; `concat_masked_chunks` is the strict byte-identity concatenation;
`aggregate_chunk_timings` rolls up per-chunk timing records. This is a substrate
module (route + streaming), not crypto/RI, so the bar is **77.91% of LOGIC
mutants**.

The machine fields asserted are the ones each function actually decides on: the
byte-parity output (secret-keyed vs job_seed), the compatibility verdict CODE
(`chunked_table_unknown`, `chunked_generate_unsupported`, `strategy_not_chunk_safe`,
`chunked_strategy_conditions_unmet`), the error `.path`, the offending
column/table NAMES carried in each message (data, not prose), the coded concat
error (`chunked_schema_mismatch`) with its column and conflicting-type names, the
aggregated timing values, the FK-passthrough / declared-dtype fail-closed codes,
the projection-policy and per-chunk `adapter.run` plumbing, and the pool-cache
state the warmer's contract specifies. Free-text explanatory prose inside a
message is left as an ACCEPTED NON-CONTRACT survivor when the code and data around
it are pinned -- a full-message-equality test COULD kill it, but the sweep does not
(prose carries no machine contract; the code / path / names ARE asserted).

## Numbers

**TRUE score: 426/488 = 87.30% LOGIC (tool-native, `scripts/tq_mutate.py`, 0
unresolved), above the 77.91% bar. 62 survivors: 23 proven equivalent + 39
accepted non-contract survivors (message prose, killable only via brittle
full-message-equality -- see Taxonomy). No killable-and-undocumented residual.**

TAXONOMY (Codex batch gate, honest labeling). The 62 survivors are NOT all "proven
equivalent": 39 are the message-PROSE class (22 in `_conditional_admission_failures`,
10 in `check_chunked_compatibility`, 7 in `concat_masked_chunks`) -- a
full-message-equality assertion COULD kill each, so they are ACCEPTED NON-CONTRACT,
not equivalent. The other 23 (default-only name reads + genuine no-ops/unreachable)
ARE proven equivalent (no test can kill them).

Grade history (all on `_chunked.py`, 488 mutants):
- mutmut in-process raw (original 3-file selection): 306/488 = 62.70% -- false-LOW
  (finding #16: mutmut's coverage map dropped the sweep's new test file).
- After finding #16 re-adjudication (original selection): 398/488 = 81.56%.
- **Full triage (this grade):** the selection was EXPANDED to also include the
  two existing DE-10 chunked-FK test files (`test_de10_chunked_fk_passthrough.py`,
  `test_de10_chunked_fk_declared_dtype.py`) -- real coverage of
  `run_mask_pipeline_chunked`'s FK guards that the original sweep selection simply
  omitted -- plus five targeted kill tests were added to
  `test_chunked_mutation_kills.py`. Result: **426/488 = 87.30%, 23 proven
  equivalent + 39 accepted non-contract, 0 residual.**

The 90 survivors under the original selection (56 documented-equivalent + 31
residual-killable + 3 that the hand count had mislabeled) are now fully resolved:
**28 additional kills** (18 from the DE-10 selection expansion + 10 from the new
tests, incl. `warm_26` which was only equivalent under an empty build_config), and
the remaining **62 survivors: 23 proven equivalent + 39 accepted non-contract**
(the accepted-non-contract 39 are the message-prose class -- killable via
full-message-equality, deliberately not killed; see Taxonomy).

| Function | Total mutants | Killed | Proven equiv | Accepted non-contract |
|---|---|---|---|---|
| `_conditional_admission_failures` | -- | -- | 0 | 22 (prose) |
| `_warm_faker_pools` | -- | -- | 7 | 0 |
| `aggregate_chunk_timings` | -- | -- | 0 | 0 |
| `check_chunked_compatibility` | -- | -- | 3 | 10 (prose) |
| `concat_masked_chunks` | -- | -- | 0 | 7 (prose) |
| `run_mask_pipeline_chunked` | -- | -- | 13 | 0 |
| **Total** | **488** | **426** | **23** | **39** |

## Kills added by the full triage (28)

### DE-10 FK-guard selection expansion (18)
`run_mask_pipeline_chunked`'s FK guards (`reject_lossy_chunked_fk_passthrough`,
`reject_mismatched_chunked_fk_declared_dtype`) and their argument plumbing were
never exercised by the original 3-file selection -- but the module IS covered by
`test_de10_chunked_fk_passthrough.py` (15 tests) and
`test_de10_chunked_fk_declared_dtype.py` (25 tests). Adding those two files to the
selection kills, with existing coverage: passthrough guard mut 73, 75, 85, 86, 87,
105, 107, 108, 109, 110; declared-dtype guard mut 78, 80, 111, 112, 113, 114, 115,
116. (This is a selection fix -- crediting real coverage -- not new tests.)

### New targeted kills in `test_chunked_mutation_kills.py` (10)
- **mut_106** (`reject_lossy_chunked_fk_passthrough(table=None)`):
  `test_lossy_passthrough_reject_names_the_table` -- asserts the offending TABLE
  name (`orders`, data) is in the fail-closed message. The declared-dtype twin
  already asserted its table name; this closes the same gap on the passthrough side.
- **mut_88** (`chunked_adapter_touches_pandas_ingestion(adapter, config, None)`):
  `test_polars_nonnative_table_still_applies_fk_passthrough_guard` -- `table` is
  load-bearing ONLY on the polars branch (the pandas adapter returns True before
  reading it, which is why the pandas-route reject tests cannot reach this mutant).
  A polars adapter whose table carries a non-native strategy (`top_code`, the one
  chunk-safe strategy outside `POLARS_SCALAR_HANDLERS`) falls to the pandas oracle,
  so the guard must fire; with `table=None` the native-check sees no columns,
  wrongly reports the adapter never touches pandas, and the lossy big-int
  passthrough FK rounds silently.
- **mut_59 / mut_60 / mut_124 / mut_132** (projection policy `= None` /
  `resolve_unconfigured_column_policy(None)` / `adapter.run(unconfigured_column_policy=None)`
  / dropped kwarg): `test_unconfigured_error_policy_threaded_to_each_chunk` -- an
  explicit `error` policy plus an undeclared chunk column must fail closed
  (`undeclared_output_columns`); each mutation drops the policy to the pre-GA warn
  default so the raw column passes through.
- **mut_14** (`empty_input_profile(table=None)`):
  `test_empty_input_gate_profiles_the_real_table` -- at GA `RELEASE_PHASE`, a
  None-named empty-input profile drops the keyed column from the seed envelope, so
  the fail-closed gate no longer demands a secret; the test pins `raises(
  KeyedStrategyRequiresSecret)`.
- **mut_37 / mut_43** (`_warm_faker_pools` `identity_for(config=None)` / dropped
  config): `test_warm_dedup_identity_includes_full_build_config` -- a faker column
  with a NON-empty `build_config` (`provider_config.domain`) so the dedup identity
  depends on it; a null/empty config misses the cached pool and rebuilds. (The
  pre-existing empty-build_config warmer tests could not reach these -- config
  `None` and `{}` hash identically. This same fixture also killed **mut_26**, which
  was only equivalent under the empty build_config.)

## Non-residual survivors (62): 23 proven equivalent + 39 accepted non-contract

The reasoning below holds by construction; each was confirmed surviving the full
(expanded) selection under standalone re-adjudication. The message-prose class (39)
is ACCEPTED NON-CONTRACT (killable via full-message-equality, deliberately not); the
name-reads + no-ops/unreachable classes (23) are PROVEN EQUIVALENT (unkillable).

### Message-prose class (39) -- ACCEPTED NON-CONTRACT (code + data pinned independently)
Wrapping a message literal in `XX...XX` or upper-casing it changes only
explanatory text; the verdict code, error `.path`, and column/table NAMES are
asserted separately. A full-message-equality test could kill these; the sweep
leaves them as accepted non-contract (prose carries no machine contract). Includes: `_conditional_admission_failures` 16-19, 25, 40-47,
55, 57, 58, 66, 68-71, 77 (22 -- deterministic/pool_size/cardinality/from_profile
prose, and XX-wraps whose keyword survives in an adjacent fragment);
`check_chunked_compatibility` 44-47, 107, 113, 122-125 (generate-message prose,
enumeration/join separators that are message reference lists, conditions_unmet
fixed prose); `concat_masked_chunks` 11, 12, 34-38 (column-names /
type-mismatch trailing-sentence prose, incl. mut_12's uppercased trailing
sentence).

### Default-only name reads (3) -- PROVEN EQUIVALENT (name key present in every asserted case)
`check_chunked_compatibility` 19, 21, 24: `get("name", None|"?"|...)` differs from
the original only when the `name` key is ABSENT, and every asserted path carries a
real name.

### Genuine no-ops / unreachable (20) -- PROVEN EQUIVALENT (run_mask_pipeline_chunked + warm)
- `run_mask_pipeline_chunked` 29 (`decoy_engine_version=None` to compile_plan --
  version not in masked-value derivation), 30/34/35 (`no_profile` None/dropped/False
  -- profile supplied so not re-derived), 49 (up-front `require_mask_key` result
  feeds only the unused vault-key guard), 70 (`RelationshipGraph(ordering=None)` --
  empty edges for a single-table mask), 94 (`_warm_faker_pools(table=None)` --
  handler rebuilds lazily on a cache miss).
- **15 / 22** (`empty_input_profile`/`first_chunk_profile` `engine_version=None`):
  `profile.decoy_engine_version` has ZERO consumers in `src/` (the plan stamps
  `engine_version` from its own argument, `plan/_compile.py:458`); masked output
  carries no version field.
- **63 / 123** (`ns_registry = None` / `adapter.run(namespace_registry=None)`): the
  only execution-path consumer of `ctx.namespace_registry` is the composite-FK
  handler, and composite/nested strategies are rejected by
  `check_chunked_compatibility` as not chunk-safe; the authoritative masking key on
  this route is the per-column `ColumnSeed.namespace`.
- **121 / 129** (`adapter.run(pool_cache=None)` / dropped): `adapter.run` defaults
  `pool_cache=None` and builds a fresh `PoolCache()` internally; the faker handler
  rebuilds its pool lazily under the same identity/seed, so output is byte-identical
  (same rationale as mut_94).
- `_warm_faker_pools` 9 (`!= "faker" or provider is None` -> `and`; operands never
  disagree for admitted seeds), 16-18 (the `raise` for `pool_size is None` is
  unreachable -- admission guarantees it), 47 (already-cached `continue`->`break`
  never taken -- production always warms a FRESH PoolCache), 53/59 (`build_config`/
  `config` None at a call site that does not change pool identity).

## Candidate findings

None. No mutation exposed a wrong admission verdict, wrong error code/path, wrong
aggregated timing, or a chunked/full-frame byte divergence that current behavior
does not already intend.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `src/decoy_engine/execution/_chunked.py`
and the test selection to the FIVE files:
`tests/unit/execution/test_chunked_mutation_kills.py`,
`tests/unit/execution/test_chunked.py`,
`tests/unit/execution/test_auto_chunk_routing.py`,
`tests/unit/execution/test_de10_chunked_fk_passthrough.py`, and
`tests/unit/execution/test_de10_chunked_fk_declared_dtype.py`; then
`rm -rf mutants && python scripts/tq_mutate.py --run` (survived re-adjudication is
on by default). `source_paths` stays at the package root.
