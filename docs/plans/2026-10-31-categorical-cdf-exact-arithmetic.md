# Categorical CDF: exact integer arithmetic for interpreter-independent output

Status: plan, revision 5 (handoff "Categorical CDF exact arithmetic: plan revision 5, take the cuts"). Revision 4 closed the seven Codex findings on revision 3 by mechanism, and its own section 14 said it was not the smallest design. The owner's delegation ("go with recommended") accepts the cuts that section 14 proposed. Revision 5 is the smaller plan, rewritten rather than patched. It cuts the CDF memo (revision 4 slices 2a, 2b, 2c, rules K1 and R1a to R1f, tests T9 to T9e and T10b) and replaces it with construction that needs no cache (section 4, rule L1, slice 2). It moves the sampler weight totals (revision 4 slice 3, T6) to a backlog entry (section 15) and drops the workflow edits and CI job (revision 4 slice 4b, T4, T15). Every earlier finding is still mapped (section 0). No code is on this branch.

Rules consulted: decoy-platform `docs/guides/engineering-best-practices.md` (read at decoy-platform `2d40c9a`) section 1.3 (behavior change is its own slice), 2.4 (land the assertion test first), 3.4 (contract as gate), 4.1 (module size), 4.3 (explicit state objects), 5.2 (sentry tests), 6.2 (established methodology), 6.4 (performance claims trace to dated benchmarks), 7.3 (no benchmarks on shared runners), 8.1 (pre-GA hard delete), 10.3 (no em-dashes); the revision 5 handoff (the cuts, the worker rules, slice bound 10 production files and 800 lines); the revision 4 handoff (findings 1 to 7, the A-4 coupling note); `/home/cam/dev-rules/` `risk-and-exceptions.md`, `testing.md`, `verification.md`, `api-and-compatibility.md`, `reuse-first.md`, `documentation.md`, `performance.md`; decoy-platform `handoffs/queue` `DEFECT-CATALOG.md`, `HOSTILE-PLAN-REVIEW.md`, `OWNER-DECISIONS-2026-10-30.md`; engine `CLAUDE.md`, `docs/reasons.md` (Versioning), `docs/compatibility-contract.md` section 3.3, `docs/records/2026-10-31-main-health-engine.md` row 4. The `/home/cam/dev-rules` and decoy-platform `handoffs/queue` files are not in this container, so their content comes from the handoff text.

Risk: R2. Reason: the change is visible in masked output (compatibility-contract section 3.3) on a pre-GA engine that can be reverted. It is not R3: there is no destructive or irreversible operation, no vault or production data the change could strand (owner decision 2026-10-31: no old-engine recovery path), and rollback is a revert. It must land before the D-4 launch-engine freeze (decoy-platform `docs/ROADMAP.md:480`), because after the freeze the same change is a major-version determinism break.

Baseline commits. Revision 4 text carried over keeps `545d913` line numbers. On `a3fe49e` (`origin/main` on 2026-11-01) every cited file has the same line numbers except `src/decoy_engine/execution/out_of_core/_mask_group_b.py` (`:316` to `:395` are 5 lower, so `:344` is `:339` and `:387` is `:382`) and `docs/compatibility-contract.md` (6 lines inserted at `:44`, so `:253`, `:254`, `:301-307` are `:259`, `:260`, `:307-313`). Material new in revision 5 (the section 3.2 inventory, rule L1, slice 2) is cited on `a3fe49e` and marked. Files that exist only on `a3fe49e` are cited there: `scripts/release/reason_release_pin.py`, `tests/unit/test_reason_release_pin.py`, `tests/fixtures/f29/reason-registry-release.json`.

## 0. Revision 5 changes and the finding map

### 0.1 What revision 5 cuts, keeps and re-slices

- Cut: the memo. A run builds each weight vector at a fixed construction point and holds the tuple in an object that lives only as long as the run (rule L1, section 4). Out of core, `decide_table` builds once per table and the Python path reads that tuple. The chunked oracle builds once per run, holds the result in a local of its masking generator and passes it to every chunk. One rule, one lifetime test (T9) with a seeded violation. Gone: `CdfMemo`, rule K1, rules R1a to R1f, P7, T9 to T9e and T10b.
- Cut: revision 4 slice 3 (sampler weight totals, P5, T6, the `_window_admission.py` left fold). It is a separate defect and gets its own plan (section 15).
- Cut: revision 4 slice 4b (the `native-companion.yml` steps and trigger paths, T15) and the T4 `cdf-interpreter-matrix` CI job and its sentry. GitHub Actions is off for this repo (`.github/workflows/native-companion-release.yml:20`, `scripts/release/native_release_checks.sh:4`, on `a3fe49e`). The local gate commands in section 5.4, run in each slice's evidence, are the check. T14, the compiled-kernel test, stays as an ordinary native-suite test in slice 4a and runs locally with the companion built.
- Kept: slice 1r (registry bump with a literal `since`), 1a, 1b, 1c and 1d. Also kept: every mechanism fix from revision 4: N1 by exact type using C type codes, T12 over a named function scope, the T5 independent KAT, the contract digest test, and T17.
- Re-sliced to fit the bound of 10 production files and 800 lines per slice (section 10). Revision 4's 1a was about 1,080 lines, so it is split. Slice 1a now holds the builder and its red-first tests. The integer-only sentry T12 and the performance gate T10a move to a new slice 1e. The three route-entry `float(w)` deletions, the two bare-error `except` deletions, T2b, T2c and T12b move from 1a to 1c, which already rewrites those functions. Revision 4's 4a was about 1,000 lines, so it is split into 4a (route parity: T5, T8, T14) and 4c (coverage sentries: T7, T16).

### 0.2 Every prior finding, where it is closed now

Triage round (on revision 1, closed in revision 2):

| Finding | Class | Section | Slice | Test |
|---|---|---|---|---|
| 1 HIGH: below-resolution message uses float arithmetic (`_categorical.py:112`) | 1 and P1 | 4 rule M1 | 1a | T3m, T12 (1e) |
| 2 HIGH: an `lru_cache` keyed on the raw weight tuple collapses `int`, `float`, `bool`, signed zero | 6 and 3 | 4 rule N1; rule L1 (no cache or lookup by value exists; reuse is gated by object identity, not equality) | 1a, 2 | T2, T3m (1a); T19 (2) |
| 3 MEDIUM: `list[int]` to `tuple[int, ...]` declarations unverified; companion absent | 9 | 3.5 | 1a; 4a | T13; T14 |
| 4 MEDIUM: T5, T7, T8 without a fail-before location | 8 | 5.0 register | 4a, 4c | T5, T7, T8 |
| 5 LOW: `except (ValueError, OverflowError)` kept, unreachability unspecified | 6 and 9 | 3.3 | 1c | T12b |
| Re-read: bare `ValueError`/`TypeError` from `float(w)`; `bool` silently `1.0` | 6 and 4 | 4 rule N1 | 1a, 1c | T3m, T3, T2c |
| Re-read: F29 fixtures said to be regenerated with `--write` | 8 | 5.3 | 1r | T17 |

Codex round on revision 2 (closed in revision 3):

| Finding | Class | Section | Slice | Test |
|---|---|---|---|---|
| 1 HIGH: admission prechecks decide before the builder | 4 and 6 | 4 rule A1, 3.3 | 1c | T3 |
| 2 MEDIUM: native-present gate placed in a container with no engine | 8 | 5.5: no workflow edit remains, and T14 runs locally with the flag that forbids skips | 4a | T14 |
| 3 MEDIUM: T5 missed the `run_seq` route | 8 and 4 | 3.2 (S2), 5.1 T5 | 4a | T5 cell S2 |
| 4 MEDIUM: T1, T6, T10 lacked node ids; T10 cannot be red first | 8 | 5.0 register (T6 left with slice 3; T10 is now the build-count test, red on the baseline) | 1a, 1e, 2 | T1, T10a, T10 |
| 5 MEDIUM: messages carry a scalar `repr` | 1 | 4 rule M2 | 1a | T3m, T11, T12 |
| 6 MEDIUM: process-wide `lru_cache` retains about 290 MB across jobs | 3 | 4 rule L1 (no cache at all) | 2 | T9 |
| Re-read: N1 admitted `Fraction`; `longdouble` platform-dependent | 6 | 4 rule N1 | 1a, 1c | T3m, T2c, T3 |
| Re-read: E8 config gate raises a bare `ValueError` | 6 and 4 | 3.3 | 1a (root), 1c | T3 entry E8 |
| Re-read: `numpy.int64`/`float32` declined natively, built in Python | 4 | 4 rule A1 and N1 | 1c | T3 valid rows |
| Re-read: shadow binding and chunked step consumers missing | 8 and 4 | 3.2 | 4c | T16 |
| Re-read: wrong workflow file named for the release sentry | 8 | closed by removal: revision 5 edits and pins no workflow | none | none |

Codex round on revision 3 (closed in revision 4, carried into revision 5):

| Finding | Class | Section | Slice | Test |
|---|---|---|---|---|
| 1 HIGH: two identities added without a registry bump fail `pin_violations` | 10 | 5.3 "Reason registry effect", 8 | 1r | T17, `test_live_registry_honours_the_released_pin` |
| 2 MEDIUM: T16 scan omitted two callers | 8 and 4 | 3.2 inventory (re-derived for revision 5, 72 sites) | 1c (T3 E9, E10), 4c (T16) | T3, T16 |
| 3 MEDIUM: a module `_CACHE` dict passes the memo sentries | 3 | 4 rule L1: the memo and its sentries are gone; lifetime is one rule with one test | 2 | T9 with its seeded violation |
| 4 MEDIUM: `isinstance` admits registered integrals and lying subclasses | 6 | 4 rule N1 | 1a (builder), 1c (every entry) | T12n (1a), T2c (1c), T3 rows |
| 5 MEDIUM: T12 swept unrelated handler messages | 8 | 5.1 T12 scope | 1e; 1d | T12; T11b |
| 6 MEDIUM: route agreement test green on the baseline | 8 | 5.1 T5 | 4a | `test_every_route_matches_the_kat_on_thirty_vectors` |
| 7 MEDIUM: fixture and digest can move without a contract edit | 10 | 5.1 T1, 8 | 1b, 4a | `test_contract_entry_records_the_fixture_digests` |
| Re-read: route-layer messages render the config and type names | 1 | 4 (route-layer messages) | 1d | T11b |
| Re-read: N1 by named numpy types is platform-dependent | 6 and P1 | 4 rule N1 step 1 | 1a | T12n; T3 row `numpy.longlong` (1c) |
| Re-read: pre-mortem item listed the required bump as a failure | 10 | 11 item 14 | 1r | T17 |

### 0.3 Found while writing revision 5 (reported, see section 0.4)

| Finding | Class | Disposition |
|---|---|---|
| A. Revision 4's T16 scan resolved calls only through the `from ... import` table and same-module names. It does not follow calls through a module alias (`_chunked_entry.run_mask_chunked(...)`, `categorical_gate.rejects_nondeterministic(...)`) or a function passed as a module attribute (`_route_exec.run_mask_chunked`). With those followed, `a3fe49e` has 8 more call sites and 2 more value hand-offs (section 3.2, rows marked `attr`). The "positional hand-off" row that revision 4 listed by hand is now found by the scan (constructor rule) | 8 with 4 (route coverage), third occurrence | Reported as a stop item (0.4). The plan text applies the complete resolution because the handoff requires the inventory to equal the scan |
| B. `PreparedCategorical` (`native/_categorical_prepared.py:25`) and `CategoricalColumnDecision` (`out_of_core/_categorical_native.py:83`) are dataclasses with the default `repr`, which renders `categories` (config values) and the CDF. I did not check whether any path renders them | 1 (value-bearing repr) | Not patched here (stop rule). This plan adds no new value-bearing repr: `PrebuiltCdf` and the new `CategoricalColumnDecision.cdf` field use `repr=False` (slice 2). Backlog entry in section 15 |
| C. Ordering hazard, probed: if the slice 2 construction lands before the builder raises only `StrategyError`, a bare `ValueError` escapes the table-level build. The prototype with today's builder turned three tests red (`test_every_stage_a_rejection_declines_with_a_value_free_reason[weights-not-numeric, weight-nan, weight-inf]`) | 4 (error position moves) | Not a defect in the plan. It is why slice 2 is ordered after 1a and 1c, and the red-first evidence for T18 |

### 0.4 Stop-on-repeat report: each class's fix and why it cannot recur

- Class 3 (memo state). Earlier instances: triage 2 (key collapse), Codex round 2 finding 6 (retention across jobs), Codex round 3 finding 3 (a module dict passes a name-based sentry). The fix is removal. No memo, cache, key or shared store remains on the CDF path, so no sentry is needed to police one. The only CDF holders are the run objects that rule L1 names. T9 fails if any CDF built during a run can still be reached after the run returns. The value-key collapse cannot recur because reuse is gated by object identity (`pre.seed is plan`, T19), never by equality. A cache added later would be new code in its own reviewed plan.
- Class 8 with 4 (route coverage). Earlier instances: Codex round 2 finding 3, Codex round 3 finding 2, and now finding A. Revision 4 replaced the hand list with a computed closure, but that closure resolved only two of the four static ways Python code can reference a module-level function. Revision 5 resolves all four: a from-import name, a module alias attribute, a same-module name (nested scopes included), and `self.` methods of the enclosing class. Revision 5 also turns the last hand-listed row into a scanned rule (CDF-carrying constructors). Reported as the third occurrence. What can still escape is a call through `getattr`, a registry lookup or `importlib`. Those are named in section 3.2, and the routes they reach are covered by behavior instead (T3 drives each E entry, T5 each S route). If the gate applies the stop rule strictly to finding A, slice 4c is blocked. The alternative in section 14 item 4 then replaces T16 with a closed-world check that does not depend on a caller closure.
- Class 6 (N1). Earlier instances: revision 3's re-read (`Fraction`), Codex round 3 finding 4. Fix: an exact-type allowlist built from C type codes, conversion only through `int(w)` and `float.as_integer_ratio(float(w))`, and T12n pinning the shape of the conversion helper. It cannot recur through subclassing or ABC registration, because membership is decided by `type(w)` identity against a frozenset. T2c seeds both cases.
- Class 1 (messages). Fix: rule M2 (integer replacement fields only), enforced over a named function scope by T12 and at the route layer by T11b. Finding B is a different surface (reprs) and is reported, not patched.

## 1. Problem and evidence (reproduced from the lock)

`_build_cdf` (`src/decoy_engine/execution/_strategies/_categorical.py:69-119`) computes `total = sum(weights)` (`:75`), a plain running sum (`:92`), then `int(running / total * _WEIGHTED_CDF_RES)` (`:94`). CPython 3.12 made the builtin `sum()` of floats compensated (Neumaier summation; CPython gh-100425, cited from memory, behavior confirmed by the probe below). So `total` differs from 3.10 and 3.11 for many weight vectors, and the truncated thresholds move by one slot. Every weighted-categorical route consumes this one list (section 3.2), so the band boundaries, and with them the masked output, depend on the interpreter.

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

## 2. Invariants as properties

- P1 (interpreter independence). For every weight vector `w`, on every CPython in `requires-python` (`pyproject.toml:13`, `>=3.10,<3.13`), on any numpy version and on any platform, `_build_cdf(w)` returns the same tuple or raises the same registered code with the same message text. After rule N1 converts each weight to its exact rational value, the builder and every message it formats use integer operations only (T12).
- P2 (exactness). `cdf[i] = floor((2 * C_i * RES + T) / (2 * T))`, where `C_i` and `T` are the exact rational prefix sum and total (round half up). So `cdf[-1] == RES`, `cdf` is non-decreasing, a zero weight has zero width, and every band is within one slot of `w_i / T * RES`.
- P2a (representable total). A vector whose exact total exceeds `sys.float_info.max`, or that holds a NaN or an infinity, is rejected with `categorical_weights_nonfinite`. This keeps today's decline of overflowing sums (`tests/native/test_chunked_nondet_categorical_admission.py:122`; `tests/parity/test_phase_d_d1b2_categorical_disposition.py:150`).
- P3 (exact, position-free resolution rule). A positive weight is rejected with `categorical_weight_below_resolution` if and only if `w_i * RES < T` exactly.
- P4 (one builder). Every route that maps a bucket to a weighted category reads a CDF produced by `_build_cdf` and nothing else.
- P6 (total, interpreter-free error precedence). The shape check (`categorical_weights_shape`) runs first at the route layer, as today on every route. Inside the builder, each pass completes over the whole vector before the next starts: (1) not numeric (N1), (2) a NaN or infinite element (`nonfinite`), (3) a negative element, (4) the exact total (above the largest double is `nonfinite`, zero is `nonpositive`), (5) below resolution. Each pass reports the lowest failing index.
- P8 (admission agrees with execution). For every vector and every admission entry, admission declines with exactly `{code}:{name}` when, and only when, the Python route raises `StrategyError(code)`. It admits when, and only when, the Python route builds, and the admitted column carries the same tuple. No admission entry runs a weight check of its own.
- P9 (construction does not move errors; new in revision 5). Building a CDF ahead of the batch or chunk that uses it (rule L1) never raises. A config the builder rejects raises from the same function, at the same point and with the same code as on the baseline.

P5 (guard equals consumer) and P7 (validation precedes memoization) are removed: P5 belongs to the slice 3 backlog plan (section 15), and P7 had no subject once the memo was cut.

## 3. Enumerations

### 3.1 Sum and threshold sites (every `sum(`, `cumsum`, cumulative table and weight normalization in `src/`)

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

### 3.2 Routes that build or consume the weighted CDF, and the T16 inventory

Builder entries (`ERROR_ROUTES`, driven by T3), unchanged from revision 4:

- E1 `_build_cdf` (`_categorical.py:69`).
- E2 `CategoricalStrategyHandler.run` (`_categorical.py:188`). Reached by the in-memory run, the sequential run's Python path, and the chunked oracle per chunk.
- E3 `categorical_array` through `_categorical_config` (`_mask_group_b.py:309-345`, build at `:387`).
- E4 `prepare_categorical` (`_categorical_prepared.py:34-76`).
- E5 `prepare_positional_categorical` (`_categorical_prepared.py:79-103`).
- E6 `decline_reason` (`out_of_core/_categorical_native.py:58-80`).
- E7 `_categorical_admit` (`_sequential_native_ops.py:310-321`).
- E8 `categorical_config_rejection` (`native/_operator_config_rejections.py:55-66`).
- E9 `positional_config_of_entry` (`native/_categorical_positional.py:24-34`).
- E10 `positional_categorical_bindable` (`physical/_shadow_bindings.py:192-216`).

Selection routes (`SELECTION_ROUTES`, driven by T5): S1 in-memory handler (`_categorical.py:237-241`); S2 sequential native (`_categorical.py:193-206` to `SeqNativeRun.categorical_column`, `_sequential_native.py:355-376`, to `_categorical_apply`, `_sequential_native_ops.py:324-337`); S3 sequential Python fallback; S4 out-of-core Python (`_mask_group_b.py:392-398`); S5 out-of-core native (`_categorical_native.py:198-231`, `native/_categorical_ext.py:136-137`); S6 chunked native deterministic (`_categorical_prepared.py:111-134`, `native/_operator_step.py:423`); S7 chunked native positional (`_operator_step.py:436`); S8 chunked oracle (E2 per chunk, using the run-built tuple after slice 2); S9 physical shadow binding (`physical/_shadow_bindings.py:352-358`); S10 compiled kernel (S5 to S7 with `_CompiledIndexKernel`, T14). The Rust companion has no CDF code; it receives the Python-built integers.

Scan rule (revision 5; class 8 with 4, finding A). An AST closure over `src/decoy_engine/`, starting at `_build_cdf`:

1. Resolution. A call or load names a function when it resolves through any of: the module's `from ... import` table (followed through re-exports), a module alias (`import m as x` or `from pkg import m` followed by `x.f`), a same-module definition (nested scopes searched outward), or `self.f` inside a method of the class that defines `f`. Not resolved: `getattr`, registry lookup, `importlib`. The handler registry reaches E2 by name, and E2 is a direct entry.
2. Closure membership. A function joins the closure when its own body (nested functions excluded) calls a closure member and the result reaches one of its `return` expressions. It can reach the return directly, through a name bound from an expression that contains the call, or through one container step (`box[k] = ...` or `box.attr = ...`, with `box` then returned).
3. Sites. (a) every call to a closure member; (b) every non-call load of a closure member, as a name or a module attribute, outside its definition, imports and `__all__`; (c) under `src/decoy_engine/execution/`, every call that passes a keyword matching `^(.*_)?cdfs?$`, every call of a CDF-carrying constructor (`CategoricalCall`, `PreparedCategorical`, `CategoricalColumnDecision`, `PrebuiltCdf`), and every `bisect_right` or `searchsorted`. A call that matches two rules is one site. The keys are (file, enclosing qualified name, callee, occurrence). Line numbers are not keys.

Run in this container (scratch `t16_scan_rev5.py`, AST only, run with the container's CPython 3.13.18). On `a3fe49e` the rule gives 63 sites: 48 calls, 3 value hand-offs and 12 consumer calls. Restricted to revision 4's two resolution forms, the same script reproduces revision 4's 40 calls and 1 value hand-off exactly, plus the 8 consumer calls revision 4 scanned (its ninth was listed by hand). That cross-check is independent of revision 4's own script, which is not in this container. On a scratch prototype of slice 2 (section 4, rule L1; applied to an `a3fe49e` worktree and never committed) the rule gives 72 sites: 52 calls, 3 value hand-offs and 17 consumer calls. Slices 1a and 1c add no caller of a closure member: their new helpers are callees of `_build_cdf`. So 72 is the inventory T16 holds when it lands in 4c.

All paths are under `src/decoy_engine/execution/`. Lines are `a3fe49e`. "attr" marks a site that only module-alias resolution finds (finding A). "new" marks a site that slice 2 adds; its line is set by the slice.

| Kind | Site | Enclosing function | Callee | Entry |
|---|---|---|---|---|
| call | `_strategies/_categorical.py:188` | `CategoricalStrategyHandler.run` | `_build_cdf` | E2, S1, S2, S3, S8 |
| call new | `_strategies/_categorical.py` | `prebuilt_cdfs` | `_build_cdf` | S8 |
| call new | `_chunked_oracle.py` | `_oracle_masked._masked` | `prebuilt_cdfs` | S8 |
| call | `out_of_core/_mask_group_b.py:382` | `categorical_array` | `_build_cdf` | E3, S4 |
| call new | `out_of_core/_categorical_native.py` | `_python_cdf` | `_build_cdf` | S4 |
| call new | `out_of_core/_categorical_native.py` | `decide_table` | `_python_cdf` | S4 |
| call | `native/_categorical_prepared.py:73` | `prepare_categorical` | `_build_cdf` | E4 |
| call | `native/_categorical_prepared.py:96` | `prepare_positional_categorical` | `prepare_categorical` | E5 |
| call | `native/_categorical_prepared.py:125, :129` | `prepare_chunked_categoricals` | `prepare_categorical`, `prepare_positional_categorical` | S6, S7 |
| call | `native/_chunked_pools.py:74` | `prepared_categoricals` | `prepare_chunked_categoricals` | S6, S7 |
| call | `native/_chunked_entry.py:539` | `_route_chunked` | `prepared_categoricals` | S6, S7 |
| call | `native/_chunked_entry.py:455` | `_run_chunked` | `_route_chunked` | S6, S7 |
| call | `native/_chunked_entry.py:648` | `run_mask_chunked` | `_run_chunked` | S6, S7 |
| call | `native/_dispatch.py:646` | `run_native_or_oracle_chunked` | `_run_chunked` | S6, S7 |
| call | `physical/drivers/_chunked.py:121` | `NativeOrOracleChunkedAdapter.run` | `run_mask_chunked` (native) | S6, S7 |
| call attr | `_pipeline_auto_chunk.py:274` | `_run_dispatcher` | `_chunked_entry.run_mask_chunked` | S6, S7 |
| call attr | `_pipeline_auto_chunk.py:329` | `_run_streamed` | `_chunked_entry.run_mask_chunked` | S6, S7 |
| call attr | `_pipeline_auto_chunk.py:420` | `run_auto_chunk` | `_run_dispatcher` | S6, S7 |
| call attr | `_pipeline_route_exec.py:580` | `run_mask_chunked` | `run_auto_chunk` | S6, S7 |
| call attr | `_pipeline_mixed_route.py:195` | `run_mixed_route` | `run_auto_chunk` | S6, S7 |
| call attr | `_pipeline_multi_table.py:323` | `run_multi_table_split` | `run_auto_chunk` | S6, S7 |
| call attr | `physical/drivers/_chunked.py:166` | `ResidentChunkedAggregatorAdapter.run` | `run_mask_chunked` (route exec) | S6, S7 |
| call | `out_of_core/_categorical_native.py:68` | `decline_reason` | `prepare_categorical` | E6 |
| call | `out_of_core/_categorical_native.py:186` | `decide_table` | `decline_reason` | E6, S5 |
| call | `out_of_core/_hash_run.py:359` | `OocHashRun.prepare_table` | `decide_table` | S4, S5 |
| call | `_sequential_native_ops.py:318` | `_categorical_admit` | `decline_reason` | E7 |
| call | `native/_operator_config_rejections.py:60` | `categorical_config_rejection` | `prepare_categorical` | E8 |
| call | `native/_plan.py:340` | `_config_rejection` | `categorical_config_rejection` | E8 |
| call | `native/_plan.py:306` | `_column_rejection` | `_config_rejection` | E8 |
| call | `native/_plan.py:254` | `native_route_eligibility` | `_column_rejection` | E8 |
| call | `native/_requirements.py:542` | `_config_gate_rejection` | `categorical_config_rejection` | E8 |
| call | `native/_requirements.py:624` | `requirements_for` | `_config_gate_rejection` | E8 |
| call | `native/_plan.py:173` | `compile_native_plan` | `requirements_for` | E8 |
| call | `physical/_compiler.py:325` | `_build_nodes` | `requirements_for` | E8 |
| call | `native/_when_admission.py:152` | `when_native_rejection` | `_column_rejection` | E8 |
| call | `native/_categorical_positional.py:29` | `positional_config_of_entry` | `prepare_positional_categorical` | E9 |
| call | `native/_categorical_positional.py:46` | `positional_config_for_column` | `positional_config_of_entry` | E9 |
| call | `_chunked_categorical.py:51` | `rejects_nondeterministic` | `positional_config_of_entry` | E9 |
| call | `_chunked_categorical.py:117` | `reject_nondeterministic_when` | `positional_config_of_entry` | E9 |
| call attr | `_chunked.py:332` | `check_chunked_compatibility` | `categorical_gate.rejects_nondeterministic` | E9 |
| call | `_chunked_when_guard.py:111` | `plan_positional_when_guard` | `positional_config_of_entry` | E9 |
| call | `native/_when_admission.py:147` | `when_native_rejection` | `positional_config_of_entry` | E9 |
| call | `native/_faker_positional_admission.py:140` | `chunked_positional_column` | `positional_config_for_column` | E9 |
| call | `native/_dispatch.py:325, :500` | `_static_route_decision`, `plan_native_route` | `chunked_positional_column` | E9 |
| call | `native/_chunked_evidence.py:109` | `plan_column_backends` | `chunked_positional_column` | E9 |
| call | `physical/_shadow_bindings.py:207` | `positional_categorical_bindable` | `prepare_positional_categorical` | E10 |
| call | `physical/_shadow_bindings.py:279` | `execution_binding_for_slice_node` | `positional_categorical_bindable` | E10 |
| call | `physical/_shadow_bindings.py:352, :356` | `execution_binding_for_slice_node` | `prepare_positional_categorical`, `prepare_categorical` | S9 |
| call | `physical/_compiler.py:347` | `_build_nodes` | `execution_binding_for_slice_node` | S9, E10 |
| value | `_sequential_native_ops.py:505` | `categorical_op` | `_categorical_admit` (into `NativeOp`) | E7 |
| value attr | `_pipeline_generate_mask.py:246` | `run_generate_and_mask_steps` | `_pipeline_auto_chunk.run_auto_chunk` | S6, S7 |
| value attr | `_pipeline_generate_mask.py:248` | `run_generate_and_mask_steps` | `_route_exec.run_mask_chunked` | S6, S7 |
| consumer | `_strategies/_categorical.py:195` | `CategoricalStrategyHandler.run` | `categorical_column(cdf=)` | S2 |
| consumer | `_strategies/_categorical.py:238` | `CategoricalStrategyHandler.run` | `bisect_right` | S1, S3, S8 |
| consumer new | `_strategies/_categorical.py` | `prebuilt_cdfs` | `PrebuiltCdf(...)` | S8 |
| consumer new | `_chunked_oracle.py` | `_oracle_masked._masked` | `adapter.run(categorical_cdfs=)` | S8 |
| consumer new | `_pandas_adapter.py` | `PandasExecutionAdapter.run` | `StrategyContext(categorical_cdfs=)` | S8 |
| consumer | `_sequential_native.py:370` | `SeqNativeRun.categorical_column` | `CategoricalCall(...)` | S2 |
| consumer | `_sequential_native_ops.py:328` | `_categorical_apply` | `native_categorical(cdf=)` | S2 |
| consumer | `out_of_core/_mask_group_b.py:390` | `categorical_array` | `bisect_right` | S4 |
| consumer new | `out_of_core/_mask.py` | `mask_batch` | `mask_column(categorical_cdf=)` | S4 |
| consumer new | `out_of_core/_mask.py` | `mask_column` | `categorical_array(cdf=)` | S4 |
| consumer | `out_of_core/_categorical_native.py:187` | `decide_table` | `CategoricalColumnDecision(...)` (gains `cdf=` in slice 2) | S4, S5 |
| consumer | `out_of_core/_categorical_native.py:222` | `native_categorical_array` | `native_categorical(cdf=)` | S5 |
| consumer | `native/_categorical_prepared.py:76` | `prepare_categorical` | `PreparedCategorical(...)` | E4, S5, S6, S9 |
| consumer | `native/_categorical_prepared.py:103` | `prepare_positional_categorical` | `PreparedCategorical(...)` | E5, S7, S9 |
| consumer | `native/_operator_step.py:420` | `run_kernel_step` | `native_categorical(cdf=)` | S6, S9 |
| consumer | `native/_operator_step.py:432` | `run_kernel_step` | `native_categorical_positional(cdf=)` | S7, S9 |
| consumer | `native/_categorical_ext.py:137` | `_select` | `searchsorted` | S5, S6, S7, S9, S10 |

Rows that list two lines are two sites. Count: 52 calls (46 rows with one site, 3 rows with two), 3 value hand-offs and 17 consumer calls, so 72. The search sites outside `execution/` (`generation/statistical/_sample.py:168`, `generation/window.py:323`, `io/bounded_reader.py:246`) are not CDF consumers and are outside rule 3c. The three search sites in the table are the only CDF consumers (T7).

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

- Slice 1a (builder only). One set of validation passes in P6 order (N1 first). Every exit is a registered `StrategyError` with an M2 message, using `categorical_weights_nonfinite` and `categorical_weights_not_numeric` (added by 1r to `src/decoy_engine/reasons/runtime/_part3.py` beside `:97-104` and to its `__all__` at `:8-15`). The builder returns a tuple. From here on `_build_cdf` raises `StrategyError` only, so E8's crash and the bare escapes are removed at the root. Route entries still call `float(w)` until 1c. The intermediate state is consistent: the routes hand the builder floats, as today, and every error the builder raises is registered.
- Slice 1c (route layer, rule A1). Delete the three `float(w)` conversions, the prechecks at `_categorical_prepared.py:68-71`, the two `except (ValueError, OverflowError)` clauses (`_categorical_prepared.py:99`, `_categorical_native.py:74`; unreachable after 1a, and T12b keeps them deleted) and the literal `categorical_weights_unbuildable_cdf` (pre-GA hard delete; `grep -rn unbuildable_cdf` over decoy-platform is empty). `prepare_categorical` passes the raw weights and maps `except StrategyError as exc` to `f"{exc.code}:{name}"`. The `tuple(...)` wrap around the builder call goes, so the artifact holds the builder's own object (section 4, L1, and T9 depend on that). Rewrite the docstrings at `_categorical_prepared.py:44-52`, `:88-92` and `_categorical_native.py:63-66`.
- Slice 2 (construction, rule L1). The added table-level and run-level builds catch `StrategyError` and leave the column without a prebuilt tuple. The consumer then builds and raises from the same function as on the baseline (P9, T18).

### 3.4 FK shapes

- A single-column FK key that is a deterministic categorical column stays on Python (`docs/compatibility-contract.md:253`). Parent and child keys call the same builder in the same process, so a run joins with itself. Cross-run join stability breaks once at the cutover; that is the section 8 contract entry.
- Composite, self-referencing, cross-table and children-in-another-batch FK shapes have no categorical-specific path. The builder is a pure per-column function of the config weights, so they reduce to the single-column case. T8 covers single and composite keys.

### 3.5 Fields of the public and shared result types touched

- `PreparedCategorical.cdf` (`native/_categorical_prepared.py:26-31`): stays `tuple[int, ...] | None`, with values changed per P2.
- `CategoricalCall.cdf` (`_sequential_native_ops.py:148-155`): unchanged shape.
- `StrategyError.code`, `.strategy` and `.message` for each builder exit: two new codes and rewritten messages (M2).
- Admission reasons: `unbuildable_cdf` is replaced by the specific code (A1).
- Return-type declarations, verified at `545d913`: the only `list[int]` is `SeqNativeRun.categorical_column(..., cdf: list[int] | None)` at `_sequential_native.py:364`. Its body converts with `tuple(cdf)` at `:374`. Mypy does not check it at its one call site (`_categorical.py:193` reads `seq_native` through `getattr`). Slice 1a changes `:364` to `tuple[int, ...] | None` without changing the line count (the module is at 697 of the 700 maximum). T13 pins it.
- New in slice 2 (on `a3fe49e`): `StrategyContext` (`_adapter.py:121`) gains `categorical_cdfs: Mapping[str, PrebuiltCdf] | None = None`, beside `code_set_records` (`:210`). The `ExecutionAdapter.run` protocol (`_adapter.py:329`, beside `code_set_records` at `:354`) and `PandasExecutionAdapter.run` (`_pandas_adapter.py:163`, `:176`, seeded at `:251`) gain a `categorical_cdfs` keyword. `CategoricalColumnDecision` (`out_of_core/_categorical_native.py:83`) gains `cdf: tuple[int, ...] | None = field(default=None, repr=False)`. `TableCategoricalPlan` gains `cdf_for(column)`; `TableHashPlan` (`_hash_run.py:137`) gains `categorical_cdf(column)`. `mask_column` gains `categorical_cdf`, and `categorical_array` gains `cdf`. New `PrebuiltCdf` (`@dataclass(frozen=True, repr=False)` with fields `seed`, `cdf`) in `_categorical.py`.
- Compiled companion: built and loaded in this container for revision 3 (`present-ok`, ABI `decoy-native-abi-3`). It has no categorical code of its own.

### 3.6 Output surfaces

- Masked data: weighted categorical columns on every route in 3.2 (one-slot band moves; section 4).
- Error text: every builder message (M2) and the route-layer categorical messages (1d). None carries a weight value, a label, a source value, or the `repr` or type name of any object.
- Admission reasons: `{code}:{name}` only.
- Run metrics: `quality_metrics["sequential_categorical"]` (contract `:254`), unchanged.
- Reprs: the new holder types do not render values (`repr=False`); the existing ones are finding B.
- Proof manifest: `docs/proof-manifest.json:441-445` has no weights; `scripts/gen_proof_manifest.py` is re-run as the check.

## 4. Method, rules, and output consequence

Method. Every finite IEEE 754 binary64 value is a dyadic rational `n / 2**k`, available exactly through `float.as_integer_ratio()` (IEEE 754-2019 section 3.3; Python documentation). Scale every weight to the largest power-of-two denominator in the vector. Each weight is then an exact `int`, and prefix sums and the total are exact. Thresholds are `(2 * C_i * RES + T) // (2 * T)`. This is the integer-frequency normalization used where a CDF must be bit-identical across encoder and decoder, as in range coding and asymmetric numeral systems (Witten, Neal, Cleary 1987, CACM 30(6); Duda 2013, arXiv:1311.2540), with the exact input taken from the float. `fractions.Fraction` is the independent test oracle (T1, T2), not the implementation.

Rule N1 (weight normalization, by exact type). The builder takes the raw config values. One helper, `_exact_ratio(w, i) -> tuple[int, int]`, decides on `type(w)` only:

1. `type(w) in _EXACT_INT_TYPES = frozenset({int} | {numpy.dtype(c).type for c in "bBhHiIlLqQ"})` gives `(int(w), 1)`. The set is built from C type codes because named types differ by platform. On Linux with numpy 2.5.0, `numpy.longlong is numpy.int64` is false (probed); on Windows they are the same type. `bool` and `numpy.bool_` are not members.
2. `type(w) in _EXACT_FLOAT_TYPES = frozenset({float, numpy.float16, numpy.float32, numpy.float64})`: `x = float(w)`, then `math.isfinite(x)` or `nonfinite`, then `float.as_integer_ratio(x)` called on the built-in type, never `w.as_integer_ratio()`.
3. Anything else is `not_numeric`, reported with the index only. That covers `bool`, `numpy.bool_`, `str`, `None`, `Decimal`, `Fraction`, `numpy.longdouble`, complex values, any subclass of an allowed type (a lying `float` subclass, probed), and any class registered with `numbers.Integral` or `numbers.Real`.

The common denominator is the largest `2**k`, so scaling is a shift. T12n pins the shape of the helper: it calls only `type`, `int`, `float`, `float.as_integer_ratio`, `math.isfinite` and the `StrategyError` constructor. It never calls `isinstance`, `issubclass` or a method of `w`.

Rule M1 (below-resolution bound). The exact minimum admissible weight is `T / RES` as `n / d`. The message prints the smallest three-significant-digit decimal that is not below `n / d`, using integers only. The exponent comes from integer comparison, the mantissa is `m = ceil(n * 10**(2 - e) / d)`, `m == 1000` carries, and the text is `f"{m // 100:d}.{m % 100:02d}e{e:+03d}"`. Known answers on 3.10.20, 3.11.18 and 3.12.3: `[1.0, 1e-20, 1.0]` gives `2.01e-06`, `[1e-9, 1]` gives `1.01e-06`, and `[1.0, 2.0e-6, 1.0]` gives `2.01e-06`. It uses the ceiling, not rounding, so the printed bound always satisfies P3.

Rule M2 (canonical message text). A builder message never renders a weight. Its only replacement fields are Python `int`s with an integer spec (`d`, `02d`, `+03d`): the index, `_WEIGHTED_CDF_RES` and the M1 parts.

| Code | Message text |
|---|---|
| `categorical_weights_not_numeric` | `categorical weight at index {i:d} is not a number; weights must be int or float values (bool, string and null are rejected).` |
| `categorical_weights_nonfinite` (element) | `categorical weight at index {i:d} is NaN or infinite.` |
| `categorical_weights_negative` | `categorical weight at index {i:d} is negative.` |
| `categorical_weights_nonfinite` (total) | `categorical weights sum above the largest finite double; cannot normalize.` |
| `categorical_weights_nonpositive` | `categorical weights sum to 0; cannot normalize.` |
| `categorical_weight_below_resolution` | `categorical weight at index {i:d} is below the CDF resolution (1 / {RES:d} of total). The category would never be selected. Either remove the category or use weights >= {M1}.` |

Route-layer messages (slice 1d). `_categorical.py:149`, `:172` and `_mask_group_b.py:321-322` render the categories config with `!r` and type names. Under M2 they keep the column name and integer lengths and drop the value and the type name: `column {column!r} uses categorical with categories that are not a list; provide categories as a list.` and `column {column!r}: weights must be a list with the same length as categories ({n:d}).` The out-of-core reader omits `{column!r}`, as it does today.

Rule A1 (admission reason). No admission entry (E4 to E10) runs a weight check of its own. Each calls the builder through `prepare_categorical` and maps `StrategyError` to `f"{exc.code}:{name}"`. E9 and E10 return E5's verdict as an artifact or a bool. Rejected alternative: collapse every error to `unbuildable_cdf`. It loses the diagnosis and loosens two existing tests.

Rule L1 (construction and lifetime; replaces the memo, new in revision 5). A weighted CDF is built only at these points, and only these objects hold it, each for the life of one run:

| Route | Built at | Held by | Builds per run (column with weights) |
|---|---|---|---|
| in-memory, sequential (E2, S1 to S3) | `CategoricalStrategyHandler.run`, once per handler call; the sequential route is whole-table (`_sequential.py:199-209`) | the handler's frame; S2 passes it into `CategoricalCall` for that call | one per table slice, as today |
| chunked oracle (S8) | `prebuilt_cdfs(per_column)`, called once at the top of `_oracle_masked._masked` (`_chunked_oracle.py:393`, `a3fe49e`) | a local of the `_masked` generator, passed to every chunk as `state.adapter.run(..., categorical_cdfs=...)` (`:429-440`), seeded into the per-chunk `StrategyContext` exactly like `code_set_records` | one, independent of the chunk count |
| out of core (S4, S5) | `decide_table` (`out_of_core/_categorical_native.py:174-195`), once per table before the first batch: a native-admitted column reuses `prepared.cdf`; every other weighted column gets `_python_cdf(seed)` | `CategoricalColumnDecision.cdf` inside the run's `TableCategoricalPlan`. `mask_batch` passes `hash_plan.categorical_cdf(column)` to `mask_column`, which passes it to `categorical_array(cdf=...)` | one (two when stage B declines a column: `decline_reason` builds and discards before `_python_cdf` builds), independent of the batch count |
| chunked native, physical (S6, S7, S9) | `prepare_chunked_categoricals`, the shadow binding (already once per run) | the run's prepared artifacts | unchanged |

Statement of the rule. No module, class, default-argument or global state holds a CDF, a weight vector or anything derived from one, and there is no cache or lookup by value anywhere on the CDF path. Reuse is gated by object identity: the handler uses `ctx.categorical_cdfs[column]` only if `pre.seed is plan`, and otherwise builds. A plan holds one seed per column (`plan/_serialize.py:112` writes `per_column` as a dict keyed by name), so identity is defense in depth, not a key. A config the builder rejects gets no prebuilt tuple (`except StrategyError`), so the consumer builds and raises where it does today (P9). Every CDF built during a run is unreachable once the run returns and its stream is drained or closed (`release_when_done`, `_chunked_oracle.py:145`). That is the subject of T9, the one lifetime test.

Other paths stay as they are: `categorical_array` called with no `cdf` (E3 directly, and the resident `mask_table` call at `_mask.py:350`, which runs once per table) builds for itself.

Memory: a run holds at most one tuple per weighted column, `len(weights)` ints each (about 36 MB at the 999,000-category ceiling). That is the same order as the config list of 999,000 floats the run already holds, and it is released with the run.

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
| T2b | `...test_categorical_cdf_exact.py::test_integer_weights_above_2_pow_53_convert_exactly_on_every_entry` | `_categorical.py:176`, `_mask_group_b.py:344`, `_categorical_prepared.py:73` (`float(w)` gives `[333333, 333334, 1000000]` after 1a's builder) | reinstate one `float(w)` | 1c |
| T2c | `...test_categorical_cdf_exact.py::test_subclasses_and_registered_numeric_types_are_rejected[<case>-<entry>]` | `_categorical.py:75` (E1, recorded on `a3fe49e`), `:176`, `_mask_group_b.py:344` (E2, E3), `_categorical_prepared.py:68` (E4 to E10 admit a `float` subclass) | `type(w) in` replaced by `isinstance`; `float.as_integer_ratio(float(w))` replaced by `w.as_integer_ratio()` | 1c |
| T3 | `tests/unit/execution/test_categorical_cdf_route_errors.py::test_every_route_reports_the_same_code[<entry>-<row>]` over E1 to E10 | measured per cell (section 5.1 table) | reinstate a precheck at `_categorical_prepared.py:68-71`; collapse to `unbuildable_cdf`; swap the negative and non-finite passes | 1c |
| T12b | `tests/sentry/test_cdf_no_bare_catch.py::test_no_bare_error_catch_around_the_builder` | `_categorical_prepared.py:99`, `_categorical_native.py:74` | reinstate either clause | 1c |
| T11b | `...test_categorical_cdf_exact.py::test_route_layer_messages_carry_no_config_value_or_type_name[<entry>-<case>]` | `_categorical.py:149`, `:172`, `_mask_group_b.py:321-322` | put `{raw_categories!r}` or a `type(...).__name__` back | 1d |
| T1 | `...test_categorical_cdf_kat.py::test_contract_entry_records_the_fixture_digests[<fixture>]` (`kat` in 1b, `slot_hits` in 4a) | red: contract section 3.3 (`:307-313` on `a3fe49e`) records no digest | regenerate a fixture and its digest without the contract edit | 1b, 4a |
| T9 | `tests/unit/execution/test_categorical_cdf_lifetime.py::test_no_cdf_outlives_its_run[<run>]` for `in_memory`, `sequential`, `chunked_oracle`, `out_of_core_python`, `out_of_core_native` | non-red gate (nothing retains today; probed with the same mechanism over 38 test nodes on the prototype, 0 retained) | seeded: `test_seeded_retention_is_reported` (below); source mutation run once: `_python_cdf` appends its tuple to a module list (probed: 9 more nodes retained) | 2 |
| T9 | `...test_categorical_cdf_lifetime.py::test_seeded_retention_is_reported` | non-red gate: the test wraps `_build_cdf` in `out_of_core._categorical_native` and `_strategies._categorical` with a wrapper that appends each result to a list on a `types.ModuleType` it creates, runs the out-of-core and chunked-oracle runs, and asserts the lifetime check reports both | make the check ignore live tokens | 2 |
| T10 | `tests/unit/execution/test_categorical_cdf_construction.py::test_build_count_is_independent_of_batch_count[out_of_core]` | red: `_mask_group_b.py:382` (`a3fe49e`) builds once per batch (20 builds over 20 batches) | `categorical_array` ignores `cdf=`; `mask_batch` stops passing `categorical_cdf` | 2 |
| T10 | `...test_categorical_cdf_construction.py::test_build_count_is_independent_of_batch_count[chunked_oracle]` | red: `_categorical.py:188` builds once per chunk (5 builds over 5 chunks) | drop `categorical_cdfs=` from the per-chunk `adapter.run` call | 2 |
| T18 | `...test_categorical_cdf_construction.py::test_prebuild_never_raises_and_errors_surface_where_they_did[<route>-<row>]` | non-red gate on the baseline (no prebuild exists). Red if slice 2 lands before 1a and 1c (prototype, finding C: three disposition nodes red) | `_python_cdf` or `prebuilt_cdfs` lets `StrategyError` propagate (the zero-batch and guard-first cases go red) | 2 |
| T19 | `...test_categorical_cdf_construction.py::test_a_prebuilt_cdf_for_another_seed_is_ignored` | non-red gate (no prebuilt mapping exists on the baseline) | drop the `pre.seed is plan` check; key the mapping by `seed` value | 2 |
| T5 | `tests/parity/test_categorical_cdf_routes.py::test_every_route_emits_the_kat_category_for_slot_hitting_values[<route>]` | `_categorical.py:94`, observed at `_categorical.py:238`, `_mask_group_b.py:395`, `_categorical_ext.py:137`; S2 measured | a local float builder in `_mask_group_b.py`; `tuple(cdf)` at `_sequential_native.py:374` replaced by a rebuilt tuple; `prebuilt_cdfs` built with the float reference (S8 red) | 4a |
| T5 | `...test_categorical_cdf_routes.py::test_slot_hit_fixture_values_hit_the_moved_slot[<route>]` | non-red gate (precondition) | edit a fixture value | 4a |
| T5 | `...test_categorical_cdf_routes.py::test_every_route_matches_the_kat_on_thirty_vectors[<route>]` | `_categorical.py:94`, at each consumer (moved-slot hit guaranteed per key pair) | rebind one route's builder name to `tests/perf/_float_cdf_reference._build_cdf` (exactly that route goes red) | 4a |
| T5 | `...test_categorical_cdf_routes.py::test_every_route_key_pair_has_a_moved_slot_hit` | non-red gate (precondition) | delete the generator's hit search | 4a |
| T8 | `tests/parity/test_categorical_cdf_fk.py::test_weighted_categorical_fk_keys_join_and_match_the_kat` | `_categorical.py:94` (slot-hitting keys mask to `z`, expected `y`) | build the child key with the float builder | 4a |
| T14 | `tests/native/test_cdf_native_present.py::test_weighted_cdf_selection_on_the_compiled_kernel` | `_categorical.py:94` (compiled kernel emits `z`, expected `y`; probed) | any change that moves the CDF before `native_categorical` | 4a |
| T14 | `tests/native/test_cdf_native_present.py::test_the_compiled_kernel_buckets_the_slot_hit_values` | non-red gate (raw buckets are 666666, probed) | edit a fixture value | 4a |
| T7 | `tests/sentry/test_cdf_single_builder.py::test_every_cdf_consumer_reads_the_one_builder` | non-red gate; asserts exactly three search consumers | replace the import at `_mask_group_b.py:59` with a local loop | 4c |
| T7 | `...test_cdf_single_builder.py::test_seeded_local_builder_is_reported` | non-red gate; fixture `tests/fixtures/sentry/cdf_local_builder.py` | weaken the sentry | 4c |
| T16 | `...test_cdf_single_builder.py::test_site_inventory_equals_the_scan` | red: `CDF_SITES` absent | add a caller of `positional_config_of_entry` through a module alias (finding A's form); pass a closure function as a module attribute; delete a scanned site from the inventory | 4c |
| T16 | `...test_cdf_single_builder.py::test_every_site_names_an_existing_registry_entry` | red: registry absent | point a site at an undeclared entry id | 4c |
| T16 | `...test_cdf_single_builder.py::test_seeded_unregistered_route_is_reported` | non-red gate; fixture `tests/fixtures/sentry/cdf_unregistered_route.py` (a from-import caller, a module-alias caller, a module-attribute value hand-off, a `cdf=` hand-off) | weaken the sentry | 4c |

Removed with the cuts: T4 and its workflow sentry, T6 (all three), T9 to T9e (memo), T10b, T15. Node ids moved: T12n from `test_cdf_builder_integer_only.py` to `test_cdf_exact_ratio.py`, and T12b to `test_cdf_no_bare_catch.py`, so that each lands red in its own slice. T10 is a new id: the build-count test replaces revision 4's T9 call counts.

### 5.1 Tests

- T1 Known-answer vectors. `tests/vectors/categorical_cdf_kat.json`, generated by the committed `tests/vectors/gen_categorical_cdf_kat.py`, which uses only `fractions.Fraction` and imports nothing from `decoy_engine`. Provenance goes in `tests/vectors/README.md`. Corpus: ties; zero weights (leading, trailing, all but one); tiny weights at, one ulp below and one ulp above `T / RES`; subnormals; 10, 1,000, 100,000 and 999,000 categories (digests for the large ones); vectors normalized to `1.0 +- 1 ulp` and `+- 3 ulp`; integers above `2**53`; `-0.0`; `1e308` pairs; `[0.6, 0.3, 0.1]`; `[0.1] * 10`; the 20,000-vector corpus as a digest. Non-vacuity: at least 50 vectors on which today's float builder, re-implemented inline, disagrees. Contract partner (class 10): the contract section 3.3 entry carries one `` `<fixture path>` sha256 `<64 hex>` `` line per golden fixture. `test_contract_entry_records_the_fixture_digests` matches exactly one such line per fixture by regex and compares it with `hashlib.sha256` of the file. The fixtures get `-text` in `.gitattributes`.
- T2 Property test (Hypothesis 6.155.7, in the lock). Draws finite non-negative vectors mixing `float`, `int`, `numpy.float64`, `numpy.float32`, `numpy.float16`, `numpy.int64` and ints above `2**53`. Checks: builder equals the `Fraction` oracle; P2; P3 in both directions; exact `2**k` scaling leaves the CDF unchanged.
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
- T5 Route parity with known-answer slot hits. `SELECTION_ROUTES` in `tests/parity/_categorical_routes.py` declares S1 to S9, each with its `src` site, its driver, its `(mask_key, namespace)` pair and the backend evidence it must show. The slot-hit fixture (generated by `tests/vectors/gen_categorical_cdf_slot_hits.py`, which records its search) holds, per key pair, source values whose bucket is 666666. That slot moves for `[1.0, 1.0, 1.0]` (today `[333333, 666666, 1000000]`, exact `[333333, 666667, 1000000]`). Cells: S1 unit key `(0x0123456789).to_bytes(8, "big")`, namespace `ns`, values `u1959551`, `u3243963`, `u3267463`. S2 through `run_seq` with `install_cat(monkeypatch, PythonReferenceIndexKernel())`, key pair `MASK_KEY` and `ns_cat`, values `u608104`, `u727084`, `u1948468`; evidence `{"table": "P", "column": "c_kat", "path": "scalar", "executed_backend": "rust_companion"}` and a spy recording `call.cdf == (333333, 666667, 1000000)` (baseline `["z", "z", "z", None]`, probed). S3 with `hide_cat` (`executed_backend == "python_scalar"`, `reason == "crypto_extension_unavailable"`). S4 to S9 use each harness's own key pair, searched by the generator. S8 runs at least five chunks, and its evidence includes that the handler used the run's `PrebuiltCdf` (slice 2 is in place when 4a lands). If S9 cannot take the Python reference kernel, it runs only under T14 locally, and the slice records which. Plus 30 KAT vectors over 2,000 values: each route's output must equal `categories[bisect_right(KAT[v], bucket)]` for its own bucket derivation, with a moved-slot hit guaranteed per key pair. No route is compared to another.
- T8 Relational known-answer keys: parent key `P.k` is a deterministic weighted categorical with weights `[1.0, 1.0, 1.0]`, and parent rows include slot-hitting values. Shapes `chain_shape` and `composite_shape` (`tests/parity/_d2b_shapes.py:118,253`) run through `run_seq` and `run_full`, and the out-of-core FK route through `tests/parity/test_out_of_core_fk_parity.py:448`. Assertions: 100% join; every slot-hitting key masks to `y` on parent and child (red on the baseline: `z`); two runs are byte-identical.
- T9 Lifetime (rule L1; the one lifetime test). Weak references cannot watch the CDF directly: tuples and tuple subclasses refuse them (probed: `TypeError: cannot create weak reference to 'TrackedCdf' object`). So T9 wraps `_build_cdf` in every importing module to return `TrackedCdf(tuple)` carrying a fresh `_Token` in its `__dict__`, and records `weakref.ref(token)`. Only that instance references the token, so the token dies exactly when the CDF does. For each run kind, T9 drives a full run through its public entry, drains or closes its stream, calls `gc.collect()` and asserts every recorded reference is dead. The handlers and prebuilds pass the builder's object through unchanged (1c removes the `tuple(...)` wrap in `prepare_categorical`, and slice 2 adds none), so the tracked instance is the one the run holds. Limit, stated: a holder that copies the tuple's contents (for example `list(cdf)`) is not seen by T9. Rule L1 forbids any such holder, and no code on the CDF path makes one. Revision 4's `tracemalloc` backstop was cut with the memo it policed.
- T10 Build count: a counting wrapper around `_build_cdf` in each importing module. Out of core: the same weighted column over 1 batch and over 20 batches (Python path, no kernel) gives one build in both. Chunked oracle: 1 chunk and 5 chunks give one build in both.
- T18 Error position (P9). Every invalid T3 row: (a) `prebuilt_cdfs` and `_python_cdf` return without raising; (b) a zero-batch out-of-core table completes as it does on the baseline; (c) a one-batch out-of-core run and a one-chunk chunked-oracle run raise the T3 code from `categorical_array` and the handler respectively, by traceback frame name; (d) a chunked run whose chunk 0 also fails a pre-adapter guard (the passthrough-FK guard, `_chunked_oracle.py:396-399`) still raises the guard's error. Case (d) is stated from the code and not probed.
- T19 Identity gate: a `StrategyContext` with `categorical_cdfs={"c": PrebuiltCdf(other_seed, other_cdf)}`. The handler, run with `plan=seed` (a distinct object with different weights), must emit its own KAT categories.
- T7 One builder: an AST sentry asserts that every `bisect_right`/`searchsorted` whose first argument is CDF-bound has no other binding in its function, that `_WEIGHTED_CDF_RES` is multiplied or divided only in `_categorical.py`, and that there are exactly three consumers.
- T11, T11b Messages: marker corpora (labels, source values, a `str` weight equal to the marker, an object whose `__repr__` and class name are the marker). No message contains the marker. T11b also commits each full route-layer message.
- T12 Integer-only builder. `BUILDER_FUNCTIONS` = `_build_cdf`, `_scaled_weights`, `_check_total`, `_check_resolution`, `_thresholds`, `_resolution_bound_text`, all in `_categorical.py`. The walk finds no `ast.Div`, no float `Constant`, no call to `float`, `sum`, `round`, `repr` or `math.*`, and every `FormattedValue` has `conversion == -1` and a spec in `{"d", "02d", "+03d"}`. The closure test asserts that the set reachable from `_build_cdf` equals `BUILDER_FUNCTIONS` plus `_exact_ratio` (which T12n covers). `prebuilt_cdfs` and the handler are outside the scope.
- T12b: no `except` clause in `_categorical_prepared.py`, `_categorical_native.py`, `_mask_group_b.py`, `_categorical.py` or `_operator_config_rejections.py` names `ValueError`, `OverflowError` or `TypeError`. The `except StrategyError` clauses slice 2 adds are allowed by name.
- T13 Annotations: `get_type_hints` of `SeqNativeRun.categorical_column["cdf"]`, `_build_cdf["return"]`, `CategoricalCall`, `PreparedCategorical` and, from slice 2, `PrebuiltCdf.cdf` resolve to `tuple[int, ...]` (optional where declared). The `PrebuiltCdf` assertion is added in slice 2.
- T14 Compiled kernel (local; section 5.5).
- T16 Route registry coverage. The section 3.2 scan runs over `src/` and is compared with `CDF_SITES` in `tests/parity/_categorical_routes.py`, keyed by (file, enclosing function, callee, occurrence). Equality is asserted in both directions, and every site names a declared E or S entry. When 4c lands the inventory is the 72 sites of section 3.2.
- T17: section 5.3.

### 5.2 Existing tests that change

Each change replaces an expected value with a stricter or corrected one. None is widened or skipped, and each needs the owner's sign-off (Q2, Q7, Q8).

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
- Slice 1r. Read `REGISTRY_VERSION` (`src/decoy_engine/reasons/_registry.py:24`, `1.1.0` on `a3fe49e`) on the `origin/main` the slice rebases onto, as `M.m.p`. Set it to `M.(m+1).0`. Declare both codes in `_part3.py` with that literal `since`, add them to `__all__`, and move `docs/reasons.md:21`, `tests/fixtures/f29/consumer-contract.json` (regenerated by running `tests/unit/progress/test_consumer_contract.py` as a script) and a `CHANGELOG.md` line in the same commit. Never edited: `reason-registry-release.json`, `reason-manifest-v1.json`, `reason-baseline.json`, `producer-inventory.json`.
- Coupling with A-4 (`claude/jolly-carson-3k5stf`, which bumps to `1.2.0`). Whichever branch merges second rebases, bumps the minor once more, and rewrites only its own `since` literals and partners. No test names a version. The slice gate records `origin/main`'s value and the branch value (check: branch equals main plus one minor, and both new codes' `since` equal the branch value).
- T17 asserts `Version(live) > Version(pin)` and `Version(pin) < Version(since) <= Version(live)` for both identities. The merge-time equality `since == live` is a gate check, not a permanent assertion, so a later legitimate bump does not turn it red.

### 5.4 Local gate (replaces the CI job)

GitHub Actions is off for this repo, so no workflow is edited and no workflow sentry is added. Each slice's evidence runs the slice's new and changed test files with these commands, from the lock, on all three supported interpreters, and attaches the JUnit counts:

```bash
for v in 3.10 3.11 3.12; do UV_PROJECT_ENVIRONMENT=.venv-$v uv sync --frozen --extra dev --python $v; done
```

```bash
for v in 3.10 3.11 3.12; do .venv-$v/bin/python -c 'import sys, numpy; print(sys.version, numpy.__version__)'; .venv-$v/bin/python -m pytest -p no:cacheprovider --junitxml=cdf-$v.xml $SLICE_TEST_FILES; done
```

```bash
for v in 3.10 3.11 3.12; do python3 -c 'import sys, xml.etree.ElementTree as E; s=E.parse(sys.argv[1]).getroot(); s=s if s.tag=="testsuite" else s[0]; t,f,e,k=(int(s.get(a,0)) for a in ("tests","failures","errors","skipped")); sys.exit(0 if t-f-e-k>0 and f==e==k==0 else 1)' cdf-$v.xml || echo "GATE FAIL $v"; done
```

The guard is the logic of `dp-certified-gate` (`.github/workflows/ci.yml:259-281`): fail unless passed > 0 and skipped, errors and failures are all 0. A slice whose files include an interpreter-specific red-first cell records that cell's red run on the interpreter where it is red (for example T1's `[0.6, 0.3, 0.1]` cell on 3.12).

### 5.5 Compiled-kernel test (T14, local)

`tests/native/test_cdf_native_present.py`. When `DECOY_REQUIRE_COMPILED_COMPANION=1`, `native_companion_status().ok` (`native/_companion_status.py:296`) must be true or the test fails. Without the flag it skips with the reason "compiled companion absent", which is counted. `test_the_compiled_kernel_buckets_the_slot_hit_values` loads the kernel through `load_compiled_index_kernel` (`native/_index_ext.py:157`) and asserts the raw buckets of the S1 values are 666666. `test_weighted_cdf_selection_on_the_compiled_kernel` runs `native_categorical` with `["x", "y", "z"]` and `_build_cdf([1.0, 1.0, 1.0])` and asserts `["y", "y", "y", None]`; it also drives S5 to S7 and S9. Probed with the companion built into the 3.10.20 lock venv: buckets `[666666, 666666, 666666, None]`, baseline output `["z", "z", "z", None]`, exact output `["y", "y", "y", None]`. Slice 4a evidence:

```bash
uv pip install --python .venv-3.10/bin/python ./decoy-engine-native
```

```bash
DECOY_REQUIRE_COMPILED_COMPANION=1 .venv-3.10/bin/python -m pytest tests/native/test_cdf_native_present.py -v --junitxml=cdf-native.xml
```

Then the section 5.4 guard on `cdf-native.xml`. In the default suite without the companion, T14 skips, and the skip is counted, not silent.

## 6. Class table (DEFECT-CATALOG section 3)

| # | Class | Structure that closes it here | Test | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | M2 builder messages (integer fields only); A1 reasons (code and column only); 1d route-layer messages; new holder types `repr=False` (finding B reported for the existing ones) | T3m, T11, T11b, T12 | `{w!r}` back; `f"{total / RES:.2e}"`; interpolate `categories[i]` |
| 2 | Cleanup on exit paths | The builder owns no resource. Every CDF built in a run is unreachable after the run returns (L1); the chunked holder is a generator local, released by `release_when_done` on every exit | T9 | a module list that keeps CDFs; T9's own seeded wrapper |
| 3 | State and concurrency | No cache, memo, key or shared state on the CDF path (L1). Reuse is by object identity. Tuples are immutable | T9, T19 | drop the identity check; key by seed value |
| 4 | Route parity and silent fallback | One builder (P4); admission reports the builder's code (A1, P8); construction never moves an error (P9); the full route inventory (section 3.2) | T3, T5, T7, T10, T16, T18 | reinstate a precheck; a module-alias caller; rebind one route's builder; let a prebuild raise |
| 5 | Referential integrity | Parent and child use the same pure builder in one process | T8 | build the child key with the float builder |
| 6 | Untrusted producer | N1 by exact type (C type codes); every rejection a registered `StrategyError`; `float(w)`, prechecks and bare `except` deleted | T2, T2b, T2c, T3, T3m, T12b, T12n | `isinstance` for exact type; `w.as_integer_ratio()`; reinstate `float(w)` or an `except (ValueError, OverflowError)` |
| 7 | TOCTOU | N/A: no path or file is involved | none | none |
| 8 | Weak tests and overclaimed evidence | Section 5.0 register; KAT from an independent oracle; per-route KAT cells with preconditions; a scanned inventory (finding A closed in the rule); local gate commands with JUnit guards, since no CI runs; probe numbers labelled with interpreter, numpy and commit | T1 non-vacuity, T5 preconditions, T7, T12 closure, T16 | regenerate the KAT from the implementation; route-agreement instead of KAT; a module-alias caller missing from the inventory |
| 9 | Behavior-preserving refactor drift | The change is intentionally not behavior-preserving; the characterization is the measured delta and the T3 matrix. Slice 2 is behavior-preserving and shows it on the existing suite (prototype: same results on 15 files) | T3, T1 digests, T13, T18 | swap two P6 passes; `:364` back to `list[int]` |
| 10 | Frozen contract drift | Contract entry records fixture digests; registry bump with literal `since` and partners in the same commit; released pin untouched; module-size census moves with its file | T17, `test_live_registry_honours_the_released_pin`, `test_reasons_doc_states_the_live_registry_version`, `test_the_consumer_contract_fixture_is_current`, `test_module_size.py`, `test_contract_entry_records_the_fixture_digests` | a code without `since`; skip the bump; regenerate a fixture without the contract line |

## 7. Reuse inventory

- `_build_cdf` stays the single owner (`_categorical.py:69`). `prebuilt_cdfs` and `PrebuiltCdf` live beside it (244 lines today, about 410 after 1a and 2).
- Run-scoped threading precedent: `code_set_records` (`_adapter.py:210`, `:354`; `_pandas_adapter.py:176`, `:251`; `_chunked_oracle.py:439`), copied for `categorical_cdfs`. Per-table plan precedent: `TableCategoricalPlan` and `TableHashPlan.categorical_for` (`_hash_run.py:164-166`) for `categorical_cdf`.
- Reasons registry and release pin: `reasons/runtime/_part3.py`, `reasons/_registry.py:24`, `scripts/release/reason_release_pin.py`, the consumer-contract regenerator.
- `numpy.dtype(c).type` for N1; `float.as_integer_ratio`; `fractions`, `weakref`, `numbers`, `hashlib`, `gc` (stdlib); Hypothesis (in the lock).
- Harnesses: `tests/parity/_d2c_cat_data.py`, `tests/parity/_d1b2_categorical_data.py`, `tests/parity/_d2b_shapes.py`, `tests/parity/test_out_of_core_fk_parity.py:448`.
- JUnit guard logic: `dp-certified-gate` (`.github/workflows/ci.yml:259-281`), reproduced locally in section 5.4.
- Methodology citation: `docs/methodology-registry.yaml` entry for `_categorical.py` (`tests/sentry/test_methodology_citations.py`).
- Module size: `_pandas_adapter.py` census at 689 (`tests/sentry/test_module_size.py:107`); slice 2 adds 2 lines. `_sequential_native.py` is at 697 with no census entry needed below 700; 1a changes one annotation in place. Every other touched module stays below 600 (largest: `_hash_run.py` 546 to about 549).

## 8. Frozen contracts touched

- `docs/compatibility-contract.md` section 3.3: a dated "Current state" entry, in the style of `:307-313` (`a3fe49e`), records what moved and the sha256 of each golden fixture (slices 1b and 4a). A standing sentence states that weighted categorical output and its error text are identical on every supported CPython.
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

Decided, no longer questions: Q4 (CI matrix scope) has no subject, because no CI job is added and the local gate is section 5.4. Memo ownership is decided as no memo (rule L1).

## 10. Slices

Bound: each slice touches at most 10 production files and at most 800 changed lines. Changed lines count production, test and doc lines. Generated data files (the KAT and slot-hit JSON, the regenerated `consumer-contract.json`) are excluded and named. Line figures are estimates from revision 4's test designs and, for slice 2, the measured prototype; each slice's evidence records the actual diffstat.

| Slice | Production files | Est. changed lines | Content | Gate |
|---|---|---|---|---|
| 1r | 2: `reasons/_registry.py`, `reasons/runtime/_part3.py` | about 90 (+ regenerated `consumer-contract.json`, about 27 `registry_version` lines) | T17 red first; minor bump relative to `origin/main` at build time; two codes with literal `since`; `docs/reasons.md:21`; CHANGELOG; gate check recorded | Codex (R2) |
| 1a | 2: `_strategies/_categorical.py`, `_sequential_native.py` (annotation only) | about 760 (production about 220; tests and generator about 540) (+ KAT JSON) | T1 (four), T2 (three), T3m (two), T11, T12n, T13 red first; N1, P2, P2a, P3, P6, M1, M2 in the builder, returning a tuple; KAT generator and provenance; 5.2 items 1 and 2 | Codex (R2) |
| 1e | 0 (adds `tests/perf/_float_cdf_reference.py`, a verbatim measurement copy of today's builder) | about 260 | T12 (three, with seeded fixture) recorded red against the 1a parent; T10a with its mutation run | argon |
| 1c | 4: `_strategies/_categorical.py`, `out_of_core/_mask_group_b.py`, `native/_categorical_prepared.py`, `out_of_core/_categorical_native.py` | about 600 | T2b, T2c, T3, T12b red first; three `float(w)` deletions; rule A1; two `except` deletions; `tuple(...)` wrap removed; `unbuildable_cdf` deleted; docstrings; `ERROR_ROUTES` E1 to E10; 5.2 items 3 to 5; 47-file re-run | Codex (R2) |
| 1d | 2: `_strategies/_categorical.py`, `out_of_core/_mask_group_b.py` | about 90 | T11b red first; route-layer messages per M2 | argon |
| 1b | 0 (docs: contract, `docs/strategies.md`, `docs/methodology-registry.yaml`, CHANGELOG, `.gitattributes`) | about 110 | `test_contract_entry_records_the_fixture_digests[kat]` red first; contract entry with the KAT sha256 line | argon |
| 2 | 8: `_strategies/_categorical.py`, `_chunked_oracle.py`, `_adapter.py`, `_pandas_adapter.py`, `out_of_core/_categorical_native.py`, `out_of_core/_hash_run.py`, `out_of_core/_mask.py`, `out_of_core/_mask_group_b.py` | about 520 (production about 85, measured 80 in the prototype; tests about 420; census 1) | T10 red first; T9, T18, T19 non-red gates with their mutations and the seeded retention run recorded; rule L1 construction; `repr=False` holders; T13 `PrebuiltCdf` assertion; module-size census; 47-file re-run with no change | Codex (R2) |
| 4a | 0 | about 680 (+ slot-hit JSON) | `SELECTION_ROUTES`; T5 (four tests) and the slot-hit generator with the moved-slot search; T8; T14 (two) with the local compiled-kernel run; the slot-hit sha256 line and its digest parameter | argon |
| 4c | 0 | about 400 | `CDF_SITES` (72); T16 (three, with seeded fixture), using the section 3.2 scan; T7 (two, with seeded fixture) | Codex (R2) |

Nine slices instead of eleven. The 1a and 4a splits are forced by the size bound; the content is revision 4's minus the cuts. 1c's gate is Codex because it carries the route-parity change. 2 and 4c are gated by Codex because they carry the two mechanisms that replaced recurring classes (L1, the scan).

Order: 1r, 1a, 1e, 1c, 1d, 1b, 2, 4a, 4c.

- 1r first, because 1a raises the two codes.
- 1e after 1a, so its sentries land green and are recorded red against the 1a parent.
- 1c after 1a: once the builder raises only `StrategyError`, the `except` deletions are safe.
- 2 after 1a and 1c. Before 1a, a prebuild can let a bare error escape (finding C, probed). Before 1c, the route entries still call `float(w)` while the prebuild passes raw weights, so ints above `2**53` would get a different tuple on the chunked oracle than in memory.
- 4a after 2, so S8's cell exercises the run-built tuple.
- 4c last, because its inventory is the post-slice-2 tree.

Finding to slice map: section 0.2.

## 11. Pre-mortem (ways a gate reviewer breaks it, each with its red-first test)

1. Common denominator built from the smallest power of two: T1 subnormal rows, T2.
2. A leftover float operation or `repr` keeps a message or threshold interpreter-dependent: T12, T3m, the 3.12 cell of the local gate.
3. One route builds its own CDF, or still builds the float list: T5, T7, T16.
4. A route ignores the run-built tuple and rebuilds per batch or chunk (999,000 categories times batches): T10.
5. NaN or infinity escapes as a bare error on any entry: T3 E1 to E10.
6. Error precedence depends on the total, or admission decides before the builder: T3 rows `[nan, -1]`, `[-inf, 1.0]`, `[1e16, 1.0, -1e16]`.
7. `-0.0`, bools, ints above `2**53` and numpy scalars convert differently on one route: T2, T2b, T3 type rows, T5.
8. A `from_profile` long-tail column now fails where it sometimes passed: T1 share-below-one-slot row; Q2.
9. The KAT is generated by the implementation: T1 independence and non-vacuity.
10. The local gate runs one interpreter three times, or hides skips: the version line printed per venv and the JUnit guard (section 5.4).
11. A prebuilt tuple for one config is served to another: T19 (identity, not value).
12. Building ahead of the batch moves an error earlier, or makes an empty table fail: T18.
13. A long-lived worker keeps CDFs from earlier runs: T9.
14. The worker regenerates the released F29 fixtures, or adds codes without a bump or with the default `since`: T17, `test_live_registry_honours_the_released_pin`, `test_the_baseline_fixture_is_frozen`. The bump itself is required.
15. A `Fraction` or `longdouble` weight is scaled as if dyadic: T3 rows, N1.
16. A new caller reaches the builder through a module alias or a value hand-off: T16 (finding A's forms are in its seeded fixture).
17. A-4 merges between this plan's rebase and its merge: the merge-order rule (5.3); the gate check compares with `origin/main` at that moment.
18. A lying `float` subclass or a registered `Integral` slips through: T2c, T12n.
19. A route agrees with its siblings but not with the KAT: T5 per-route KAT assertion and moved-slot precondition.
20. A fixture recapture lands without a contract edit: the contract digest test.
21. Slice 2 lands before 1a or 1c: the order rule; T18 goes red (probed for the 1a case).
22. The compiled kernel is never exercised: T14 run locally with the flag (5.5); the evidence attaches its JUnit counts.

## 12. Worker evidence rules (DEFECT-CATALOG section 5)

- Name the class of every finding and fix the class, not the instance. If a catalogued class recurs, stop and report it with a class-level proposal; do not patch a third time.
- Write the tests first and record them red, with the interpreter, numpy version, commit and the file:line named in section 5.0. For 1e, record the red run against the 1a parent.
- Before pushing, run each mutation in section 5.0 once and show the test go red. For non-red gates, that run is the only failure evidence. Never commit while a mutation is applied.
- Self-attack the diff with value-bearing canaries in ordinary shapes (labels, source values and weight objects carrying a marker) through every message and repr the slice adds.
- Run baselines from `uv.lock` in the same environment as the branch, and state every suite not run.
- Run the section 5.4 local gate on 3.10, 3.11 and 3.12 and attach the JUnit counts. No CI job stands in for it.
- Never widen a threshold or allowlist beyond what is stated, skip a test, weaken an assertion, or edit a pinned test or golden except as named in 5.2.

## 13. What this plan did not run or verify

- Revision 5 ran, in one container, on a fresh lock venv (`uv sync --frozen --extra dev --python 3.12`: CPython 3.12.3, numpy 2.5.0): the section 3.2 scan over `a3fe49e` and over a scratch slice 2 prototype (scratch `t16_scan_rev5.py`, run with the container's Python 3.13); the prototype on 15 categorical test files (603 tests) with a `_build_cdf` call counter, and the same files on `a3fe49e`; the lifetime mechanism (token weak references, 38 nodes, 0 retained) and its seeded module-list violation (9 nodes retained); the weak-reference refusal of tuple subclasses; the finding C ordering hazard. 3.10 and 3.11 were not run in revision 5.
- The prototype is not the slice. It used today's builder with a broadened `except` standing in for slice 1a, and the probes removed the `tuple(...)` wraps. Its line counts are evidence for the slice 2 estimate only.
- T18 case (d) (guard error before an invalid weight vector) and the E10 cells are from the code, not probed. T10's per-run counts were measured per test node, not per run, on existing tests; T10 itself is not written.
- Whether any path renders the default dataclass `repr` of `PreparedCategorical` or `CategoricalColumnDecision` (finding B) was not checked.
- Earlier probes stand as recorded in revisions 2 to 4: the T3 matrix on 3.10.20 and 3.12.3 at `545d913`; the N1 type facts; the registry-pin cases against `a3fe49e`; E9 over twelve rows at `c7a4f18`; the compiled-kernel slot hits on 3.10.20; the 20,000-vector corpus and performance figures (revision 2 scratch prototype, 3.12, one container).
- The engine suite outside the 47 files; the full suite on 3.11 under the exact builder; Postgres, `ml`, `dp_certified`. Whether S9 can take the Python reference kernel. The Rust sources of `decoy-engine-native` (read by grep, built once).
- No CI can be observed: GitHub Actions is off for this repo.
- Literature citations (Witten, Neal, Cleary 1987; Duda 2013; IEEE 754-2019) and the CPython issue number are from memory and were not fetched.
- `plan-lint` does not exist in this repo or container (not on `PATH`; no `plan-lint*` file found). The plan contract was checked by hand: `Rules consulted:` within the first 30 lines; every section 5.0 row has a node id, a red-first location or non-red label, and a mutation; every slice is stated within 10 production files and 800 lines; no em-dash (checked with `grep -c` for U+2014).

## 14. Architecture view

Is the remaining scope the smallest design that closes the defect? Yes, with the cuts accepted. The defect is P1, and closing it needs the exact builder (1a), the registry codes it raises (1r), and the route layer that hands it raw weights and reports its codes (1c), because a builder change that one route does not see is not a fix (P4, P8). The rest is the cost of proving that across ten entries and nine routes (T3, T5, T16), the cost of the exact builder (slice 2, which pays back the 5x by building once per run instead of once per batch, with no cache), and three small items the earlier rounds found in the same code (1d messages, 1b contract digests, 1e sentries). No slice exists only to police a mechanism this plan introduced.

What I would still cut, in order:

1. Slice 1d into its own plan. It is a real class 1 defect in the route-layer messages but shares no code path with P1. It stays here only because the handoff keeps it. Folding it into 1c (both edit the same two route functions) is the cheaper alternative; it would keep 1c within the bound at about 690 lines.
2. T14 until Actions is on. It runs only locally, and the S5 to S7 cells of T5 already pin the CDF the kernel receives. It keeps the one check that the compiled kernel and the Python reference agree on a moved slot, which is why it is not cut here.
3. T10a. The construction in slice 2 removes the per-batch multiplier, and T10 proves it. T10a only bounds the one-off build at 999,000 categories.
4. T16 in its current form, if the gate holds the stop rule on finding A. The class-level alternative is a closed-world check that needs no caller closure. Assert that `_build_cdf` is referenced (as a name, an attribute or a value) only at the sites T16 lists as direct (E2, E3, E4, `prebuilt_cdfs`, `_python_cdf`). That set is complete because the name is private and the check matches every reference form. Then prove each route behaviorally with T3 and T5, as this plan already does. That removes the open-ended closure that produced the class 8/4 recurrences. It also loses the inventory's view of which drivers carry a verdict onward, which only T3 and T5 would then cover.

Kept and why: rule A1 and T3 over E1 to E10 (the route-dependent precedence is itself a defect the exact builder exposes); slice 2 (without it the exact builder makes long out-of-core and chunked runs slower; with it they are faster than today); the contract digest test (cheap, permanently closes a class 10 gap).

## 15. Out of scope (backlog entries)

- Sampler weight totals (revision 4 slice 3; its own defect, its own plan). `generation/_window_admission.py:129-130,151-157,211-217,224-228` admits year bins and top values with `isfinite(sum(weights))` and `> 0`. The consumers `generation/statistical/_sample.py:268,296,336,366` guard with `sum(...) <= 0` and then draw through `random.choices`, which totals with `itertools.accumulate`. On 3.12, `sum` is compensated, so a near-overflow vector such as `[M, 0.6h, 0.6h]` (`M` the largest double, `h` half its ulp) totals to `inf` in the admission guard while `random.choices` totals it to the finite `M`. The guard and the consumer disagree on 3.12 only (derived from the probed `sum` values and the code, not run end to end). The fix is one left-fold total helper shared by the guard and the consumer (P5, guard equals consumer), with an AST sentry against builtin `sum` over weights at guard sites. Its own plan should state a red-first cell on 3.12 and the local gate in section 5.4.
- Finding B: default dataclass reprs of `PreparedCategorical` and `CategoricalColumnDecision` render category values (class 1). Its own S plan: decide `repr=False` per field, and add a marker test over every repr reachable from an error or log path.
- Report-plane float sums (Q5) and the NumPy-per-interpreter draw (Q6).
