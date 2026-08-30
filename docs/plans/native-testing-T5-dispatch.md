Status: record

# T5 dispatch + route integrity: measurement, fill, and adjudication

- **Plan:** `docs/plans/2026-08-29-native-efficiency-test-plan.md`, batch T5 (section 4) and the
  method in section 3.
- **Scope:** `src/decoy_engine/execution/native/_dispatch.py` (`run_native_or_oracle_chunked`,
  `plan_native_route`, `_static_route_decision`, `_table_in_declared_relationship`,
  `_downgrade_to_oracle`, `_resolve_truncate_keep`, `_mask_chunk_native`, `_mask_native`,
  `NativeRouteEvidence`/`NodeRouteRecord`). Bar: kill every mutant that changes a route TAG
  (native_kernel vs oracle, whole-table atomicity), the runtime `compiled_kernel_executed` flag, a
  fail-closed PREFLIGHT reroute, or the FK reroute.
- **Branch:** `docs/native-efficiency-test-plan`, worktree `.claude/worktrees/native-test-plan`.
- **Harness reused, not re-derived:** `scripts/native-testing/python_mutation_pilot.py` (T0).

## Complete test selection (T4 lesson applied)

`grep -rln "native._dispatch\|run_native_or_oracle_chunked\|plan_native_route\|_static_route_decision\|NativeRouteEvidence" tests/`
returns exactly two files:

```
tests/native/test_native_dispatch.py
tests/parity/native/test_phase2_gate.py
```

Both were used together for every measurement below (no third file exists; T4's incomplete-
selection trap does not recur here since the module has only these two guards). `_dispatch.py`
imports the crypto/keyed hash surface but does not implement crypto itself, matching T4's
precedent for the non-crypto modules: the pilot ran WITHOUT `--readjudicate-killed`.

## BEFORE: coverage

```
coverage run --branch -m pytest -q tests/native/test_native_dispatch.py tests/parity/native/test_phase2_gate.py
coverage report --include=*/execution/native/_dispatch.py -m
```

| Module | Stmts | Branch | Cover | Missing |
|---|---:|---:|---:|---|
| `_dispatch.py` | 149 | 56 | 89% | 132, 136->130, 137->136, 168, 181-182, 188, 322, 327-331, 380->382, 403->406 |

## BEFORE: mutation

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_dispatch.py \
  --tests tests/native/test_native_dispatch.py tests/parity/native/test_phase2_gate.py --timeout 90
```

| Module | Total mutants | Killed | Survived | LOGIC score |
|---|---:|---:|---:|---:|
| `_dispatch.py` | 397 | 315 | 82 | 79.35% |

Every one of the 82 survivors' mutant body was read (`mutants/src/.../_dispatch.py`) against what
actually consumes each changed value, using a diff-against-original extraction script rather than
inspection alone (a T4 lesson: verify by reproducing the mutant, not by reading the diff). Sorted
into three groups:

1. **Demonstrated route-tag / fail-closed / FK-reroute gaps** (real: a route TAG, the atomicity
   invariant, the FK reroute's boolean logic, a config value silently defaulting instead of
   threading through, or evidence fields (`.table`, `.column`, `.strategy`, `kernel_calls`,
   `kernel_elapsed_s`) that are job evidence, not cosmetic). These got a test.
2. **Message-text-only or unconsumed-argument** survivors (the value never reaches a boolean the
   dispatch or a downstream consumer branches on). Adjudicated equivalent.
3. **Unreachable-by-contract** survivors: branches a defensive `# pragma: no cover` guard, or an
   admission-time guarantee proven upstream, means no live call site can ever reach.

## Fill: gaps closed

All additions are new tests in `tests/native/test_native_dispatch.py`; one production change (see
below).

- **`.table` field pinned on every evidence-producing path**
  (`test_evidence_table_field_is_pinned_on_every_producing_path`): `NativeRouteEvidence.table` is
  named "job evidence" in the module's own docstring, but no existing test asserted it anywhere --
  a mutant nulling it on the admitted path, the FK-reroute path, the `no_mask_nodes` path, or the
  empty-input path all survived. One test pins it across all four.
- **`_downgrade_to_oracle` preserves per-column identity**
  (`test_downgrade_to_oracle_preserves_column_and_strategy_identity`): existing reroute tests only
  checked `all(r.route == "oracle" ...)`, never that `.column`/`.strategy` still names the RIGHT
  node after the reroute. A mutant nulling either field survived; job evidence that can't say
  which column had which strategy is useless for an audit.
- **`no_mask_nodes` path, entirely untested before this batch**
  (`test_table_with_zero_mask_nodes_reroutes_with_exact_reason`): a table the profile carries but
  the config never declares columns for produces zero WorkNodes; this whole branch (and its six
  survivors: `table=None`, `reason=None`, wrong reason text, a dropped argument) was unexercised.
- **Non-FK composite node exclusion, and that hitting it does not stop the scan**
  (`test_composite_non_fk_node_is_skipped_without_stopping_the_scan`): every existing test reaches
  a non-scalar (`composite`/`composite_fk_group`) node only via a table that ALSO has a declared FK
  relationship, so `_table_in_declared_relationship` always short-circuits before this loop's
  `kind != "scalar"` branch ever runs. A non-FK composite provider (`composite_name_email`,
  `composite_city_state_zip`, no `relationships` key) reaches it directly. TWO independent
  composite groups on one table prove the loop `continue`s past a non-scalar node rather than
  `break`ing (a `break` mutant survived: it would silently stop evaluating every node after the
  first non-scalar one, including real scalar columns).
- **Crypto-extension probe fires for an all-hash table**
  (`test_extension_probe_runs_for_an_all_hash_table`): `plan_native_route`'s
  `any(n.strategy == "hash" ...)` gate had a mutant inverting it to `!=`, which stays TRUE for a
  mixed hash+other-strategy table (every prior test) but wrongly stays FALSE -- skipping the
  extension probe entirely -- for a table whose admitted columns are hash-only. Fixed the gap
  in the test suite, not production (the original condition was already correct).
- **FK reroute as a differential property against the oracle's own edge walker**
  (`test_fk_reroute_agrees_with_oracle_side_walker_across_edge_shapes`,
  `test_malformed_relationship_entry_does_not_short_circuit_the_scan`,
  `test_non_participating_table_stays_admitted_despite_other_tables_relationships`,
  `test_malformed_child_entry_mixed_with_a_matching_one_is_still_found`): parametrized over
  composite, single-column, self-referential, and chained edges, each table's participation
  verdict is checked against `fk_passthrough_columns_for_table` (`_chunked_fk.py`, the REAL,
  unmodified oracle-side edge walker over the identical `config["relationships"]` shape) --
  genuinely cross-module, not a hand-rolled restatement of the same function. This caught three
  real bugs: a `break` instead of `continue` on a malformed (non-dict) relationship entry (would
  silently stop scanning and miss every LATER entry, including one that matches the queried
  table), and TWO `and`-to-`or` corruptions of the parent/child dict-type guards (either would
  reroute a table with NO relationship of its own to the oracle, just because some OTHER table's
  relationship entry happens to be a dict -- over-broad, not under-broad, but still a real
  admission-verdict change for the wrong table).
- **Provider-config values thread to the kernels, not just their defaults**
  (`test_provider_config_values_reach_the_kernels_not_just_the_defaults`): every prior test left
  `redact_with`/`mask_char`/hash `truncate` at their default value, so twelve mutants that deleted
  the kwarg, mis-keyed the config lookup, or swapped the key's case all survived (the kernel's own
  default happened to match). One test with non-default values for all three closes them at once.
- **`kernel_calls` / `kernel_elapsed_s` accumulate across chunks**
  (`test_kernel_evidence_accumulates_across_multiple_chunks`): a `.get(None, ...)` corruption of
  either counter always misses the real prior value, silently resetting the running total instead
  of accumulating it. Consuming two chunks one at a time and asserting the SECOND reading is
  strictly greater than the first (not just non-zero) catches the reset, which a single-chunk
  check could not.
- **Key resolution from `config.global_settings.mask_secret_ref` when no explicit key_provider is
  given** (`test_mask_native_resolves_key_provider_from_config_mask_secret_ref`): every existing
  test passes an explicit `key_provider`, leaving this whole fallback branch (eleven mutants: the
  `is None` check inverted, the dict-key lookups mis-keyed, the ref resolution call itself
  replaced with `None`) unexercised.
- **Empty-input short-circuit still threads `key_provider` to its oracle delegation**
  (`test_empty_chunk_stream_with_keyed_strategy_and_explicit_key_provider_succeeds`): a mutant
  dropped the `key_provider=key_provider` kwarg on the zero-chunk delegation call specifically;
  with a keyed strategy and no configured `mask_secret_ref`, the DE-02 fail-closed gate
  (`require_mask_key`) would then reject a job that should succeed on the caller's supplied key.
- **Schema drift, the plan's named scope correction.** "No partial output" holds only for
  PREFLIGHT-detectable failures:
  - `test_configured_column_missing_from_first_chunk_reroutes_and_reports_both_sides`: a configured
    column absent from the first chunk IS preflight-detectable (the covered/actual check runs
    before any chunk yields) -- this is the production fix below, verified.
  - `test_second_chunk_introducing_an_unconfigured_column_fails_after_first_chunk_consumed`: a
    schema change on a LATER chunk is NOT preflight-detectable; the first chunk's output already
    reached the caller before the fault surfaces as a `KeyError`. Pins the honest limit rather than
    promising more.
  - `test_second_chunk_missing_a_configured_column_silently_drops_it`: the mirror case -- no crash,
    but a per-chunk schema that silently shrinks. Pinned as current, defined behavior, not "fixed"
    (the plan's scope correction says exactly this: do not over-claim a staging contract that does
    not exist).

## Production change: symmetric-difference diagnostic (disclosed)

`run_native_or_oracle_chunked`'s uncovered-column reroute compared `actual != covered` (correct:
catches drift in EITHER direction) but its diagnostic message only reported `actual - covered` (a
column the chunk has that the plan doesn't cover). A configured column MISSING from the chunk
(`covered - actual`) still correctly rerouted -- the `!=` check already covers it -- but the
message showed `uncovered_columns:[]`, silently hiding the real cause. This is not a killed/
survived mutant (no test measured the message content before this batch, so nothing "survived"
here in the mutation sense); it's the exact gap the plan names directly: "fix the column-coverage
diagnostic, which reports only `actual - covered` and so misses a missing configured column."

Minimal, behavior-preserving fix in `_dispatch.py`: the reroute reason now reports BOTH sides
(`uncovered_columns:[...];missing_configured_columns:[...]`). The reroute DECISION is unchanged
(still keyed on `actual != covered`); only the diagnostic text changed. Verified both directions
independently (extra-column-only, missing-column-only) and together.

## AFTER: coverage

```
coverage run --branch -m pytest -q tests/native/test_native_dispatch.py tests/parity/native/test_phase2_gate.py
coverage report --include=*/execution/native/_dispatch.py -m
```

| Module | Stmts | Branch | Cover | Missing |
|---|---:|---:|---:|---|
| `_dispatch.py` | 149 | 56 | 98% | 188, 322, 380->382 |

`188` and `322` map onto two of the unreachable-by-contract findings below. `380->382` is the
`route_evidence_sink is None` skip-append branch on the empty-input call site: every test passes a
real sink, so the FALSE branch is uncovered, but no mutant of that condition survived -- flipping
it makes literally every test's `sink[0]` access raise `IndexError` (every existing test pre-dates
this batch and already exercises the TRUE branch pervasively), so this is a coverage-only gap with
no surviving mutant behind it, not a demonstrated gap.

## AFTER: mutation

Two rounds. The first (44 survivors) exposed two of this batch's OWN test gaps rather than real
production bugs -- caught and fixed before the second, final round:

- `_downgrade_to_oracle`'s `table=None` mutant still survived: the new `.table`-pinning test
  covered the admitted/no_mask_nodes/FK-reroute/empty-input paths but missed the
  crypto-extension-absent downgrade path specifically. Added the assertion to the existing
  identity test rather than a new one.
- The key_provider/config-`mask_secret_ref` test used too weak an assertion (`isinstance(v, str)
  and len(v) > 0`), which passes even when the resolution is silently SKIPPED: pre-GA,
  `require_mask_key(plan, None)` falls back to `job_seed` rather than raising, so a dropped
  `mask_secret_ref` still produces a valid-LOOKING hash from the WRONG key material. Fixed by
  comparing the auto-resolved run's output against an explicit `key_provider_from_ref` built from
  the identical ref -- they must be byte-identical, which only holds if the ref was actually
  threaded through. Same root cause explains why the empty-input key_provider-drop mutants
  (`run_native_or_oracle_chunked` 21/26) survived the first round too: on truly empty input there
  is no masked value to observe at all, pre-GA. Forcing GA mode (`monkeypatch.setattr(
  "decoy_engine.keyprovider.is_pre_ga", lambda: False)`, the same pattern `test_de02_keyprovider.py`
  already uses) makes the drop observable via the DE-02 fail-closed gate.
- One kernel-elapsed-time mutant pair (`_mask_chunk_native` 88/89: a wrong accumulator base, and
  `+` corrupted to `+t0` instead of `-t0`) survived a `> 0` check, since both a wrong base and an
  epoch-scale value from adding two `perf_counter()` readings are still positive. Tightened to
  `0 < elapsed_after_one < 1.0` (a real kernel call is sub-second; a corrupted accumulation is not).

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_dispatch.py \
  --tests tests/native/test_native_dispatch.py tests/parity/native/test_phase2_gate.py --timeout 90
```

| Module | Total mutants | Killed | Survived | LOGIC score |
|---|---:|---:|---:|---:|
| `_dispatch.py` | 401 | 371 | 30 | 92.52% |

The raw killed/survived count varies +/-2 across pilot runs (a run may report 371-373 killed /
28-30 survived / 92.52-93.02%). The variance is entirely mutmut flaky-kills on the six defensive
`# pragma: no cover` AssertionError string-mutants in `_mask_chunk_native`: that branch is
unreachable, so NO test can genuinely kill a mutant on it, and mutmut's in-process runner
spuriously marks 0-2 of the six as killed on a given run (the same flaky-kill vector T0's
`--readjudicate-killed` exists to correct; this batch is non-crypto so it was not run). The
ADJUDICATED five-field count below treats all six as unreachable-by-contract, which is the stable,
honest verdict (371 killed / 30 survived); a raw run's extra 0-2 "kills" are spurious and land on
that same unreachable branch. The bar -- every REACHABLE route-tag / fail-closed / FK-reroute
mutant killed -- is met and stable regardless of the flaky count.

## Five-field adjudication

| Field | Count |
|---|---:|
| (a) Branch coverage | 98% |
| (b) Killed | 371 |
| (c) Equivalent (reason below) | 15 |
| (d) Unreachable-by-contract | 15 |
| (e) Tool-excluded | 0 |

**Equivalent mutants (15), by shape:**

- **`engine_version=None` on any call in the chain** (9 mutants: `_static_route_decision`
  mutmut_14 -> `compile_native_plan`; `plan_native_route` mutmut_5 -> `_static_route_decision`;
  `_mask_native` mutmut_10/17 -> `first_chunk_profile`/`compile_plan`; `run_native_or_oracle_
  chunked` mutmut_20/30/38/69/79 -> `first_chunk_profile`/`plan_native_route`/`_mask_native`/the
  two `run_mask_pipeline_chunked` delegation calls): confirmed by grep across `_seed_envelope.py`
  and `_compile.py` that `decoy_engine_version` is stored ONLY on `Plan.engine_version` (read by
  `_serialize.py`/`_manifest.py` for provenance) and never consulted by node classification, seed
  derivation, or any route/output-value logic -- the same finding T4 already made for
  `compile_native_plan`'s own `engine_version` parameter, reused here rather than re-derived.
- **`_resolve_truncate_keep`'s `from_end` default value identity** (2 mutants:
  `cfg.get("from_end", None)`, `cfg.get("from_end", )`): `bool(None) == bool(False)`, so no input
  can ever distinguish a `None` default from a `False` default here -- both coerce to the same
  boolean the function branches on.
- **Message-separator in the joined `reroute_reason`** (1 mutant, `"; ".join` -> `"XX; XX".join`):
  grep confirms nothing downstream splits or parses `reroute_reason` on its separator; it exists
  for a human/log reader only.
- **`no_profile`'s distinction between `True` and any falsy value in `_mask_native`'s
  `compile_plan` call** (3 mutants: `no_profile=None`, the kwarg dropped entirely, and
  `no_profile=False` -- all three collapse to the same falsy behavior): `no_profile=True` gates two
  checks in `compile_plan` (`check_null_bearing_int_unsupported`, `check_basic_uniqueness_pre_
  flight`), but BOTH are reachable only through concerns outside this phase's admitted strategy
  set: the null-bearing-int guard fires on an "int" dtype LABEL, and any int-with-null column
  already profiles as `float64` (pandas' upcast in `walk_dataframe`, the SAME profiling step used
  at admission), so hash/truncate never see it profiled as int-with-nulls either way (confirmed
  empirically: a truncate column over a real int64-with-null Arrow array profiles as `float64` and
  admits/executes identically under either `no_profile` value); the uniqueness guard targets
  pool-backed `UNIQUE` faker columns, none of which exist in `NATIVE_KERNEL_STRATEGIES`. Equivalent
  for THIS phase's admitted strategy set, not a claim that `no_profile` is inert in general.

**Unreachable-by-contract (15):**

- **`_static_route_decision`'s kernel-availability `elif` body** (1 mutant, line 188,
  `reasons.append(None)`): for a SCALAR node, `fallback_policy == "native"` requires `kernel_
  reason is None` (`_requirements._fallback_policy`), and `native_kernel_rejection` returns `None`
  only when the strategy is `"<composite>"`/`"<group>"` (impossible for a scalar node's real
  strategy string) OR the strategy is already in `NATIVE_KERNEL_STRATEGIES`. So for any scalar
  node reaching this `elif`, the condition `node.strategy not in NATIVE_KERNEL_STRATEGIES` can
  never be true -- the body is dead code by construction, not merely untested. (A mutant flipping
  the CONDITION itself, rather than the body, IS observable and was already killed by the
  all-admitted-table test, since it would spuriously reject every currently-admitted node.)
- **`_static_route_decision`'s empty-`node.columns` label fallback** (1 mutant, the `else "?"` arm):
  every WorkNode kind this module ever produces (scalar, composite, composite_fk_group) carries at
  least one column by construction (mirrors the identical finding already recorded for
  `_requirements._config_gate_rejection`'s empty-columns guard in T4).
- **`_mask_chunk_native`'s truncate-length else-fallback** (1 mutant, `0` vs `1` when `length` is
  not an int): `truncate_config_rejection` already proves `length` is a valid positive int before
  any table reaches the native route (the source comment states this explicitly); the branch using
  the non-int fallback value never executes for an admitted config.
- **`_mask_chunk_native`'s defensive `AssertionError` for a non-kernel strategy** (6 mutants, all on
  the `# pragma: no cover` else-branch message): `_static_route_decision` only ever admits a scalar
  node whose strategy is in `NATIVE_KERNEL_STRATEGIES`, so this branch cannot execute for any
  admitted table; the comment already marks it defensive.
- **`_mask_native`'s empty-`chunks` guard and its downstream `AssertionError`s** (6 mutants: `next(
  chunks, )`, `iter(None)`, and 4 mutants on the `table_seed is None` guard's default-arg/message):
  confirmed by grep that `_mask_native` has exactly one call site
  (`run_native_or_oracle_chunked`), which always feeds `_rechain(first, chunk_iter)` where `first`
  is already proven non-`None` by the caller's own preceding check -- neither guard can ever fire
  for any real call.

## Property tests added, and evidence each is non-vacuous

- **FK-participation differential** (`test_fk_reroute_agrees_with_oracle_side_walker_across_edge_shapes`):
  verified non-vacuous by the mutation rerun itself -- `_table_in_declared_relationship` mutants 6
  (`break`), 11, and 21 (the two `and`->`or` corruptions) are absent from the AFTER survivor list,
  present before.
- **Provider-config plumbing** (`test_provider_config_values_reach_the_kernels_not_just_the_defaults`):
  non-vacuous by construction -- every asserted value (`"CUSTOM_TAG"`, `"ABCD####"`, a 6-char hash)
  differs from what the kernel's OWN default would produce, so a mutant reverting to that default
  fails the assertion directly (confirmed against the BEFORE survivor list, which named exactly
  these twelve mutants).
- **Kernel-evidence accumulation** (`test_kernel_evidence_accumulates_across_multiple_chunks`):
  non-vacuous by construction -- asserts the SECOND reading is strictly greater than the first, not
  merely non-zero, so a `.get(None, ...)` reset-to-zero-then-add mutant fails (elapsed time after
  the reset would be smaller than or equal to, never strictly greater than, the first reading).
- **Schema-drift trio**: each demonstrated non-vacuous directly against the real (unmutated) code
  before being added, per the sanity checks run during this batch (the "missing configured column"
  case showed the exact pre-fix `uncovered_columns:[]` defect; the "new column on a later chunk"
  case showed the real `KeyError` after the first chunk was already consumed; the "column silently
  dropped on a later chunk" case showed the real schema shrink) -- these are pinning tests for
  demonstrated CURRENT behavior, not speculative assertions.

## Gates

- `ruff check` on the changed source and test files: clean.
- `ruff format --check` on the same: clean.
- `mypy src/decoy_engine testflight`: clean (428 source files).
- `pytest tests/native/ tests/parity/native/ -q`: 490 passed, 1 skipped (pre-existing: the shared
  KAT fixture file is not generated in this environment), 59 xfailed (pre-existing).

## Bar

Kill every mutant that changes a route TAG, the `compiled_kernel_executed` flag, a fail-closed
PREFLIGHT reroute, or the FK reroute: met. Every survivor in those four categories from the BEFORE
run is gone in the AFTER run; the AFTER survivors are all adjudicated equivalent (engine_version
provenance-only, the truncate-keep default identity, the message separator, `no_profile`'s
inert-for-this-phase distinction) or unreachable-by-contract (the kernel-availability elif body,
the empty-columns label fallback, the truncate-length else-fallback, the defensive
`AssertionError`s on both the non-kernel-strategy branch and the empty-chunks guard), none of which
touches an admission verdict, a route field, or the FK reroute.
