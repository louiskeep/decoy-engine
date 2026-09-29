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
