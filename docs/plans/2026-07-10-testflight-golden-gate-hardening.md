# Program: Test-Flight Golden-Gate Hardening (TH-1 .. TH-4)

Source of goal: adversarial review of the acceptance test-flight suite by an independent
Fable-model reviewer, 2026-07-10. The suite (`docs/acceptance-test-flight.md`,
`scripts/test_flight.py`, `testflight/`) is the team's designated "does the engine actually
work" ship/freeze gate. The review's verdict: **medium-high trust — it deserves to be a ship
gate, but not yet the "green means correct by definition" authority we treat it as**, because
of three false-confidence holes plus a set of real coverage gaps.

This is a **program** (a set of sprints), authored for Cam sign-off in the same shape as
`docs/plans/2026-07-09-consultant-f1-f2-bounded-profiling.md`. Nothing here is built yet.
Task-ID prefix is **TH** (Test-flight Hardening). Build tiers are assigned per the
model-tiering directive.

Related: wiki `[[decoy-golden-gate-testflight]]`; `docs/acceptance-test-flight.md` (the plan
this hardens); `CONTRIBUTING.md` ("Pre-merge gate for large engine blocks").

## The one-paragraph problem

A review agent that is told to find problems has a ~100% hit rate, so agent output cannot be
the completion signal for Decoy — the test-flight is meant to be that signal instead. But the
test-flight only earns that role if a green run genuinely means "the engine is correct," not
"the engine did last week's thing on three fixed shapes." The Fable review found three ways a
**broken engine ships green today** (false confidence, P0), plus real coverage gaps where a
plausible production failure is simply never exercised (P1) and a set of smaller hardening and
doc-truth items (P2). This program closes them in priority order so the gate earns the
authority it currently holds on trust.

## Verification status of the findings (what is confirmed vs asserted)

Confirmed directly against the code on branch `bench/fk-probe-heartbeat` (engine `main` +
bench heartbeat) while authoring this plan:

- **P0-2 confirmed.** The live validator registry `_REGISTRY`
  (`src/decoy_engine/validators/_registry.py:63-75`) has **11** validators; the coverage
  guard's `_ALL_VALIDATORS` (`testflight/_coverage.py:155-157`) hardcodes **6**. The five
  invisible to the guard are `leak_check`, `regex_match`, `column_in_set`,
  `parent_window_respected`, `reconciliation_holds`. `_ALL_CHECKSUM_SCHEMES`
  (`_coverage.py:150`) and `_ALL_GENERATE_TYPES` (`_coverage.py:136`) are static snapshots too.
- **P0-3 confirmed.** `testflight/_computed.py:106` imports `compile_expr, evaluate` from
  `decoy_engine.expressions._lark_parser` — the same evaluator the engine's `derived` strategy
  uses. Row-wise recomputation is therefore circular; only the aggregate path is independent.
- **P0-1 tooth exists, wiring is the gap.** `check_value_changing_not_passthrough`
  (`testflight/_fk_remap.py:85-140`) is real and its docstring describes the exact FPE-charset
  no-op failure mode. It is only invoked for the Job B correlation pair and the Job C orphan
  remap; it is not run for value-changing columns generally.

Asserted by the review, to be **reproduced as the first (red) step** of the relevant task
before any fix (do not fix on the review's word alone):

- The `members.mrn` live no-op: fixture builds uppercase MRNs
  (`jobs/a_healthcare_claims/fixture.py` ~line 274) masked with `charset: alphanum`
  (lowercase+digits; `manifest.yaml` ~line 126) → uppercase chars emitted verbatim. TH-1.1
  starts by running the tooth against `mrn` and watching it fail.
- Cross-process determinism blind spot, safe-harbor length-5-only scan, and the P1/P2 details:
  each task's first step is a failing check that demonstrates the gap.

## Program principles (apply to every task)

1. **Teeth-first (TDD for a test suite).** For every hole, add or enable a mutation-control
   "tooth" that is **red against the current gate** first, then change the gate until it is
   green. A fix with no tooth proving it bites is not done. This mirrors the existing
   `testflight/test_testflight_teeth.py` discipline, which the review praised as the suite's
   best feature.
2. **The gate stays green.** `python scripts/test_flight.py` must PASS at the end of every
   task (30/30 today). A task that legitimately turns a hidden failure into a visible one
   (e.g. the mrn no-op) fixes the underlying config in the same task so the suite ends green.
3. **Do not loosen a tolerance to pass.** Per the suite's own rule: fix the root cause; a
   tolerance change needs a recorded reason in the manifest and a PR comment.
4. **Docs match code.** Any claim in `docs/acceptance-test-flight.md` that a task proves false
   is corrected in that same task.
5. **Review gate.** Each sprint ends with a dennis adversarial review before merge; the suite
   is a safety gate, so rigor matches stakes.

---

## Sprint TH-1 — Close the three false-confidence P0s (gate-blocking)

**Goal:** a green run stops being able to hide a broken mask, a rotted coverage guard, or a
self-graded computed column. Until this sprint lands, treat test-flight green as "necessary,
not sufficient." **Build tier: Opus** (subtle correctness work on the safety gate itself;
P0-1 and P0-3 change what the gate can prove).

### TH-1.1 — Wire the value-changing-passthrough tooth for every value-changing column (P0-1)

- **Red first:** run `check_value_changing_not_passthrough` against `members.mrn` in Job A and
  confirm it fails (reproduces the uppercase/alphanum no-op). This both proves the tooth and
  confirms the live bug.
- In `check_distribution_mask` (evaluation path, `testflight/_evaluate.py` / `_distribution.py`),
  invoke the tooth for **every** spec column whose strategy is in `_VALUE_CHANGING_STRATEGIES`
  (fpe, hash, code_set), not only the correlation pair and orphan remap.
- Fix the `members.mrn` root cause: set `charset: ALPHANUM` (uppercase-inclusive) in
  `jobs/a_healthcare_claims/manifest.yaml`, OR, if uppercase-verbatim is deliberate, document
  it explicitly and exclude with a recorded reason. Default: fix the charset.
- **Declare `leak_check` in at least one job.** It is the engine's purpose-built "forgot to
  mask" net (`src/decoy_engine/validators/_leak_check.py`) and no test-flight job uses it. This
  is the strongest structural guard against P0-1's whole class and also pre-satisfies part of
  TH-1.2's validator coverage.
- **Acceptance:** the tooth runs for all value-changing columns across all three jobs; a
  planted verbatim-passthrough on any of them turns the suite red; `mrn` is masked; a
  `leak_check` validator is declared and green; `scripts/test_flight.py` PASS.

### TH-1.2 — Derive coverage-guard axes from live registries; resolve the 5 invisible validators (P0-2)

- Replace the static `_ALL_VALIDATORS`, `_ALL_CHECKSUM_SCHEMES`, and `_ALL_GENERATE_TYPES`
  frozensets in `testflight/_coverage.py` with reads of the live registries, exactly as the
  scalar axis already reads `SCALAR_HANDLERS`:
  - validators from `decoy_engine.validators._registry._REGISTRY`
  - checksum schemes from the live `decoy_engine.checksums` scheme registry
  - generate types from the live `generation/synthesize.py` dispatch table
- The five now-visible validators (`leak_check`, `regex_match`, `column_in_set`,
  `parent_window_respected`, `reconciliation_holds`) each get resolved: **exercised by a job**
  (preferred for `leak_check` — done in TH-1.1 — and for `reconciliation_holds` /
  `parent_window_respected`, which are relationship checks a multi-table job can drive) **or**
  allowlisted with a specific reason.
- **Red first:** a tooth that registers a fake validator in the live registry and asserts the
  suite-level guard now fails (mirrors the existing scalar `test_testflight_teeth.py:873-923`
  fake-handler tooth). Add equivalent teeth for the checksum and generate-type axes.
- **Acceptance:** adding a validator/checksum/generate-type to a live registry without a job or
  allowlist entry fails the guard; the guard summary reports the true live counts;
  `scripts/test_flight.py` PASS.

### TH-1.3 — Independent recomputation for row-wise computed columns (P0-3)

- In `testflight/_computed.py`, recompute row-wise (non-aggregate) formulas with **plain
  Python/pandas operators in the harness** rather than the engine's `compile_expr`/`evaluate`.
  The three jobs' formulas are simple (multiply, string-equality case_when); implement a small
  independent evaluator for exactly those forms. Keep the grammar compile as a **secondary**
  smoke check, not the source of truth.
- **Red first:** temporarily inject a wrong result into the engine's shared evaluator path (or
  a stub) and confirm the harness now catches it where before both sides agreed.
- Correct `docs/acceptance-test-flight.md:139-141` ("recomputed in pure Python") to state that
  only aggregates were independent before this change, and that row-wise is now independent.
- **Acceptance:** a bug in the shared `_lark_parser` evaluator would make a computed-column
  invariant red; doc corrected; `scripts/test_flight.py` PASS.

**Sprint TH-1 done when:** all three P0 teeth bite, the suite is green, docs corrected, dennis
review clean. **At this point the gate earns "green = correct" for the covered surface** and
the wiki note / memory can drop the "trust on authority" caveat.

---

## Sprint TH-2 — Teeth for untested families and determinism integrity (P1)

**Goal:** every invariant family has a tooth proving it bites, and the determinism claim is
honest about what it can and cannot see. **Build tier: Sonnet** (follows the established teeth
pattern; TH-2.2 nightly wiring is the one novel piece — Opus if it touches CI logic).

### TH-2.1 — Mutation teeth for determinism, checksums, safe_harbor (P1-4)
- Add red/green teeth in `test_testflight_teeth.py` for the three families that currently have
  none: corrupt a check digit (checksums), diff a mutated second run (determinism), strip a
  cascade decision / plant a surviving restricted prefix (safe_harbor).
- **Acceptance:** each new tooth is red before the family's check runs and green after.

### TH-2.2 — Cross-process determinism fingerprint + schema compare (P1-5)
- The two runs share one process (`_runner.py:177-179`), so hash-seed / set-iteration
  nondeterminism is invisible and reappears only across CI runs. Add: (a) the nightly workflow
  hashes each job's output tables and compares to the previous nightly artifact (or a committed
  golden hash per job, updated deliberately); (b) compare arrow **schemas** between the two
  in-process runs, not just `to_pydict()` values; (c) compare the full `quality_metrics` block,
  not only `fidelity_reports`.
- Correct the doc's "byte-identical" wording (`acceptance-test-flight.md:85-87`) — the check is
  value-equality of pydicts, not bytes.
- **Acceptance:** a `PYTHONHASHSEED`-sensitive ordering bug is catchable via the nightly
  fingerprint; schema drift between runs is red; doc corrected.

### TH-2.3 — Safe Harbor: prefix-at-any-length + independent count (P1-6)
- Replace the length-5-only scan (`_invariants.py:240`) with a check that no output value
  **starts with** a restricted prefix at any length ≥ 3 (catches zip3 `"036"` and zip+4
  `"03601-1234"`). Cross-check the suppression count against the actual suppressed values in the
  output column, not only the engine's self-reported `cascade_decisions` detail.
- **Acceptance:** a restricted prefix emitted at zip3 or zip+4 turns the suite red.

### TH-2.4 — Independent checksum validation (P1-4 cont.)
- Validate luhn/npi output with `stdnum` in the harness (already a dep; used only in fixture
  generation today) instead of the engine's own `decoy_engine.checksums.validate`, so the
  engine is not grading its own homework.
- **Acceptance:** engine and harness use different validators for the same output; a
  check-digit regression is caught independently.

---

## Sprint TH-3 — Coverage breadth: config-derived axis + new jobs (P1)

**Goal:** exercise the strategy families, data shapes, and correlation patterns a green run
currently never touches. **Build tier: Sonnet** for the config-derived axis and edge-case job;
**Opus** for the group-aware / statistical Job D (novel composition inside `run_pipeline`).

### TH-3.1 — Config-derived scalar strategy coverage (P1-7)
- The scalar axis trusts the hand-maintained `strategy_coverage` manifest list rather than
  scanning actual column strategies in `m.config` (unlike the generate-type/validator axes).
  Derive the covered set from config columns' `strategy` fields; keep the manifest list only as
  an assertion target (`list == config-derived set`).
- **Acceptance:** deleting a strategy's only column from a config while leaving it in the
  manifest list fails the guard.

### TH-3.2 — Job D: group-aware family + statistical generation (P1-8)
- New job exercising `group_key` / `grouped_series` / `windowed_date` (longitudinal/time-series,
  currently allowlisted-out and only unit-tested) and the `statistical` generate type (the
  SDV-style parameterized path, a shipped flagship capability) **composed inside `run_pipeline`**
  with relationships, quarantine, and quality reporting — which unit tests do not prove.
- Remove those items from the allowlist once the job covers them.
- **Acceptance:** Job D green; coverage guard no longer allowlists the group-aware trio or
  `statistical`.

### TH-3.3 — Job E: hostile / edge-case data (P1-10)
- New job with: unicode names/notes through fpe/text_mask/text_redact, an all-null column, a
  single-row table, duplicate parent-FK values, and (if the topology supports it) an empty
  table. These shapes appear nowhere in the current fixtures (verified: zero non-ASCII).
- **Acceptance:** Job E green; a regression on unicode PII masking or empty/degenerate handling
  is now catchable by the gate.

### TH-3.4 — Correlation-through-masking on real pairs (P1-9)
- The Cramérs V invariant runs on exactly one fpe-fpe pair; Job A's declared joint is
  passthrough-passthrough (trivially preserved, tests the metric not the mask). Add a hash-hash
  pair and, with a corpus ≥ source cardinality, a code_set pair at chapter granularity; replace
  or supplement Job A's trivial pair with a value-changing one.
- **Acceptance:** a mask that destroys a real correlation (independent post-mask shuffle) turns
  the suite red on more than one strategy family.

---

## Sprint TH-4 — P2 hardening and doc truth

**Goal:** close the smaller silent-skip and doc-drift items. **Build tier: Sonnet**, docs via
barry.

- **TH-4.1** Degenerate Cramérs V reports PASS today (`_evaluate.py:364-371`,
  `_correlation.py:187-189`) — make it a distinct SKIP status or a failure, so a future pair on
  a synthetic/coarsen column cannot silently vanish.
- **TH-4.2** Silent `None`-guards on shape floors (`_distribution.py:324,347,359,388,510`) pass
  when the shape/similarity entry is absent — fail on a missing entry for a declared fpe/hash
  column, so the floor cannot evaporate if `compute_quality_report` stops emitting it.
- **TH-4.3** Assert generate-table row counts against `TableSpec.row_count` (currently checked
  against nothing; a 0-row generate passes only incidentally).
- **TH-4.4** Doc-truth bundle (barry): fix "byte-identical" (if not already in TH-2.2), the
  case_when branch-coverage claim vs Job A's `branch_count: 0` (`acceptance-test-flight.md:141-143`
  / `manifest.yaml:505`), and the "What it proves" checksum list reading as six schemes when two
  are exercised.
- **Acceptance:** no invariant silently skips without an explicit SKIP status; docs match code.

---

## Out of scope for this program (GA "must also be green", tracked elsewhere)

The suite is structurally pandas-only and does not certify privacy/compliance or scale. These
are **separate green lights** a solo founder needs before GA, not test-flight tasks — recorded
here so they are not mistaken for covered:

1. Substrate parity (`tests/parity/` + `engine-v2-substrate-matrix` green) — polars ships
   untested by this suite.
2. Scale/memory: actually run the GCP `fk_memory_probe` battery and the out-of-core path at
   50M rows (built, per the SC0-SC6 + SC7 programs; run it).
3. Privacy posture: DP parameter review, vault/key-handling security review, product claims
   aligned to `docs/what-we-cannot-prove.md`.
4. One genuinely messy real-world dataset through the full pipeline (TH-3.3 approximates,
   does not replace).
5. Property-based FPE/checksum tests (hypothesis is already a dev dep): bijectivity and
   check-digit recompute across all charsets.

Also structural, not a code task: the gate is manual/nightly and not a required status check
(`.github/workflows/testflight.yml:6-8`, deliberate per ADR-0005), so the freeze signal depends
on a human running it and reading the evidence report — and `--job` single-job runs skip the
coverage guard (`scripts/test_flight.py:88-97`). Keep the merge checklist habit.

## Definition of done (whole program)

- [ ] Every P0/P1 invariant family has a mutation-control tooth that is red before its check
      and green after.
- [ ] The coverage guard derives **all** axes (strategies, validators, checksums, generate
      types) from live registries; nothing load-bearing sits in an allowlist without a job.
- [ ] `python scripts/test_flight.py` PASS after every task (target: 30/30 → higher as jobs D/E
      land).
- [ ] `docs/acceptance-test-flight.md` contains no claim the code does not back.
- [ ] Each sprint passed a dennis adversarial review before merge.
- [ ] The wiki note `[[decoy-golden-gate-testflight]]` and the auto-memory drop the
      "trust on authority" caveat once TH-1 lands.

## Sequencing

TH-1 first and alone gates the "green = correct" claim — do it before leaning on the gate for
any freeze decision. TH-2 and TH-3 are independent of each other and can run in either order or
in parallel worktrees. TH-4 last (depends on nothing but is lowest-value). Do **not** flip
`decoy_engine.RELEASE_PHASE` to `"ga"` (which makes the compatibility contract binding) until at
least TH-1 and the out-of-scope items 1-2 are green.
