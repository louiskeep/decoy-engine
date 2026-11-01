# Categorical CDF: exact integer arithmetic for interpreter-independent output

Status: plan, revision 8 (handoff "Categorical CDF exact arithmetic: plan revision 8, delete the completeness language"). The Codex gate on revision 7 was NO-GO with one BLOCKER (completeness still claimed in five sentences: "every weighted-categorical route", every registry member crossed with every weighted site, "built only at these points", no holder "anywhere", "single owner"; and the 47 no-path pairs described as if proven), one HIGH (the T5r `replaced_member` seed could not fail as written: `CategoricalStrategyHandler.run` calls the module-global `_build_cdf`, so a subclass has no builder seam to override), and two LOWs (the T20 reviewed-list count stated twice with two values; the D-4 citation). Revision 8 adds the claims table (section 1.1): one row for every guarantee sentence in the plan, each with the test that proves it and what that test does not prove; every sentence whose mechanism was weaker than its wording is rewritten or deleted, and section 1 is a strict subset of the table. The 47 no-path reasons are reviewed assertions, not proofs. The `replaced_member` seed is a concrete handler class whose `run` buckets through the float reference, probed red in this container. The threat model is V1: accidental drift by a reviewed author, not an adversarial one. No code is on this branch.

Rules consulted: decoy-platform `docs/guides/engineering-best-practices.md` (read at decoy-platform `2d40c9a`) section 1.3 (behavior change is its own slice), 2.4 (land the assertion test first), 3.4 (contract as gate), 4.1 (module size), 4.3 (explicit state objects), 5.2 (sentry tests), 6.2 (established methodology), 6.4 (performance claims trace to dated benchmarks), 7.3 (no benchmarks on shared runners), 8.1 (pre-GA hard delete), 10.3 (no em-dashes); the revision 8 handoff (Codex findings 1 to 4 on revision 7, the claims table and its grep census, the concrete replacement handler, the two LOWs, the section 14 question, the worker rules); the revision 7 handoff (Codex findings 1 to 4 on revision 6, the four-claim guarantee, the delegated owner decision on Q10, the ratchet label, the registry closure, the override-free gate, the T9c copy seed); the revision 6 handoff (Codex findings 1 to 3 on revision 5, the two route-coverage mechanisms, the gate script, the copied-content check); the revision 5 handoff (the cuts, the worker rules, slice bound 10 production files and 800 lines); the revision 4 handoff (findings 1 to 7, the A-4 coupling note); `/home/cam/dev-rules/` `risk-and-exceptions.md`, `testing.md`, `verification.md`, `api-and-compatibility.md`, `reuse-first.md`, `documentation.md`, `performance.md`; decoy-platform `handoffs/queue` `DEFECT-CATALOG.md`, `HOSTILE-PLAN-REVIEW.md`, `OWNER-DECISIONS-2026-10-30.md`; engine `CLAUDE.md`, `docs/reasons.md` (Versioning), `docs/compatibility-contract.md` section 3.3, `docs/records/2026-10-31-main-health-engine.md` row 4. The `/home/cam/dev-rules` and decoy-platform `handoffs/queue` files are not in this container, so their content comes from the handoff text.

Risk: R2. Reason: the change is visible in masked output (compatibility-contract section 3.3) on a pre-GA engine that can be reverted. It is not R3: there is no destructive or irreversible operation, no vault or production data the change could strand (owner decision 2026-10-31: no old-engine recovery path), and rollback is a revert. It must land before the D-4 launch-engine freeze (decoy-platform `docs/ROADMAP.md:516`, read at decoy-platform `8a581fe`), because after the freeze the same change is a major-version determinism break.

Baseline commits. Revision 4 text carried over keeps `545d913` line numbers. On `a3fe49e` (`origin/main` on 2026-11-01) every cited file has the same line numbers except `src/decoy_engine/execution/out_of_core/_mask_group_b.py` (`:316` to `:395` are 5 lower, so `:344` is `:339` and `:387` is `:382`) and `docs/compatibility-contract.md` (6 lines inserted at `:44`, so `:253`, `:254`, `:301-307` are `:259`, `:260`, `:307-313`). Material new in revision 5 (the section 3.2 inventory, rule L1, slice 2) is cited on `a3fe49e` and marked. Files that exist only on `a3fe49e` are cited there: `scripts/release/reason_release_pin.py`, `tests/unit/test_reason_release_pin.py`, `tests/fixtures/f29/reason-registry-release.json`. Revision 6 and 7 material (sections 3.2, 3.3a, 5.4, T2x, T5r, T9c, T20) is cited on `5154830` (`origin/main` when revisions 6 and 7 were written). `5154830` differs from `a3fe49e` under `src/` only in `_fk_keys.py`, `_pipeline_routing_fk_dtype.py`, `out_of_core/_batch_join.py`, `out_of_core/_emit.py`, `out_of_core/_join.py` and `out_of_core/_stream_join.py`, none of which this plan cites, so every `a3fe49e` line number above also holds on `5154830`.

## 0. Revision 8 changes and the finding map

### 0.1 What revision 8 changes

- The claims table (section 1.1). Census: every sentence in the plan that contains `every`, `only`, `never`, `anywhere`, `single`, `all`, `none`, `nothing else`, `no other`, `no module`, `no copy`, `no cache`, `closed-world`, `by construction`, `cannot`, `guaranteed` or `exactly` was listed (217 sentences on revision 7, section 0.2 history tables excluded), and each one that states what the code does after the plan lands, rather than history, a description of what a test reads or runs, or a limit, became a row: the sentence, the test ids that prove it, and what those tests do not prove. Rows C1 to C36. Every sentence whose wording was stronger than its row's mechanism is rewritten to the row's wording or deleted: section 1 (the problem statement and the four claims), 2 (P1, P4, P6, P8, P9), 3.1 (heading), 3.2 (the R1-only draw-site test, the no-path reasons, the delegation grep), 3.4, 3.5, 3.6, 4 (N1, M2, A1, rule L1's construction table and its statement, the lifetime and memory sentences), 5.4 ("nothing can narrow that"), 6 (rows 2, 4 and 6), 7 ("single owner"), 8 (the contract sentence), 11 (items 3, 5, 13) and 14. Section 1 states four claims, rows C2 to C7 of the table; the table has thirty rows more.
- The 47 no-path entries of T5r are reviewed assertions (row C35): the test checks that each member is in exactly one of a cell or the table, and nothing checks that a reason is true.
- The `replaced_member` seed of T5r is a concrete class, `FloatCdfCategoricalHandler` (`name = "categorical"`, `run` overridden to build thresholds with `tests/perf/_float_cdf_reference._build_cdf` and bucket through them), registered in a test-local copy of `SCALAR_HANDLERS` by `monkeypatch.setattr` on the module attribute. `members()` and the drivers read the module attribute at call time, and `NestedStrategyHandler` imports `SCALAR_HANDLERS` inside its method (`_strategies/_nested.py:131`), so the copy reaches both the R1 `categorical` cell and the R1 `nested` delegated cell. Probed in this container (section 5.1 T5r, section 13): with the production handler bound to an exact builder the two deterministic cells emit `y` and match the KAT; with the copy installed they emit `z` and both cells fail; a driver that binds the registry at import time does not see the copy (the register's mutation for this seed).
- LOWs: the D-4 citation is decoy-platform `docs/ROADMAP.md:516`. The T20 reviewed list is counted once, in section 3.2 (16 entries); section 0.1a no longer states a count.
- Section 14 answers whether the plan is the smallest design and lists what is still removable, row by row against the table.
- Section 0.4 records the third occurrence of the completeness class and the structural fix: guarantees live in one table with a non-proof column, and the section 13 hand check greps the census words and requires every hit outside the table to be history, a run description, a limit, or a sentence that names its row.

### 0.1a What revision 7 changed (carried, with revision 8's corrections)

- The guarantee is narrowed to what the mechanisms prove (owner decision on Q10, delegated: accept the narrower guarantee; add no production dispatch registry and no further scan shapes to chase evasion). Section 1 states four claims, each next to its mechanism. The threat model is V1, accidental drift by a reviewed author, not an adversarial one; the plan says so wherever a check could be evaded on purpose. Revision 7 deleted completeness sentences in sections 0, 2 P4, 3.2, 3.3a, 6, 11 and 14 and left five standing (Codex round 7 finding 1); revision 8's census (section 1.1) is the complete list.
- T20 is relabelled a ratchet in the plan, in the test docstring it prescribes and in the guarantee. Its shape set is broadened to the accidental variants Codex named and the handoff lists: `x += w`, `x = x + w`, `x = operator.add(x, w)`, a walrus addition, `prefix.append(prefix[-1] + w)`, `functools.reduce`, `itertools.accumulate`, NumPy `cumsum`, and `operator.add` handed to any helper, each with a seeded fixture. The reviewed list of unrelated matching functions is kept (counted once, in section 3.2). It is explicitly not a proof against a differently written builder.
- T5r is closed independently of the draw-site map: each member of each of the five registries must have a known-answer cell (derived from the map, or one of the committed delegated cells) or an entry in the committed `NO_WEIGHTED_CATEGORICAL_PATH` table that names the member and why. A member in neither, or in both, fails. Seeds: `categorical_fast` added to `GROUP_B_STRATEGIES` without a map entry (fails: in neither), with a map entry and no driver (fails: cell without driver), and one no-path entry removed (fails).
- The gate script has no environment override. `CDF_GATE_PYTHONS` is gone; the three interpreters are a constant in the script; arguments must be paths; pytest runs under a fixed environment (`env -i`). T14's cells carry a `compiled_companion` marker, which the script runs in a second phase on 3.12 only, with `DECOY_REQUIRE_COMPILED_COMPANION=1` set by the script itself. Proven with the seeded 3.10-only failure under all 32 combinations of five environment variables (section 5.4).
- T9c's tuple seed is a real copy, `tuple(list(cdf))` (probed: `tuple(cdf) is cdf` for an exact tuple, so the revision 6 seed retested identity). The list and Arrow seeds stay.
- New test T2x (slice 1e): exhaustive small cases against the `Fraction` oracle, because claim (i) names them and no earlier revision had them.

### 0.1b What revision 6 changed (carried, with revision 7's corrections)

- The caller inventory (revision 5's T16, its site table and every count of sites) is deleted and stays deleted.
- T20 (slice 1e) scans function bodies for a running total scaled onto an integer grid. Revision 6 called it closed-world ownership; revision 7 calls it what it is, a ratchet.
- T5r (slices 4a and 4c) derives its cells from five production registries and the weighted draw sites; revision 7 closes the member set as above.
- Section 3.3a lists the dispatch branches no registry drives, with their cells or as limits.
- The local gate is a committed script with seeded cells (section 5.4); revision 7 removes its override.
- T9c checks retention by content beside T9 by identity.

### 0.1c What revision 5 cut (carried unchanged)

- The memo. A run builds each weight vector at a fixed construction point and holds the tuple in an object that the five T9 run kinds release with the run (rule L1, section 4; rows C21 to C23). Out of core, `decide_table` builds once per table and the Python path reads that tuple. The chunked oracle builds once per run, holds the result in a local of its masking generator and passes it to every chunk. Gone: `CdfMemo`, rule K1, rules R1a to R1f, P7, the memo tests and T10b.
- Revision 4 slice 3 (sampler weight totals, P5, T6, the `_window_admission.py` left fold). It is a separate defect and gets its own plan (section 15).
- Revision 4 slice 4b (the `native-companion.yml` steps and trigger paths, T15) and the T4 `cdf-interpreter-matrix` CI job and its sentry. GitHub Actions is off for this repo (`.github/workflows/native-companion-release.yml:20`, `scripts/release/native_release_checks.sh:4`, on `a3fe49e`). The section 5.4 script, run in each slice's evidence, is the check. T14, the compiled-kernel test, stays as an ordinary native-suite test in slice 4a and runs locally with the companion built.
- Kept: slice 1r (registry bump with a literal `since`), 1a, 1b, 1c and 1d, and every mechanism fix from revision 4: N1 by exact type using C type codes, T12 over a named function scope, the independent KAT (T1), the contract digest test, and T17.

### 0.2 Every prior finding, where it is closed now

Triage round (on revision 1, closed in revision 2):

| Finding | Class | Section | Slice | Test |
|---|---|---|---|---|
| 1 HIGH: below-resolution message uses float arithmetic (`_categorical.py:112`) | 1 and P1 | 4 rule M1 | 1a | T3m, T12 (1e) |
| 2 HIGH: an `lru_cache` keyed on the raw weight tuple collapses `int`, `float`, `bool`, signed zero | 6 and 3 | 4 rule N1; rule L1 (no cache or lookup by value exists; reuse is gated by object identity, not equality) | 1a, 2 | T2, T3m (1a); T19 (2) |
| 3 MEDIUM: `list[int]` to `tuple[int, ...]` declarations unverified; companion absent | 9 | 3.5 | 1a; 4a | T13; T14 |
| 4 MEDIUM: T5, T7, T8 without a fail-before location | 8 | 5.0 register | 4a, 4c | T5r, T5s, T7, T8 |
| 5 LOW: `except (ValueError, OverflowError)` kept, unreachability unspecified | 6 and 9 | 3.3 | 1c | T12b |
| Re-read: bare `ValueError`/`TypeError` from `float(w)`; `bool` silently `1.0` | 6 and 4 | 4 rule N1 | 1a, 1c | T3m, T3, T2c |
| Re-read: F29 fixtures said to be regenerated with `--write` | 8 | 5.3 | 1r | T17 |

Codex round on revision 2 (closed in revision 3):

| Finding | Class | Section | Slice | Test |
|---|---|---|---|---|
| 1 HIGH: admission prechecks decide before the builder | 4 and 6 | 4 rule A1, 3.3 | 1c | T3 |
| 2 MEDIUM: native-present gate placed in a container with no engine | 8 | 5.5: no workflow edit remains, and T14 runs locally with the flag that forbids skips | 4a | T14 |
| 3 MEDIUM: T5 missed the `run_seq` route | 8 and 4 | 3.2 (registry `_OPS`), 3.3a (D1), 5.1 T5r and T5s | 4a | T5r cell `_OPS/categorical`, T5s cell S2 |
| 4 MEDIUM: T1, T6, T10 lacked node ids; T10 cannot be red first | 8 | 5.0 register (T6 left with slice 3; T10 is now the build-count test, red on the baseline) | 1a, 1e, 2 | T1, T10a, T10 |
| 5 MEDIUM: messages carry a scalar `repr` | 1 | 4 rule M2 | 1a | T3m, T11, T12 |
| 6 MEDIUM: process-wide `lru_cache` retains about 290 MB across jobs | 3 | 4 rule L1 (no cache at all) | 2 | T9, T9c |
| Re-read: N1 admitted `Fraction`; `longdouble` platform-dependent | 6 | 4 rule N1 | 1a, 1c | T3m, T2c, T3 |
| Re-read: E8 config gate raises a bare `ValueError` | 6 and 4 | 3.3 | 1a (root), 1c | T3 entry E8 |
| Re-read: `numpy.int64`/`float32` declined natively, built in Python | 4 | 4 rule A1 and N1 | 1c | T3 valid rows |
| Re-read: shadow binding and chunked step consumers missing | 8 and 4 | superseded in revision 6: no caller inventory exists; the consumers are closed by T7 and the routes by T5r and T5s (3.2, 3.3a D5) | 1e, 4a, 4c | T20, T7, T5r, T5s |
| Re-read: wrong workflow file named for the release sentry | 8 | closed by removal: no workflow is edited or pinned | none | none |

Codex round on revision 3 (closed in revision 4, carried):

| Finding | Class | Section | Slice | Test |
|---|---|---|---|---|
| 1 HIGH: two identities added without a registry bump fail `pin_violations` | 10 | 5.3 "Reason registry effect", 8 | 1r | T17, `test_live_registry_honours_the_released_pin` |
| 2 MEDIUM: the caller scan omitted two callers | 8 and 4 | superseded in revision 6 (caller scans deleted); the two callers' entries stay as T3 entries E9 and E10 | 1c (T3), 1e, 4a, 4c | T3, T20, T5r |
| 3 MEDIUM: a module `_CACHE` dict passes the memo sentries | 3 | 4 rule L1: the memo and its sentries are gone | 2 | T9 and T9c with their seeded violations |
| 4 MEDIUM: `isinstance` admits registered integrals and lying subclasses | 6 | 4 rule N1 | 1a (builder), 1c (every entry) | T12n (1a), T2c (1c), T3 rows |
| 5 MEDIUM: T12 swept unrelated handler messages | 8 | 5.1 T12 scope | 1e; 1d | T12; T11b |
| 6 MEDIUM: route agreement test green on the baseline | 8 | 5.1 T5r, T5s (each cell against the KAT, never against another route) | 4a | T5r, T5s |
| 7 MEDIUM: fixture and digest can move without a contract edit | 10 | 5.1 T1, 8 | 1b, 4a | `test_contract_entry_records_the_fixture_digests` |
| Re-read: route-layer messages render the config and type names | 1 | 4 (route-layer messages) | 1d | T11b |
| Re-read: N1 by named numpy types is platform-dependent | 6 and P1 | 4 rule N1 step 1 | 1a | T12n; T3 row `numpy.longlong` (1c) |
| Re-read: pre-mortem item listed the required bump as a failure | 10 | 11 item 14 | 1r | T17 |

Confirmed closed by the Codex gate on revision 5, unchanged in revision 6: the registry minor bump and literal `since`; exact-type N1; scoped T12; the independent KAT; contract-pinned golden digests; slice order (2 after 1a and 1c); slice bounds; the `1.1.0` to `1.2.0` rule.

Codex round on revision 5 (closed in revision 6):

| Finding | Class | Section | Slice | Test |
|---|---|---|---|---|
| 1 BLOCKER: the caller inventory misses control-flow and streaming propagation (`execution/_chunked.py:309`, `_chunked_oracle.py:113`, `:213`, `_pipeline_auto_chunk.py:409`, `native/_when_admission.py:211`, `:232`); fourth occurrence | 4 with 8 (route coverage) | 0.4 (stop: no fifth scan), 1 (guarantee), 3.2 (mechanisms a and b), 3.3a (each named path, with its cell or its stated limit), 14 | 1e, 4a, 4c | T20, T5r, T5s; the six named paths are rows D3, D4 and D7 of 3.3a |
| 2 HIGH: the local interpreter gate is fail-open (no status accumulation, `|| echo`) | 8 | 5.4 (committed script, seeded cells, probe of both versions) | 1r (script), every slice (evidence) | the five seeded gate cells of 5.4 |
| 3 MEDIUM: T9 cannot see copied contents (`LEAK.append(list(cdf))` passes) | 3 | 4 rule L1 statement, 5.1 T9c | 2 | T9c with `list`, `tuple` and Arrow seeded copies |

Codex round on revision 6 (closed in revision 7):

| Finding | Class | Section | Slice | Test |
|---|---|---|---|---|
| 1 BLOCKER: T20 is evadable (`running = running + weight`, append-based prefix construction, `operator.add`), so the revision 6 guarantee was false | 8 (overclaimed evidence) with 4 | 1 (guarantee narrowed to four claims; T20 a ratchet), 3.2 (broadened shapes, ratchet label, limits), 0.4 | 1e | T20 with nine seeded shapes; the revision 6 rule probed against them: three missed |
| 2 BLOCKER: T5r derives cells only from the draw-site map; `categorical_fast` in `GROUP_B_STRATEGIES` with no map entry gets no cell and the set test stays green | 4 with 8 | 3.2 (member closure with `NO_WEIGHTED_CATEGORICAL_PATH` and delegated cells), 5.1 T5r | 4a, 4c | T5r closure test with the three handoff seeds plus `remove_delegated_cell` |
| 3 HIGH: `CDF_GATE_PYTHONS=3.12` narrows an ordinary slice gate to one interpreter | 8 (fail-open gate; second occurrence after Codex round 5 finding 2) | 5.4 (no override, paths only, fixed environment, marker phase for T14), 5.5 | 1r | the 5.4 seeded cells and the 32-combination environment run |
| 4 MEDIUM: the T9c `tuple(cdf)` seed returns the same object, so it retests identity | 8 (a seed that tests the wrong thing) | 5.1 T9c | 2 | T9c seed `tuple_of_list` |

Codex round on revision 7 (closed in revision 8):

| Finding | Class | Section | Slice | Test |
|---|---|---|---|---|
| 1 BLOCKER: completeness still claimed in five sentences (section 1 line "Every weighted-categorical route", claim (ii), rule L1 "built only at these points" and "anywhere", section 7 "single owner"); the 47 no-path pairs described as proven | 8 (overclaimed evidence) with 4; third occurrence | 1.1 (claims table, rows C1, C3, C4, C21, C22, C31, C35), 0.4, and every rewritten sentence listed in 0.1 | none (plan text) | the section 13 hand check (grep census) |
| 2 HIGH: the `replaced_member` seed would not fail as written (`CategoricalStrategyHandler.run` calls the module-global `_build_cdf`, `_categorical.py:188`; a subclass has no builder seam) | 8 (a seed that cannot go red) | 5.1 T5r (`FloatCdfCategoricalHandler`), 5.0 register, 13 (probe) | 4c | `test_seeded_registry_changes_are_reported[replaced_member]`, probed red |
| 3 LOW: section 0.1 said 17 T20 scopes, section 3.2 listed 16 | 8 | 3.2 (one count), 0.1a (no count) | 1e | T20 equality in both directions |
| 4 LOW: D-4 cited at platform `docs/ROADMAP.md:480`; it is at `:516` | 8 | risk paragraph | none | none |

### 0.3 Found while writing revision 8

| Finding | Class | Disposition |
|---|---|---|
| J. `test_mask_strategy_map_covers_live_registry_exactly` compares `MASK_STRATEGY_TO_SITE` with `SCALAR_HANDLERS` only (`tests/native/test_draw_site_inventory_coverage.py:130`); revision 7 wrote "a new handler cannot be added without choosing its draw site" as if it covered R2 to R5 | 8 | Reworded (section 3.2, row C29): the sentence names R1; members of R2 to R5 are classified by the T5r closure, not by that test |
| K. A T5r driver that binds `SCALAR_HANDLERS` with a `from` import at test-module load holds the shared dict, so a test-local copy installed by `monkeypatch.setattr` is invisible to it and the `replaced_member` seed passes (probed, section 13) | 8 (a seed that would not fail) | `members()` and the drivers read `importlib.import_module("decoy_engine.execution._strategies").SCALAR_HANDLERS` on every call; the register names the import-time binding as the mutation for the seed |
| L. `PandasExecutionAdapter` copies `dict(SCALAR_HANDLERS)` when it is constructed (`_pandas_adapter.py:118`), so the R1 cell, which calls the registry member directly, does not show that the adapter dispatches to the same object | 4 | Stated as a limit of row C4. The cells that run through the adapter are S3 (`run_seq`, Python backend), S8 (chunked oracle) and the three D4 lanes; no new cell is added |
| M. Rule L1 and section 6 said "every CDF built during a run" and "no copy survives" for all runs; T9 and T9c drive five run kinds | 8 | Narrowed to the five run kinds (rows C22, C23); other run kinds are listed as not proven |

### 0.3a Found while writing revision 7 (carried)

| Finding | Class | Disposition |
|---|---|---|
| G. `SCALAR_HANDLERS["nested"]` reaches the categorical handler through a registry lookup (`execution/_strategies/_nested.py:155`), with a weighted config under `strategy_config`, while `MASK_STRATEGY_TO_SITE` maps `nested` to no draw. A map-derived cell set misses it, the same shape as Codex finding 2, and a reviewer would have had to enter it in the no-path table falsely | 4 with 8 | Covered: `DELEGATED_CELLS` in `tests/parity/_categorical_routes.py` declares the two `nested` cells explicitly (both weighted variants, each asserting the KAT category on the JSON leaf or the registered decline), and the closure test treats a delegated cell exactly like a derived one. A grep of `src/decoy_engine` on `5154830` for `SCALAR_HANDLERS[`, `SCALAR_HANDLERS.get`, `_build_cdf` and `CategoricalStrategyHandler` finds no other delegation (a reviewed assertion, row C15) |
| H. The repo's `addopts` carries `-m 'not benchmark and ...'` (`pyproject.toml:408`), and an explicit `-m` on the command line replaces it. A gate that passes its own `-m` would silently re-include benchmark, testflight, packaging, codspeed and determinism-harness tests | 9 (a gate that changes what the default suite means) | The script reads the default expression from `pyproject.toml` and combines it with its own marker term; failing to read it fails the gate |
| I. Claim (i) of the delegated guarantee names exhaustive small cases; no revision had such a test | 8 | T2x added (slice 1e) |

Revision 6's findings, carried: D (P8 and A1 over the named entries E1 to E10 only) stays a stated limit; E (`generation/statistical/_sample.py:140` `_cumulative`, a float prefix table outside the guarantee) stays named; F (revision 5's gate exited 0 on a 3.10-only failure) is closed. Revision 5's: A is superseded with the closure; B (default dataclass reprs, class 1) stays in the section 15 backlog; C (prebuild before 1a) is why slice 2 follows 1a and 1c.

### 0.4 Stop-on-repeat report

- Class 8 with 4 (claims of completeness), third occurrence. Instances: revisions 3 to 5 claimed route coverage by enumerating callers (four findings); revision 6 claimed it by a closed-world scan and a map-derived registry set (Codex round 6 findings 1 and 2); revision 7 narrowed the guarantee to four claims and still wrote five sentences whose wording exceeded their mechanism (Codex round 7 finding 1). The worker rules say a class that recurs is reported, not patched a third time; the revision 8 handoff names this class and prescribes the fix, so revision 8 applies it and records the limit of the fix here. The structural fix: every guarantee sentence is a row of one table (section 1.1) with a mechanism column and a non-proof column; a sentence outside the table may describe what a test reads or runs, state history, or state a limit, and may not state what the code does without naming its row. The limit of the fix: the census is a grep over a word list, read by the author, and a guarantee phrased without any census word would be missed; the section 13 hand check records the word list and the hit count so a reviewer can repeat it. The class is not a particular scan; it is a guarantee sentence that says "every" or "only" about code a static check reads. The guarantee is the four claims of section 1, each a statement about a named object (the builder, the five registries' members on the commit the test runs against, the named branches) that a test executes, plus one statement that T20 is a ratchet; the rest of the table holds the invariants and rules with the same two columns. What a ratchet does not see is a limit, written next to it. A new evasion of T20 is now a known limit of a ratchet, not a false guarantee, because no remaining sentence claims more than its test runs; a reviewer who finds an evasion should find it already listed as a limit in section 3.2.
- Class 4 (route coverage, the registry side). Codex finding 2 and finding G are both members missed because the cell set was derived from a mapping that does not list them. Fix: the closure is over the registries' own membership, and every member must be classified (cell, delegated cell, or no-path with a reason); the mapping only decides which variants a classified member gets.
- Class 8 (gate). Second occurrence (Codex round 5 finding 2: fail-open status; round 6 finding 3: a narrowing override). The class is any input that can make the gate run less than the three interpreters. Fix: the script has no input but paths; the interpreter list is a constant; pytest runs under `env -i`; the default marker expression is read from the committed `pyproject.toml`. Evidence: the script reads no environment variable (every `$` expansion is its own variable or a positional argument), pytest runs under `env -i`, and the seeded 3.10-only failure exits 1 under all 32 subsets of the five variables most likely to narrow a run.
- Class 3 (state). Unchanged from revision 6: no memo, cache or shared store; T9 by identity, T9c by content, now with a real copy as the tuple seed.
- Class 6 (N1) and class 1 (messages): unchanged from revision 6.

## 1. Problem and evidence (reproduced from the lock)

`_build_cdf` (`src/decoy_engine/execution/_strategies/_categorical.py:69-119`) computes `total = sum(weights)` (`:75`), a plain running sum (`:92`), then `int(running / total * _WEIGHTED_CDF_RES)` (`:94`). CPython 3.12 made the builtin `sum()` of floats compensated (Neumaier summation; CPython gh-100425, cited from memory, behavior confirmed by the probe below). So `total` differs from 3.10 and 3.11 for many weight vectors, and the truncated thresholds move by one slot. The routes the T5r and T5s cells drive consume this one list (section 3.2; row C1), so their band boundaries, and with them the masked output, depend on the interpreter.

Probe, run from `uv.lock` (`uv sync --frozen --extra dev --python 3.X` into three scratch venvs; CPython 3.10.20, 3.11.18, 3.12.3; same checkout `545d913`):

```
python -I cdf_probe.py   # print(sum([0.6, 0.3, 0.1]), _build_cdf([0.6, 0.3, 0.1]))
3.10.20 sum= 0.9999999999999999 cdf= [600000, 900000, 1000000]
3.11.18 sum= 0.9999999999999999 cdf= [600000, 900000, 1000000]
3.12.3  sum= 1.0                cdf= [600000, 899999, 1000000]
```

A seeded corpus of 20,000 weight vectors gives an identical digest on 3.10 and 3.11 and a different one on 3.12: 460 of 20,000 CDFs differ (2.3%). The corpus has 2 to 200 categories and mixes rounded decimals, raw `random()`, vectors normalized to 1.0, and a small repeated-value set with zeros. Edge vectors:

| Input | 3.10 / 3.11 | 3.12 |
|---|---|---|
| `[0.1] * 10` | `..., 800000, 900000, 1000000` | `..., 799999, 899999, 1000000` |
| `[1e16, 1.0, -1e16]` | `categorical_weights_nonpositive` | `categorical_weight_below_resolution` |
| `[M, 0.6h, 0.6h]` (`M` max double, `h` half its ulp) | `sum` finite | `sum` = inf |
| `[1e308, 1e308]` | `categorical_weight_below_resolution` | same |
| `[nan, 1.0]`, `[inf, 1.0]` | bare `ValueError` | bare `ValueError` |

`random.Random.choices` has byte-identical source on 3.10 and 3.12 and uses `itertools.accumulate`, not `sum`. A 10,000-draw probe digest is identical on all three interpreters. The same invalid vector also gets different outcomes on different routes today, whatever the interpreter (section 5.1 T3 table).

Guarantee (revision 8). Threat model: V1, accidental drift by a reviewed author; a deliberately evasive author is V2 and out of scope. After this plan lands, section 1 claims the following four rows of the claims table (section 1.1) and nothing that is not a row of that table:

1. (i) `_build_cdf` returns the `Fraction` oracle's tuple, or the oracle's error code at the oracle's index, for every T1 corpus vector, every T2x grid vector and every T2 draw, on CPython 3.10, 3.11 and 3.12 as the section 5.4 script runs them (row C2).
2. (ii) Each member of the five named registries, read on the commit the test runs against, is classified: it has a derived or delegated cell, or a reviewed no-path entry (row C3). For the cells that exist (12 on `5154830`: 10 derived, 2 delegated), the member's output on the slot-hit values is the category the KAT predicts for the builder's tuple, or the registered decline (row C4). The 47 no-path entries are reviewed assertions (row C35).
3. (iii) T20 is a RATCHET that catches the common accidental shapes and is explicitly not a proof against a differently written builder (row C5); T7 is a ratchet of the same kind (row C6).
4. (iv) The branches D1 to D6 of section 3.3a have the cells named there; D7 and D8 are limits. The list of branches is a reviewed reading of the dispatchers on `5154830`, not a derived set (row C7).

### 1.1 Claims table

Method. Every sentence in revision 7 containing one of the census words (section 0.1) was read; the ones that say what the code does after the plan lands are rows here, in plan order. History (sections 0 and 13), descriptions of what a test reads or runs, and limits are not rows. Each row names the mechanism that proves the claim as worded, and what that mechanism does not prove. A row's wording is the strongest the plan makes; the sentence at the cited place says the same or less. Rows C2 to C7 are section 1; everything else in the plan that reads as a guarantee is one of the remaining rows or is history, a run description or a limit.

| Id | Where | Claim, as the plan now states it | Mechanism (test ids) | What the mechanism does not prove |
|---|---|---|---|---|
| C1 | 1, problem | The routes the T5r and T5s cells drive consume `_build_cdf`'s output | T5r and T5s observe the builder's tuple at each cell's consumer through a slot-hit value (`_categorical.py:238`, `_mask_group_b.py:390`, `_categorical_ext.py:137`) | any route no cell drives (D7, D8) |
| C2 | 1 (i), 2 P1 | `_build_cdf` returns the oracle's tuple or error code for the T1 corpus, the T2x grid and the T2 draws on CPython 3.10, 3.11 and 3.12 | T1 (KAT and digests), T2x, T2, T3m, run by the 5.4 script on the three lock venvs; T12 and T12n (integer-only operations after N1) | platforms other than Linux; numpy versions other than the lock's one per interpreter; vectors outside the corpus, grid and draws (T12 is the structural argument, not a proof) |
| C3 | 1 (ii), 3.2 | Each member of R1 to R5, read at test time, is in exactly one of: a member with a cell, or the no-path table | T5r closure (`test_every_registry_member_is_classified`) with its four live-probed seeds | that a no-path reason is true (C35); a dispatcher that is not one of R1 to R5 (C7) |
| C4 | 1 (ii), 2 P4, 3.6 | For each existing cell (12 on `5154830`), the member's output on the slot-hit values equals the KAT category, or the registered decline | T5r cells against the KAT with the moved-slot precondition; the `replaced_member` seed shows a cell goes red for a member that builds differently | a variant outside the two weighted draw sites; that `PandasExecutionAdapter`, which copies the registry at construction (`_pandas_adapter.py:118`), dispatches to the registry member: the R1 cell calls the member directly, and only S3, S8 and the D4 lanes run through the adapter |
| C5 | 1 (iii), 3.2 | The set of scopes matching the T20 rule equals `_thresholds` plus the 16 reviewed entries, in both directions | T20 (equality, nine seeded shapes, control, non-vacuity) | a builder in a shape the rule does not match (the limits list of 3.2); the ratchet is not an ownership proof |
| C6 | 1 (iii), 3.2, 5.1 T7 | The set of search calls in the named forms equals the reviewed six; `_WEIGHTED_CDF_RES` is loaded only in the three named places | T7 (equality, seeded fixture) | a hand-written search loop; a `getattr` call; a resolution constant written as a literal |
| C7 | 1 (iv), 3.3a | D1 to D6 have the cells named in 3.3a; D7 and D8 are limits | T5s cells S2 to S9 and the three D4 lanes; T14 for D6 | that the branch list is complete (it is a reading of the dispatchers on `5154830`); anything about D7 and D8 |
| C8 | 2 P2 | `cdf[i] = floor((2 C_i RES + T) / (2 T))`, so `cdf[-1] == RES`, non-decreasing, zero width for zero weight, each band within one slot | T2, T2x, T1 against the `Fraction` oracle | inputs outside those tests (T12 is the structural argument) |
| C9 | 2 P3 | Below-resolution rejection if and only if `w_i * RES < T` exactly | T2 `test_resolution_rule_holds_both_directions`, T1 rows at, one ulp below and one ulp above `T / RES` | inputs outside those tests |
| C10 | 2 P2a | A total above the largest double, a NaN or an infinity is `nonfinite` | T3 rows `[nan, -1]`, `[inf, 1]`, `[-inf, 1.0]`, `[-1.0, inf]`, `[numpy.float32("nan"), 1.0]`, `[1e308] * 4`, T3m | vectors outside those rows |
| C11 | 2 P6 | The shape check runs first on E1 to E10; inside the builder the five passes complete in order and report the lowest failing index | T3 rows that fail in two passes (`[nan, -1]`, `[-inf, 1.0]`, `[-1.0, inf]`, `[1e16, 1.0, -1e16]`, `[True, -1.0]`), T3m | pass order for vectors outside those rows |
| C12 | 2 P8, 4 A1 | On every T3 row and T2c case, E4 to E10 report exactly the builder's code (or admit with the builder's tuple), and no T3 row is decided before the builder runs | T3 over E1 to E10, T2c, T2b | a precheck that agrees with the builder on every T3 row; entries outside E1 to E10 (D8) |
| C13 | 2 P9, 3.3, 5.1 T18 | For every invalid T3 row, `prebuilt_cdfs` and `_python_cdf` return without raising, and the consumer raises the same code from the same function as on the baseline | T18 (a) to (c); (d) is from the code | inputs outside the T3 rows; case (d) is not probed |
| C14 | 3.3 planned | After 1a, `_build_cdf` raises `StrategyError` only, on every T3 row at E1 | T3 E1 rows, T3m, T12b (no bare catch remains to hide another exception) | inputs outside the rows; the argument is N1's dispatch on `type(w)` (everything outside two allowlists is `not_numeric` before any arithmetic, T12n) and integer-only arithmetic after it (T12) |
| C15 | 3.2 | Besides `nested`, no member of R1 reaches the categorical handler through a registry lookup | a grep on `5154830` for `SCALAR_HANDLERS[`, `SCALAR_HANDLERS.get`, `_build_cdf` and `CategoricalStrategyHandler` (finding G), read by the author | delegation spelled another way; the grep is not a test; the T5r closure would classify such a member as no-path on the reviewer's word (C35) |
| C16 | 3.2 | A new `SCALAR_HANDLERS` key cannot land without a `MASK_STRATEGY_TO_SITE` entry | `tests/native/test_draw_site_inventory_coverage.py::test_mask_strategy_map_covers_live_registry_exactly` (existing) | R2 to R5 (their members are classified by C3 instead) |
| C17 | 3.4 | Parent and child FK keys mask to the KAT category and join | T8 (`chain_shape`, `composite_shape`, the out-of-core FK route) | self-referencing, cross-table and children-in-another-batch shapes (argued from the builder being a pure function of the config; not run) |
| C18 | 3.5 | The named annotations resolve to `tuple[int, ...]` | T13 over the named hints; a grep on `545d913` found one `list[int]` | a new `list[int]` annotation elsewhere |
| C19 | 3.6, 4 M2 | A builder message renders no weight, label, source value, `repr` or type name; its replacement fields are ints with `d`, `02d` or `+03d` specs | T3m (committed constants), T11 (marker corpora), T12 (`FormattedValue` check over `BUILDER_FUNCTIONS`) | a message formatted outside `BUILDER_FUNCTIONS` and the three T11b sites |
| C20 | 3.6, 4 A1 | Admission reasons from E4 to E8 are exactly `{code}:{name}` | T3 (exact string equality) | reasons produced elsewhere |
| C21 | 4 M1 | The printed bound is the smallest three-significant-digit decimal not below `T / RES` | T3m `test_below_resolution_bound_is_not_below_the_exact_minimum` and the three known answers | bounds for vectors outside those |
| C22 | 4 N1 | `_exact_ratio` decides on `type(w)` with two allowlists and calls only `type`, `int`, `float`, `float.as_integer_ratio`, `math.isfinite` and `StrategyError` | T12n (AST over the helper), T2c, T3 type rows, T3m | a call reached through an alias or `getattr`; the C type code membership facts on platforms other than Linux (probed here only) |
| C23 | 4 L1 table | Slice 2 adds builds at the three points of the L1 table; the out-of-core route builds once per run over 1 and 20 batches, the chunked oracle once over 1 and 5 chunks | T10 | build counts on the in-memory, sequential, chunked-native and physical routes (unchanged code, not counted) |
| C24 | 4 L1 statement, 6 row 3 | Slice 2 adds no module, class, default-argument or global holder and no cache or lookup by value; the handler reuses a prebuilt tuple only by identity | T9 (identity, five run kinds), T9c (content, five run kinds, 1 MiB tolerance), T19 | a holder on a run kind outside the five; a cache that is emptied before the run returns (passes T9 and T9c; T19 sees a value-keyed lookup only in the handler); memory outside the Python allocator and the default Arrow pool |
| C25 | 4 L1 statement, 6 row 2 | For each of the five T9 run kinds, no CDF built during the run is reachable after it returns and its stream is drained or closed, and no copy of its contents above the tolerance survives | T9, T9c with their seeded violations | as C24 |
| C26 | 4 memory | Under L1 the out-of-core and chunked-oracle routes hold one tuple per weighted column per run | T10 | the other routes (unchanged) |
| C27 | 4 output consequence | The measured deltas on the 20,000-vector corpus (one slot per moved threshold, 20 rejected) | the revision 2 scratch prototype on 3.12, one container | the corpus is seeded and fixed; other vectors |
| C28 | 5.1 T1 | The KAT generator imports nothing from `decoy_engine` and disagrees with the float builder on at least 50 vectors | `test_kat_generator_imports_nothing_from_the_engine`, `test_kat_disagrees_with_the_float_builder_on_at_least_50_vectors` | that the generator's `Fraction` arithmetic matches section 4 (reviewed by reading) |
| C29 | 5.2 | Each existing-test change replaces an expected value with a stricter or corrected one; none is widened or skipped | the gate reviewer reads the 5.2 diff; the 47-file comparison (5.4) exits 1 on any outcome change outside the named node ids | nothing beyond the named nodes; this is a review rule, not a test |
| C30 | 5.3 | No committed golden uses weighted categorical; the four F29 files are not edited | a grep over the golden directories; `test_the_baseline_fixture_is_frozen`, `test_live_registry_honours_the_released_pin` (digest pins) | the grep is a reviewed assertion for goldens outside the pinned files |
| C31 | 5.4 | No argument and no environment variable narrows the interpreter list or the test selection of the gate | seeded cells (table in 5.4), the 32-subset run, the `grep` over `$` expansions; the interpreter check per cell; the JUnit skip and count checks | an edit to the script, `conftest.py` or `pyproject.toml` on the branch under review (the reviewer reads the diff); a venv whose packages differ from the lock (the script checks the minor version and prints numpy's version only); a test deselected by `collect_ignore` rather than skipped |
| C32 | 7 | `_build_cdf` stays the builder the cells drive, and the set of functions reachable from it equals `BUILDER_FUNCTIONS` plus `_exact_ratio` | T12 closure test; T20 as a ratchet | ownership (C5) |
| C33 | 7 | Touched modules stay within the size rule | `tests/sentry/test_module_size.py` with the census entry moved in the same commit | nothing further |
| C34 | 8 | The contract's standing sentence: `_build_cdf` output and error text are identical on CPython 3.10, 3.11 and 3.12 for the T1, T2x and T3m inputs | the 5.4 gate on those tests; the digest lines checked by `test_contract_entry_records_the_fixture_digests` | routes (C4); other platforms and numpy versions (C2) |
| C35 | 3.2, 1 (ii) | Each of the 47 no-path entries names a member that, by the author's reading of `5154830`, reads no categorical `weights` and references none of the six builder entry names | a grep of `src/decoy_engine` on `5154830`, read by the author; T5r checks only that the member is present in exactly one place | that any reason is true; a member that reaches a weighted CDF while carrying a no-path entry passes T5r. These are REVIEWED ASSERTIONS |
| C36 | 5.3 | The two new reason codes land under one minor bump with literal `since` values and the partners in the same commit | T17, `test_live_registry_honours_the_released_pin`, `test_reasons_doc_states_the_live_registry_version`, `test_the_consumer_contract_fixture_is_current` | the merge-order rule with A-4 (a gate check, not a test) |

Census result on revision 8 (section 13 records the command): every hit of a census word outside this table is in section 0 or 13 (history), describes what a test reads or runs, states a limit, or names its row.

## 2. Invariants as properties

- P1 (interpreter independence). For the T1 corpus, the T2x grid and the T2 draws, on CPython 3.10, 3.11 and 3.12 (the `requires-python` range, `pyproject.toml:13`, `>=3.10,<3.13`) with the lock's numpy per interpreter, on Linux, `_build_cdf(w)` returns the same tuple or raises the same registered code with the same message text (row C2). For other inputs, platforms and numpy versions the plan has an argument, not a measurement: after rule N1 converts each weight to its exact rational value, the builder and the messages it formats use integer operations only (T12).
- P2 (exactness). `cdf[i] = floor((2 * C_i * RES + T) / (2 * T))`, where `C_i` and `T` are the exact rational prefix sum and total (round half up). So `cdf[-1] == RES`, `cdf` is non-decreasing, a zero weight has zero width, and every band is within one slot of `w_i / T * RES` (row C8).
- P2a (representable total). A vector whose exact total exceeds `sys.float_info.max`, or that holds a NaN or an infinity, is rejected with `categorical_weights_nonfinite`. This keeps today's decline of overflowing sums (`tests/native/test_chunked_nondet_categorical_admission.py:122`; `tests/parity/test_phase_d_d1b2_categorical_disposition.py:150`).
- P3 (exact, position-free resolution rule). A positive weight is rejected with `categorical_weight_below_resolution` if and only if `w_i * RES < T` exactly (row C9).
- P4 (one reviewed builder). Every route that the T5r cells and the T5s cells execute maps a bucket to the category the KAT predicts for the builder's tuple (row C4). The ratchets T20 (threshold arithmetic) and T7 (search call sites) flag the common accidental ways of adding a second builder or a second search; they are not a proof that none exists (claim iii).
- P6 (total, interpreter-free error precedence). The shape check (`categorical_weights_shape`) runs first at the route layer on E1 to E10 (T3, row C11). Inside the builder, each pass completes over the whole vector before the next starts: (1) not numeric (N1), (2) a NaN or infinite element (`nonfinite`), (3) a negative element, (4) the exact total (above the largest double is `nonfinite`, zero is `nonpositive`), (5) below resolution. Each pass reports the lowest failing index. The T3 rows that fail in two passes are the evidence; the order for other vectors is not measured.
- P8 (admission agrees with execution). For every T3 row and T2c case, on each named admission entry E4 to E10 (section 3.2), admission declines with exactly `{code}:{name}` when the Python route raises `StrategyError(code)`, and admits with the builder's tuple when the Python route builds (row C12). The rows include vectors a precheck decides differently from the builder, so a precheck that disagrees with the builder on any row is seen; one that agrees on every row is not.
- P9 (construction does not move errors; new in revision 5). Building a CDF ahead of the batch or chunk that uses it (rule L1) does not raise on any invalid T3 row (T18, row C13). A config the builder rejects raises from the same function, at the same point and with the same code as on the baseline.

P5 (guard equals consumer) and P7 (validation precedes memoization) are removed: P5 belongs to the slice 3 backlog plan (section 15), and P7 had no subject once the memo was cut.

## 3. Enumerations

### 3.1 Sum and threshold sites (grep inventory on `545d913`)

Grep: `grep -rnE '(^|[^.a-z_])sum\(|fsum|Fraction|cumsum|cumulative|bisect|searchsorted|choices|\.choice\(|weights' src/` (161 `sum(` lines). Integer-only sums are listed by family.

| Site (`545d913`) | What it computes | Interpreter-dependent today? | Decision |
|---|---|---|---|
| `_strategies/_categorical.py:75` | `total = sum(weights)` | YES (probe) | 1a: exact integers |
| `_strategies/_categorical.py:92-94` | running sum and truncated threshold | follows `:75` | 1a |
| `_strategies/_categorical.py:90,108,112` | messages: `{w!r}`, and `1.0 / RES * total` with `:.2e` | YES by input type and numpy version (`np.float64(-0.5)` vs `-0.5`, probed) | 1a: rules M1, M2 |
| `_strategies/_categorical.py:176`, `out_of_core/_mask_group_b.py:344`, `native/_categorical_prepared.py:73` | `float(w)` at route entry: rounds ints above `2**53`, turns `bool` and `Decimal` into floats, bare `ValueError`/`TypeError` for `str`/`None` | YES by type (class 6) | 1c: delete all three |
| `native/_categorical_prepared.py:68-71` | admission prechecks | YES by route (class 4) | 1c: delete (rule A1) |
| `generation/statistical/_sample.py:268,296,336,366`, `generation/_window_admission.py:129-130,151-157,211-217,224-228` | sampler and admission weight totals | YES for near-overflow totals | out of scope: section 15 backlog |
| `generation/statistical/_sample.py:140-146,324,356`; `generation/synthesize.py:395,626`; `generation/_column_streams.py:139,210`; `generators/columns.py:413,527` | left folds and `random.choices` | no | none |
| `generators/_distribution.py:143,157-158,259-272,379-388` | NumPy totals and `choice(p=...)` | YES by NumPy version | out of scope; Q6 |
| `quality/fidelity.py`, `quality/shape_fidelity.py`, `quality/_distribution_gof.py` | report-plane float sums, rounded to 6 places | YES for the raw float | out of scope; Q5 |
| `quality/dp_ledger.py:58-64` | `math.fsum` | no | precedent |

### 3.2 Builder entries, route registries, search sites, and the threshold ratchet

Lines in this section are `5154830` (equal to `a3fe49e` for every file cited, see the baseline paragraph). Paths are under `src/decoy_engine/execution/` unless given in full.

Builder entries (`ERROR_ROUTES`, driven by T3), a named list that P8 and rule A1 cover and nothing more (section 0.3 finding D):

- E1 `_build_cdf` (`_strategies/_categorical.py:69`).
- E2 `CategoricalStrategyHandler.run` (`_strategies/_categorical.py:188`).
- E3 `categorical_array` through `_categorical_config` (`out_of_core/_mask_group_b.py:309-345`, build at `:382`).
- E4 `prepare_categorical` (`native/_categorical_prepared.py:34-76`).
- E5 `prepare_positional_categorical` (`native/_categorical_prepared.py:79-103`).
- E6 `decline_reason` (`out_of_core/_categorical_native.py:58-80`).
- E7 `_categorical_admit` (`_sequential_native_ops.py:310-321`).
- E8 `categorical_config_rejection` (`native/_operator_config_rejections.py:55-66`).
- E9 `positional_config_of_entry` (`native/_categorical_positional.py:24-34`).
- E10 `positional_categorical_bindable` (`physical/_shadow_bindings.py:192-216`).

Mechanism (a): the threshold ratchet (T20, slice 1e; claim iii). T20 is a ratchet against accidental drift. It catches the common ways a second CDF builder gets written by accident and is explicitly not a proof that no other builder exists: a builder written in a shape the rule does not match passes it. The test module's docstring says so in those words. The rule is applied to the body of every function, method, lambda and module top level in every `.py` file under `src/decoy_engine/` (nested definitions are scanned as their own scopes):

1. Accumulator. Inside a `for`, `while` or comprehension of the scope: the target of `x += e`; the target of `x = x + e` or `x = e + x`; the target of `x = operator.add(x, e)` (any call whose final name is `add`, `iadd` or `__add__` with `x` among its arguments); the target of a walrus whose value is an addition; a list `L` that receives `L.append(... L[i] ... + ...)` (a prefix list built from its own last element). Anywhere in the scope: a name bound, by assignment or as a loop target, from an expression containing a cumulative call. A cumulative call is a call whose final name is `accumulate`, `cumsum`, `cumulative_sum`, `cumprod` or `reduce`, a `sum(...)` over a slice, or any call that receives `operator.add` (or a name `add`, `iadd`, `__add__`) as an argument. Propagation to a fixed point within the scope: a name assigned from an expression that loads an accumulator, a loop target over an accumulator, and a list that receives an accumulator through `append`, `extend` or `insert` are accumulators too.
2. Grid operation. A `/` or `//` binary operation; a call whose final name is `int`, `round`, `floor`, `ceil`, `trunc`, `rint` or `fix`; a load of `_WEIGHTED_CDF_RES`.
3. Match. A scope matches when one of its grid operations has, in its own subtree, a load of an accumulator or a cumulative call.

T20 asserts that the set of matching scopes, keyed by (module path, qualified name), equals `{("execution/_strategies/_categorical.py", "_thresholds")}` plus `NON_CDF_GRID_SCOPES`, the reviewed list of unrelated matching functions, kept in the test module with one reason per entry. No entry may lie under `execution/_strategies/`. Equality is asserted in both directions, so a new matching function is red until reviewed and a stale entry is red.

Measured on `5154830` with a scratch prototype of this rule (AST only, container CPython 3.13.18). It matches `_build_cdf` (`_categorical.py:94`) and these 16 unrelated functions (the count is stated here only; section 0.1a stated 17 and was wrong), which become `NON_CDF_GRID_SCOPES` with the stated reasons: `execution/out_of_core/_multi_parent.py` `resolve_incoming` (a `Path` join with a loop counter); `execution/out_of_core/_spill_estimate.py` `predict_ooc_disk_bytes` (byte estimate); `expressions/_lark_parser.py` `_ExprTransformer.mul_expr` (the expression evaluator's left fold of `*`, `/`, `//`); `generation/window.py` `generate_rows` and `io/bounded_reader.py` `cut_batch` (byte budgets over `np.cumsum`); `profiling/_classification.py` `_classify_numeric_range`, `profiling/eval/corpus.py` `_cryptic_headers`, `profiling/features/builder.py` `_char_class_fractions`, `_regex_signals` and `_checksum_pass_rates`, `profiling/model_pack/trainer.py` `train_and_evaluate` (profiling features and ratios); `providers_v2/identifiers/_cusip.py` `_luhn_check_digit` (a checksum digit sum); `quality/_dcr.py` `_gower_min_distances`, `quality/_distribution_gof.py` `chi_square_cramers_v`, `quality/synth_report.py` `compute_new_row_synthesis` (report statistics); `validators/_leak_check.py` `validate_leak_check` (a rate). A version that also treated `astype` as a grid operation matched `execution/_strategies/_nested.py` `NestedStrategyHandler.run` (a dtype cast) and was dropped; the NumPy seed still matches through its `/`. After slice 1a the owner is `_thresholds`, and 1a must write `_resolution_bound_text` (rule M1) and the validation passes without a scaled accumulator, so that `_categorical.py` matches only in `_thresholds`.

Seeded shapes (T20), each a function in a fixture under `tests/fixtures/sentry/` that the test scans as if it were `src/decoy_engine/execution/_strategies/_categorical_copy.py`: `aug_assign` (`running += w`, then `int(running / total * RES)`), `assign_add` (`running = running + w`, then `thresholds.append(running * RES // total)`), `append_prefix` (`prefix.append(prefix[-1] + w)`, then `p * RES // total` over it), `operator_add` (`running = operator.add(running, w)`), `functools_reduce` (`functools.reduce(operator.add, weights[: i + 1]) * RES // total`), `itertools_accumulate` (`int(c / total * RES)` over `accumulate(weights)`), `numpy_cumsum` (`np.cumsum(w) / w.sum() * RES`), `walrus` (`(running := running + w) * RES // total`) and `verbatim_float_builder` (today's `_build_cdf`); plus a control that counts rows and must not match. Probed: the revision 6 rule missed `assign_add`, `append_prefix` and `operator_add` (Codex finding 1); the revision 7 prototype matched all nine and not the control, both from a scratch file and with the nine placed in a scratch module under `src/decoy_engine/execution/_strategies/` (deleted afterwards; worktree checked clean).

Limits of the ratchet, stated (claim iii): a builder split across two functions (one returns a prefix list, another scales a list it did not accumulate); a builder that reaches its total through a helper the rule does not name; a float prefix table that is never scaled onto a grid (`generation/statistical/_sample.py:140` `_cumulative`, section 3.1 and 15); thresholds written as literals; and any such builder handing its thresholds to an existing search helper, which T7 and T5r would not see unless that route is a registry member. Under the V1 threat model these are accepted; the plan does not chase them with more shapes (owner decision).

Search sites (T7, slice 4c; a ratchet of the same kind). Every call in `src/decoy_engine` whose final name is `bisect_right`, `bisect_left`, `bisect`, `insort_right`, `insort_left`, `insort` or `searchsorted`, and every call through a local alias imported from `bisect`, keyed by (module path, qualified name), must equal this reviewed set: the CDF searches `_strategies/_categorical.py` `CategoricalStrategyHandler.run` (`:238`), `out_of_core/_mask_group_b.py` `categorical_array` (`:390`) and `native/_categorical_ext.py` `_select` (`:137`); and the non-CDF searches `generation/statistical/_sample.py` `_numeric_row` (`:168`), `generation/window.py` `generate_rows` (`:323`) and `io/bounded_reader.py` `cut_batch` (`:246`). It flags a new search written in those common forms; it does not see a hand-written search loop or a `getattr` call.

Mechanism (b): registry-derived known-answer cells (T5r, slices 4a and 4c; claim ii). These are the production tables that choose which route runs a strategy. The test imports each one and reads its membership when it runs:

| Id | Registry (file:line) | What it chooses | Member key |
|---|---|---|---|
| R1 | `SCALAR_HANDLERS` (`_strategies/__init__.py:44`) | the Python handler for a strategy: in-memory run, sequential Python path, and the chunked oracle per chunk | the dict key (`handler.name`) |
| R2 | `_OPS` (`_sequential_native.py:164`; read at `:241` and `:582`) | the sequential run's native op (admission and kernel call) | the dict key |
| R3 | `OPERATORS` (`_operator_registry.py:287`, built from `_SPECS` at `:131`; categorical spec at `:216-224`) | the native operator descriptor the chunked native route, the physical shadow plan (`physical/_shadow_operators.py:74`, `:224`) and the unified slice (`_unified_slice_admission.py:111`) derive their tables from | `spec.strategy` |
| R4 | `GROUP_B_STRATEGIES` (`out_of_core/_mask_group_b.py:84`; read at `out_of_core/_mask.py:156`, `:257`) | the out-of-core group B masking path | the set element |
| R5 | `CHUNK_CONDITIONAL_STRATEGIES` (`_chunked.py:186`; read at `:331`) | the chunked route's conditional admission, under which the chunked oracle and the chunked native route run the strategy | the set element |

Variant axis. The weighted draw sites are read from `DRAW_SITES` (`native/_determinism_protocol.py:52`): every entry whose `config_fingerprint_source` names `weights`. On `5154830` these are `mask.categorical_deterministic` (`native/_draw_sites_mask.py:56`) and `mask.categorical_nondeterministic` (`:82`). A strategy's variants are its `MASK_STRATEGY_TO_SITE` site (`native/_determinism_protocol.py:69`, categorical at `:83`) and its `_POSITIONAL_DRAW_SITE` site (`physical/_shadow_bindings.py:84`), intersected with the weighted sites. The existing test `tests/native/test_draw_site_inventory_coverage.py::test_mask_strategy_map_covers_live_registry_exactly` already forces every `SCALAR_HANDLERS` key into `MASK_STRATEGY_TO_SITE`, so a new R1 member cannot land without a draw site. That test compares R1 only (finding J, row C16); members of R2 to R5 are classified by the closure below.

Cells and the member closure (claim ii). The test classifies every member of every registry; the draw-site tables decide only which variants a classified member gets.

1. Members. `members()` reads R1 to R5 at test time and returns every (registry, member key). On `5154830` (3.12.3 lock venv) that is 53 pairs: 25 in R1, 9 in R2, 12 in R3, 3 in R4, 4 in R5.
2. Derived cells. For each member, its variants are its `MASK_STRATEGY_TO_SITE` site and its `_POSITIONAL_DRAW_SITE` site, intersected with the weighted sites. Each (registry, member, variant) is a cell. On `5154830`: `categorical` in each of R1 to R5, two variants each.
3. Delegated cells. `DELEGATED_CELLS` in `tests/parity/_categorical_routes.py` is a committed set of cells for members that reach the weighted builder through another member (section 0.3 finding G). On `5154830` it holds (R1, `nested`, each weighted site): `NestedStrategyHandler` looks up `SCALAR_HANDLERS[child]` (`_strategies/_nested.py:155`), so a `nested` column with a weighted `categorical` child runs the categorical handler on its JSON leaves. A delegated cell is driven and checked exactly like a derived one.
4. No-path table. `NO_WEIGHTED_CATEGORICAL_PATH` in the same module is a committed mapping from (registry, member) to a one-line reason. On `5154830` it holds the other 47 pairs. The reasons are REVIEWED ASSERTIONS (row C35), recorded from a grep and read by the author, per strategy: `hash`, `tokenize`, `fpe`, `truncate`, `redact`, `passthrough`, `text_redact`, `text_mask`, `group_key`, `date_shift`, `bucket_perturb`, `bucketize`, `top_code`, `geo_generalize`, `shuffle`, `faker`, `code_set`, `formula`, `derived`, `derived_aggregate`, `joint_mask`, `grouped_series` and `windowed_date` read no categorical `weights` and do not reference `_build_cdf`, `prepare_categorical`, `prepare_positional_categorical`, `categorical_array`, `native_categorical` or `SCALAR_HANDLERS["categorical"]` (grep of `src/decoy_engine` on `5154830`). The test checks that each member is present in exactly one place; nothing checks that a reason is true, and a member that reaches a weighted CDF while carrying a no-path entry passes T5r.
5. Closure. Every member must be in exactly one of: a member with at least one cell (derived or delegated), or a no-path entry. A member in neither fails, a member in both fails, a no-path entry for a member no registry holds fails, a cell without a driver in `KAT_DRIVERS` fails, and a driver for a cell that does not exist fails.

Probed with a scratch prototype of `members()`, the derivation and the closure against the live registries on `5154830` (3.12.3 lock venv): 53 members, 12 cells (10 derived, 2 delegated), 47 no-path entries, no failure. Seeds: `categorical_fast` added to `GROUP_B_STRATEGIES` without touching the draw-site map gave `neither: (GROUP_B_STRATEGIES, categorical_fast)`; the same with `MASK_STRATEGY_TO_SITE["categorical_fast"] = "mask.categorical_deterministic"` gave `no driver: (GROUP_B_STRATEGIES, categorical_fast, mask.categorical_deterministic)`; removing the (R4, `fpe`) no-path entry gave `neither: (GROUP_B_STRATEGIES, fpe)`; removing one `nested` delegated driver gave `no driver`.

Expected behavior per derived cell, from the code (each recorded red-first or as a decline in slice 4a):

| Registry | Deterministic variant | Positional (seeded non-deterministic) variant |
|---|---|---|
| R1 | KAT through `SCALAR_HANDLERS["categorical"].run` (S1) | KAT, row-ordinal buckets |
| R2 | KAT through `run_seq` with the Python reference index kernel (S2), the op taken from `_OPS` | decline: `_categorical_admit` returns `decline_reason`'s code (`_sequential_native_ops.py:310-321`) |
| R3 | KAT through `run_kernel_step` with `CategoricalParams` (S6) | KAT through the positional step (S7) |
| R4 | KAT through the out-of-core Python path with no kernel (S4) | decline: the compat gate's registered code (`out_of_core/_compat.py:327`) |
| R5 | KAT through the chunked oracle over at least five chunks, using the run's `PrebuiltCdf` (S8) | KAT for the config-complete seeded variant (`_chunked_categorical.py:46-50` admits it) |
| R1 `nested` (delegated) | KAT on the JSON leaf for a `nested` column whose child is `categorical` with weights `[1.0, 1.0, 1.0]` | KAT or the registered decline the child handler gives for the seeded variant, recorded by slice 4a |

For a registry whose member is a callable (R1, R2), the cell calls the member object taken from the registry, so replacing the member changes what the cell runs. `members()` and the drivers read the registry through its module attribute on every call (`importlib.import_module("decoy_engine.execution._strategies").SCALAR_HANDLERS`, and the same for `_OPS`), never through a `from` import bound at test-module load, so a test-local copy installed with `monkeypatch.setattr` is what the cell runs (finding K). For a descriptor or set registry (R3, R4, R5), the member names a route, and the cell drives that route's public entry for the member's strategy.

What the registries do not cover is section 3.3a. The provider registry `_PROVIDERS` (`native/_draw_site_providers.py:223`, with `_SOURCE_KEYED_PRIMITIVE` at `:163` mapping both weighted sites to `derive_index`) was considered and is not a route registry: it reproduces the bucket derivation and holds no CDF. `_WHEN_DECLINE_CODES` (`native/_when_admission.py:191`) maps verdict prefixes to decline codes and chooses no route. `_CAPS` (`native/_capabilities.py:483`) is a capability table read by admission, not a dispatcher. The Rust companion has no CDF code; it receives the Python-built integers.

### 3.3 Exit paths of the builder and admission (today, then planned)

Today (`545d913`), from the probe and the code:

- Return of the threshold list: `_categorical.py:119`.
- `nonpositive` (`:76-81`) is checked before any negative check, so a mixed-sign vector's code depends on the total and the interpreter.
- `negative` (`:86-91`), per element inside the loop.
- `below_resolution` (`:103-114`) depends on band position.
- A bare `ValueError` from `int(...)` at `:94` for NaN or infinity. It is caught only by `_categorical_prepared.py:99` and `_categorical_native.py:74`, while E4 called directly and E8 let it escape (`native_route_eligibility` raises it for `[inf, 1]`, probed). `tests/parity/test_phase_d_d1b2_categorical_baseline.py:100-104` pins it.
- A bare `TypeError` from `sum` (`:75`) for `str`, `None`, `Decimal` or arbitrary objects at E1. A bare `ValueError` or `TypeError` from `float(w)` at E2 and E3. `bool`, `numpy.bool_` and `Decimal` pass silently.
- Admission prechecks (`_categorical_prepared.py:68-71`) run before the builder. Every other builder error collapses to `categorical_weights_unbuildable_cdf:{name}`.
- A finite vector whose float sum overflows exits as `below_resolution` on E1 to E3 and as `unbuildable_cdf` on E4 to E8.

Planned:

- Slice 1a (builder only). One set of validation passes in P6 order (N1 first). On every T3 row at E1 the exit is a registered `StrategyError` with an M2 message (row C14), using `categorical_weights_nonfinite` and `categorical_weights_not_numeric` (added by 1r to `src/decoy_engine/reasons/runtime/_part3.py` beside `:97-104` and to its `__all__` at `:8-15`). The builder returns a tuple. From here on `_build_cdf` raises `StrategyError` only on those rows (row C14 states the argument for other inputs), so E8's crash and the bare escapes on the rows are removed at the root. Route entries still call `float(w)` until 1c. The intermediate state is consistent: the routes hand the builder floats, as today, and every error the builder raises on a T3 row is registered.
- Slice 1c (route layer, rule A1). Delete the three `float(w)` conversions, the prechecks at `_categorical_prepared.py:68-71`, the two `except (ValueError, OverflowError)` clauses (`_categorical_prepared.py:99`, `_categorical_native.py:74`; unreachable after 1a, and T12b keeps them deleted) and the literal `categorical_weights_unbuildable_cdf` (pre-GA hard delete; `grep -rn unbuildable_cdf` over decoy-platform is empty). `prepare_categorical` passes the raw weights and maps `except StrategyError as exc` to `f"{exc.code}:{name}"`. The `tuple(...)` wrap around the builder call goes, so the artifact holds the builder's own object (section 4, L1, and T9 depend on that). Rewrite the docstrings at `_categorical_prepared.py:44-52`, `:88-92` and `_categorical_native.py:63-66`.
- Slice 2 (construction, rule L1). The added table-level and run-level builds catch `StrategyError` and leave the column without a prebuilt tuple. The consumer then builds and raises from the same function as on the baseline (P9, T18).

### 3.3a Dispatch not driven by a registry (not covered by construction)

These branches choose, admit or reject a categorical route with ad hoc conditionals. No registry lists them, so mechanism (b) cannot derive them; this section is claim (iv). The ratchets T20 and T7 scan them like any other code and flag the common accidental shapes, nothing more. Where a branch selects a different consumer path, it gets its own named cell (T5s, slice 4a); where it only admits or rejects, it is listed as a limit. Paths are under `src/decoy_engine/execution/`, lines `5154830`.

| Id | Branch | What it decides | Cell or limit |
|---|---|---|---|
| D1 | handler's sequential-native branch (`_strategies/_categorical.py:193-206`, `ctx.seq_native` read through `getattr`) and the `_OPS` admission verdict (`_sequential_native.py:241-250`) | native op (S2) or Python fallback (S3) | T5s cells S2 (admitted, `executed_backend == "rust_companion"` with the reference kernel) and S3 (`hide_cat`, `executed_backend == "python_scalar"`) |
| D2 | out-of-core native or Python (`out_of_core/_mask.py:173-181`, `:417-420`; `out_of_core/_hash_run.py:354`) | `native_categorical_array` (S5) or `categorical_array` (S4) | T5s cells S4 and S5 |
| D3 | chunked native or oracle (`native/_dispatch.py:227` `_static_route_decision`, `:383` `plan_native_route`, `:595` `run_native_or_oracle_chunked`) | chunked native (S6, S7) or chunked oracle (S8) | T5s cells S6, S7 (native) and S8 (oracle) |
| D4 | auto-chunk lane (`_pipeline_auto_chunk.py:408-425`; lane constants at `:56-57`) | streamed (`_run_streamed`, `:409`), dispatcher (`LANE_DISPATCHER`) or legacy oracle (`LANE_LEGACY`) | T5s cells: the S6 and S8 fixtures run once per lane, three lanes |
| D5 | deterministic or positional kernel step (`native/_operator_step.py:418-436`), physical binding (`physical/_shadow_bindings.py:278`, `:346`), shadow operator check (`physical/_shadow_operators.py:224`) | which prepared CDF and kernel a native node runs | T5s cells S6, S7 and S9 (physical shadow binding; if S9 cannot take the Python reference kernel it runs under T14 locally, and the slice records which) |
| D6 | compiled or reference index kernel (`native/_index_ext.py:157` `load_compiled_index_kernel`) | the kernel that buckets values | T14 (local, section 5.5) |
| D7 | admission and rejection gates that hand no CDF to a consumer: `_chunked.py:309` (`reject_nondeterministic_when`), `:332` (`rejects_nondeterministic`); `_chunked_oracle.py:113` (`check_chunked_compatibility`), `:213` (`plan_positional_when_guard`); `native/_when_admission.py:211` (`first_when_decline`), `:232` (`admitted_when_columns`); `out_of_core/_compat.py:327`; `native/_plan.py:335`; `native/_requirements.py:542`; `_pipeline_multi_table.py:71` | whether a column reaches a member at all | Limit. The verdicts these gates take from the builder are covered where the gate is a named entry (E6 to E10, T3). The positional declines of R2 and R4 are cells of T5r. A new gate of this kind, or a change that makes one of them admit a column it should reject, is not covered by construction; the guarantee does not claim it |
| D8 | any new branch outside R1 to R5, and any new admission entry outside E1 to E10 | anything | Limit. T20 and T7 flag it only if it writes thresholds or a search in a shape they match. It could build thresholds another way, drop the CDF (a silent uniform fallback), misroute it, or gate wrongly; only a new cell would show it. The owner declined a production route table for this (Q10, decided) |

The six paths of Codex round 5 finding 1 are rows D3 and D4 (`_pipeline_auto_chunk.py:409`) and D7 (the other five). D3 and D4 are claimed only through their named cells; D7 is a limit.

### 3.4 FK shapes

- A single-column FK key that is a deterministic categorical column stays on Python (`docs/compatibility-contract.md:253`). Parent and child keys call the same builder in the same process, so a run joins with itself. Cross-run join stability breaks once at the cutover; that is the section 8 contract entry.
- Composite, self-referencing, cross-table and children-in-another-batch FK shapes have no categorical-specific path. The builder is a pure per-column function of the config weights, so they are argued to reduce to the single-column case; T8 runs the chain and composite shapes and the out-of-core FK route, and the other shapes are not run (row C17).

### 3.5 Fields of the public and shared result types touched

- `PreparedCategorical.cdf` (`native/_categorical_prepared.py:26-31`): stays `tuple[int, ...] | None`, with values changed per P2.
- `CategoricalCall.cdf` (`_sequential_native_ops.py:148-155`): unchanged shape.
- `StrategyError.code`, `.strategy` and `.message` for each builder exit: two new codes and rewritten messages (M2).
- Admission reasons: `unbuildable_cdf` is replaced by the specific code (A1).
- Return-type declarations (grep at `545d913`, row C18): the one `list[int]` found is `SeqNativeRun.categorical_column(..., cdf: list[int] | None)` at `_sequential_native.py:364`. Its body converts with `tuple(cdf)` at `:374`. Mypy does not check it at its one call site (`_categorical.py:193` reads `seq_native` through `getattr`). Slice 1a changes `:364` to `tuple[int, ...] | None` without changing the line count (the module is at 697 of the 700 maximum). T13 pins it.
- New in slice 2 (on `a3fe49e`): `StrategyContext` (`_adapter.py:121`) gains `categorical_cdfs: Mapping[str, PrebuiltCdf] | None = None`, beside `code_set_records` (`:210`). The `ExecutionAdapter.run` protocol (`_adapter.py:329`, beside `code_set_records` at `:354`) and `PandasExecutionAdapter.run` (`_pandas_adapter.py:163`, `:176`, seeded at `:251`) gain a `categorical_cdfs` keyword. `CategoricalColumnDecision` (`out_of_core/_categorical_native.py:83`) gains `cdf: tuple[int, ...] | None = field(default=None, repr=False)`. `TableCategoricalPlan` gains `cdf_for(column)`; `TableHashPlan` (`_hash_run.py:137`) gains `categorical_cdf(column)`. `mask_column` gains `categorical_cdf`, and `categorical_array` gains `cdf`. New `PrebuiltCdf` (`@dataclass(frozen=True, repr=False)` with fields `seed`, `cdf`) in `_categorical.py`.
- Compiled companion: built and loaded in this container for revision 3 (`present-ok`, ABI `decoy-native-abi-3`). It has no categorical code of its own.

### 3.6 Output surfaces

- Masked data: weighted categorical columns on the routes the 3.2 and 3.3a cells drive (one-slot band moves; section 4; row C4).
- Error text: the builder messages (M2, over `BUILDER_FUNCTIONS`) and the three route-layer categorical messages (1d). None of those carries a weight value, a label, a source value, or the `repr` or type name of any object (row C19); messages formatted elsewhere are not covered.
- Admission reasons from E4 to E8: `{code}:{name}` (row C20).
- Run metrics: `quality_metrics["sequential_categorical"]` (contract `:254`), unchanged.
- Reprs: the new holder types do not render values (`repr=False`); the existing ones are finding B.
- Proof manifest: `docs/proof-manifest.json:441-445` has no weights; `scripts/gen_proof_manifest.py` is re-run as the check.

## 4. Method, rules, and output consequence

Method. Every finite IEEE 754 binary64 value is a dyadic rational `n / 2**k`, available exactly through `float.as_integer_ratio()` (IEEE 754-2019 section 3.3; Python documentation). Scale every weight to the largest power-of-two denominator in the vector. Each weight is then an exact `int`, and prefix sums and the total are exact. Thresholds are `(2 * C_i * RES + T) // (2 * T)`. This is the integer-frequency normalization used where a CDF must be bit-identical across encoder and decoder, as in range coding and asymmetric numeral systems (Witten, Neal, Cleary 1987, CACM 30(6); Duda 2013, arXiv:1311.2540), with the exact input taken from the float. `fractions.Fraction` is the independent test oracle (T1, T2), not the implementation.

Rule N1 (weight normalization, by exact type). The builder takes the raw config values. One helper, `_exact_ratio(w, i) -> tuple[int, int]`, decides on `type(w)` only (row C22):

1. `type(w) in _EXACT_INT_TYPES = frozenset({int} | {numpy.dtype(c).type for c in "bBhHiIlLqQ"})` gives `(int(w), 1)`. The set is built from C type codes because named types differ by platform. On Linux with numpy 2.5.0, `numpy.longlong is numpy.int64` is false (probed); on Windows they are the same type. `bool` and `numpy.bool_` are not members.
2. `type(w) in _EXACT_FLOAT_TYPES = frozenset({float, numpy.float16, numpy.float32, numpy.float64})`: `x = float(w)`, then `math.isfinite(x)` or `nonfinite`, then `float.as_integer_ratio(x)` called on the built-in type, never `w.as_integer_ratio()`.
3. Anything else is `not_numeric`, reported with the index only. That covers `bool`, `numpy.bool_`, `str`, `None`, `Decimal`, `Fraction`, `numpy.longdouble`, complex values, any subclass of an allowed type (a lying `float` subclass, probed), and any class registered with `numbers.Integral` or `numbers.Real`; T2c and T3m run the named cases, and the rest follows from the helper's shape (T12n).

The common denominator is the largest `2**k`, so scaling is a shift. T12n pins the shape of the helper: it calls only `type`, `int`, `float`, `float.as_integer_ratio`, `math.isfinite` and the `StrategyError` constructor. T12n asserts no call named `isinstance` or `issubclass` and no method call on `w` (row C22; a call reached through an alias or `getattr` is not seen).

Rule M1 (below-resolution bound). The exact minimum admissible weight is `T / RES` as `n / d`. The message prints the smallest three-significant-digit decimal that is not below `n / d`, using integers only. The exponent comes from integer comparison, the mantissa is `m = ceil(n * 10**(2 - e) / d)`, `m == 1000` carries, and the text is `f"{m // 100:d}.{m % 100:02d}e{e:+03d}"`. Known answers on 3.10.20, 3.11.18 and 3.12.3: `[1.0, 1e-20, 1.0]` gives `2.01e-06`, `[1e-9, 1]` gives `1.01e-06`, and `[1.0, 2.0e-6, 1.0]` gives `2.01e-06`. It uses the ceiling, not rounding, so the printed bound always satisfies P3.

Rule M2 (canonical message text). A builder message in `BUILDER_FUNCTIONS` renders no weight (row C19). Its replacement fields are Python `int`s with an integer spec (`d`, `02d`, `+03d`): the index, `_WEIGHTED_CDF_RES` and the M1 parts.

| Code | Message text |
|---|---|
| `categorical_weights_not_numeric` | `categorical weight at index {i:d} is not a number; weights must be int or float values (bool, string and null are rejected).` |
| `categorical_weights_nonfinite` (element) | `categorical weight at index {i:d} is NaN or infinite.` |
| `categorical_weights_negative` | `categorical weight at index {i:d} is negative.` |
| `categorical_weights_nonfinite` (total) | `categorical weights sum above the largest finite double; cannot normalize.` |
| `categorical_weights_nonpositive` | `categorical weights sum to 0; cannot normalize.` |
| `categorical_weight_below_resolution` | `categorical weight at index {i:d} is below the CDF resolution (1 / {RES:d} of total). The category would never be selected. Either remove the category or use weights >= {M1}.` |

Route-layer messages (slice 1d). `_categorical.py:149`, `:172` and `_mask_group_b.py:321-322` render the categories config with `!r` and type names. Under M2 they keep the column name and integer lengths and drop the value and the type name: `column {column!r} uses categorical with categories that are not a list; provide categories as a list.` and `column {column!r}: weights must be a list with the same length as categories ({n:d}).` The out-of-core reader omits `{column!r}`, as it does today.

Rule A1 (admission reason). The admission entries E4 to E10 run no weight check of their own (row C12: T3 sees a precheck that disagrees with the builder on any row; one that agrees on every row is not seen). Each calls the builder through `prepare_categorical` and maps `StrategyError` to `f"{exc.code}:{name}"`. E9 and E10 return E5's verdict as an artifact or a bool. Rejected alternative: collapse every error to `unbuildable_cdf`. It loses the diagnosis and loosens two existing tests.

Rule L1 (construction and lifetime; replaces the memo, new in revision 5). Slice 2 adds the builds and the holders in this table, each for the life of one run (rows C23 to C25). It is a table of what the slice adds, not an inventory of every build in the engine:

| Route | Built at | Held by | Builds per run (column with weights) |
|---|---|---|---|
| in-memory, sequential (E2, S1 to S3) | `CategoricalStrategyHandler.run`, once per handler call; the sequential route is whole-table (`_sequential.py:199-209`) | the handler's frame; S2 passes it into `CategoricalCall` for that call | one per table slice, as today |
| chunked oracle (S8) | `prebuilt_cdfs(per_column)`, called once at the top of `_oracle_masked._masked` (`_chunked_oracle.py:393`, `a3fe49e`) | a local of the `_masked` generator, passed to every chunk as `state.adapter.run(..., categorical_cdfs=...)` (`:429-440`), seeded into the per-chunk `StrategyContext` exactly like `code_set_records` | one, independent of the chunk count |
| out of core (S4, S5) | `decide_table` (`out_of_core/_categorical_native.py:174-195`), once per table before the first batch: a native-admitted column reuses `prepared.cdf`; every other weighted column gets `_python_cdf(seed)` | `CategoricalColumnDecision.cdf` inside the run's `TableCategoricalPlan`. `mask_batch` passes `hash_plan.categorical_cdf(column)` to `mask_column`, which passes it to `categorical_array(cdf=...)` | one (two when stage B declines a column: `decline_reason` builds and discards before `_python_cdf` builds), independent of the batch count |
| chunked native, physical (S6, S7, S9) | `prepare_chunked_categoricals`, the shadow binding (already once per run) | the run's prepared artifacts | unchanged |

Statement of the rule. Slice 2 adds no module, class, default-argument or global holder for a CDF, a weight vector or anything derived from one, and no cache or lookup by value on the CDF path (row C24; T9 and T9c measure five run kinds and T19 the handler's identity gate, and a cache emptied before the run returns would pass them). Reuse is gated by object identity: the handler uses `ctx.categorical_cdfs[column]` only if `pre.seed is plan`, and otherwise builds. A plan holds one seed per column (`plan/_serialize.py:112` writes `per_column` as a dict keyed by name), so identity is defense in depth, not a key. A config the builder rejects gets no prebuilt tuple (`except StrategyError`), so the consumer builds and raises where it does today (P9). For each of the five T9 run kinds, no CDF built during the run is reachable once the run returns and its stream is drained or closed (`release_when_done`, `_chunked_oracle.py:145`), and no copy of its contents above the tolerance survives (row C25). T9 checks the first by identity (the builder's own object dies) and T9c the second by content (two maximal runs leave at most 1 MiB behind); run kinds outside the five are not measured.

Other paths stay as they are: `categorical_array` called with no `cdf` (E3 directly, and the resident `mask_table` call at `_mask.py:350`, which runs once per table) builds for itself.

Memory: under L1 the out-of-core and chunked-oracle routes hold one tuple per weighted column per run (T10, row C26), `len(weights)` ints each (about 36 MB at the 999,000-category ceiling). That is the same order as the config list of 999,000 floats the run already holds, and it is released with the run.

Prototype evidence (scratch worktree of `a3fe49e` with slice 2 applied, 77 lines added and 3 removed in 8 production files; never committed). On the lock venv (`uv sync --frozen --extra dev --python 3.12`: CPython 3.12.3, numpy 2.5.0), 15 categorical test files (603 tests) gave the same result as `a3fe49e`: 543 passed, 59 skipped, and 1 failed (`test_skewed_weights_skew_cdf`, the known 3.12 baseline failure). A pytest plugin counting `_build_cdf` calls per test node measured 582 calls in total on `a3fe49e` and 317 on the prototype. `test_ooc_categorical_matches_the_pinned_baseline[1-*]` went from 40 to 1, and `test_chunked_equals_full_frame[1]` from 103 to 4 (that node also runs a full-frame pass). Ten nodes rose by one build: invalid configs now built once at table level and once in the batch, and stage-B declines built twice. To make the old builder raise only `StrategyError` for this run, as slice 1a will, the prototype also caught `ValueError`, `TypeError` and `OverflowError` around both prebuilds. Without that, three disposition tests went red (finding C).

Output consequence (measured on the 20,000-vector corpus against today's 3.10 output):

- Every moved threshold moves by at most one slot. The mean is 21.85 moved slots per vector, so a row changes category with probability at most about 2.2e-5 for these vectors.
- 20 of 20,000 vectors (0.1%) that build today are rejected by P3.
- `[0.6, 0.3, 0.1]` stays `[600000, 900000, 1000000]`; `[1.0, 1.0, 1.0]` becomes `[333333, 666667, 1000000]`.
- `[1e308, 1e308]` and `[1e308] * 4` change code from `below_resolution` to `nonfinite`.
- `[4503609762803756, 13510822533, 9007199259373711]` changes from `[333333, 333334, 1000000]` on every entry today to `[333334, 333335, 1000000]` exactly (probed on all three interpreters).
- `[True, 1.0]`, `[numpy.bool_(True), 1.0]` and `[Decimal("1"), 1.0]` change from a silent build to `not_numeric`. `["x", 1.0]` and `[None, 1.0]` change from a bare error to `not_numeric`. `[numpy.int64(1), numpy.int64(2)]` and `numpy.float32` pairs change from a native decline to native admission.
- Admission reasons become specific codes instead of `unbuildable_cdf`. `[nan, -1]` and `[-inf, 1]` become `nonfinite` instead of `negative`.

Performance (3.12, best of 3, one build, `1.0 + U(0, 1e-3)` weights; revision 2 scratch prototype): 10 categories 0.003 ms today vs 0.02 ms exact (3.5x); 1,000: 0.31 vs 1.18 ms (3.8x); 100,000: 33 vs 131 ms (4.0x); 999,000: 272 vs 1,358 ms (5.0x). Above 1,000,000 positive categories no vector passes P3. Under L1 the out-of-core route and the chunked oracle build once per run, not once per batch or chunk. They cost less than today for any column processed in six or more batches or chunks. A single-batch run at 999,000 categories pays the one-off 5x (about 1.1 s more here). T10 measures the build count; T10a bounds one build.

Rejected alternatives: `math.fsum` for the total (fixes P1 only, keeps the float threshold and the position-dependent guard); pinning the 3.10 left fold (kept as Q1's fallback); exact arithmetic with floor (moves `[0.6, 0.3, 0.1]` to `[599999, 899999]` and turns two existing tests red for no gain); a run-scoped memo (revision 4; the source of every class 3 finding in three rounds, section 0.4).

## 5. Acceptance tests, red first

### 5.0 Test register

"Red on baseline at" names the `545d913` file:line that fails the test, unless marked `a3fe49e`. A "non-red gate" is green on the baseline by design; the entry then names the seeded fixture or mutation that turns it red. "Red against the 1a parent" marks a slice 1e test run against the commit before 1a and recorded red in the 1e evidence.

| Id | Node id | Red on baseline at, or non-red gate | Mutation that must turn it red | Slice |
|---|---|---|---|---|
| T17 | `tests/unit/execution/test_categorical_cdf_reasons.py::test_new_weight_codes_claim_an_unreleased_since` | red: the two identities are absent from `reasons.manifest()` (`_part3.py:8-15`) | declare a code without `since=` (defaults to `1.0.0`, `_registry.py:81`); keep `REGISTRY_VERSION` at the pinned value; `since` above the live version | 1r |
| T1 | `tests/unit/execution/test_categorical_cdf_kat.py::test_builder_matches_every_kat_vector` | `_categorical.py:94` (truncation); `:75` on 3.12 | floor instead of round half up | 1a |
| T1 | `...test_categorical_cdf_kat.py::test_kat_corpus_digests_match` | `_categorical.py:94` | `sum(weights)` back as the total (3.12) | 1a |
| T1 | `...test_categorical_cdf_kat.py::test_kat_disagrees_with_the_float_builder_on_at_least_50_vectors` | non-red gate (non-vacuity) | regenerate the KAT through `_build_cdf` | 1a |
| T1 | `...test_categorical_cdf_kat.py::test_kat_generator_imports_nothing_from_the_engine` | red: generator `tests/vectors/gen_categorical_cdf_kat.py` absent | add `from decoy_engine` to the generator | 1a |
| T2 | `tests/unit/execution/test_categorical_cdf_exact.py::test_builder_matches_the_fraction_oracle` | `_categorical.py:94` | integer total replaced by `sum(weights)` | 1a |
| T2 | `...test_categorical_cdf_exact.py::test_power_of_two_scaling_leaves_the_cdf_unchanged` | `_categorical.py:94` | common denominator from the smallest `2**k` | 1a |
| T2 | `...test_categorical_cdf_exact.py::test_resolution_rule_holds_both_directions` | `_categorical.py:103` | compare thresholds instead of `w * RES < T` | 1a |
| T3m | `...test_categorical_cdf_exact.py::test_every_error_family_message_is_a_known_answer[<family>-<variant>]` | `_categorical.py:90`, `:108` (`{w!r}`), `:112` (`2.00e-06`), `:94` (bare `ValueError`), `:75` (bare `TypeError` for `"x"`, `None`, `Decimal`) | `{w!r}` back; bound as `f"{total / RES:.2e}"`; `isinstance` in N1 (the `LyingFloat` not-numeric variant then builds) | 1a |
| T3m | `...test_categorical_cdf_exact.py::test_below_resolution_bound_is_not_below_the_exact_minimum` | `_categorical.py:112` | rounding instead of ceiling in M1 | 1a |
| T11 | `...test_categorical_cdf_exact.py::test_builder_messages_carry_no_label_or_object_text` | `_categorical.py:75` (a class named `MARKER` appears in the `sum` `TypeError` text), `:90` (`{w!r}` of a negative float subclass whose `repr` is `MARKER`) | interpolate `categories[i]` or `w!r` | 1a |
| T12n | `tests/sentry/test_cdf_exact_ratio.py::test_exact_ratio_converts_by_exact_type_only` | red: `_exact_ratio` absent | an `isinstance`/`issubclass` call; `w.as_integer_ratio()`; any other call | 1a |
| T13 | `...test_categorical_cdf_exact.py::test_native_call_annotations_accept_the_builder_tuple` | `_sequential_native.py:364`, `_categorical.py:69` | `:364` back to `list[int] \| None` | 1a |
| T12 | `tests/sentry/test_cdf_builder_integer_only.py::test_the_builder_uses_no_float_operation` (over exactly `BUILDER_FUNCTIONS`) | red against the 1a parent at `_categorical.py:94` (`ast.Div`), `:83` (`0.0`), `:90`, `:108` (`!r`), `:112` (`:.2e`) | any of those back | 1e |
| T12 | `...test_cdf_builder_integer_only.py::test_builder_function_set_equals_the_call_closure_of_build_cdf` | red against the 1a parent (helpers absent) | a new helper called from `_build_cdf` but not listed | 1e |
| T12 | `...test_cdf_builder_integer_only.py::test_seeded_float_message_is_reported` | non-red gate; fixture `tests/fixtures/sentry/cdf_float_message.py` | weaken the sentry | 1e |
| T10a | `tests/perf/test_categorical_cdf_budget.py::test_one_build_at_999k_categories_within_6x_of_the_float_builder` | non-red performance gate | recompute each prefix as `sum(scaled[: i + 1])` | 1e |
| T20 | `tests/sentry/test_cdf_threshold_ratchet.py::test_threshold_ratchet_matches_only_the_owner_and_reviewed_scopes` (docstring: "a ratchet against accidental drift, not a proof") | red against the 1a parent: the owner `_thresholds` is absent and `_build_cdf` matches at `_categorical.py:94` | a verbatim copy of today's builder in a new module under `src/decoy_engine/execution/` (run once); delete one reviewed entry; a reviewed entry under `execution/_strategies/` | 1e |
| T20 | `...test_cdf_threshold_ratchet.py::test_seeded_shapes_are_reported[<shape>]` for `aug_assign`, `assign_add`, `append_prefix`, `operator_add`, `functools_reduce`, `itertools_accumulate`, `numpy_cumsum`, `walrus`, `verbatim_float_builder`, and `test_control_is_not_reported` | non-red gate; fixtures `tests/fixtures/sentry/cdf_ratchet_shapes.py`, scanned as if under `execution/_strategies/`. Red-first evidence for the broadening: the revision 6 rule misses `assign_add`, `append_prefix`, `operator_add` (probed) | drop `x = x + e`, the `append` prefix form or the `operator.add` form from the rule (exactly that seed goes unreported) | 1e |
| T20 | `...test_cdf_threshold_ratchet.py::test_scan_matches_the_owner_and_every_reviewed_scope` | non-red gate (non-vacuity) | point the scan at an empty root | 1e |
| T2x | `tests/unit/execution/test_categorical_cdf_exact.py::test_every_small_vector_matches_the_fraction_oracle` | red against the 1a parent at `_categorical.py:94` (`[1, 1, 1]` builds `[333333, 666666, 1000000]`, oracle `[333333, 666667, 1000000]`) | floor instead of round half up; `sum(weights)` back as the total (3.12 cells) | 1e |
| T2b | `...test_categorical_cdf_exact.py::test_integer_weights_above_2_pow_53_convert_exactly_on_every_entry` | `_categorical.py:176`, `_mask_group_b.py:344`, `_categorical_prepared.py:73` (`float(w)` gives `[333333, 333334, 1000000]` after 1a's builder) | reinstate one `float(w)` | 1c |
| T2c | `...test_categorical_cdf_exact.py::test_subclasses_and_registered_numeric_types_are_rejected[<case>-<entry>]` | `_categorical.py:75` (E1, recorded on `a3fe49e`), `:176`, `_mask_group_b.py:344` (E2, E3), `_categorical_prepared.py:68` (E4 to E10 admit a `float` subclass) | `type(w) in` replaced by `isinstance`; `float.as_integer_ratio(float(w))` replaced by `w.as_integer_ratio()` | 1c |
| T3 | `tests/unit/execution/test_categorical_cdf_route_errors.py::test_every_route_reports_the_same_code[<entry>-<row>]` over E1 to E10 | measured per cell (section 5.1 table) | reinstate a precheck at `_categorical_prepared.py:68-71`; collapse to `unbuildable_cdf`; swap the negative and non-finite passes | 1c |
| T12b | `tests/sentry/test_cdf_no_bare_catch.py::test_no_bare_error_catch_around_the_builder` | `_categorical_prepared.py:99`, `_categorical_native.py:74` | reinstate either clause | 1c |
| T11b | `...test_categorical_cdf_exact.py::test_route_layer_messages_carry_no_config_value_or_type_name[<entry>-<case>]` | `_categorical.py:149`, `:172`, `_mask_group_b.py:321-322` | put `{raw_categories!r}` or a `type(...).__name__` back | 1d |
| T1 | `...test_categorical_cdf_kat.py::test_contract_entry_records_the_fixture_digests[<fixture>]` (`kat` in 1b, `slot_hits` in 4a) | red: contract section 3.3 (`:307-313` on `a3fe49e`) records no digest | regenerate a fixture and its digest without the contract edit | 1b, 4a |
| T9 | `tests/unit/execution/test_categorical_cdf_lifetime.py::test_no_cdf_outlives_its_run[<run>]` for `in_memory`, `sequential`, `chunked_oracle`, `out_of_core_python`, `out_of_core_native` | non-red gate (nothing retains today; probed with the same mechanism over 38 test nodes on the prototype, 0 retained) | seeded: `test_seeded_retention_is_reported` (below); source mutation run once: `_python_cdf` appends its tuple to a module list (probed: 9 more nodes retained) | 2 |
| T9 | `...test_categorical_cdf_lifetime.py::test_seeded_retention_is_reported` | non-red gate: the test wraps `_build_cdf` in `out_of_core._categorical_native` and `_strategies._categorical` with a wrapper that appends each result to a list on a `types.ModuleType` it creates, runs the out-of-core and chunked-oracle runs, and asserts the lifetime check reports both | make the check ignore live tokens | 2 |
| T9c | `...test_categorical_cdf_lifetime.py::test_no_cdf_content_outlives_its_run[<run>]` for the same five runs | non-red gate (nothing retains today). Copy sizes probed at the builder level on 3.12.3 for a 999,000-entry CDF: 38.1 MiB traced for a `list` or `tuple(list(...))` copy, 7.6 MiB in the Arrow pool for a `pyarrow.array` copy, 0.0 MiB with no copy | source mutation run once: `_python_cdf` appends `list(cdf)` to a module list | 2 |
| T9c | `...test_categorical_cdf_lifetime.py::test_seeded_content_copies_are_reported[<copy>]` for `list`, `tuple_of_list`, `arrow` | non-red gate: the test wraps `_build_cdf` in `out_of_core._categorical_native` and `_strategies._categorical` so that it appends `list(cdf)`, `tuple(list(cdf))` or `pyarrow.array(cdf)` to a list on a `types.ModuleType` it creates, runs the out-of-core and chunked-oracle runs at 999,000 categories, and asserts the check reports each. The seed is `tuple(list(cdf))`, not `tuple(cdf)`: the latter returns the same object for an exact tuple (probed), so it would retest identity | a tolerance at or above 7.6 MiB; measuring `tracemalloc` alone (the `arrow` seed then passes); the seed back to `tuple(cdf)` (it then also trips T9, which is not what the seed is for) | 2 |
| T10 | `tests/unit/execution/test_categorical_cdf_construction.py::test_build_count_is_independent_of_batch_count[out_of_core]` | red: `_mask_group_b.py:382` (`a3fe49e`) builds once per batch (20 builds over 20 batches) | `categorical_array` ignores `cdf=`; `mask_batch` stops passing `categorical_cdf` | 2 |
| T10 | `...test_categorical_cdf_construction.py::test_build_count_is_independent_of_batch_count[chunked_oracle]` | red: `_categorical.py:188` builds once per chunk (5 builds over 5 chunks) | drop `categorical_cdfs=` from the per-chunk `adapter.run` call | 2 |
| T18 | `...test_categorical_cdf_construction.py::test_prebuild_never_raises_and_errors_surface_where_they_did[<route>-<row>]` | non-red gate on the baseline (no prebuild exists). Red if slice 2 lands before 1a and 1c (prototype, finding C: three disposition nodes red) | `_python_cdf` or `prebuilt_cdfs` lets `StrategyError` propagate (the zero-batch and guard-first cases go red) | 2 |
| T19 | `...test_categorical_cdf_construction.py::test_a_prebuilt_cdf_for_another_seed_is_ignored` | non-red gate (no prebuilt mapping exists on the baseline) | drop the `pre.seed is plan` check; key the mapping by `seed` value | 2 |
| T5r | `tests/parity/test_categorical_cdf_routes.py::test_every_registry_cell_matches_the_kat[<registry>-<member>-<site>]`, parametrized at collection from R1 to R5 and the weighted draw sites (section 3.2) | `_categorical.py:94`, observed at each cell's consumer (`_categorical.py:238`, `_mask_group_b.py:390`, `_categorical_ext.py:137`); a cell with no driver fails with the cell named | the handler's `_build_cdf` call at `_categorical.py:188` rebound to `tests/perf/_float_cdf_reference._build_cdf` (the four R1 cells red: `categorical` and `nested`, both variants); a local float builder in `_mask_group_b.py` (R4 deterministic cell red) | 4a |
| T5r | `...test_categorical_cdf_routes.py::test_every_registry_cell_key_pair_has_a_moved_slot_hit` | non-red gate (precondition) | delete the generator's hit search | 4a |
| T5s | `...test_categorical_cdf_routes.py::test_every_sub_route_matches_the_kat[<cell>]` for S2 to S9 and the three D4 lanes (section 3.3a) | `_categorical.py:94` at the same consumers; S2 measured (baseline `["z", "z", "z", None]`) | `tuple(cdf)` at `_sequential_native.py:374` replaced by a rebuilt tuple (S2 red); `prebuilt_cdfs` built with the float reference (S8 red); one lane drops `categorical_cdfs` | 4a |
| T5s | `...test_categorical_cdf_routes.py::test_slot_hit_fixture_values_hit_the_moved_slot[<cell>]` | non-red gate (precondition) | edit a fixture value | 4a |
| T8 | `tests/parity/test_categorical_cdf_fk.py::test_weighted_categorical_fk_keys_join_and_match_the_kat` | `_categorical.py:94` (slot-hitting keys mask to `z`, expected `y`) | build the child key with the float builder | 4c |
| T14 | `tests/native/test_cdf_native_present.py::test_weighted_cdf_selection_on_the_compiled_kernel` | `_categorical.py:94` (compiled kernel emits `z`, expected `y`; probed) | any change that moves the CDF before `native_categorical` | 4a |
| T14 | `tests/native/test_cdf_native_present.py::test_the_compiled_kernel_buckets_the_slot_hit_values` | non-red gate (raw buckets are 666666, probed) | edit a fixture value | 4a |
| T5r | `tests/sentry/test_cdf_route_registry_cells.py::test_every_registry_member_is_classified` | red: `KAT_DRIVERS`, `DELEGATED_CELLS` and `NO_WEIGHTED_CATEGORICAL_PATH` absent | delete one `KAT_DRIVERS` entry; delete one no-path entry; put a member in both a cell and the no-path table; add a no-path entry for a member no registry holds | 4c |
| T5r | `...test_cdf_route_registry_cells.py::test_seeded_registry_changes_are_reported[<seed>]` for `fast_in_group_b_without_map`, `fast_in_group_b_with_map`, `no_path_entry_removed`, `delegated_cell_removed`, `replaced_member`, `new_weighted_site` | non-red gate (seeds in 5.1 T5r; the first four probed against the live registries; `replaced_member` probed red in this container, section 13) | derive the member set from the draw-site map instead of the registries (`fast_in_group_b_without_map` then passes); bind `SCALAR_HANDLERS` with a `from` import at test-module load (`replaced_member` then passes, probed) | 4c |
| T7 | `tests/sentry/test_cdf_search_sites.py::test_search_sites_equal_the_named_set` | non-red gate (the named set holds today) | a `bisect_right` over a list built in a new function; `from bisect import bisect_right as br` in a new module | 4c |
| T7 | `...test_cdf_search_sites.py::test_seeded_searches_are_reported` | non-red gate; fixture `tests/fixtures/sentry/cdf_local_search.py` (a direct call, an aliased import, a `.searchsorted` attribute call) | drop alias resolution from the scan | 4c |

Removed with the cuts: T4 and its workflow sentry, T6 (all three), T9 to T9e (memo), T10b, T15. Node ids moved: T12n from `test_cdf_builder_integer_only.py` to `test_cdf_exact_ratio.py`, and T12b to `test_cdf_no_bare_catch.py`, so that each lands red in its own slice. T10 is a new id: the build-count test replaces revision 4's T9 call counts. Revision 6: T16 (all three) and the revision 5 T5 and T7 rows are deleted. T5r (registry-derived cells and their coverage test), T5s (sub-route cells), T7 (search sites, now a closed set), T9c and T20 are new or rewritten ids; T8 moves to 4c.

### 5.1 Tests

- T1 Known-answer vectors. `tests/vectors/categorical_cdf_kat.json`, generated by the committed `tests/vectors/gen_categorical_cdf_kat.py`, which uses only `fractions.Fraction` and imports nothing from `decoy_engine`. Provenance goes in `tests/vectors/README.md`. Corpus: ties; zero weights (leading, trailing, all but one); tiny weights at, one ulp below and one ulp above `T / RES`; subnormals; 10, 1,000, 100,000 and 999,000 categories (digests for the large ones); vectors normalized to `1.0 +- 1 ulp` and `+- 3 ulp`; integers above `2**53`; `-0.0`; `1e308` pairs; `[0.6, 0.3, 0.1]`; `[0.1] * 10`; the 20,000-vector corpus as a digest. Non-vacuity: at least 50 vectors on which today's float builder, re-implemented inline, disagrees. Contract partner (class 10): the contract section 3.3 entry carries one `` `<fixture path>` sha256 `<64 hex>` `` line per golden fixture. `test_contract_entry_records_the_fixture_digests` matches exactly one such line per fixture by regex and compares it with `hashlib.sha256` of the file. The fixtures get `-text` in `.gitattributes`.
- T2 Property test (Hypothesis 6.155.7, in the lock). Draws finite non-negative vectors mixing `float`, `int`, `numpy.float64`, `numpy.float32`, `numpy.float16`, `numpy.int64` and ints above `2**53`. Checks: builder equals the `Fraction` oracle; P2; P3 in both directions; exact `2**k` scaling leaves the CDF unchanged.
- T2x Exhaustive small cases (claim i). Every vector of length 1 to 3 over the value grid `{0, 1, 2, 3, 7, 0.1, 0.3, 0.5, 1e-7, 2**53 + 1, -0.0}` (11 + 121 + 1,331 = 1,463 vectors), plus every vector of length 4 over `{0, 1, 0.1, 0.3, 1e-7}` (625), is built and compared with the `Fraction` oracle: the same tuple, or the same error code and failing index for the vectors the oracle rejects (the oracle applies the P6 pass order with `Fraction` arithmetic). It runs on all three interpreters through the section 5.4 script. The grid holds the values behind the section 1 probes (`0.1`, `0.3`, ties at `[1, 1, 1]`, a weight below one slot, an integer above `2**53`, signed zero).
- T2b Entry conversion: `[4503609762803756, 13510822533, 9007199259373711]` builds `[333334, 333335, 1000000]` through E1, E2, E3 and E4.
- T2c Exact-type rejection over every `ERROR_ROUTES` entry. Each case sits at index 1 of `[1.0, <case>, 1.0]`: `LyingFloat(0.5)`, `LyingFloat64(0.5)`, `LyingInt(1)`, `RegisteredIntegral()`, `RegisteredReal()`. Expected: `not_numeric` (message `... index 1 ...`) on E1 to E3, `not_numeric:c` on E4 to E8, decline on E9 and E10. Positive control: every type in both allowlists builds, including `numpy.longlong(1)` and `numpy.uintc(1)`.
- T3 Route by row matrix (P6, P8, A1). Parametrized over `ERROR_ROUTES` E1 to E10 from `tests/parity/_categorical_routes.py`, which is created in 1c. E1 to E3 must raise `StrategyError` with the expected code. E4 to E8 must return exactly `{code}:c`. E9 and E10 decline invalid rows and admit valid rows with the KAT tuple (E10 on a fixture whose source is `string` and whose table has no FK). Baseline per cell, probed from the lock on 3.10.20 and 3.12.3 against `545d913` (E9 partly probed at `c7a4f18`, E10 from the code):

| Row | Expected | E1 | E2, E3 | E4 to E7 | E8 | E9, E10 |
|---|---|---|---|---|---|---|
| `[True, 1.0]`, `[numpy.bool_(True), 1.0]` | not_numeric | builds (red) | builds (red) | green | green | decline (green) |
| `["x", 1.0]`, `["x", nan]` | not_numeric | bare `TypeError` (red) | bare `ValueError` (red) | green | green | green |
| `[None, 1.0]`, `[object_with_marker_repr, 1.0]` | not_numeric | bare `TypeError` (red) | bare `TypeError` (red) | green | green | green |
| `[Decimal("1"), 1.0]` | not_numeric | bare `TypeError` (red) | builds (red) | green | green | green |
| `[Fraction(1, 3), 1.0]`, `[numpy.longdouble(1), 1.0]` | not_numeric | builds (red; from the code) | builds (red) | green | green | green |
| `[True, -1.0]` | not_numeric | nonpositive (red) | nonpositive (red) | green | green | green |
| `[nan, -1]` | nonfinite | bare `ValueError` (red) | bare `ValueError` (red) | negative (red) | negative (red) | decline (green) |
| `[inf, 1]` | nonfinite | bare `ValueError` (red) | bare `ValueError` (red) | E4 bare `ValueError`, E5 to E7 `unbuildable_cdf` (red) | bare `ValueError` (red) | decline (green) |
| `[-inf, 1.0]` | nonfinite | nonpositive (red) | nonpositive (red) | negative (red) | negative (red) | decline (green) |
| `[-1.0, inf]` | nonfinite | negative (red) | negative (red) | negative (red) | negative (red) | decline (green) |
| `[numpy.float32("nan"), 1.0]` | nonfinite | bare `ValueError` (red) | bare `ValueError` (red) | not_numeric (red) | not_numeric (red) | decline (green) |
| `[1e308] * 4` | nonfinite | below_resolution (red) | below_resolution (red) | `unbuildable_cdf` (red) | `unbuildable_cdf` (red) | decline (green) |
| `[1e16, 1.0, -1e16]` | negative | nonpositive on 3.10, below_resolution on 3.12 (red) | same (red) | green | green | green |
| `[-1.0, 0.0]` | negative | nonpositive (red) | nonpositive (red) | green | green | green |
| `[0, 0]`, `[-0.0, 0.0]` | nonpositive | green | green | `unbuildable_cdf` (red) | `unbuildable_cdf` (red) | decline (green) |
| `[1, 1e-20, 1]` | below_resolution | green | green | `unbuildable_cdf` (red) | `unbuildable_cdf` (red) | decline (green) |
| `[1.0, 2.0e-6, 1.0]` | below_resolution | builds (red) | builds (red) | admitted (red) | admitted (red) | admitted (red) |
| weights `[1.0]` for two categories | shape | n/a | green | green | green | green |
| valid `numpy.int64`, `numpy.float32` pairs | builds, same tuple | green | green | not_numeric (red) | not_numeric (red) | decline (red) |
| valid `[1.0, -0.0, 1.0]` | builds, zero-width middle | green | green | green | green | green |
| `[LyingFloat(0.5), 1.0]`, `[RegisteredIntegral(), 1.0]` | not_numeric | builds / bare `TypeError` (red) | builds / bare `TypeError` (red) | admitted / green | same | admitted (red) / green |
| valid `numpy.longlong` pair | builds, same tuple | green | green | not_numeric (red) | not_numeric (red) | decline (red) |

  T3 lands in 1c, after 1a. Recorded red-first evidence for 1c is therefore the matrix run against the 1a commit, whose cells differ from the table only on E1 (all green there, since 1a fixed E1). The table above stays as the measured `545d913` record.
- T3m Message known answers. For each M2 family, the exact `str(exc)` and `exc.message` are committed constants, asserted over type variants of the same value: negative over `float`, `numpy.float64`, `numpy.float32`, `numpy.float16`, `int`, `numpy.int64` (all give `strategy='categorical': categorical weight at index 1 is negative.`); below resolution over three variants, each with its own committed M1 bound; nonfinite element over `nan`, `inf`, `-inf` in three types; nonfinite total; nonpositive over three variants; not-numeric over `True`, `numpy.bool_(True)`, `"x"`, `None`, `Decimal("1")`, `Fraction(1, 3)` and `LyingFloat(0.5)`. Cross-interpreter evidence: the same constants pass under the section 5.4 local gate on 3.10.20, 3.11 and 3.12.
- T5r Registry-derived known-answer cells (mechanism b; claim ii). `tests/parity/_categorical_routes.py` holds `members()`, `derive_registry_cells()`, `DELEGATED_CELLS`, `NO_WEIGHTED_CATEGORICAL_PATH` and `KAT_DRIVERS` (section 3.2). The parametrization of `test_every_registry_cell_matches_the_kat` is `derive_registry_cells() | DELEGATED_CELLS` evaluated at collection, so the test iterates the registries, not a hand list; a cell with no driver fails with the cell in the message. A driver returns either the masked values or the registered decline code; a KAT cell asserts `categories[bisect_right(KAT[v], bucket)]` for its own bucket derivation over the slot-hit values and the thirty KAT vectors over 2,000 values, with a moved-slot hit guaranteed per key pair; a decline cell asserts the exact code. No cell is compared with another. The closure test (4c) applies the five closure rules of section 3.2 and runs six seeds under `monkeypatch`, asserting each is reported: `fast_in_group_b_without_map` (`GROUP_B_STRATEGIES | {"categorical_fast"}`, draw-site map untouched: a member in neither), `fast_in_group_b_with_map` (the same plus `MASK_STRATEGY_TO_SITE["categorical_fast"] = "mask.categorical_deterministic"`: a cell without a driver), `no_path_entry_removed` (drop (R4, `fpe`): a member in neither), `delegated_cell_removed` (drop one `nested` driver: a cell without a driver), `replaced_member` (Codex round 7 finding 2: a subclass cannot replace the builder, because `CategoricalStrategyHandler.run` calls the module-global `_build_cdf` at `_categorical.py:188`. The seed is a class in the closure test module, `FloatCdfCategoricalHandler`, with `name = "categorical"` and a `run` that reads `categories` and `weights` from the plan's provider config, builds thresholds with `tests/perf/_float_cdf_reference._build_cdf`, derives each row's bucket with `derive_index(ctx.mask_key, plan.namespace, _canonicalize_source(value), pool_size=_WEIGHTED_CDF_RES)` and writes `categories[bisect_right(cdf, bucket)]`, deterministic rows only. It is installed as `registry = dict(SCALAR_HANDLERS); registry["categorical"] = FloatCdfCategoricalHandler(); monkeypatch.setattr(decoy_engine.execution._strategies, "SCALAR_HANDLERS", registry)`. The seed test runs the R1 `categorical` deterministic driver and the R1 `nested` deterministic delegated driver under that copy and asserts each cell's KAT assertion fails: on the slot-hit values the float thresholds `[333333, 666666, 1000000]` put bucket 666666 in `z`, and the KAT tuple `(333333, 666667, 1000000)` puts it in `y`. `NestedStrategyHandler` reaches the copy because it imports `SCALAR_HANDLERS` inside its method (`_nested.py:131`). Probed on the 3.12.3 lock venv against this branch's source (`_categorical.py`, `_nested.py`, `_strategies/__init__.py`, `determinism/_derive.py` and `generation/pool/_canonicalize.py` are identical on `545d913` and `5154830`): with the production handler and its `_build_cdf` rebound to a `Fraction` builder standing in for slice 1a, both cells emit `['y', 'y', 'y']`; with the copy installed, both emit `['z', 'z', 'z']` and the member's class is `FloatCdfCategoricalHandler`; after the monkeypatch context the shared registry holds `CategoricalStrategyHandler` again. The positional variant is not part of the seed), and `new_weighted_site` (a `DrawSite` whose fingerprint names `weights`, mapped to `categorical` in `_POSITIONAL_DRAW_SITE` under a new id: new cells without drivers).
- T5s Sub-route cells (section 3.3a D1 to D5). The same harness drives S2 to S9 and the three D4 lanes, each with its `src` site, its driver, its `(mask_key, namespace)` pair and the backend evidence it must show. Slot hits: the fixture (generated by `tests/vectors/gen_categorical_cdf_slot_hits.py`, which records its search) holds, per key pair, source values whose bucket is 666666. That slot moves for `[1.0, 1.0, 1.0]` (today `[333333, 666666, 1000000]`, exact `[333333, 666667, 1000000]`). S1 (the R1 deterministic cell) uses key `(0x0123456789).to_bytes(8, "big")`, namespace `ns`, values `u1959551`, `u3243963`, `u3267463`. S2 runs through `run_seq` with `install_cat(monkeypatch, PythonReferenceIndexKernel())`, key pair `MASK_KEY` and `ns_cat`, values `u608104`, `u727084`, `u1948468`; evidence `{"table": "P", "column": "c_kat", "path": "scalar", "executed_backend": "rust_companion"}` and a spy recording `call.cdf == (333333, 666667, 1000000)` (baseline `["z", "z", "z", None]`, probed). S3 uses `hide_cat` (`executed_backend == "python_scalar"`, `reason == "crypto_extension_unavailable"`). S4 to S9 use each harness's own key pair, searched by the generator. S8 runs at least five chunks, and its evidence includes that the handler used the run's `PrebuiltCdf`. Each D4 lane runs the S6 and S8 fixtures through `run_auto_chunk`.
- T8 Relational known-answer keys: parent key `P.k` is a deterministic weighted categorical with weights `[1.0, 1.0, 1.0]`, and parent rows include slot-hitting values. Shapes `chain_shape` and `composite_shape` (`tests/parity/_d2b_shapes.py:118,253`) run through `run_seq` and `run_full`, and the out-of-core FK route through `tests/parity/test_out_of_core_fk_parity.py:448`. Assertions: 100% join; every slot-hitting key masks to `y` on parent and child (red on the baseline: `z`); two runs are byte-identical.
- T9 Lifetime by identity (rule L1). Weak references cannot watch the CDF directly: tuples and tuple subclasses refuse them (probed: `TypeError: cannot create weak reference to 'TrackedCdf' object`). So T9 wraps `_build_cdf` in every importing module to return `TrackedCdf(tuple)` carrying a fresh `_Token` in its `__dict__`, and records `weakref.ref(token)`. Only that instance references the token, so the token dies exactly when the CDF does. For each run kind, T9 drives a full run through its public entry, drains or closes its stream, calls `gc.collect()` and asserts every recorded reference is dead. The handlers and prebuilds pass the builder's object through unchanged (1c removes the `tuple(...)` wrap in `prepare_categorical`, and slice 2 adds none), so the tracked instance is the one the run holds. T9 cannot see a holder that copies the tuple's contents (for example `list(cdf)`); T9c covers that.
- T9c Lifetime by content (rule L1; Codex round 5 finding 3, seed corrected for Codex round 6 finding 4). For each T9 run kind: build the config and source for three runs first (a warm-up at 1,000 categories, then two measured runs at 999,000 categories with different weights), so that what they hold is allocated before any measurement. Start `tracemalloc`, run the warm-up, then for each measured run: `gc.collect()`, read `tracemalloc.get_traced_memory()[0]` and `pyarrow.total_allocated_bytes()`, run the full run through its public entry, drain or close its stream, `gc.collect()`, read both again. Assert each growth is at most 1 MiB (`TOLERANCE_BYTES = 1 << 20`). The smallest seeded copy is 7.6 MiB (Arrow), so the tolerance is below one seventh of it. Distinct weights per measured run make a memo keyed by config grow on each run. The seeds are `list(cdf)`, `tuple(list(cdf))` and `pyarrow.array(cdf)`, each a real copy; `tuple(cdf)` is not used as a seed because it returns the same object for an exact tuple (probed on 3.12.3), which is identity retention and belongs to T9. Limits, stated: memory outside the Python allocator and the default Arrow pool (the Rust companion's heap, a NumPy array with a custom allocator) is not measured; the companion holds no CDF state between calls. NumPy data buffers are reported to `tracemalloc` by NumPy itself (from the NumPy documentation, not probed here). Runtime: the slice records it; it is not marked slow or skipped.
- T10 Build count: a counting wrapper around `_build_cdf` in each importing module. Out of core: the same weighted column over 1 batch and over 20 batches (Python path, no kernel) gives one build in both. Chunked oracle: 1 chunk and 5 chunks give one build in both.
- T18 Error position (P9). Every invalid T3 row: (a) `prebuilt_cdfs` and `_python_cdf` return without raising; (b) a zero-batch out-of-core table completes as it does on the baseline; (c) a one-batch out-of-core run and a one-chunk chunked-oracle run raise the T3 code from `categorical_array` and the handler respectively, by traceback frame name; (d) a chunked run whose chunk 0 also fails a pre-adapter guard (the passthrough-FK guard, `_chunked_oracle.py:396-399`) still raises the guard's error. Case (d) is stated from the code and not probed.
- T19 Identity gate: a `StrategyContext` with `categorical_cdfs={"c": PrebuiltCdf(other_seed, other_cdf)}`. The handler, run with `plan=seed` (a distinct object with different weights), must emit its own KAT categories.
- T7 Search sites (a ratchet; claim iii covers it). An AST scan over `src/decoy_engine` collects every call whose final name is `bisect_right`, `bisect_left`, `bisect`, `insort_right`, `insort_left`, `insort` or `searchsorted`, plus calls through a local alias imported from `bisect`, keyed by (module path, qualified name), and asserts equality with the reviewed set of section 3.2. It also asserts that `_WEIGHTED_CDF_RES` is loaded only in `_categorical.py`, `categorical_array` and `native_categorical`. It flags the common forms of a new search; a hand-written search loop passes it.
- T11, T11b Messages: marker corpora (labels, source values, a `str` weight equal to the marker, an object whose `__repr__` and class name are the marker). No message contains the marker. T11b also commits each full route-layer message.
- T12 Integer-only builder. `BUILDER_FUNCTIONS` = `_build_cdf`, `_scaled_weights`, `_check_total`, `_check_resolution`, `_thresholds`, `_resolution_bound_text`, all in `_categorical.py`. The walk finds no `ast.Div`, no float `Constant`, no call to `float`, `sum`, `round`, `repr` or `math.*`, and every `FormattedValue` has `conversion == -1` and a spec in `{"d", "02d", "+03d"}`. The closure test asserts that the set reachable from `_build_cdf` equals `BUILDER_FUNCTIONS` plus `_exact_ratio` (which T12n covers). `prebuilt_cdfs` and the handler are outside the scope.
- T12b: no `except` clause in `_categorical_prepared.py`, `_categorical_native.py`, `_mask_group_b.py`, `_categorical.py` or `_operator_config_rejections.py` names `ValueError`, `OverflowError` or `TypeError`. The `except StrategyError` clauses slice 2 adds are allowed by name.
- T13 Annotations: `get_type_hints` of `SeqNativeRun.categorical_column["cdf"]`, `_build_cdf["return"]`, `CategoricalCall`, `PreparedCategorical` and, from slice 2, `PrebuiltCdf.cdf` resolve to `tuple[int, ...]` (optional where declared). The `PrebuiltCdf` assertion is added in slice 2.
- T14 Compiled kernel (local; section 5.5).
- T20 Threshold ratchet (mechanism a, section 3.2; claim iii). The rule, the owner, the reviewed list, the nine seeded shapes, the control and the limits are in section 3.2. The scan reads the installed `decoy_engine` package directory (the same tree that is imported), parses every module with `ast`, and never imports a scanned module. Its module docstring states that it is a ratchet against accidental drift and not a proof that no other builder exists.
- T17: section 5.3.

### 5.2 Existing tests that change

Each change replaces an expected value with a stricter or corrected one. None is widened or skipped (row C29: a review rule, not a test), and each needs the owner's sign-off (Q2, Q7, Q8).

1. `tests/parity/test_phase_d_d1b2_categorical_baseline.py:100-104` (`nan`, `inf`): `pytest.raises(ValueError, match="cannot convert float NaN to integer")` becomes the exact `StrategyError` code `categorical_weights_nonfinite` and strategy (slice 1a: the builder change alone moves it).
2. `tests/unit/execution/test_categorical_weighted.py:106-112` (`test_weight_just_above_resolution_passes`) feeds `[1.0, 2.0e-6, 1.0]`, whose exact share is below one slot. It gets a genuinely-above input from the KAT, plus a sibling asserting that the old input raises (slice 1a).
3. `tests/parity/test_phase_d_d1b2_categorical_disposition.py:114-135`: specific reasons replace `unbuildable_cdf`, and the comment at `:122-123` is rewritten (slice 1c).
4. `tests/native/test_chunked_categorical_admission.py:245-285`: `unbuildable_cdf` becomes `below_resolution`, the `nonfinite` and `nonpositive` cases are added, and "eight codes" becomes ten (slice 1c).
5. `tests/parity/test_phase_d_d2c_ii_cat_seq.py:140-147`: the two specific reasons (slice 1c).
6. `tests/fixtures/f29/consumer-contract.json` is regenerated in 1r. The diff may touch only `registry_version` fields.
7. `tests/sentry/test_module_size.py:107` (`a3fe49e`): `_pandas_adapter.py` from 689 to its exact new count (691 in the prototype), in the slice 2 commit that grows it. This census entry moves with its partner; nothing is loosened.

Unchanged, verified by reading: `tests/native/test_chunked_nondet_categorical_admission.py:107-122` (admissibility only). The D1b2 pinned baseline (`WEIGHTS = (5, 1, 2, 0.5, 8)`) moves threshold 3 from 515151 to 515152, and no pinned source value hits that slot.

### 5.3 Probe of the existing suite, and the reason registry effect

Revision 2 probe (scratch plugin rebinding `_build_cdf` to a P2, P2a, P3, P6 prototype over the 47 test modules that mention both `weights` and `categorical` or `_build_cdf`): on 3.10.20, 3 failures (5.2 items 1 and 2); on 3.12.3, the same 3 plus the unrelated audit row 7, and `test_skewed_weights_skew_cdf` passes. Slice 1c re-runs the 47 files and must show exactly the 5.2 item 3 to 5 changes. Slice 2 re-runs them and must show no change. The revision 5 prototype ran 15 of them on 3.12.3 with no change (section 4).

Goldens, proof manifest, Faker fixtures: no committed golden uses weighted categorical (`grep -rl weights` over the golden directories, `docs/proof-manifest.json` and `scripts/gen_proof_manifest.py` is empty). `tests/native/test_determinism_goldens.py:514-533` rebuilds its expectation through `_build_cdf`.

Reason registry effect:

- `tests/sentry/_reason_baseline.py --write` refuses off the F29 baseline commit, and `reason-baseline.json` is digest-pinned. The worker does not run `--write`.
- `tests/fixtures/f29/producer-inventory.json:190-192` is a snapshot that `test_every_producer_resolves` resolves by code, not by line. It stays byte-identical.
- The two new identities need a minor bump (Codex finding 1). `pin_violations` (`scripts/release/reason_release_pin.py:48-66`, `a3fe49e`) rejects any catalog change at the pinned `1.1.0` and any count change under a released `since`. Probed against `a3fe49e`: no bump with the default `since` gives two violations; a bump to `1.2.0` with both codes at `since="1.2.0"` gives none.
- Slice 1r. Read `REGISTRY_VERSION` (`src/decoy_engine/reasons/_registry.py:24`, `1.1.0` on `a3fe49e`) on the `origin/main` the slice rebases onto, as `M.m.p`. Set it to `M.(m+1).0`. Declare both codes in `_part3.py` with that literal `since`, add them to `__all__`, and move `docs/reasons.md:21`, `tests/fixtures/f29/consumer-contract.json` (regenerated by running `tests/unit/progress/test_consumer_contract.py` as a script) and a `CHANGELOG.md` line in the same commit. Never edited (row C30, digest-pinned): `reason-registry-release.json`, `reason-manifest-v1.json`, `reason-baseline.json`, `producer-inventory.json`.
- Coupling with A-4 (`claude/jolly-carson-3k5stf`, which bumps to `1.2.0`). Whichever branch merges second rebases, bumps the minor once more, and rewrites only its own `since` literals and partners. No test names a version. The slice gate records `origin/main`'s value and the branch value (check: branch equals main plus one minor, and both new codes' `since` equal the branch value).
- T17 asserts `Version(live) > Version(pin)` and `Version(pin) < Version(since) <= Version(live)` for both identities. The merge-time equality `since == live` is a gate check, not a permanent assertion, so a later legitimate bump does not turn it red.

### 5.4 Local gate (replaces the CI job)

GitHub Actions is off for this repo, so no workflow is edited and no workflow sentry is added. Each slice's evidence runs the slice's new and changed test files through one committed script, `scripts/dev/cdf_interpreter_gate.sh` (slice 1r), and attaches its output and its JUnit files. The script has one mode. It runs every given path on CPython 3.10, 3.11 and 3.12; no argument and no environment variable narrows that (row C31; an edit to the script, `conftest.py` or `pyproject.toml` on the branch is for the reviewer's diff). Revision 5's loop was fail-open (Codex round 5 finding 2) and revision 6's `CDF_GATE_PYTHONS` override could narrow a slice gate to one interpreter (Codex round 6 finding 3); both are gone.

Inputs, all of them: the test paths given as arguments (an argument starting with `-` or naming a missing path is refused with exit 2, so `-k`, `-m` and `-p` cannot be passed), the three venvs `.venv-3.10`, `.venv-3.11`, `.venv-3.12`, and the committed `pyproject.toml`, from which the script reads the default marker expression (section 0.3 finding H). The script reads no environment variable. Every Python and pytest process runs under `/usr/bin/env -i` with fixed `PATH`, `LANG` and `HOME`, so variables such as `PYTEST_ADDOPTS`, `PYTEST_PLUGINS`, `PYTHONPATH` or a stray `DECOY_REQUIRE_COMPILED_COMPANION` never reach them.

Two phases, both declared in the script:

1. Ordinary cells: on each of 3.10, 3.11 and 3.12, the paths with `-m "(<default>) and not compiled_companion"`.
2. Companion cells: tests marked `compiled_companion` (T14's, section 5.5; the marker is registered in `pyproject.toml` by slice 1r) run on 3.12 only, with `-m "(<default>) and compiled_companion"` and `DECOY_REQUIRE_COMPILED_COMPANION=1` set by the script. The phase runs when the paths hold such tests; if they hold none, the script prints that and adds no cell. A companion test that lost its marker runs in phase 1 without the flag, skips, and fails phase 1.

Each cell fails on a missing venv or one of another minor version, a pytest exit other than 0 (including 5, nothing collected), or a JUnit file that is missing, unparsable, records zero tests, or records any failure, error or skip. Each failing cell sets the status without stopping the run, so every cell's result is printed; the script exits 1 if any cell failed.

Venvs, from the lock, once per container (a separate step; the script never changes an environment, so the companion installed in section 5.5 survives it):

```bash
(for v in 3.10 3.11 3.12; do UV_PROJECT_ENVIRONMENT=.venv-$v uv sync --frozen --extra dev --python $v || exit 1; done)
```

The script, exact text (sha256 `992f29bb0d47cfe7ba9b7f30570c74f30912492f1a9c9f283a3b5d18695dd59c` as probed):

```bash
#!/usr/bin/env bash
# Run the given test paths on CPython 3.10, 3.11 and 3.12; exit nonzero if any cell fails.
# A cell fails on a missing venv or one of another minor version, a pytest exit other than 0,
# or a JUnit file that is missing, unparsable, empty, or records any failure, error or skip.
# Tests marked compiled_companion run in a second phase on 3.12 only, with the compiled
# companion required. No environment variable changes what runs: the interpreter list is fixed
# here, arguments must be paths, and pytest runs under a fixed environment.
# Create .venv-3.10, .venv-3.11 and .venv-3.12 from uv.lock first.
set -u -o pipefail
readonly PYTHONS="3.10 3.11 3.12"
readonly COMPANION_PYTHON="3.12"
readonly FIXED_ENV="/usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 HOME=/nonexistent"

if [ "$#" -eq 0 ]; then echo "GATE FAIL: no test paths given" >&2; exit 2; fi
for arg in "$@"; do
  case "$arg" in
    -*) echo "GATE FAIL: an option was given; pass test paths only" >&2; exit 2 ;;
  esac
  if [ ! -e "$arg" ]; then echo "GATE FAIL: a test path does not exist" >&2; exit 2; fi
done

# The default marker expression from pyproject.toml, so an explicit -m keeps its exclusions.
if ! base_m=$($FIXED_ENV ".venv-$COMPANION_PYTHON/bin/python" -I -c '
import shlex, tomllib
with open("pyproject.toml", "rb") as fh:
    args = shlex.split(tomllib.load(fh)["tool"]["pytest"]["ini_options"]["addopts"])
print(args[args.index("-m") + 1])'); then
  echo "GATE FAIL: cannot read the default marker expression" >&2; exit 2
fi

junit_ok() {
  $FIXED_ENV "$1" -I - "$2" <<'PY'
import sys
import xml.etree.ElementTree as ET

path = sys.argv[1]
root = ET.parse(path).getroot()
suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
keys = ("tests", "failures", "errors", "skipped")
total = {k: sum(int(s.get(k, 0)) for s in suites) for k in keys}
print(f"junit {path}: {total}")
ok = bool(suites) and total["tests"] > 0 and total["failures"] == total["errors"] == total["skipped"] == 0
sys.exit(0 if ok else 1)
PY
}

cell() {  # cell <version> <label> <marker expression> [extra env assignment]
  local v="$1" label="$2" mark="$3" extra="${4:-}" py=".venv-$1/bin/python" xml="cdf-$2.xml" rc
  rm -f "$xml"
  if ! $FIXED_ENV "$py" -I -c "import sys, numpy; print(sys.version.split()[0], numpy.__version__); sys.exit('%d.%d' % sys.version_info[:2] != '$v')"; then
    echo "GATE FAIL $label: interpreter" >&2; return 1
  fi
  $FIXED_ENV $extra "$py" -m pytest -p no:cacheprovider -q -m "$mark" --junitxml="$xml" "${@:5}"
  rc=$?
  local bad=0
  if [ "$rc" -ne 0 ]; then echo "GATE FAIL $label: pytest exit $rc" >&2; bad=1; fi
  if ! junit_ok "$py" "$xml"; then echo "GATE FAIL $label: junit" >&2; bad=1; fi
  return "$bad"
}

status=0
for v in $PYTHONS; do
  cell "$v" "$v" "($base_m) and not compiled_companion" "" "$@" || status=1
done

companion_mark="($base_m) and compiled_companion"
$FIXED_ENV ".venv-$COMPANION_PYTHON/bin/python" -m pytest -p no:cacheprovider -q --collect-only -m "$companion_mark" "$@" >/dev/null
rc=$?
if [ "$rc" -eq 0 ]; then
  cell "$COMPANION_PYTHON" "$COMPANION_PYTHON-companion" "$companion_mark" "DECOY_REQUIRE_COMPILED_COMPANION=1" "$@" || status=1
elif [ "$rc" -eq 5 ]; then
  echo "no compiled_companion cells in these paths"
else
  echo "GATE FAIL companion collection: pytest exit $rc" >&2; status=1
fi
exit "$status"
```

Seeded files (slice 1r), under `tests/fixtures/gate/`; their names do not start with `test_`, so the default collection skips them, and pytest collects them when passed as paths: `seeded_gate_pass.py` (one passing test), `seeded_gate_fail_310.py` (`assert sys.version_info[:2] != (3, 10)`), `seeded_gate_skip_311.py`, `seeded_gate_error_312.py` (requests a missing fixture on 3.12), `seeded_gate_empty.py` (no test), `seeded_gate_companion.py` (an ordinary test that asserts the flag is absent, and a `compiled_companion` test that asserts it runs on 3.12 with the flag set) and `seeded_gate_companion_fail.py` (a failing `compiled_companion` test). Probed on `5154830` with the files inside the repository's `tests/fixtures/gate/` (so the repository's `conftest.py` and `pyproject.toml` applied, with the marker registered in a scratch copy of `pyproject.toml`) and the three lock venvs (CPython 3.10.20 with numpy 2.2.6, 3.11.18 with 2.4.6, 3.12.3 with 2.5.0):

| Invocation (`G` = `scripts/dev/cdf_interpreter_gate.sh`, `F` = `tests/fixtures/gate`) | Exit | Failing checks printed |
|---|---|---|
| `G F/seeded_gate_pass.py` | 0 | none (the control) |
| `G F/seeded_gate_fail_310.py` | 1 | `3.10: pytest exit 1`, `3.10: junit` |
| `G F/seeded_gate_skip_311.py` | 1 | `3.11: junit` (pytest exits 0 on a skip; the JUnit check catches it) |
| `G F/seeded_gate_error_312.py` | 1 | `3.12: pytest exit 1`, `3.12: junit` |
| `G F/seeded_gate_empty.py` | 1 | `pytest exit 5` and `junit` on all three |
| `G F/seeded_gate_companion.py` | 0 | none; four cells, each `tests: 1`, the fourth `cdf-3.12-companion.xml` |
| `G F/seeded_gate_companion_fail.py` | 1 | `3.12-companion: pytest exit 1`, `3.12-companion: junit` |
| `G tests/native/test_draw_site_inventory_coverage.py` | 0 | none (a real suite file under the fixed environment) |
| `G -k x F/seeded_gate_pass.py` | 2 | `an option was given` |
| `G F/no_such_file.py` | 2 | `a test path does not exist` |
| `G` | 2 | `no test paths given` |

Environment proof: `G F/seeded_gate_fail_310.py` was run under all 32 subsets of `CDF_GATE_PYTHONS=3.12`, `PYTEST_ADDOPTS=-k nothing_matches_this`, `PYTHONPATH=/nonexistent-gate`, `DECOY_REQUIRE_COMPILED_COMPANION=1` and `PYTEST_PLUGINS=no_such_plugin_module`, including none and all five. All 32 exited 1. Every `$` expansion in the script is one of its own variables or a positional argument (checked by `grep` over the script text).

Slice 1r's evidence repeats the table and the 32-subset run on its own commit, and every later slice's evidence starts with the `seeded_gate_fail_310.py` row, so a gate that has stopped failing is visible before its green run is read. A slice whose files include an interpreter-specific red-first cell records that cell's red run through the script (for example T1's `[0.6, 0.3, 0.1]` cell, red on 3.12 only).

The existing-suite comparisons (section 5.3: the 47 files on 1c and 2) are expected to differ from green, so they do not use the script's all-pass rule. They run `pytest --junitxml` on the parent and on the slice, on each of the three interpreters under the same fixed environment, and compare the sets of (node id, outcome) with a `python -I` comparison, recorded in the evidence, that exits 1 on any difference outside the node ids named in section 5.2.

### 5.5 Compiled-kernel test (T14, local)

`tests/native/test_cdf_native_present.py`; both T14 tests carry `@pytest.mark.compiled_companion`. When `DECOY_REQUIRE_COMPILED_COMPANION=1`, `native_companion_status().ok` (`native/_companion_status.py:296`) must be true or the test fails. Without the flag it skips with the reason "compiled companion absent", which is counted. `test_the_compiled_kernel_buckets_the_slot_hit_values` loads the kernel through `load_compiled_index_kernel` (`native/_index_ext.py:157`) and asserts the raw buckets of the S1 values are 666666. `test_weighted_cdf_selection_on_the_compiled_kernel` runs `native_categorical` with `["x", "y", "z"]` and `_build_cdf([1.0, 1.0, 1.0])` and asserts `["y", "y", "y", None]`; it also drives S5 to S7 and S9. Probed in revision 3 with the companion built into the 3.10.20 lock venv (the gate now runs companion cells on 3.12; the 3.12 build is not yet probed): buckets `[666666, 666666, 666666, None]`, baseline output `["z", "z", "z", None]`, exact output `["y", "y", "y", None]`. Slice 4a evidence:

```bash
uv pip install --python .venv-3.12/bin/python ./decoy-engine-native
```

```bash
scripts/dev/cdf_interpreter_gate.sh tests/native/test_cdf_native_present.py tests/parity/test_categorical_cdf_routes.py
```

The T14 tests run in the script's companion phase on 3.12 with the flag set by the script; the routes file gives phase 1 its ordinary cells. The JUnit check fails on any skip, so a run in which the companion did not load is red, not green. In the default suite without the companion, T14 skips with its stated reason, and the skip is counted.

## 6. Class table (DEFECT-CATALOG section 3)

| # | Class | Structure that closes it here | Test | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | M2 builder messages (integer fields only); A1 reasons (code and column only); 1d route-layer messages; new holder types `repr=False` (finding B reported for the existing ones) | T3m, T11, T11b, T12 | `{w!r}` back; `f"{total / RES:.2e}"`; interpolate `categories[i]` |
| 2 | Cleanup on exit paths | The builder owns no resource. For the five T9 run kinds, no CDF built in the run is reachable after it returns, and no copy above the tolerance survives (L1, row C25); the chunked holder is a generator local, released by `release_when_done` on every exit | T9, T9c | a module list that keeps CDFs or copies of them; T9's and T9c's seeded wrappers |
| 3 | State and concurrency | Slice 2 adds no cache, memo, key or shared state on the CDF path (L1, row C24). Reuse is by object identity. Tuples are immutable. Retention is checked by identity and by content | T9, T9c, T19 | drop the identity check; key by seed value; `LEAK.append(list(cdf))` |
| 4 | Route parity and silent fallback | Registry cells with every registry member classified (T5r: cell, delegated cell, or a no-path entry with a reason) and named sub-route cells (T5s); admission reports the builder's code on the named entries (A1, P8); construction moves no error on any invalid T3 row (P9, row C13); ratchets for a second builder (T20) and a second search (T7); what no cell covers is listed as a limit (3.3a D7, D8) | T3, T5r, T5s, T7, T10, T18, T20 | reinstate a precheck; `categorical_fast` in `GROUP_B_STRATEGIES` without a map entry; a registry member without a cell; rebind one member's builder; a float builder copy in a new module; let a prebuild raise |
| 5 | Referential integrity | Parent and child use the same pure builder in one process | T8 | build the child key with the float builder |
| 6 | Untrusted producer | N1 by exact type (C type codes); every rejection on a T3 row a registered `StrategyError` (row C14); `float(w)`, prechecks and bare `except` deleted | T2, T2b, T2c, T3, T3m, T12b, T12n | `isinstance` for exact type; `w.as_integer_ratio()`; reinstate `float(w)` or an `except (ValueError, OverflowError)` |
| 7 | TOCTOU | N/A: no path or file is involved | none | none |
| 8 | Weak tests and overclaimed evidence | Section 5.0 register; KAT from an independent oracle and exhaustive small cases; cells derived from the registries, each against the KAT with preconditions; every guarantee is a row of the claims table with its mechanism and its non-proof column (1.1), and T20 and T7 are labelled ratchets; a committed gate script with no input but paths, proven against seeded cells and 32 environment subsets; seeds that are real copies; probe numbers labelled with interpreter, numpy and commit | T1 non-vacuity, T2x, T5r closure seeds, T5r and T5s preconditions, T7, T12 closure, T20 seeds, control and non-vacuity, T9c seeds, the 5.4 seeded gate cells | regenerate the KAT from the implementation; derive members from the draw-site map; route agreement instead of the KAT; an environment override in the gate; `tuple(cdf)` as a copy seed |
| 9 | Behavior-preserving refactor drift | The change is intentionally not behavior-preserving; the characterization is the measured delta and the T3 matrix. Slice 2 is behavior-preserving and shows it on the existing suite (prototype: same results on 15 files) | T3, T1 digests, T13, T18 | swap two P6 passes; `:364` back to `list[int]` |
| 10 | Frozen contract drift | Contract entry records fixture digests; registry bump with literal `since` and partners in the same commit; released pin untouched; module-size census moves with its file | T17, `test_live_registry_honours_the_released_pin`, `test_reasons_doc_states_the_live_registry_version`, `test_the_consumer_contract_fixture_is_current`, `test_module_size.py`, `test_contract_entry_records_the_fixture_digests` | a code without `since`; skip the bump; regenerate a fixture without the contract line |

## 7. Reuse inventory

- `_build_cdf` stays the builder the cells drive (`_categorical.py:69`; row C32; T20 ratchets and does not prove ownership). `prebuilt_cdfs` and `PrebuiltCdf` live beside it (244 lines today, about 410 after 1a and 2).
- Run-scoped threading precedent: `code_set_records` (`_adapter.py:210`, `:354`; `_pandas_adapter.py:176`, `:251`; `_chunked_oracle.py:439`), copied for `categorical_cdfs`. Per-table plan precedent: `TableCategoricalPlan` and `TableHashPlan.categorical_for` (`_hash_run.py:164-166`) for `categorical_cdf`.
- Reasons registry and release pin: `reasons/runtime/_part3.py`, `reasons/_registry.py:24`, `scripts/release/reason_release_pin.py`, the consumer-contract regenerator.
- `numpy.dtype(c).type` for N1; `float.as_integer_ratio`; `fractions`, `weakref`, `numbers`, `hashlib`, `gc` (stdlib); Hypothesis (in the lock).
- Harnesses: `tests/parity/_d2c_cat_data.py`, `tests/parity/_d1b2_categorical_data.py`, `tests/parity/_d2b_shapes.py`, `tests/parity/test_out_of_core_fk_parity.py:448`.
- JUnit guard logic: `dp-certified-gate` (`.github/workflows/ci.yml:259-281`): fail unless passed > 0 and skipped, errors and failures are 0. The section 5.4 script applies the same rule per interpreter and accumulates the result.
- Live-registry coverage precedent: `tests/native/test_draw_site_inventory_coverage.py:129` (`test_mask_strategy_map_covers_live_registry_exactly`) compares a catalogue with the live `SCALAR_HANDLERS`, and `:313` closes the RNG-bearing file set with a reviewed allowlist. T5r and T20 follow those two patterns.
- Methodology citation: `docs/methodology-registry.yaml` entry for `_categorical.py` (`tests/sentry/test_methodology_citations.py`).
- Module size: `_pandas_adapter.py` census at 689 (`tests/sentry/test_module_size.py:107`); slice 2 adds 2 lines. `_sequential_native.py` is at 697 with no census entry needed below 700; 1a changes one annotation in place. Every other touched module stays below 600 (largest: `_hash_run.py` 546 to about 549; row C33).

## 8. Frozen contracts touched

- `docs/compatibility-contract.md` section 3.3: a dated "Current state" entry, in the style of `:307-313` (`a3fe49e`), records what moved and the sha256 of each golden fixture (slices 1b and 4a). A standing sentence states that `_build_cdf` output and error text are identical on CPython 3.10, 3.11 and 3.12 for the T1, T2x and T3m inputs the section 5.4 gate runs (row C34).
- `SEED_PROTOCOL_VERSION`: not bumped (precedent C1b-i, C5b-i). Q3.
- `docs/strategies.md:296-297` (`weights`): exact minimum share, exact thresholds, accepted types, three-significant-digit ceiling bound.
- Reasons registry `decoy-reasons/v1`: two additive codes under one minor bump (5.3). Partners `docs/reasons.md:21`, `consumer-contract.json` and `CHANGELOG.md` move in the same commit; the released pin is not edited.
- Admission reason vocabulary: `categorical_weights_unbuildable_cdf` deleted. Historical records that mention it are not edited.
- `CHANGELOG.md`: one entry for the output change, the new rejections and the reason change.
- Golden baselines: the KAT and slot-hit files, digest-pinned in the contract.

## 9. Owner questions

The owner's delegation ("go with recommended") covers the cuts. The questions below stay listed with their recommendations so the gate can see what each slice depends on.

- Q1. Method: exact integer arithmetic with round half up (recommended), or pin the 3.10 left fold?
- Q2. Accept P3 (about 0.1% of random vectors newly rejected) and the 5.2 item 2 change? Recommended yes.
- Q3. Leave `SEED_PROTOCOL_VERSION` unchanged (recommended)?
- Q5. Report-plane float sums: a separate follow-up with `math.fsum` (recommended).
- Q6. NumPy differs per interpreter in the lock; `generators/_distribution.py` draws through `choice(p=...)`. A separate item.
- Q7. Replace the pinned bare `ValueError` with `nonfinite` (5.2 item 1)? Recommended yes.
- Q8. Rule A1 with the 5.2 item 3 to 5 changes? Recommended yes.
- Q9. N1 rejects `Decimal`, `Fraction`, `numpy.longdouble` and `bool`? Recommended yes.
- Q10 (decided by delegation for revision 7: the narrower guarantee). The owner accepted the guarantee of section 1 as narrowed: no production dispatch registry and no further scan shapes to chase evasion. The registry-derived cells read R1 to R5 and the draw-site tables as they are; the ad hoc branches of section 3.3a stay limits with their named cells. A production route table stays in the section 15 backlog.

Decided, no longer questions: Q10 (above). Q4 (CI matrix scope) has no subject, because no CI job is added and the local gate is section 5.4. Memo ownership is decided as no memo (rule L1).

## 10. Slices

Bound: each slice touches at most 10 production files and at most 800 changed lines. Changed lines count production, test and doc lines. Generated data files (the KAT and slot-hit JSON, the regenerated `consumer-contract.json`) are excluded and named. Line figures are estimates from revision 4's test designs and, for slice 2, the measured prototype; each slice's evidence records the actual diffstat.

| Slice | Production files | Est. changed lines | Content | Gate |
|---|---|---|---|---|
| 1r | 2: `reasons/_registry.py`, `reasons/runtime/_part3.py` (plus the `compiled_companion` marker line in `pyproject.toml`) | about 230 (+ regenerated `consumer-contract.json`, about 27 `registry_version` lines) | T17 red first; minor bump relative to `origin/main` at build time; two codes with literal `since`; `docs/reasons.md:21`; CHANGELOG; gate check recorded; `scripts/dev/cdf_interpreter_gate.sh` (about 70 lines) and the seven seeded gate files (about 60), with the section 5.4 table and the 32-subset environment run repeated on the slice commit | Codex (R2) |
| 1a | 2: `_strategies/_categorical.py`, `_sequential_native.py` (annotation only) | about 760 (production about 220; tests and generator about 540) (+ KAT JSON) | T1 (four), T2 (three), T3m (two), T11, T12n, T13 red first; N1, P2, P2a, P3, P6, M1, M2 in the builder, returning a tuple; KAT generator and provenance; 5.2 items 1 and 2 | Codex (R2) |
| 1e | 0 (adds `tests/perf/_float_cdf_reference.py`, a verbatim measurement copy of today's builder) | about 680 | T12 (three, with seeded fixture), T20 (four tests: the ratchet, nine seeded shapes, the control, non-vacuity) and T2x recorded red against the 1a parent; the T20 source mutation run once; T10a with its mutation run | Codex (R2) |
| 1c | 4: `_strategies/_categorical.py`, `out_of_core/_mask_group_b.py`, `native/_categorical_prepared.py`, `out_of_core/_categorical_native.py` | about 600 | T2b, T2c, T3, T12b red first; three `float(w)` deletions; rule A1; two `except` deletions; `tuple(...)` wrap removed; `unbuildable_cdf` deleted; docstrings; `ERROR_ROUTES` E1 to E10; 5.2 items 3 to 5; 47-file re-run | Codex (R2) |
| 1d | 2: `_strategies/_categorical.py`, `out_of_core/_mask_group_b.py` | about 90 | T11b red first; route-layer messages per M2 | argon |
| 1b | 0 (docs: contract, `docs/strategies.md`, `docs/methodology-registry.yaml`, CHANGELOG, `.gitattributes`) | about 110 | `test_contract_entry_records_the_fixture_digests[kat]` red first; contract entry with the KAT sha256 line | argon |
| 2 | 8: `_strategies/_categorical.py`, `_chunked_oracle.py`, `_adapter.py`, `_pandas_adapter.py`, `out_of_core/_categorical_native.py`, `out_of_core/_hash_run.py`, `out_of_core/_mask.py`, `out_of_core/_mask_group_b.py` | about 640 (production about 85, measured 80 in the prototype; tests about 540; census 1) | T10 red first; T9, T9c, T18, T19 non-red gates with their mutations and the seeded retention runs (identity and the three content copies) recorded; rule L1 construction; `repr=False` holders; T13 `PrebuiltCdf` assertion; module-size census; 47-file re-run with no change | Codex (R2) |
| 4a | 0 | about 740 (+ slot-hit JSON) | `members`, `derive_registry_cells`, `DELEGATED_CELLS` and `KAT_DRIVERS`; T5r cells (two tests, including the two `nested` delegated cells) and T5s cells (two tests, including the D4 lanes); the slot-hit generator with the moved-slot search; T14 (two, marked `compiled_companion`) with the companion run on 3.12; the slot-hit sha256 line and its digest parameter | argon |
| 4c | 0 | about 560 | `NO_WEIGHTED_CATEGORICAL_PATH` (47 entries with reasons); T5r closure (two tests, six seeds); T7 (two, with seeded fixture); T8 | Codex (R2) |

Nine slices instead of eleven. The 1a and 4a splits are forced by the size bound; the content is revision 4's minus the cuts. 1c's gate is Codex because it carries the route-parity change. 1e, 2 and 4c are gated by Codex because they carry the mechanisms that replaced recurring classes: 1e the threshold ownership (mechanism a), 2 rule L1 with T9 and T9c, 4c the registry-cell coverage (mechanism b).

Order: 1r, 1a, 1e, 1c, 1d, 1b, 2, 4a, 4c.

- 1r first, because 1a raises the two codes.
- 1e after 1a, so its sentries land green and are recorded red against the 1a parent.
- 1c after 1a: once the builder raises only `StrategyError`, the `except` deletions are safe.
- 2 after 1a and 1c. Before 1a, a prebuild can let a bare error escape (finding C, probed). Before 1c, the route entries still call `float(w)` while the prebuild passes raw weights, so ints above `2**53` would get a different tuple on the chunked oracle than in memory.
- 4a after 2, so S8's cell exercises the run-built tuple.
- 4c last, because its closure test classifies the registries' members against 4a's `KAT_DRIVERS` and `DELEGATED_CELLS`.

Finding to slice map: section 0.2.

## 11. Pre-mortem (ways a gate reviewer breaks it, each with its red-first test)

1. Common denominator built from the smallest power of two: T1 subnormal rows, T2.
2. A leftover float operation or `repr` keeps a message or threshold interpreter-dependent: T12, T3m, the 3.12 cell of the local gate.
3. One route builds its own CDF, or still builds the float list: T5r and T5s on the covered routes; T20 and T7 flag the common accidental shapes elsewhere (ratchets, claim iii).
4. A route ignores the run-built tuple and rebuilds per batch or chunk (999,000 categories times batches): T10.
5. NaN or infinity escapes as a bare error on any entry: T3 rows over E1 to E10 (row C14).
6. Error precedence depends on the total, or admission decides before the builder: T3 rows `[nan, -1]`, `[-inf, 1.0]`, `[1e16, 1.0, -1e16]`.
7. `-0.0`, bools, ints above `2**53` and numpy scalars convert differently on one route: T2, T2b, T3 type rows, T5r.
8. A `from_profile` long-tail column now fails where it sometimes passed: T1 share-below-one-slot row; Q2.
9. The KAT is generated by the implementation: T1 independence and non-vacuity.
10. The local gate runs one interpreter three times, hides a skip, passes on a failure in one interpreter, or is narrowed by an environment variable or an option: the interpreter check, the JUnit check, the accumulated status, the paths-only arguments and the fixed environment, each proven by a seeded cell or the 32-subset run (section 5.4).
11. A prebuilt tuple for one config is served to another: T19 (identity, not value).
12. Building ahead of the batch moves an error earlier, or makes an empty table fail: T18.
13. A long-lived worker keeps CDFs, or copies of their contents, from earlier runs: T9 (identity), T9c (content), on the five run kinds (row C25).
14. The worker regenerates the released F29 fixtures, or adds codes without a bump or with the default `since`: T17, `test_live_registry_honours_the_released_pin`, `test_the_baseline_fixture_is_frozen`. The bump itself is required.
15. A `Fraction` or `longdouble` weight is scaled as if dyadic: T3 rows, N1.
16. A new dispatcher reaches the builder: if it is a member of R1 to R5 it must be classified (T5r closure; `categorical_fast` seeds); if it is an ad hoc branch it is the stated limit D8, and T20 and T7 flag it only if it writes thresholds or a search in a shape they match.
17. A-4 merges between this plan's rebase and its merge: the merge-order rule (5.3); the gate check compares with `origin/main` at that moment.
18. A lying `float` subclass or a registered `Integral` slips through: T2c, T12n.
19. A route agrees with its siblings but not with the KAT: T5r and T5s per-cell KAT assertion and moved-slot precondition.
20. A fixture recapture lands without a contract edit: the contract digest test.
21. Slice 2 lands before 1a or 1c: the order rule; T18 goes red (probed for the 1a case).
22. The compiled kernel is never exercised: T14 run in the script's companion phase on 3.12 (5.4, 5.5); the evidence attaches its JUnit counts.
23. A member reaches the weighted builder through another member (as `nested` does through `SCALAR_HANDLERS`) and gets no cell: the closure forces a classification, and `DELEGATED_CELLS` covers the known case; a no-path entry that is wrong is a review error (claim ii).

## 12. Worker evidence rules (DEFECT-CATALOG section 5)

- Name the class of every finding and fix the class, not the instance. If a catalogued class recurs, stop and report it with a class-level proposal; do not patch a third time.
- Write the tests first and record them red, with the interpreter, numpy version, commit and the file:line named in section 5.0. For 1e, record the red run against the 1a parent.
- Before pushing, run each mutation in section 5.0 once and show the test go red. For non-red gates, that run is the only failure evidence. Never commit while a mutation is applied.
- Self-attack the diff with value-bearing canaries in ordinary shapes (labels, source values and weight objects carrying a marker) through every message and repr the slice adds.
- Run baselines from `uv.lock` in the same environment as the branch, and state every suite not run.
- Run the section 5.4 script on 3.10, 3.11 and 3.12, starting with its `fails_on_310_only` seeded cell, and attach its output and the JUnit files. No CI job stands in for it.
- Never widen a threshold or allowlist beyond what is stated, skip a test, weaken an assertion, or edit a pinned test or golden except as named in 5.2.

## 13. What this plan did not run or verify

- Revision 8 ran, in one container, on this branch's tree (its `src/` equals `545d913`; the five files the probe imports are identical on `545d913` and `5154830`) with a lock venv from `uv sync --frozen --extra dev --python 3.12` (CPython 3.12.3, numpy 2.5.0): the `replaced_member` probe (section 5.1 T5r: slot-hit buckets `[666666, 666666, 666666]`; float thresholds `[333333, 666666, 1000000]`, exact `(333333, 666667, 1000000)`; production handler on the baseline `['z', 'z', 'z']` for both the R1 `categorical` and the R1 `nested` cells; with `_build_cdf` rebound to a `Fraction` builder `['y', 'y', 'y']` for both; with the test-local copy installed `['z', 'z', 'z']` for both) and its mutation (a driver that binds `SCALAR_HANDLERS` with a `from` import at module load emits `['y', 'y', 'y']` under the seed, so the seed is not detected); the census grep for section 1.1 (`grep -nEi` for the word list of section 0.1 over the plan, section 0.2 history tables excluded: 217 sentences on revision 7), read sentence by sentence; the platform `docs/ROADMAP.md` D-4 line (`:516` at decoy-platform `8a581fe`).
- Revision 8 did not run: the T20 prototype (it was scratch and is not on the branch; the count of 16 is the reviewed list as written in section 3.2, and slice 1e measures the set); the 3.10 and 3.11 venvs or the probe on them; the positional variant of the replaced handler; anything else listed under revisions 5 to 7 below.
- Revision 7 ran, in one container on `5154830` (still `origin/main`), with lock venvs from `uv sync --frozen --extra dev --python 3.X` (CPython 3.10.20 with numpy 2.2.6, 3.11.18 with 2.4.6, 3.12.3 with 2.5.0): the section 5.4 script against its seven seeded files and a real suite file, from inside the repository's `tests/fixtures/gate/` with the marker registered in a scratch `pyproject.toml` (table in 5.4), and the seeded 3.10-only failure under all 32 environment subsets (all exited 1); the revised T20 rule over `src/decoy_engine` (AST only, container CPython 3.13.18), which gave the reviewed list in section 3.2, against the nine seeded shapes and the control (from a scratch file, and with the nine placed in a scratch module under `src/decoy_engine/execution/_strategies/`, deleted afterwards), and the revision 6 rule against the same seeds (three missed); the T5r member closure against the live registries with four seeds (3.12.3); the T9c copy sizes with the corrected seed (3.12.3, today's builder, 999,000 weights: 38.1 MiB traced for `list` and for `tuple(list(...))`, 7.6 MiB in the Arrow pool for an Arrow copy, about 0 otherwise; `tuple(cdf) is cdf` for an exact tuple); the grep for delegation through `SCALAR_HANDLERS` (finding G).
- Revision 7 did not run: T2x, T5r, T5s, T7, T9c or T20 themselves (none is written); the `replaced_member` and `new_weighted_site` seeds; the `nested` delegated cells (their expected output is from the code); T20 against a slice 1a tree; T9c around full runs or its runtime; T14 on a 3.12 companion build (revision 3 probed it on 3.10.20); whether S9 can take the Python reference kernel.
- Revision 6 ran (carried), in one container on `5154830`, with lock venvs from `uv sync --frozen --extra dev --python 3.X` (CPython 3.10.20 with numpy 2.2.6, 3.11.18 with 2.4.6, 3.12.3 with 2.5.0): the section 5.4 script against its seeded cells on all three interpreters (table in 5.4) and the revision 5 loop against the 3.10-only cell (exit 0); the registry-cell derivation of section 3.2 against the live registries (3.12.3); a scratch prototype of the T20 rule over `src/decoy_engine` (AST only, container CPython 3.13.18), which gave the allowlist in section 3.2, and a broader shape that was rejected; the same prototype with a scratch module `execution/_strategies/_categorical_copy.py` holding today's float builder and the NumPy `cumsum`, `accumulate` and exact-integer variants (all four matched; the module was deleted and the worktree checked clean); the `new_member` seed of T5r against the live registries (the derived set gained exactly `(SCALAR_HANDLERS, categorical_float, mask.categorical_deterministic)`); the T9c copy sizes at the builder level (3.12.3, today's builder, 999,000 weights: 38.1 MiB traced for a `list` or `tuple` copy (the `tuple` seed was identity retention, Codex round 6 finding 4), 7.6 MiB in the Arrow pool for an Arrow copy, 0.0 MiB otherwise).
- Revision 5 ran, in one container, on a fresh lock venv (CPython 3.12.3, numpy 2.5.0): a scratch slice 2 prototype on 15 categorical test files (603 tests) with a `_build_cdf` call counter, and the same files on `a3fe49e`; the lifetime mechanism (token weak references, 38 nodes, 0 retained) and its seeded module-list violation (9 nodes retained); the weak-reference refusal of tuple subclasses; the finding C ordering hazard. Its caller scan is withdrawn with T16.
- The prototype is not the slice. It used today's builder with a broadened `except` standing in for slice 1a, and the probes removed the `tuple(...)` wraps. Its line counts are evidence for the slice 2 estimate only.
- T18 case (d) (guard error before an invalid weight vector) and the E10 cells are from the code, not probed. T10's per-run counts were measured per test node, not per run, on existing tests; T10 itself is not written.
- Whether any path renders the default dataclass `repr` of `PreparedCategorical` or `CategoricalColumnDecision` (finding B) was not checked.
- Earlier probes stand as recorded in revisions 2 to 4: the T3 matrix on 3.10.20 and 3.12.3 at `545d913`; the N1 type facts; the registry-pin cases against `a3fe49e`; E9 over twelve rows at `c7a4f18`; the compiled-kernel slot hits on 3.10.20; the 20,000-vector corpus and performance figures (revision 2 scratch prototype, 3.12, one container).
- The engine suite outside the 47 files; the full suite on 3.11 under the exact builder; Postgres, `ml`, `dp_certified`. Whether S9 can take the Python reference kernel. The Rust sources of `decoy-engine-native` (read by grep, built once).
- No CI can be observed: GitHub Actions is off for this repo.
- Literature citations (Witten, Neal, Cleary 1987; Duda 2013; IEEE 754-2019) and the CPython issue number are from memory and were not fetched.
- `plan-lint` does not exist in this repo or container (not on `PATH`; no `plan-lint*` file found). The plan contract was checked by hand for revision 8: `Rules consulted:` within the first 30 lines; every section 5.0 row has a node id, a red-first location or non-red label, and a mutation; every slice is stated within 10 production files and 800 lines; no em-dash (`grep -c` for U+2014 gives 0); no caller or site count; the four claims of section 1 are rows C2 to C7 of the claims table, and the census grep (`grep -nEi '\b(every|only|never|anywhere|single|all|none|nothing else|no other|no module|no copy|no cache|closed-world|by construction|cannot|guaranteed|exactly)\b'` over the plan with the section 0.2 history tables excluded) was read hit by hit: every hit outside section 1.1 is in section 0 or 13, describes what a test reads or runs, states a limit, or names its row. The census is a word list read by the author; a guarantee phrased without any of these words would be missed (section 0.4).

## 14. Architecture view

Is the remaining scope the smallest design that closes the defect? No, by one item: T10a backs no row of the claims table and could go (it is a performance budget for one build at 999,000 categories, section 4). Everything else in the slices backs at least one row of section 1.1, so removing it would delete that row; the list below says which row each candidate backs. The defect is P1, and closing it needs the exact builder (1a), the registry codes it raises (1r), and the route layer that hands it raw weights and reports its codes (1c). Claim (i) needs T1, T2, T2x, T3m and T12 run on three interpreters, which is why the gate script exists. Claim (ii) needs the registry cells and the member closure; claim (iv) needs the named sub-route cells and the list of limits; claim (iii) is a cheap ratchet (one AST rule, a reviewed list, nine seeds) that the owner chose to keep as a ratchet rather than grow. Slice 2 pays back the exact builder's 5x by building once per run. Revision 7 added no mechanism; it narrowed claims, closed one derivation, removed one gate input and corrected one seed. Revision 8 adds no mechanism either: the claims table is documentation of the existing tests, and the replacement handler is a seed for a test the plan already had. No slice exists only to police a mechanism this plan introduced.

Does the registry-derived coverage need a new production registry? No. The owner declined one for the ad hoc branches (Q10, decided); it stays in the section 15 backlog.

Still removable, in order, with the row each one backs:

1. T10a (backs no row). The construction in slice 2 removes the per-batch multiplier, and T10 proves it (row C23). T10a only bounds the one-off build at 999,000 categories. Removing it loses a performance number and no claim.
2. Slice 1d into its own plan (backs row C19's route-layer half and T11b). It is a real class 1 defect in the route-layer messages but shares no code path with P1. Folding it into 1c (both edit the same two route functions) is the cheaper alternative; it would keep 1c within the bound at about 690 lines. Moving it out deletes the route-layer sentence of row C19.
3. T7 (backs row C6). With T20 labelled a ratchet, T7 adds a second ratchet of the same strength for searches; the three CDF searches are already executed by the T5r and T5s cells. Removing it deletes row C6 and the `_WEIGHTED_CDF_RES` load check. It is kept because it costs one small AST test and catches the most likely accidental drift (a new `bisect_right` over a list built in place).
4. The D4 lane cells of T5s (back row C7's D4 entry and the adapter limit of row C4). The three lanes end in the same two members, so the lane cells mostly re-prove S6 and S8. They stay because a lane that dropped `categorical_cdfs` would otherwise go unseen, and because with finding L they are two of the five cells that run through `PandasExecutionAdapter`.
5. The `new_weighted_site` seed of T5r (backs row C3's variant axis). It shows that a new weighted draw site creates cells without drivers. It could go if the owner accepts that a new weighted site is a rarer event than a new member; it stays because it is one `monkeypatch` on a frozen table.

Not removable without deleting a row: the T5r closure and its other five seeds (C3, C35), the T5r and T5s cells (C4, C7), T9, T9c and T19 (C24, C25), T10 (C23, C26), T18 (C13), the T3 matrix (C11, C12, C14, C20), T12 and T12n (C2, C19, C22, C32), T20 (C5), the gate script and its seeded files (C31), the contract digest test (C34), T17 and the registry partners (C36), T8 (C17), T14 (C7, D6).

Kept and why: rule A1 and T3 over E1 to E10 (the route-dependent precedence is itself a defect the exact builder exposes); slice 2; the contract digest test; T9c beside T9 (identity and content are different failure shapes); T14 (the one check that the compiled kernel agrees with the Python reference on a moved slot).

## 15. Out of scope (backlog entries)

- Sampler weight totals (revision 4 slice 3; its own defect, its own plan). `generation/_window_admission.py:129-130,151-157,211-217,224-228` admits year bins and top values with `isfinite(sum(weights))` and `> 0`. The consumers `generation/statistical/_sample.py:268,296,336,366` guard with `sum(...) <= 0` and then draw through `random.choices`, which totals with `itertools.accumulate`. On 3.12, `sum` is compensated, so a near-overflow vector such as `[M, 0.6h, 0.6h]` (`M` the largest double, `h` half its ulp) totals to `inf` in the admission guard while `random.choices` totals it to the finite `M`. The guard and the consumer disagree on 3.12 only (derived from the probed `sum` values and the code, not run end to end). The fix is one left-fold total helper shared by the guard and the consumer (P5, guard equals consumer), with an AST sentry against builtin `sum` over weights at guard sites. Its own plan should state a red-first cell on 3.12 and the local gate in section 5.4.
- Finding B: default dataclass reprs of `PreparedCategorical` and `CategoricalColumnDecision` render category values (class 1). Its own S plan: decide `repr=False` per field, and add a marker test over every repr reachable from an error or log path.
- Report-plane float sums (Q5) and the NumPy-per-interpreter draw (Q6).
- A production route table for categorical dispatch (declined for this plan by the owner, Q10; recorded for a later plan), which would let the section 3.3a D7 and D8 branches get registry-derived cells: one table every dispatcher consults, landed as a behavior-preserving refactor with the V2.0-A snapshot harness, after which T5r derives its cells from that table.
