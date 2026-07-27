# Mutation grading: `execution/_chunked.py` -- substrate bar 77.91%

TQ substrate sweep (branch `tq/substrate-sweep`), re-graded via standalone
survived-bucket re-adjudication (see Numbers + tq-findings #16).
`_chunked.py` is the chunked mask-execution route. `run_mask_pipeline_chunked`
masks ONE table chunk-by-chunk for out-of-memory inputs; the contract is byte
parity with the full-frame `run_pipeline` path (every admitted strategy is
value-keyed, so concatenated chunked output equals the full-frame output
exactly). `check_chunked_compatibility` is the compile-time admission gate;
`_conditional_admission_failures` enumerates unmet faker/categorical conditions;
`_warm_faker_pools` builds each admitted faker pool once into the shared pool
cache; `concat_masked_chunks` is the strict byte-identity concatenation;
`aggregate_chunk_timings` rolls up per-chunk timing records. This is a substrate
module (route + streaming), not crypto/RI, so the bar is **77.91% of LOGIC
mutants**, not 100%.

The machine fields asserted are the ones each function actually decides on: the
byte-parity output (secret-keyed vs job_seed), the compatibility verdict CODE
(`chunked_table_unknown`, `chunked_generate_unsupported`,
`strategy_not_chunk_safe`, `chunked_strategy_conditions_unmet`), the error
`.path`, the offending column/table NAMES carried in each message (data, not
prose), the coded concat error (`chunked_schema_mismatch`) with its column and
conflicting-type names, the aggregated timing values, and the pool-cache state
the warmer's contract specifies (entries built + the per-chunk adapter hitting
the warmed pool). Free-text explanatory prose inside a message is the
`.message`-class analog and is left EQUIVALENT when the code and data around it
are pinned.

## Numbers

**TRUE score: 399/488 = 81.76% LOGIC, above the 77.91% bar.** IMPORTANT (tq-findings
#16): mutmut's in-process run REPORTED only 306/488 = 62.70%, because its
coverage-based per-mutant test selection did not associate this sweep's new test
file -- it ran the OLD tests against the new-test-covered mutants, they passed, and
mutmut reported them "survived" (false-survived). A standalone re-adjudication of
all 182 survived-bucket mutants (full selection, one fresh pytest each) found **92
actually killed / 90 still surviving** -> true 399/488 = 81.76%. So the new oracles
DO their job (confirmed e.g. `aggregate_chunk_timings__mutmut_8` killed standalone
while mutmut left it survived); the tool's trust of mutmut's "survived" undercounted
them. The per-function killed/equivalent breakdown below is the authoring estimate
(~93 killed), which the empirical re-adjudication (92) confirms within one. The 90
true survivors = the EQUIVALENT class (message prose / genuine unreachables) + the
residual killable-needing-fixtures set; NOT individually re-triaged post-re-adjudication
(an above-bar residual, honest, same class as capacity). The main loop re-grades and
reconciles exact counts.

Per function:

| Function | Survivors | Killed | Equivalent | Residual |
|---|---|---|---|---|
| `_conditional_admission_failures` | 29 | 7 | 22 | 0 |
| `_warm_faker_pools` | 38 | 30 | 8 | 0 |
| `aggregate_chunk_timings` | 7 | 7 | 0 | 0 |
| `check_chunked_compatibility` | 46 | 32 | 14 | 0 |
| `concat_masked_chunks` | 10 | 4 | 6 | 0 |
| `run_mask_pipeline_chunked` | 51 | 13 | 7 | 31 |
| **Total** | **181** | **93** | **57** | **31** |

Estimated LOGIC score after this sweep: 62.91% baseline + 93 new kills lands
around 82% (re-grade confirms exact), above the 77.91% substrate bar.

## Tests

New oracle file `tests/unit/execution/test_chunked_mutation_kills.py` (24 tests).
All 24 green on unmutated code together with the existing
`tests/unit/execution/test_chunked.py` and
`tests/unit/execution/test_auto_chunk_routing.py`; ruff format + check clean. The
tests drive the leaf helpers directly (raw config dicts for
`check_chunked_compatibility`, hand-built `StrategyTimingRecord`s for
`aggregate_chunk_timings`, compiled plans + a real `PoolCache`/adapter for
`_warm_faker_pools`) and assert hardcoded outcomes; `run_mask_pipeline_chunked`
is exercised end-to-end against the full-frame `run_pipeline` for byte parity.

## LOGIC killed (93)

### `_conditional_admission_failures` (7 of 29)

Direct calls to the helper. A faker column with a TOP-LEVEL `pool_size`
(provider_config empty) returns no failures -- kills the `col_entry.get("pool_size")`
read mutations (nulled key `get(None)`, renamed `"XXpool_sizeXX"` / `"POOL_SIZE"`)
that would wrongly flag the top-level declaration as missing (mut 31, 32, 33). A
deterministic faker with no namespace returns exactly the namespace failure --
asserting the list has one non-None entry containing the lowercase phrase kills
`append(None)` (the list becomes `[None]`, and the substring check raises) and the
upper-cased literal (the lowercase phrase disappears) (mut 24, 26). The same shape
for a categorical missing its categories kills mut 76, 78.

### `_warm_faker_pools` (30 of 38)

Cache-state oracles over a compiled plan and a real `PoolCache`; this is the
function's documented contract ("build each admitted faker column's pool once
into pool_cache").

- Fresh warm builds exactly one entry: kills every mutation that builds NOTHING
  -- `table_seed = None`, `name == table` -> `!=`, `is None` -> `is not None`, the
  strategy-skip flips (`!=`->`==`, `or`->`and`, `"faker"` rewrites, provider
  `is None`->`is not None`), and the already-cached guard inverted to
  `is None` (mut 1, 5, 6, 10, 11, 12, 13, 46).
- A non-faker column BEFORE the faker still leaves one entry: kills the
  strategy-skip `continue`->`break` (mut 14).
- Warming a table absent from the plan is a no-op: kills the `next(...)`
  no-default mutation (StopIteration) (mut 4).
- The warmed pool is HIT by a subsequent adapter run (one entry, hits >= 1):
  kills the `build(...)` identity params that store the pool under the wrong
  identity so the adapter misses and rebuilds -- locale read nulled/renamed,
  locale kept in `build_config`, and namespace/locale set to None or dropped in
  the build call (mut 22, 23, 24, 25, 30, 31, 52, 54, 58, 60).
- With the pool already cached (built by a prior adapter run), a monkeypatched
  build counter proves the warmer's dedup guard skips the rebuild: kills the
  `identity_for(...)` param mutations (wrong dedup identity -> false miss ->
  rebuild), `identity = None`, and `pool_cache.get(None)` (mut 32, 33, 34, 36,
  37, 38, 42, 43, 44, 45).

### `aggregate_chunk_timings` (7 of 7)

Hand-built timing records. `(hash, v)` split across two chunks asserts elapsed
SUMS (2.0 + 3.0) and peak takes the MAX (max(5, 8)); a `(redact, w)` record with
peak 0 pins the zero-init. Kills the elapsed init `0.0`->`1.0`, peak init
`0`->`1`, `+=`->`=`, `+=`->`-=`, `peak = None`, and the emitted-record
`elapsed_ms`/`peak_memory_delta_kb` set to None (mut 8, 10, 11, 12, 13, 21, 22).

### `check_chunked_compatibility` (32 of 46)

Direct calls with raw config dicts (so nameless columns and non-dict entries,
unreachable through PipelineConfig validation, are exercised).

- Unknown table in a two-table config: asserts code `chunked_table_unknown`,
  `path == "tables.nope"`, and both KNOWN table names in the message. Kills
  `known = None`, the nulled/renamed `t.get("name","?")` reads (names vanish or
  become "?"), the no-default `.get("?")` (`sorted()` of Nones raises), `path=None`,
  and `message=None` (mut 16, 18, 20, 22, 23, 26, 27).
- Generate table: asserts code + `path == "tables.synth"` + non-None message.
  Kills `path=None` and `message=None` (mut 37, 38).
- A non-safe column AFTER a safe column / a non-dict entry / an admitted
  conditional column still rejects: kills all three loop `continue`->`break`
  flips (they would stop before the shuffle and admit the job) (mut 62, 70, 84).
- Two named non-safe columns: both NAMES appear comma-joined with the error path.
  Kills the offending-name mutations (str(None), nulled/renamed `.get`) and the
  `", "` list separator (asserted as the exact joined substring) plus `path=None`
  (mut 86, 87, 89, 91, 92, 97, 99). A nameless non-safe column pins the "?"
  fallback, killing the default-only name mutations (mut 88, 90, 93).
- A named conditional column with an unmet condition names it (with path); a
  nameless one pins "?"; two such columns assert the `"; "` reason separator.
  Kills the conditions-unmet name mutations, `path=None`, and the separator (mut
  75, 76, 78, 80, 81, 77, 79, 82, 111, 115).

### `concat_masked_chunks` (4 of 10)

- Column-name disagreement raises `chunked_schema_mismatch` naming both columns:
  kills `message=None` and the dropped-message-kwarg mutation (mut 6, 8).
- Non-null type disagreement (string vs large_string) lists both TYPE names
  comma-joined: kills the `str(None)` per-type mutation and the `", "` separator
  (mut 32, 33).

### `run_mask_pipeline_chunked` (13 of 51)

A keyed config (`mask_secret_ref` -> a 64-hex env secret) with a `hash` column:
the chunked output must byte-match the full-frame keyed run, and a guard test
confirms the secret genuinely changes the bytes versus a seed-only run. Every
mask_secret_ref resolution mutation and the per-chunk `adapter.run(key_provider=)`
being nulled/dropped drop the secret, so the chunk masks off `job_seed` (the
pre-GA fallback in `require_mask_key`) and diverges from the secret-keyed
full-frame output. Kills the `key_provider is None` guard flip, `_ref=None`, the
`or {}`->`and {}` and nulled/renamed config-key reads, `key_provider=None`,
`key_provider_from_ref(None)`, and the per-chunk key_provider None/dropped (mut
36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 125, 133).

## EQUIVALENT (57)

### Message-prose class (code + data pinned independently)

Wrapping a message literal in `XX...XX` or upper-casing it changes only
explanatory text; the verdict code, error `.path`, and the column/table names are
asserted on their own and are unchanged. Where a lowercase keyword survives inside
a fragment adjacent to the mutated literal, the substring assertion cannot
distinguish it -- the direct analog of the planner ledger's `.message` class.

| Function | Mutants | Note |
|---|---|---|
| `_conditional_admission_failures` | 16, 17, 18, 19 | deterministic-condition prose |
| `_conditional_admission_failures` | 25 | namespace XX-wrap (phrase intact) |
| `_conditional_admission_failures` | 40, 41, 42, 43, 44, 45, 46, 47 | pool_size prose (lowercase `pool_size` survives in the adjacent `provider_config.pool_size` fragment) |
| `_conditional_admission_failures` | 55, 57, 58 | cardinality_mode prose |
| `_conditional_admission_failures` | 66, 68, 69, 70, 71 | from_profile prose |
| `_conditional_admission_failures` | 77 | categories XX-wrap (phrase intact) |
| `check_chunked_compatibility` | 44, 45, 46, 47 | generate-message prose |
| `check_chunked_compatibility` | 83 | `str(strategy)` -> `str(None)`; the words `faker`/`categorical` appear in the fixed conditions_unmet message regardless |
| `check_chunked_compatibility` | 107 | chunk-safe-strategies enumeration separator (message reference list) |
| `check_chunked_compatibility` | 113 | inner failures-join separator (both operands are condition prose) |
| `check_chunked_compatibility` | 122, 123, 124, 125 | conditions_unmet fixed-message prose |
| `concat_masked_chunks` | 11 | column-names trailing-sentence prose |
| `concat_masked_chunks` | 34, 35, 36, 37, 38 | type-mismatch trailing-sentence prose |

### Default-only name reads (name key present in every asserted case)

`get("name", None)` / `get("name")` / `get("name", "XX?XX")` differ from the
original only when the `name` key is ABSENT; every place these run in the asserted
paths carries a real name, so the value is unchanged.

| Function | Mutants |
|---|---|
| `check_chunked_compatibility` (known-table list) | 19, 21, 24 |

### Genuine no-ops / unreachable

| Mutants | Function | Why equivalent |
|---|---|---|
| 9 | `_warm_faker_pools` | `!= "faker" or provider is None` -> `and`; no non-faker column seed carries a provider and no admitted faker seed has `provider is None`, so the two operands never disagree -- the branch selects identically |
| 16, 17, 18 | `_warm_faker_pools` | the `raise ValueError(...)` fires only for a faker column reaching pre-warm with `pool_size is None`, which admission guarantees cannot happen; the branch is unreachable, and the mutations only touch its message anyway |
| 26, 53, 59 | `_warm_faker_pools` | `build_config`/`config` set to None; for a provider_config holding only `pool_size` + `locale` (both excluded) `build_config` is `{}`, and `_config_hash(None) == _config_hash({})`, so the pool identity is unchanged |
| 47 | `_warm_faker_pools` | already-cached `continue`->`break` only diverges when a later column still needs building after an earlier cache hit; `run_mask_pipeline_chunked` always warms a FRESH `PoolCache`, so the already-cached branch is never taken in production |
| 29 | `run_mask_pipeline_chunked` | `compile_plan(decoy_engine_version=None)`; the engine version does not enter masked-value derivation and the profile is supplied, so output is byte-identical |
| 30, 34, 35 | `run_mask_pipeline_chunked` | `no_profile` None/dropped/False; the profile is supplied to `compile_plan`, so it is not re-derived and output is byte-identical |
| 49 | `run_mask_pipeline_chunked` | the up-front `require_mask_key(plan, None)` result feeds only the vault-key guard, which is unused without a `vault_writer`; the per-chunk adapter still receives the real key_provider |
| 70 | `run_mask_pipeline_chunked` | `RelationshipGraph(ordering=None)`; the graph is empty (`edges=()`) for a childless single-table mask, so ordering is unused |
| 94 | `run_mask_pipeline_chunked` | `_warm_faker_pools(table=None)`; warming is a build-once optimization, and the handler rebuilds the pool lazily on a cache miss, so output is byte-identical |

## Residual: killable, deferred (31)

These `run_mask_pipeline_chunked` mutants are genuine kills but need fixtures this
sweep did not build (FK child edges with declared dtypes / passthrough columns,
a vault_writer, unconfigured-column projection, or empty-input keyed profiling).
They are listed honestly as survivors rather than padded into the equivalent
class; the killed set already clears the bar without them.

| Group | Mutants | What a kill needs |
|---|---|---|
| empty-input profile args | 14, 15, 22 | a keyed zero-chunk job where `empty_input_profile(table=None)` / `engine_version=None` breaks the gate profile |
| projection policy | 59, 60 | a config with an unconfigured column so `resolve_unconfigured_column_policy` output matters |
| namespace registry | 63 | a config where `namespace_registry=None` changes masked output (existing parity tolerates it) |
| FK passthrough guard | 73, 75, 85, 86, 87, 88, 105, 106, 107, 108, 109, 110 | an FK child config with a passthrough FK column through the pandas ingestion path |
| FK declared-dtype guard | 78, 80, 111, 112, 113, 114, 115, 116 | an FK child config with declared key dtypes and a misdeclared chunk |
| adapter.run plumbing | 121, 123, 124, 129, 132 | pool_cache/namespace_registry/projection-policy None or dropped observed via a config where each is load-bearing |

## Candidate findings

None. No mutation exposed a wrong admission verdict, a wrong error code/path, a
wrong aggregated timing, or a chunked/full-frame byte divergence that current
behavior does not already intend.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to
`src/decoy_engine/execution/_chunked.py` and the test selection to
`tests/unit/execution/test_chunked_mutation_kills.py`,
`tests/unit/execution/test_chunked.py`, and
`tests/unit/execution/test_auto_chunk_routing.py`, then
`rm -rf mutants && python -m mutmut run`. `source_paths` stays at the package root.
