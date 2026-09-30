---
Status: plan
---

# Track A: Rust fast-path input-format coverage (CSV + fixed-width)

> Part of the "Rust is the default for all real jobs" program (decoy-platform ROADMAP top
> priority, 2026-09-29). This is Track A, the smallest, highest-leverage slice. Author: Opus.
> Codex plan-gate: PENDING (must pass before any build).

## End goal (the acceptance signal, not "code merged")

A real single-table mask job whose input is CSV or fixed-width runs on the Rust unified-slice
lane, not pandas, producing byte-identical output to the pandas oracle. Concretely, DONE when:

1. A CSV single-table mask job AND a fixed-width single-table mask job, using native
   strategies on currently-admitted resident types, are ADMITTED to the unified-slice lane and
   their output is byte-for-byte identical to the pandas full-frame route.
2. Those jobs' evidence shows `compiled_kernel_executed = True`.
3. Existing parquet parity and every current decline are unchanged (no regression).
4. Activated in production behind Cam's gate.

## Frame (the flaw)

Decoy's native input formats are CSV, fixed-width, and parquet. Today the unified-slice
admission gate (`cheap_admission`, `src/decoy_engine/execution/_unified_slice_admission.py`)
admits ONLY a parquet file source (the `format == "parquet"` check, ~L262-274); CSV and
fixed-width decline to the pandas oracle. Real customer inputs are predominantly CSV and
fixed-width, so the common single-table mask job silently runs pandas, not Rust. Per Cam, a
real mask job that can only run on pandas is a flaw.

Enabling fact (why widening is feasible): the platform reads ALL source formats into a resident
Arrow table before masking (`decoy-platform api/jobs/v2_runner.py::_read_sources_as_arrow`), and
BOTH the unified-slice (Rust) route and the pandas full-frame route mask that SAME resident Arrow
table (`run_pipeline` passes `sources`/`caller_sources`; neither route re-reads the file per
format). The input format does not change the data the lane masks. The gate's parquet-only
restriction is a conservative scope ("declines to the unchanged old route" for loosely-typed
readers), not a functional necessity.

Parity crux (the one thing to prove): the engine's profile step (`profile_source`) builds the
compiled plan from the config's declared source. For a loosely-typed CSV/fixed-width source, the
profile-derived column types must AGREE with the resident Arrow table's actual types, or the
compiled Rust plan could mask a different logical type than the pandas route masks. FIRST
open question the build must resolve: does `profile_source` profile the passed-in resident Arrow
table, or re-read the source file from the descriptor path? If it profiles the resident Arrow,
types agree by construction and Track A is small; if it re-reads the file, we must add an
explicit profile-vs-resident-Arrow type-agreement guard (decline, fail-closed, on mismatch).

## The change

1. `cheap_admission` source check: admit `format in {"parquet", "csv", "fixed_width"}` for the
   sanctioned single non-FK file source, instead of parquet-only. Keep EVERY other condition
   unchanged (single table, native strategies, exact schema, round-trip type/value parity
   L388-402, no transforms/when/vault/FK/STORM/post-validation).
2. Enforce the parity crux: before admitting a non-parquet source, assert the profile/plan
   column types equal the resident Arrow types (reuse/extend the existing round-trip parity
   check); decline to pandas (fail-closed) on any mismatch. Never diverge from the oracle.
3. Platform: confirm the source descriptor format flows and the resident Arrow is passed for
   CSV/fixed-width the same as parquet (expected: `binding_resolve` sets format from extension;
   `_read_sources_as_arrow` reads to Arrow regardless). Add a platform change ONLY if required;
   prefer engine-only.

## Acceptance tests (write these as the end goal)

- PARITY: for CSV and for fixed-width single-table mask fixtures (native strategies:
  keyed_hash, redact, truncate, categorical, bucket_perturb, group_key, date_shift,
  passthrough, on admitted resident types), unified-slice output == pandas full-frame output,
  BYTE-for-byte. Reuse the existing flag-off/flag-on parity harness that proves parquet parity
  today. Include tricky typings: integer-looking columns read from CSV, nulls, unicode,
  fixed-width padding/whitespace, all-null columns.
- ROUTE EVIDENCE: those jobs report `compiled_kernel_executed = True` (admitted to Rust).
- NO REGRESSION: existing parquet parity unchanged; a source whose profile types disagree with
  the resident Arrow still DECLINES (fail-closed); all other declines (FK, STORM, multi-table,
  non-native strategy, transforms, when-gate, vault) unchanged.
- FIXED-WIDTH SPECIFIC: prove the fixed-width reader's typing (positional, padding, whitespace)
  round-trips consistently. This is the loosest-typed format and the highest parity risk; if it
  cannot be proven byte-parity cleanly, it declines (fail-closed) and ships in a follow-up slice
  rather than risking divergence.

## Gates

- Codex plan-gate on THIS plan before any build (resolve the `profile_source` open question and
  the parity-guard design).
- Build by a Sonnet builder from the approved plan; then dennis adversarial gate; then Codex FINAL.
- ACTIVATION is Cam-gated: widening production admission changes what runs on Rust, so I present
  the parity + perf evidence for Cam's go. If the engine's D9 cert discipline applies to an
  admission-widening (it masks the same resident Arrow, so perf should be neutral), include a D9
  re-cert or a documented perf-neutrality rationale.

## Scope / non-goals

- IN: engine `_unified_slice_admission.py` source-format widening + the profile-vs-resident-Arrow
  parity guard + CSV/fixed-width parity tests; platform descriptor-flow confirmation.
- OUT (later tracks): multi-table / FK / generation / mixed (Track B); non-native strategies and
  faker-generation promotion (Track B strategy coverage); output-format handling (the target
  writer already emits the configured output format; confirm it is unaffected, do not change it).
- PRESERVE: the pandas oracle as the fail-closed fallback; every existing decline; the
  byte-parity invariant; the ~1% CLI-no-Rust pandas path.

## Risks

- Profile-vs-resident-Arrow type divergence for loosely-typed CSV/fixed-width (the crux):
  mitigated by the parity guard + fail-closed decline + byte-parity acceptance test.
- Fixed-width typing edge cases: covered by dedicated fixtures; decline-and-defer if not
  cleanly byte-parity.
- Perf: negligible (same resident Arrow masked; format affects only the platform's existing read).

## Codex plan-gate (2026-09-29): GO-with-revisions (0 blocker, 2 HIGH, 2 MEDIUM)

Findings that reshape scope:

1. (HIGH) Corrected enabling-fact: `profile_source` (_pipeline.py:341/365, profile/_source.py:126/207,
   profile/_readers.py:330) independently RE-READS the descriptor file to build the compiled plan; it
   does NOT profile the passed-in resident Arrow. Both execution routes mask the resident Arrow, but
   COMPILATION profiles a separate descriptor-backed read. So the divergence risk is real.
2. (HIGH) The guard already exists: `resident_contract_admission` (_unified_slice_admission.py:478/550)
   already requires the profile-derived compiled-binding input type to EXACTLY equal the resident Arrow
   type (+ strategy allowlist). Reuse/extend THAT (not the L388-402 round-trip check). Widening the
   format check is safe under it: it cannot admit a profile/resident mismatch.
   BUT: the platform reads CSV as dtype=str (v2_cloud_staging.py:242/282, all columns string) while the
   engine profiler uses pandas type inference (profile/_readers.py:242). So integer-looking and all-null
   CSV columns MISMATCH and correctly DECLINE. Consequence: format widening alone admits only
   type-AGREEING CSV (effectively all-string) + fixed-width; the COMMON integer-column CSV job still
   declines. Hitting the end goal (common CSV on Rust) requires READER-SEMANTICS ALIGNMENT, a bigger change.
3. (MEDIUM) Platform LocalRef binding derives format from the stored file EXTENSION (binding_resolve.py:103)
   and the recipe format LOSES to the extension (_service.py:446); a fixed-width `.txt` upload becomes
   format=txt and loses the fixed_width LAYOUT the engine requires (config/_sources.py:49). Fix: preserve
   the recipe's fixed_width format+layout while replacing only the locator, OR exclude LocalRef fixed-width
   from this track.
4. (MEDIUM) Acceptance: distinguish MUST-ADMIT (type-agreeing) vs MUST-DECLINE (mismatch); require
   activated=True + every node executed=True; compiled_kernel_executed=True on the HASH node (passthrough/
   redact/truncate intentionally leave it False); a native-companion env (so success is not a fallback/skip);
   a non-vacuity (poison-pandas) check; at least one fixed-width job cleanly admitted or the track is not
   complete. The parity harness compares columns/types/values/metadata/warnings/metrics, not literal file
   bytes; clarify "byte-identical" accordingly. Config strategy name is `hash`, not `keyed_hash`.

## Scope decision (for Cam)

- OPTION 1 (small, the genuinely-easy part): widen the admission format check to {parquet, csv, fixed_width}
  + fix the LocalRef fixed-width descriptor/layout loss + rely on the existing resident-contract guard.
  Result: all-string CSV and fixed-width jobs (types agree) run on Rust; integer-column CSV still declines.
  Does NOT fully hit the end goal for the common CSV job.
- OPTION 2 (hits the end goal): also align the type source of truth so the common CSV case admits. Cleanest
  candidate: make the unified-slice lane profile/compile from the RESIDENT ARROW the platform already loaded
  (the data actually masked), instead of a separate file re-read. Since a parquet file's resident Arrow
  already matches its file profile, existing parquet parity is unaffected; the change only affects the
  currently-declining CSV/fixed-width cases, so the blast radius is bounded. Bigger + needs a careful parity
  proof, but it is the change that actually makes real CSV jobs run Rust.

Recommendation: Option 2 (goal-hitting, bounded blast radius via profile-from-resident-Arrow), re-gated by
Codex before build. Option 1 alone leaves the common CSV job on pandas, which is the flaw we set out to fix.

## REVISED PLAN: Option 2 chosen (Cam, 2026-09-29). This section is the authoritative build spec.

End goal (unchanged): a real single-table mask job with CSV or fixed-width input, INCLUDING
number-looking columns, runs on the Rust unified-slice lane with output byte-identical to the
pandas oracle. Mask-as-text semantics are preserved (Cam confirmed: CSV columns stay masked as
text; masking columns as typed is a separate, deferred product change).

### Approach
Make the unified-slice lane source its input column types from the RESIDENT Arrow table (the data
the platform already loaded and that BOTH routes actually mask), instead of the profiler's
independent descriptor-backed file re-read. Once the plan compiles against the resident table, the
profile-vs-resident type divergence disappears for every format, so CSV/fixed-width jobs are
admitted and masked on Rust, on the exact same in-memory table the pandas route masks today, hence
byte-parity by construction. Parquet is unaffected (its file profile already equals its resident
types). The strategy allowlist and the admitted-resident-type domain still gate what can run.

### Changes
1. Engine `_unified_slice_admission.py`: widen the source `format` check from parquet-only to
   `{parquet, csv, fixed_width}` for the sanctioned single non-FK file source. Keep every other
   decline (single table, native strategies, exact schema, no transforms/when/vault/FK/STORM/
   post_validation).
2. Engine compilation input-type source: make the compiled binding's input type authoritative from
   the resident Arrow table (`caller_sources[table]`), not the profile-derived type
   (`_shadow_bindings.py:204`; compiler at `physical/_live_inputs.py:74`; profiler at
   `profile/_source.py`). Reconcile `resident_contract_admission` (`:478/:550`) so it still
   MEANINGFULLY validates the strategy allowlist + the resident type against the admitted-type
   domain (`_ADMITTED_RESIDENT_TYPES`), rather than becoming a moot resident-vs-resident
   self-comparison. Builder determines the minimal correct wiring; the Codex plan-gate validates it.
3. Platform: fix LocalRef fixed-width so the recipe's `fixed_width` format + layout is preserved
   when only the file locator is substituted (today the stored `.txt` extension overrides it and
   the layout is dropped, `binding_resolve.py:103` / `_service.py:446` / engine
   `config/_sources.py:49`). The CSV local path already flows unchanged.

### Acceptance tests (the end-goal signal)
- MUST-ADMIT + BYTE-PARITY: single-table mask jobs on (a) a CSV with number-looking AND text
  columns, (b) a fixed-width file, using native strategies on admitted resident types, run on the
  unified-slice lane with output equal to the pandas full-frame route via the existing parity
  harness (columns, types, values, schema metadata, warnings, row errors, metrics; note this is
  not literal target-file bytes). At least one number-column CSV job AND one fixed-width job must
  admit cleanly, else the track is not complete.
- ROUTE EVIDENCE: `activated=True`, every node `executed=True`, and `compiled_kernel_executed=True`
  on the HASH node (passthrough/redact/truncate intentionally leave that flag False). Run in a
  native-companion-present environment so success cannot be a silent fallback/skip; include a
  poison-pandas (non-vacuity) check.
- MUST-DECLINE (fail-closed, unchanged): FK/relationships, multi-table, `run_storm`/validators/
  quarantine/post_validation/fidelity_report/vault, transforms, `when:` gates, non-native
  strategies, non-admitted resident types. If any strategy diverges on a text column, it declines.
- NO REGRESSION: parquet parity and all existing declines unchanged.
- Config strategy name is `hash`, not `keyed_hash`.

### Perf / gates
Perf-neutral (same resident table masked; format affects only the platform read); confirm no D9
regression or give the perf-neutrality rationale. Gates: Codex re-plan-gate on THIS revised spec
BEFORE build; then Sonnet build; dennis; Codex FINAL; STOP at Cam activation gate (production
default flip is Cam's call).

### Scope / non-goals
IN: engine admission format-widen + binding input-type-from-resident + parity tests; platform
LocalRef fixed-width fix. OUT: multi-table/FK/generation/mixed (Track B); non-native strategies +
faker-generation promotion (Track B); mask-columns-as-typed (deferred product change); output-format
handling (unchanged). PRESERVE: pandas oracle fallback, mask-as-text, byte-parity invariant.

## Plan-gate r2 (2026-09-29): GO-with-revisions, FOLDED. Confirmed BOUNDED (one engine compilation/admission slice + platform LocalRef fix; no full-profile rebuild). Build to this.

1. Resident typing must drive ALL physical type decisions in the unified-slice compilation path, not
   only `ExecutionBinding.input_schema`. Route resident Arrow types into:
   - target + group-key-sibling `input_schema`;
   - type-preserving `output_arrow_schema`;
   - passthrough output schema (native/_requirements.py:301);
   - hash native eligibility (native/_requirements.py:375/398 - e.g. a decimal-looking CSV is resident
     string but profiles as float; without this, hash stays python_only despite fixing input_schema);
   - bucket-perturb / date-shift / group-key type gates (native/_operator_config_rejections.py:121/191/251);
   - `requirements_for` is called with the profile at physical/_compiler.py:305 - feed it resident-derived
     types for this lane.
   KEEP the rest of the descriptor profile as-is (relationships, null/cardinality stats, warnings,
   seed-envelope): both the oracle and unified routes share the same logical `Plan`
   (plan/_compile.py:281/447) and this lane already excludes relationships. Those facts are NOT
   reconstructed from Arrow.
2. Guard reconciliation (only valid after item 1 is complete): the profile-vs-resident equality at
   _unified_slice_admission.py:550 (and the group-key-sibling equality at :443) becomes tautological -
   remove it. RETAIN: per-strategy `_ADMITTED_RESIDENT_TYPES` (:154), the resident-to-pandas value/type
   round-trip protection (:378), group-key sibling domain/order checks, and the coverage / companion /
   namespace / null-bearing-integer gates.
3. Parquet decision: apply resident-authoritative typing UNIFORMLY (all formats). Normal platform
   parquet is unaffected (resident == file profile: v2_cloud_staging.py:286, profile/_readers.py:174).
   A direct caller whose supplied resident table differs from the descriptor parquet profile currently
   DECLINES on the equality and would now ADMIT: this is a SAFE route-widening (output stays
   oracle-equivalent) - explicitly ACCEPT it and add a test, do not treat it as a regression.
4. Platform LocalRef fixed-width fix: preserve the recipe's `fixed_width` format + layout, substitute
   only the local path. Make the merge CONDITIONAL on recipe format == fixed_width so CSV LocalRef is
   unchanged.
5. Acceptance additions: integer-looking AND decimal-looking CSV hash cases; a passthrough case proving
   both input and output binding schemas come from resident Arrow; targeted resident-type eligibility
   cases for hash, bucket/date, and group-key sibling resolution (not just end-to-end hash admission);
   platform end-to-end tests for LocalRef fixed-width layout preservation + a CSV LocalRef regression
   control. Classify resident `null`/all-null CSV columns as DECLINE unless separately admitted. Keep
   the route-evidence / native-companion / poison-pandas / parity / MUST-DECLINE requirements.
