Status: record

# P3-T1 pool identity + C1 eligibility: measurement, fill, and adjudication

- **Plan:** `docs/plans/2026-08-30-phase3-c1-test-plan.md`, batch P3-T1 (section 4) and the
  method in section 3 (binding by reference from `docs/plans/2026-08-29-native-efficiency-test-
  plan.md`).
- **Scope:** `src/decoy_engine/execution/native/_phase3_eligibility.py` (229 LOC, the C1 admission
  layer + the eight coded faker-column rejection reasons), `src/decoy_engine/generation/pool/
  _identity.py` (58 LOC, `resolve_faker_pool_identity`), and the JC-5 seam of `src/decoy_engine/
  execution/native/_requirements.py` (`NATIVE_POOL_STRATEGIES`, `_PARTITION_INDEPENDENT_
  CARDINALITY_MODES`, `faker_pool_precondition_met`, `native_pool_rejection`, and the four-line
  dispatch change inside `requirements_for`). `_requirements.py`'s PRE-EXISTING code was already
  fully graded in Phase 2's T4 batch (`docs/plans/native-testing-T4-eligibility.md`, `_requirements.py`:
  279 mutants, 98% branch coverage) -- this batch does not re-grade it.
- **Branch:** `feat/native-phase3`, worktree `.claude/worktrees/native-phase3-build`.
- **Harness reused, not re-derived:** `scripts/native-testing/python_mutation_pilot.py` (P3-T0),
  unchanged. Both graded modules are faker-only-lane (no compiled-companion dependency): neither
  imports `decoy_engine_native`, and their covering tests carry no `@_NEEDS_COMPANION` marker.

## Method: measure first

Ran branch coverage and the mutation pilot against each module's existing covering test file
before writing anything, per section 3 rule 1 (measure-before-adding). `_phase3_eligibility.py`
already had `tests/native/test_phase3_eligibility.py` (21 tests) from the Phase 3 build; `_identity.py`
had no dedicated test file (only indirect coverage via the three callers' own tests), so its
BEFORE state is the coverage/mutation of the CALLERS' existing convergence checks, which the plan
explicitly says is insufficient (a mutant inside the resolver changes all three callers identically
and survives caller-convergence alone).

## BEFORE: `_phase3_eligibility.py`

```
coverage run --branch -m pytest -q tests/native/test_phase3_eligibility.py
coverage report --include=*/_phase3_eligibility.py -m

python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_phase3_eligibility.py \
  --tests tests/native/test_phase3_eligibility.py --timeout 30
```

Reproduces the P3-T0 harness record's numbers exactly: 184 mutants, 154 killed, 30 survived, 0
true-timeout. Two named survivors carried forward from P3-T0:

- `x_phase3_c1_eligibility__mutmut_9`: `native_route_eligibility(config, table=table, profile=
  profile)` mutated to `profile=None` -- the caller-supplied `profile` argument dropped on the
  floor (this batch's named carry-forward, item 5 of the plan's candidate-gap list).
- The other 29 were not adjudicated by P3-T0 (out of scope for a harness batch); read in full
  below.

## Fill: gaps closed

Read every one of the 30 BEFORE survivors' mutant bodies directly (`mutmut show <name>`, not
inference from the diff alone -- the T4 record's own method note names this as necessary). Sorted
into: real gaps (closed with a test), equivalent (documented below), and one unreachable-by-
contract. Also closed one demonstrated correctness bug the reading surfaced (see Production
changes).

New tests, all in `tests/native/test_phase3_eligibility.py` unless noted:

1. **`test_dropping_the_profile_argument_silently_admits_an_unresolvable_hash_column`** -- the
   carried-forward profile survivor (mutants 9 and 12). `native_route_eligibility`'s
   `hash_config_rejection` DEFERS a hash column's input-type gate when `profile is None` (returns
   `None`, undecided, not admitted-by-default); passing a real profile with an unresolvable dtype
   (`"mixed"`) turns that deferral into `mixed_object_not_native:<name>`. A table with an
   admissible faker column plus this hash column is admitted without a profile and rejected with
   one -- the exact scenario P3-T0 found untested.
2. **`test_a_non_faker_column_does_not_hide_a_later_unsafe_faker_column`** -- a `continue`-> `break`
   mutant (38) at the "not faker" filter would drop every later column's own rejection. A hash
   column followed by a non-deterministic faker column proves the scan does not short-circuit.
3. **`test_composite_provider_faker_column_reports_only_the_base_composite_reason`** and
   **`test_composite_provider_faker_column_does_not_hide_a_later_unsafe_faker_column`** -- mutants
   39/43/44/45 all replace the composite-provider lookup key (`col.get("provider")` -> `col.get(
   None)` / `col.get("XXproviderXX")` / `col.get("PROVIDER")`), which makes `provider_is_composite`
   see `None` instead of the real provider and so never exclude a genuinely composite-provider
   faker column from Phase 3's own per-column check. The registry's existing totality test only
   checked that every reason STARTS WITH a recognized prefix, and `provider_reject_large` (the
   spurious second reason this bug adds) is itself a recognized prefix -- too permissive to catch
   an extra, wrong reason alongside the correct one. Pinning the EXACT reasons tuple for a real
   composite provider (`composite_address`) closes all four mutants at once. Mutant 46 (the same
   `continue`->`break` short-circuit pattern, on the composite-exclusion branch) needed a second
   faker column after the composite one to prove it is not silently dropped.
4. **`test_duplicate_name_across_faker_and_another_no_kernel_strategy_keeps_both_verdicts`** --
   mutant 14 (`and`->`or` in `_is_reclassified_faker_kernel_rejection`'s final return). Every
   `no_native_kernel:X:faker` in `base.rejections` happens to coincide with `X` being in
   `faker_names` under every EXISTING config, since composite-provider columns take a different
   base-rejection code entirely -- so `strategy == "faker"` alone was indistinguishable from `... and
   name in faker_names` in every prior test. The real divergence needs a DUPLICATE column name
   across two DIFFERENT strategies: `X` as an admissible faker column (in `faker_names`) and `X`
   again as a `date_shift` column (an unrelated `no_native_kernel:X:date_shift` entry). The `or`
   mutant would strip the unrelated date_shift rejection too, because it only checks `name in
   faker_names`, not that the SAME reason is actually about faker.
5. **`test_colon_bearing_column_name_still_admits_cleanly`** -- mutant 12 (`partition`-> `rpartition`
   in the same dedup helper) surfaced a REAL bug on direct reproduction, not just a mutant: see
   Production changes.
6. **`test_allow_collisions_with_explicit_reuse_mode_admits_no_conflict`** -- mutants 18/19 corrupt
   the `!= "reuse"` string literal (`"XXreuseXX"`/`"REUSE"`). No existing test declared `allow_
   collisions: true` alongside an EXPLICIT `cardinality_mode: reuse` (only the coercion-from-
   omitted-mode and the conflicting-mode cases existed); that compatible-but-explicit combination is
   the one case that distinguishes the real literal from a corrupted one.
7. **`test_empty_string_provider_rejects_with_repr_formatted_reason`** -- mutant 55 (`or`->`and` in
   `not isinstance(provider, str) or not provider`). An empty string IS a `str`, so `isinstance`
   alone doesn't reject it; only the `or not provider` half does, at the early guard (which formats
   the reason with `{provider!r}`), not at `classify_provider`'s later `reject_large` branch (which
   formats with plain `{provider}`). The `and` mutant skips the early guard for `provider=""` and
   falls through to the later branch, producing a DIFFERENT reason string for the same coded
   category -- pinning the exact `''`-repr'd reason catches it.
8. **`test_table_not_found_in_config_is_vacuously_admitted`** and **`test_conflicting_pool_size_
   locations_surface_the_compiler_own_code`** -- pure coverage-closing (branch coverage was 94%
   before these two): every existing config named a table that existed in the config's `tables`
   list, so `table_cfg is None` (and `_find_table`'s no-match loop iteration + final `return None`)
   was never exercised; and no existing faker column declared conflicting `pool_size` in both
   `pool_size` and `provider_config.pool_size`, so `resolve_pool_size`'s `PlanCompileError` path was
   never reached from this predicate.
9. **Property sweep** (`test_jc5_admitted_set_is_exactly_deterministic_source_keyed_partition_
   independent`, plan candidate gap 1): `hypothesis`-driven sweep over `deterministic in {True,
   False, None}` x `cardinality_mode in {None, reuse, unique, match_source_cardinality, scale_
   source_cardinality}` x `namespace present/absent` x `pool_size present/absent` x `allow_
   collisions in {True, False}` (300 examples), for a fixed C1-allowlisted provider so only the
   JC-5 axes vary. Asserts `result.admitted` against an independently-reconstructed reference that
   mirrors `_faker_column_rejection`'s own check order (conflict, deterministic, cardinality mode,
   namespace, pool_size) and additionally asserts `result.admitted == (result.reasons == ())` (the
   admitted-iff-no-reasons invariant) on every generated point. Demonstrated non-vacuous by the
   mutation run itself: every one of the individually-named example tests above corresponds to one
   grid cell this sweep also covers, and the sweep independently reproduces the same kills (see
   AFTER numbers).

`_requirements.py`'s JC-5 seam got its OWN new file, `tests/native/test_requirements_jc5.py` (13
tests), unit-testing `NATIVE_POOL_STRATEGIES`, `faker_pool_precondition_met` (every one of its four
axes, plus the non-`ColumnSeed` rejection already covered by `test_dispatch_faker.py`), and
`native_pool_rejection` (the strategy-set gate, the precondition-failure code, and the admit path)
directly against a hand-built `ColumnSeed`, without a compiled companion or a full plan compile --
the same faker-only-lane pattern this module's sibling uses one layer up.

`_identity.py` got its own new file, `tests/unit/generation/pool/test_identity.py` (9 tests): see
the Identity section below.

## Production changes

**One:** `_is_reclassified_faker_kernel_rejection`'s `rest.partition(":")` -> `rest.rpartition(":")`
in `src/decoy_engine/execution/native/_phase3_eligibility.py`. Reading mutant 12
(`partition`->`rpartition`) against the actual CURRENT (unmutated) behavior for a column name
containing a colon showed the mutant was not equivalent -- it was the FIX. `ColumnConfig.name: str`
carries no charset restriction, so a real column name can contain a colon (e.g. a source system's
namespace-prefixed field). Before the fix, a column named `"A:B"` that satisfied every JC-5 axis
was incorrectly REJECTED: `no_native_kernel:A:B:faker`'s ambiguous colon-delimited encoding was
parsed left-to-right, splitting `"A:B:faker"` into `name="A"`, `strategy="B:faker"`, so `strategy ==
"faker"` was false and the base predicate's rejection was never stripped. Splitting from the RIGHT
correctly isolates the strategy token (never itself colon-bearing) regardless of what the name
contains. Verified directly:

```
$ .venv/bin/python -c "
from decoy_engine.execution.native._phase3_eligibility import phase3_c1_eligibility
config = {'tables': [{'name': 't', 'columns': [
    {'name': 'A:B', 'strategy': 'faker', 'provider': 'person_first_name',
     'deterministic': True, 'namespace': 'ns', 'pool_size': 100},
]}]}
print(phase3_c1_eligibility(config, table='t'))
"
# before: Phase3Eligibility(admitted=False, reasons=('no_native_kernel:A:B:faker',))
# after:  Phase3Eligibility(admitted=True, reasons=())
```

`test_colon_bearing_column_name_still_admits_cleanly` pins the corrected behavior and kills mutant
12 (a `partition`<->`rpartition` flip is now caught either direction).

## AFTER: `_phase3_eligibility.py`

```
coverage run --branch -m pytest -q tests/native/test_phase3_eligibility.py
coverage report --include=*/_phase3_eligibility.py -m
# 86 stmts, 42 branches, 98% cover, missing: 107 (see unreachable-by-contract below)

python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_phase3_eligibility.py \
  --tests tests/native/test_phase3_eligibility.py --timeout 30 --readjudicate-killed
```

```
===== CORRECTED MUTATION TALLY =====
module:            ['src/decoy_engine/execution/native/_phase3_eligibility.py']
total mutants:     184
mutmut raw:        {'killed': 167, 'survived': 17}
killed:            167
survived:          17
true-timeout:      0
LOGIC score:       90.76%  (167/184)
```

### Five-field adjudication

| Field | Count |
|---|---:|
| (a) Branch coverage | 98% |
| (b) Killed | 167 |
| (c) Equivalent (reason below) | 16 |
| (d) Unreachable-by-contract | 1 |
| (e) Tool-excluded | 0 |

**Equivalent mutants (16), grouped by shared reason:**

- **Default-argument irrelevance under an `or ()` fallback (4):** `phase3_c1_eligibility` mutants
  19/21 (`table_cfg.get("columns", ())` -> `.get("columns", None)` / `.get("columns", )`) and
  `_find_table` mutants 3/5 (the same pattern on `config.get("tables", ())`). The default value
  only matters when the key is absent, and every call site immediately does `... or ()`, which
  coalesces `None` to `()` identically to the original default -- the two forms are behaviorally
  indistinguishable for every input this predicate can construct.
- **`bool()`-wrapped default irrelevance (6):** `_faker_column_rejection` mutants 4/6 (`col.get(
  "allow_collisions", False)` -> `.get(..., None)` / `.get(..., )`), 24/26 (same pattern on
  `"deterministic"`), 73/75 (same pattern on `"vault"`). Each read is immediately wrapped in
  `bool(...)`, and `bool(None) == bool(False) == False`, so the two default values are
  indistinguishable through this wrapper for every reachable input.
- **Discarded-diagnostic-field irrelevance (4):** `phase3_c1_eligibility` mutant 58 (`table=table`
  -> `table=None` in the call to `_faker_column_rejection`) and that same function's own mutants
  45/46 (`table_name=table`/`col_name=name` -> `None` in the call to `resolve_pool_size`).
  `table`/`table_name`/`col_name` are used ONLY to format `PlanCompileError.path` (a diagnostic
  dotted-path string); `_faker_column_rejection`'s except-handler reads only `exc.code` to build the
  returned reason (`f"{exc.code}:{name}"`), never `exc.path`. The argument's value never reaches
  anything the admission verdict or the coded reason depends on.
- **Same-singleton-registry irrelevance (2):** `_faker_column_rejection` mutants 60/63 (`registry=
  registry` -> `registry=None` in the call to `classify_provider`). `classify_provider`'s own
  `registry` parameter defaults to `get_default_registry()`, and `get_default_registry()` "two
  calls return the same object" (its own docstring) -- `phase3_c1_eligibility` never accepts a
  registry override from its caller, so the value it threads through IS always that same singleton;
  passing `None` and letting `classify_provider` re-resolve it lands on the identical object.

**Unreachable-by-contract (1):** `phase3_c1_eligibility` mutant 25 (`continue`->`break` at `if not
isinstance(col, dict)`). This guard can never fire through the function's only call path: `base =
native_route_eligibility(config, table=table, profile=profile)` runs FIRST, over the identical
`table_cfg.get("columns")` list, and its own column loop (`_plan.py::native_route_eligibility`) has
no such guard -- `col.get(...)` on a non-dict entry raises `AttributeError` there before this
function's own loop is ever reached. Confirmed by direct reproduction (a config with a bare string
column entry crashes inside `native_route_eligibility`, not inside `phase3_c1_eligibility`).
Line 107 (the guarded `continue` itself) is the sole branch-coverage gap remaining at 98%.

## Identity: `_identity.py`

```
coverage run --branch -m pytest -q tests/unit/generation/pool/test_identity.py
coverage report --include=*/_identity.py -m
# 13 stmts, 0 branches (straight-line function), 100% cover

python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/generation/pool/_identity.py \
  --tests tests/unit/generation/pool/test_identity.py --timeout 30 --readjudicate-killed
```

```
===== CORRECTED MUTATION TALLY =====
module:            ['src/decoy_engine/generation/pool/_identity.py']
total mutants:     26
mutmut raw:        {'killed': 26}
killed:            26
survived:          0
true-timeout:      0
```

### Five-field adjudication

| Field | Count |
|---|---:|
| (a) Branch coverage | 100% |
| (b) Killed | 26 |
| (c) Equivalent | 0 |
| (d) Unreachable-by-contract | 0 |
| (e) Tool-excluded | 0 |

### Independent-reconstruction grading, not caller convergence

`resolve_faker_pool_identity` has three real callers (the oracle handler, the native chunked
route, `_warm_faker_pools`); comparing their outputs against each other proves only that they
agree, and a mutant INSIDE the resolver would move all three identically and survive that
convergence check. `tests/unit/generation/pool/test_identity.py` never calls any of the three
callers: it reconstructs `pool_size`/`locale`/`build_config` by hand (a rule restated directly in
the test, not a call into the resolver or into `resolve_runtime_pool_size`), feeds them straight to
`PoolBuilder.identity_for` -- the shared primitive the resolver itself calls, not the resolver --
and asserts the resolver's actual four-tuple return matches.

Every determinant `identity_for` folds in is covered, each varied independently while holding the
rest fixed, proving each one is real (the resolver output changes) and correctly forwarded (it
still matches the independent reconstruction):

- **provider** -- two allowlisted C1 providers, everything else identical; `identity[0]` (the
  identity tuple's own provider slot) is pinned directly per provider, so a mutant that hardcodes,
  drops, or swaps the provider argument cannot hide behind three callers passing the same value.
- **namespace**
- **job_seed**
- **locale** -- additionally pinned as excluded from `build_config`'s config-hash input (locale is
  a pool-BUILD knob, not a provider-method kwarg).
- **pool_size** -- additionally pinned as excluded from `build_config`, and that `plan_pool_size`
  wins over a raw-config fallback when both are present.
- **build_config's hash input** -- two configs differing only in an unrelated key produce different
  identities.
- The raw-config `pool_size` fallback path (only reachable for a hand-built `ColumnSeed` that
  bypasses `compile_plan`) and the shared-default fallback when neither `plan_pool_size` nor a raw
  config value is present, both pinned against `DEFAULT_POOL_SIZE`.

No survivor. Zero adjudication needed for this module.

## `_requirements.py` JC-5 seam

`git diff origin/main..HEAD -- src/decoy_engine/execution/native/_requirements.py` bounds the seam
to `NATIVE_POOL_STRATEGIES`, `_PARTITION_INDEPENDENT_CARDINALITY_MODES`, `faker_pool_precondition_
met`, `native_pool_rejection`, and a four-line dispatch change inside `requirements_for` (route a
pool strategy through `native_pool_rejection`, everything else unchanged through `native_kernel_
rejection`). Phase 2's T4 batch already fully graded every OTHER line of this 496-line file (279
mutants, 98% branch coverage, `docs/plans/native-testing-T4-eligibility.md`); this batch does not
re-run that.

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_requirements.py \
  --tests tests/native/test_requirements_jc5.py --timeout 30
```

`NATIVE_POOL_STRATEGIES` and `faker_pool_precondition_met` are pure config predicates over a
`ColumnSeed` (no compiled kernel, no I/O); `tests/native/test_requirements_jc5.py` (13 new tests)
unit-tests every branch directly against a hand-built `ColumnSeed`, the same faker-only-lane
pattern as `_phase3_eligibility.py`, without the companion or a full plan compile. Running the
mutation pilot with `only_mutate` scoped to this file necessarily generates mutants across every
OTHER (already-graded) line too, since mutmut scopes by file, not line range: 300 total mutants,
13 trusted-killed on the fast in-process pass, 287 flagged suspect (no coverage from this batch's
narrow test selection) and sent to standalone readjudication.

Grepping the generated `mutants/` source for `^def x_faker_pool_precondition_met__mutmut_` and
`^def x_native_pool_rejection__mutmut_` counts exactly 10 + 3 = 13 mutants for these two
functions -- the EXACT count of the fast pass's trusted-killed bucket. Confirmed directly, not
just by count: grepping the readjudication log (worked through roughly three-quarters of the 287
suspects before being stopped, once the count match above held, to avoid grinding through
hundreds of already-out-of-scope pre-existing-code mutants for no benefit) for
`x_faker_pool_precondition_met` or `x_native_pool_rejection` returns zero matches -- neither
function's mutants ever entered the suspect/no-coverage queue, meaning all 13 were confidently
killed on the first pass by the 13 direct unit tests.

| Field | Count |
|---|---:|
| (a) Branch coverage (touched functions) | 100% (`faker_pool_precondition_met`, `native_pool_rejection`, `NATIVE_POOL_STRATEGIES` -- every branch hit by the 13 direct unit tests) |
| (b) Killed | 13 / 13 (all mutants generated for these two functions) |
| (c) Equivalent | 0 |
| (d) Unreachable-by-contract | 0 |
| (e) Tool-excluded | 0 |

**The four-line `requirements_for` dispatch branch itself** is not exercised by the unit test file
above (which never calls `requirements_for`), but is already load-bearing on two PRE-EXISTING
companion-independent integration tests in `tests/native/test_dispatch_faker.py`:
`test_deterministic_reuse_faker_admits_on_native_pool_route` (the admit side) and `test_non_c1_
faker_variant_stays_on_oracle` (six reject-side variants). Rather than a slow full companion-
required mutation run over that 51-test integration file (whose per-mutant cost the P3-T0 record
already measured at ~137s for the whole file), demonstrated by direct fault injection instead:

```
# Simulate a mutant that removes the pool-strategy dispatch entirely
# (NATIVE_POOL_STRATEGIES emptied -> every faker column falls through to
# native_kernel_rejection, which has no faker kernel):
$ PYTHONPATH=src .venv/bin/python -c "
import decoy_engine.execution.native._requirements as req
req.NATIVE_POOL_STRATEGIES = frozenset()
import pytest
raise SystemExit(pytest.main(['-q',
    'tests/native/test_dispatch_faker.py::test_deterministic_reuse_faker_admits_on_native_pool_route']))
"
# 1 failed: evidence.native_admitted is False (expected True)

# Simulate a mutant that removes the precondition check inside
# native_pool_rejection (always admits):
$ PYTHONPATH=src .venv/bin/python -c "
import decoy_engine.execution.native._requirements as req
req.native_pool_rejection = lambda node, name, strategy: None
import pytest
raise SystemExit(pytest.main(['-q',
    'tests/native/test_dispatch_faker.py::test_non_c1_faker_variant_stays_on_oracle']))
"
# 6 failed: decision.native_admitted is True (expected False), all six variants
```

Both faults are caught immediately by pre-existing tests. The dispatch branch is graded reasoned-
plus-fault-injection, not a raw mutation score, matching the T1-rust-core record's own precedent
for a case where a full mutation pass on the covering suite is disproportionate to a four-line
change already load-bearing on existing tests.

## Property tests added, and evidence each is non-vacuous

- **JC-5 admitted-set sweep** (`test_jc5_admitted_set_is_exactly_deterministic_source_keyed_
  partition_independent`): every named example test above (profile-drop, composite-provider,
  duplicate-name, colon-in-name, explicit-reuse, empty-string-provider) is one grid cell this
  property sweep also visits with the same independently-reconstructed reference; the sweep
  reproduces the identical kills the example tests demonstrate, over 300 randomized points instead
  of the hand-picked ones, and additionally the admitted-iff-no-reasons invariant on every point
  the examples don't individually check. Demonstrated able to fail: ran the same exhaustive 3x5x2x2x2
  grid (120 points) against a deliberately WRONG reference (the pool_size check inverted, `return not
  has_pool_size` instead of `return has_pool_size`) in place of `_expected_jc5_admitted`. That wrong
  reference disagrees with the real predicate on 16 of the 120 points -- proof the assertion form
  (`result.admitted is expected`) is sensitive to the reference actually matching the code, not a
  tautology that always passes regardless of what the reference says. Two other deliberately-wrong
  variants tried first (reordering the deterministic-vs-conflict checks; dropping the
  allow_collisions-forces-reuse coercion) produced zero disagreements -- both turned out to be
  genuinely inert reorderings given the conflict check's own short-circuit, not evidence the sweep is
  vacuous, but a reminder that a property test's non-vacuity has to be checked against a mutation
  that actually changes the final boolean, not any arbitrary wrong-looking rewrite.

## Gates

- `ruff check` on the four changed/new files: clean.
- `ruff format --check`: clean (one reformat applied to `test_phase3_eligibility.py` during
  authoring, reformatted before this record).
- `mypy`: BLOCKED in this worktree's venv, unrelated to this batch's diff -- see Blocked below.
- `pytest tests/native/test_phase3_eligibility.py tests/native/test_requirements_jc5.py tests/unit/
  generation/pool/test_identity.py -q`: all green.

## Blocked (environment, not this batch's diff)

`mypy` fails identically under both the repo-pinned CI version (2.1.0, installed for this check)
and this worktree's pre-installed 2.3.0, on a file BEFORE reaching any of this batch's changes:

```
.venv/lib/python3.13/site-packages/numpy/__init__.pyi:737: error: Type statement is only supported in Python 3.12 and greater  [syntax]
Found 1 error in 1 file (errors prevented further checking)
```

`pyproject.toml` pins `[tool.mypy] python_version = "3.10"` but carries no numpy version pin; this
worktree's `.venv` resolved numpy 2.5.0, whose bundled `.pyi` stubs use PEP 695 `type` statement
syntax (Python 3.12+ only), which mypy refuses to parse under a 3.10 target regardless of the mypy
version itself. Same category of drift as the mutmut 3.6.0->3.7.0 floor-pin drift the P3-T0 record
found (`pyproject.toml`'s dependency floors, not exact pins, so a fresh resolve months later can
pick up a newer release). Not a defect in this batch's changed files -- confirmed by reproducing the
identical failure on files this batch never touched -- and not fixed here: pinning numpy older or
bumping `python_version` is a repo-wide dependency decision outside a test-grading batch's scope.
Recorded rather than silently worked around.

## Bar

- Kill every mutant that changes an admission verdict, a coded rejection reason, or an identity
  determinant: MET for `_identity.py` (zero survivors) and for `_phase3_eligibility.py` (every
  survivor is equivalent or unreachable-by-contract, none changes a verdict/reason/determinant;
  see the adjudication table above) and for the `_requirements.py` JC-5 seam (see above).
  Zero unadjudicated survivors.
- Every property test demonstrably able to fail: yes (see Property tests section).
- No self-grading: `_identity.py` graded against an independent contract reconstruction (never the
  resolver's own output or the three callers' agreement); the JC-5 property sweep graded against a
  hand-written reference, not the predicate's own output.
- The P3-T0 carried-forward survivor (item 5 of the plan's candidate-gap list) is closed:
  `test_dropping_the_profile_argument_silently_admits_an_unresolvable_hash_column`.
