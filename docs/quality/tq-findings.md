# TQ findings log

Real code observations surfaced by the Test-Quality Program's oracle authoring
(distinct from mutation survivors, which are tracked in per-module ledgers).
These are potential source issues found while writing invariant tests. None were
fixed autonomously; each is pinned as observed behavior in the test suite and
listed here for a decision. Ordered by stakes.

## TQ-1 crown jewels (2026-07-25, branch `tq/crown-jewels`)

### 1. FK key route divergence: float vs Decimal of equal value (RI) -- RESOLVED (7e7be68)
`execution/_fk_keys.py`. `fk_key_value(12.5) == fk_key_value(Decimal("12.5"))` is
`True` (Python numeric tower), so the full-frame / sequential route (a plain-dict
`parent_map`) treats an equal-valued float parent key and Decimal child FK as ONE
key. But `fk_join_key` type-tags them apart (`\x00FLOAT:` vs `\x00DEC:`), so the
out-of-core route (string-token map) treats the SAME pair as a NON-match. Two
routes can disagree on whether a child row is an orphan for mixed
float64/decimal128 FK columns of equal value. RI is worst-blast-radius; confirm
whether a real schema can produce that column pairing, and if so, unify the two
routes' key identity. **FIXED in `7e7be68`** (fk_join_key encodes a fractional
float through its exact `Decimal(value)` expansion so float/Decimal fold to one
`\x00DEC:` token IFF Python `==` holds; dennis-SOUND). Regression sentries:
`test_fk_keys_invariants.py::test_fractional_float_and_equal_decimal_share_a_join_token`
and `::test_float_and_decimal_that_are_unequal_keep_distinct_join_tokens`, plus
an end-to-end route-parity case (fractional float parent vs Decimal child).

### 2. `hkdf_expand` does not validate negative length (crypto, minor)
`determinism/_hkdf.py`. A negative `length` is not rejected: `n = (length+31)//32`
is <= 0, the block loop never runs, and `b"".join([])[:length]` returns `b""`.
Silent empty output rather than the `ValueError` the over-max case raises. No
docstring contract for it and likely unreachable from real callers, but the
asymmetry (over-max raises, under-zero silently empties) deserves an explicit
guard. Not currently asserted either way in the suite.

### 3. `_compose([])` raises bare `IndexError` (DP, unreachable today)
`quality/dp_budget.py`. Composing zero certificates raises a bare `IndexError`
rather than a coded `DpBudgetError` or a zero-loss base case. Unreachable via the
public API today (`Schedule.row_count_name` is mandatory, so `query_count >= 1`).
Worth a base case if `Schedule` ever gains an optional row-count query. Pinned as
documented-unreachable in `test_dp_budget_invariants.py`.

### 4. `compute_lock_fingerprint` serialization has no escaping (DP, latent)
`quality/dp_provenance.py`. The fingerprint input is `"\n".join(f"{name}==
{version}")` with no escaping; a name/version containing `==` or a newline could
make two different distribution sets serialize identically. Not reachable via
`installed_distribution_set()` (PEP-503-canonical names only), but a latent
ambiguity in the function's contract for arbitrary callers.

## Step-4 sweep observations

### 5. `_shuffle` narrows `timestamp[ns] -> timestamp[us]` on datetime columns (latent)
`execution/_strategies/_shuffle.py`. The `to_numpy(dtype=object)` boxing (the Q13
object-dtype fix) turns datetime values into `datetime.datetime`, so the adapter
re-infers `timestamp[us]` for a `timestamp[ns]` source. Surfaced by the batch-2
dennis gate while grading `_shuffle` (the `dtype=None` mutant keeps `ns`, which is
why the mutant is killable, not equivalent). Lossy for sub-microsecond timestamps.
Pinned as current behavior by
`test_datetime_column_output_type_and_permutation_pinned`; not fixed here (a
TESTS-only batch). Worth a decision: preserve source resolution, or accept us.

### 6. `derived_aggregate` forward-reference silently yields 0 instead of failing closed (real, out-of-scope)
`transforms/derived_aggregate.py` + `config/_tables.py` + `plan/_checks_derived_aggregate.py`.
A generate-mode `derived_aggregate` column whose `column` names a sibling NOT yet
generated (forward reference, or an absent `column` key) is not caught: there is no
`derived_aggregate` branch in `_type_params_present`, and `check_derived_aggregate_refs`
short-circuits (`if source_col and ...`) when the ref is absent. It reaches
`generate_derived_aggregate_column`, where `generated.get(config.column, [])` returns
`[]` and the aggregate silently produces `0` for every row rather than a coded error.
Surfaced by the batch-6 dennis gate while grading the mask/mask-fallback defaults.
Silent-wrong-output, not fail-closed. Fix belongs in the plan-compile refs check /
`_type_params_present` (add the derived_aggregate branch), NOT in this tests-only
sweep. File as a separate product issue.

### 7. dtype=object output survivors are killable on an all-null non-object column (ledger-tightening, not a bug)
`transforms/text_mask.py`, `execution/_strategies/_nested.py` (and originally
`_text_redact.py`, now fixed). Several ledgers classified `pd.Series(col_values,
dtype=object) -> dtype=None`/dropped as EQUIVALENT with a "col_values is uniformly
str-or-null so object is inferred anyway" rationale. Batch-7 dennis showed that is
false for an ALL-NULL non-object column (e.g. all-null float64): without the
explicit `dtype=object` pandas infers `float64`, an observable output-dtype change
(the explicit object is a deliberate Arrow/concat-boundary contract). `_text_redact`
was tightened (mutmut_110/113 reclassified LOGIC, killed by
`test_all_null_float_column_output_is_object_dtype`). The equivalent
`_text_mask`(mutmut_125/128) and `_nested`(mutmut_163/166) survivors are the same
pattern -- redacted/masked VALUES are identical (all null), only the dtype differs;
gated as immaterial at the time. Tightening opportunity: add the same all-null test
to those two modules and reclassify. Not a product bug.

### 8. mutmut in-process runner misreports surviving mutants as timeout on substrate suites (harness limitation, not a product bug)
`execution/_planner.py` (and the Batch-A execution substrates generally). Grading
`_planner` with the standard focused-selection harness produced "459 mutants, 313
killed, 146 timeout, 0 survived" -- but sampling the ⏰ mutants showed pure
message-prose mutations (e.g. `reason = None`, a case-changed error string) that
cannot hang. Running one such mutant standalone
(`MUTANT_UNDER_TEST=...x__table_column_entries__mutmut_2 pytest test_execution_planner.py`)
completes in ~3s and the mutant SURVIVES (exit 0) -- so the 146 "timeouts" are 146
real survivors the harness is hiding, making the score false. The cause is mutmut's
per-mutant limit `(estimated_time_of_tests + timeout_constant) * timeout_multiplier`:
for these suites the per-test call-duration estimate is ~0, so the limit collapses,
and mutmut's in-process runner marks the survivor a timeout. Raising
`timeout_constant` to 15.0 (a ~225s wall / ~450s CPU per-mutant limit) did NOT change
the verdict -- the same 5 mutants still reported ⏰ -- so it is not a limit-tuning
issue but a runner/suite interaction. The substrate tier (biggest P0 section, Batch A)
therefore needs a different grading path (a runner that shells out to standalone
pytest per mutant, or an alternative mutation tool), NOT the in-process focused
harness that grades the strategy/transform tier cleanly. Logged, not worked around;
`_planner` and siblings are NOT falsely scored. The fast strategy/transform modules
are unaffected (their surviving runs finish well under the default limit).

### 9. mutmut does not mutate module-level constants -- constant-driven logic needs explicit per-value tests (methodology)
Surfaced by the `transforms/_fpe_checksum.py` batch-gate (2026-07-26). mutmut only
generates mutants INSIDE functions; a module-level constant like
`_EXACT_LENGTHS["gtin"] = frozenset({8, 12, 13, 14})` is copied verbatim into the
mutants tree and never mutated. So a suite can reach "LOGIC-100%" on mutmut's own
mutants while leaving a real blind spot: here GTIN was tested only at length 14,
so nothing pinned that 8/12/13 are legal or that the exact-length guard fires --
a dropped length would silently reject a valid GTIN with no failing test, and
mutmut would never flag it. Lesson for the sweep: for any module whose behavior is
driven by a module-level table/set/dict of constants (length sets, scheme maps,
threshold tables), add explicit tests that exercise EACH value, independent of the
mutation score. The mutation score is necessary, not sufficient, for
constant-driven code. (Remediated for `_fpe_checksum` by covering all four GTIN
lengths + an illegal-length fail-closed case.)

### 10. `apply_grouped_series` returns a fresh RangeIndex, not the source index (latent, out-of-scope)
`transforms/grouped_series.py`. `apply_grouped_series` builds its result from a
plain Python list into `pd.Series(result, ...)`, so the output carries a fresh
`RangeIndex` rather than the caller's `df.index`. If a caller ever passes a
non-default-indexed frame (filtered/reordered rows), downstream index-alignment
could misbehave (the same class as the `_text_redact`/`_orphan` index-alignment
oracles). Surfaced by the grouped_series batch-gate as an out-of-scope observation
(product source unchanged in this tests-only sweep). Not reproduced as a failure
on the current callers; flag for a decision on whether to align to `df.index`.

### 11. `transforms/date_shift.DateShiftStrategy` (V1 class) is dead/superseded legacy code (cleanup candidate)
`transforms/date_shift.py`. The active engine-v2 S9 date_shift is
`execution/_strategies/_date_shift.DateShiftStrategyHandler` (registered in the
strategy registry). The V1 `DateShiftStrategy` class in `transforms/date_shift.py`
(its `apply`, `_column_key`, `__init__`, `validate_rule`) and the helpers
`_parse_date`, `_shift_for_value_md5`, `_shift_for_value_keyed` are NOT
instantiated or called anywhere in `src/` -- the class appears only in its own
definition and a docstring mention (`fpe.py`). The ONLY live export reused by the
engine-v2 path is `_detect_format` (imported by `_date_shift.py`,
`bucket_perturb.py`, and referenced by out-of-core `_mask_group_c.py`). So ~150
LOC of that module is dead legacy code. Surfaced by the date_shift mutation grade
(the focused-selection grade produced 110 survivors + 38 no-tests; 141 of
those are inside the dead V1 class/helpers, only 7 in the live `_detect_format`). Implication for the TQ sweep: grade only the live `_detect_format`; do NOT
author tests for the dead class (that would lock in code slated for removal).
Flag for a decision: delete the V1 `DateShiftStrategy` class + its dead helpers
(a source change, out of this tests-only sweep), or confirm a caller I have not
found. Until then, the module's mutation grade is scoped to `_detect_format`.

### 12. `codeset_etl`-package-covered modules are un-gradeable via the in-process mutmut harness (harness limitation)
`transforms/_codeset_provenance.py`, `transforms/_codeset_loader.py`. These
modules' mutable logic is exercised by `tests/unit/codeset_etl/*` -- but those
tests `import codeset_etl`, a SEPARATE top-level package that mutmut does NOT copy
into the `mutants/` tree (mutmut only copies `source_paths = src/decoy_engine`).
So under the in-process harness those tests fail to import (`ModuleNotFoundError:
codeset_etl`) and must be dropped from the selection; with only the
`decoy_engine` code_set tests, mutmut reports "could not find any test for any
mutant" (the `code_set` transform suite touches only the provenance dataclasses'
construction + the `RESERVED_LICENSED_NAMES` constant, not the provenance/loader
logic). `_codeset_config_checks` and `_codeset_index` WERE gradeable (they are on
the `code_set` masking path that test_code_set.py drives); provenance/loader are
on the ETL WRITE path that only codeset_etl covers. Defer both to a grading run
that makes `codeset_etl` importable (add it to `source_paths` or run mutmut with
the package on the path), or grade via the codeset_etl suite directly. Not falsely
scored -- flagged as un-gradeable-in-process, same class as findings #8 (substrate
timeout) and the broad-selection tier.

### 13. Spill estimator under-counts the exact-decimal float FK token (Codex P2, 2026-07-26)
`execution/out_of_core/_spill_estimate.py`. Finding #1's RI fix changed a float FK
join token from `\x00FLOAT:{repr}` to `\x00DEC:{decimal_join_token(Decimal(float))}`,
whose exact-decimal expansion is much wider (Codex measured `0.1` -> ~76-byte tuple
token vs the old ~13; worst case is bounded by `_DECIMAL_JOIN_CONTEXT`'s prec=200, so
~230 bytes). But `_staged_key_token_bytes` still prices a float64 key at the
`MIN_KEY_TOKEN_BYTES = 28` floor (source itemsize 8 + framing 16, floored to 28). So
the out-of-core DISK preflight (`enforce_ooc_disk_preflight`) can substantially
UNDER-predict scratch usage for a fractional-float FK key column at scale; the
table-boundary budget check may then fire only after scratch is exhausted rather than
refusing up front. Under-prediction is the dangerous direction for a preflight (its
whole purpose is refuse-early). Narrow trigger: out-of-core route + FLOAT-typed FK key
(FKs are usually int/string) + fractional values + near disk limit. NOT an RI
correctness issue -- the RI fix itself is confirmed sound. **Fix direction:** make
`_staged_key_token_bytes` type-aware so a float/fractional-Decimal FK key column prices
at the decimal-token worst-case bound (derive the bound from `_fk_keys`' prec constant
so the two cannot drift), safe-direction over-count. Codex's alternative -- a compact
shared numeric encoding in `fk_join_key` -- would fix both the token bloat and the
sizing, but it touches the RI-critical path just fixed, so it is out of scope for a P2.
Tracked as a dedicated follow-up branch with its own dennis + Codex gate (the OOC
spill-estimation subsystem is delicate); NOT bundled into the TQ merge.

## Notes for grading (Phase B)
- `quality/dp.py` and parts of `quality/dp_provenance.py` have `dp_certified`-gated
  tests that SKIP off the certified 77-dist profile, so mutmut in an uncertified
  shell can only grade the non-cert-gated logic. Grade the cert-gated paths on the
  certified profile (the CI cert-gate job) or note the coverage gap.
- `dp_budget` additivity/single-cert tolerances are empirically derived over PLD
  discretization noise; confirm via mutmut they still kill a real composition-logic
  mutant rather than only noise.

## Codex verdict on finding 1 (FK float/Decimal route divergence), 2026-07-25

Cam asked Codex. Verdict: **REAL correctness bug** -- the RI outcome must not
depend on execution route. Equal-valued numeric PK/FK values should map to the
SAME FK key when the source system considers them equal, else valid children
become false orphans. **Fix direction:** make the out-of-core route (`fk_join_key`,
the string-token map) use the SAME canonical numeric equivalence as
`fk_key_value()` -- normalize float/Decimal to a shared, collision-safe numeric
token rather than type-tagging them apart. Source change to `_fk_keys.py`,
high-stakes (RI); Cam-gated, NOT done autonomously.

## Substrate sweep observations

### 16. tq_mutate TRUSTS mutmut's "survived", but mutmut's coverage-based per-mutant test selection can drop newly-added tests -> false-survived -> false-LOW score (tool gap, confirmed on _chunked) -- RESOLVED (2026-07-27)

**RESOLVED.** `scripts/tq_mutate.py` now RE-ADJUDICATES the survived bucket by
default (each survived mutant re-run against the FULL selection in a fresh pytest
subprocess, exactly like the existing suspect buckets); `--trust-survived` opts
back out. The correctness argument: a mutmut KILL is monotonic (a failing test
cannot be un-failed by running more tests), so killed stays trusted verbatim; a
mutmut SURVIVED is NOT monotonic (it only means mutmut's coverage-selected subset
did not kill it, and the full selection may contain a killing test mutmut never
ran), so it must be re-adjudicated. Trusting survived was itself a SILENT
UNDER-GRADE vector -- the exact failure mode the tool's charter says it must never
exhibit -- so the fix is default-ON. **Validated end-to-end on `_chunked`:** the
fixed tool, reusing the same on-disk mutmut run, auto-reported 398/488 = 81.56%
LOGIC with 0 unresolved and ZERO manual steps (mutmut's raw was 306/488 = 62.70%);
it re-classified the 92 false-survived mutants automatically. The reproducible 398
supersedes the hand-counted 399 the prior manual re-adjudication estimated (the
tool exists precisely to remove that off-by-N hand count). Original finding
retained below for the record.


`scripts/tq_mutate.py` re-adjudicates only mutmut's SUSPECT buckets
(timeout/suspicious/segfault/no-tests) and TRUSTS "survived" (exit 0), on dennis's
reasoning that a mutmut exit-0 means a covering test ran and passed. That
assumption has a hole: mutmut runs, per mutant, only the tests its coverage map
(`tests_by_mangled_function_name`) associates with that mutant. When a newly-added
test file's coverage is not associated (observed on `execution/_chunked.py`: the
whole new file's kills were not credited), mutmut runs the OLD tests against the
mutant, they pass, and it reports "survived" -- a FALSE survived. tq_mutate then
trusts it. Result: `_chunked` was reported 306/488 = 62.70% LOGIC, but a standalone
re-adjudication of its 182 survived-bucket mutants (full selection, one fresh
pytest each) found **92 of them actually killed** by the new tests -> TRUE score
**399/488 = 81.76%** (above the 77.91% bar). Proven decisively: the new tests kill
e.g. `aggregate_chunk_timings__mutmut_8` (`elapsed[key]=0.0`->`1.0`) STANDALONE
(exit 1) while mutmut's run left it survived. **Implications:** (a) every module's
tq_mutate-reported score is a LOWER BOUND (true >= reported); modules 1-6 all clear
their bars on the reported (lower) numbers, so their pass/fail is unaffected, but
some ledgered "survivors" there may be false-survived (killed by their own tests,
uncredited). (b) Large fixture-heavy modules are the ones that trip mutmut's
coverage association. **Fix (tool follow-up, Cam-gated):** add a
`--readjudicate-survived` mode to tq_mutate that runs the full selection standalone
against the survived bucket too (exactly the manual step done for _chunked), making
the grade independent of mutmut's coverage-selection. Until then, large modules
need manual survived-bucket re-adjudication (as done here). This is the finding-#8
class (mutmut in-process runner unreliable), opposite direction (false-low here,
false-high there).



### 15. Subprocess/isolated-execution substrates are un-gradeable by the in-process harness (methodology; scope-refining)
The runtime governor `execution/_governor.py` and its siblings run their real work
in SPAWNED CHILD PROCESSES (supervisor-kills-child architecture), and their tests
(`test_governor.py`: 65 monkeypatch/mock refs) stub the spawn + RSS monitor. So
mutmut's in-process trampoline mutants in these modules are never executed by the
tests -- the child imports the UNMUTATED installed package. `tq_mutate.py`'s
baseline sanity check (dennis P1-3) correctly ABORTED grading `_governor` with "the
tests never call the mutated functions" rather than emitting a false score (the
guard working as designed). Triage of the substrate tier by subprocess/isolated
reference count:
- **Gradeable in-process (pure logic, 0 subprocess refs):** `_planner`, `_sequential`,
  `_chunked`, `_chunked_fk`, `_pandas_adapter`, `capacity`, `_technique_class`,
  `_distribution_behavior`, `_mem_estimate_schema`. These are the sweep's in-process
  targets.
- **UN-gradeable in-process (subprocess / isolated-run heavy):** `_isolated_run` (72),
  `_governor` (59), `_mem_telemetry` (19), `_probe` (17), `_isolated_worker` (8),
  `_probe_scale` (5), `_pipeline_routing_signals` (3), `_governor_monitor` (3),
  `_mem_estimate` (2). These need a different method (mutation testing that runs the
  child with the mutated tree, or accept they are behavior-tested not mutation-graded)
  -- same deferred class as findings #8/#12. Flagged for a Cam decision on approach.



### 14. `_when_gate` docstring overstates numexpr fallback behavior (doc nit, source; LOW)
`execution/_when_gate.py:20-22` states "The numexpr backend never falls back to
Python eval, so an undefined name raises `UndefinedVariableError` instead of
executing." dennis's gate on the _when_gate grading batch (2026-07-26) empirically
showed pandas `DataFrame.eval` DOES transparently fall back to the python engine
even when `engine="numexpr"` is pinned (the code itself handles that fallback at
lines ~109-127 with a RuntimeWarning). The security posture is UNAFFECTED: the real
scope-walk block is the empty `local_dict`/`global_dict` clamp, not the engine
choice (verified -- `@os.system(...)`-style attacks are blocked identically under
both engines with the dicts clamped, which is why the `engine=` mutants mut_11/15
are correctly EQUIVALENT). Only the docstring's rationale is inaccurate. Fix in a
future source pass: reword to attribute the block to the dict clamp, not the engine
pin. Tests-only sweep; not fixed here.
