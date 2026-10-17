Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, architecture, performance, testing, verification

# Phase F slice F2: mixed generate+mask with the masking on Rust

Per program plan `docs/plans/2026-10-16-phase-f-program.md` §3 (F2) and the Cam
2026-10-17 re-sequence (F2 now; F1/F3 held for a re-baselined generation oracle).
Generation stays RESIDENT (unchanged); the MASK half of a mixed job runs on the
bounded Rust routes instead of the full-frame pandas oracle. This avoids F1's
byte-identity problem: masking is already byte-identical across routes (D-audit),
and generation is untouched.

## 1. Problem + goal

Today ANY generate table disqualifies the bounded routes, so a mixed
generate+mask job always runs full-frame/pandas: `_pipeline_routing.py:216-217`
(`if has_generate_table: return False, "generate_plus_mask"`), orchestrated by
`_pipeline_generate_mask.py:73` `run_generate_and_mask_steps` (Step 1
`generate_tables` resident, Step 2 mask the mask-tables, stitch via
`execution/_stitch.py`). A native mixed path exists but is SHADOW-ONLY / inert
(`execution/physical/_shadow_mixed.py` `dispatch_mixed`): it runs generation, then
reuses the native mask loop for the mask half, with a scoped admitted subset.

F2 goal: a PRODUCTION mixed route where the mask tables run on the bounded Rust
masking routes (chunked / out-of-core), generation resident, under ONE
transactional publish, byte-identical to the full-frame mixed oracle.

## 2. Scope (admitted subset; Cam recommended scope)

Admit the shadow-mixed contract's subset to production
(`_shadow_mixed.py` admission): exactly ONE generate->mask FK edge, single
(non-composite) int or string key, no job-level validators / quarantine / vault /
fidelity / mask_secret / source-loader. Mask-table drivers: the bounded
scalar/chunked routes (and out-of-core only if already admitted). Generation:
resident (`generate_tables`, unchanged), its outputs merged as the FK sources the
mask side reads (the existing mechanism, `_pipeline_generate_mask.py:149-151`).
DECLINE (keep full-frame, coded reason): composite FK keys, OUT_OF_CORE mask
driver mixed with generation (if not already admitted), and the generate->mask
shapes `_shadow_mixed` already declines. FOLLOW-UPS (not F2, per recommended
scope): lift nullable/NaN generated-output into the coupling first, then additional
FK key types; composite + bounded generation at 100M (needs F1) stay later.

## 3. Design

- ROUTING: `_pipeline_routing` admits a mixed job's MASK tables onto the bounded
  routes when the whole job matches the §2 admitted subset (generation resident,
  one int/string gen->mask FK edge, no disqualifying job settings); otherwise the
  existing full-frame path, coded reason. Reuse the `_shadow_mixed` admission
  predicate as the single source of truth (promote it from shadow to a shared
  admission used by both the shadow harness and the production route).
- EXECUTION: Step 1 generate resident (unchanged). Step 2 mask the mask-tables via
  the existing bounded masking entry (`run_auto_chunk` / the OOC route), with the
  generated parent table(s) supplied as FK sources (the merge already in
  `_pipeline_generate_mask.py`). The masked output streams to the sink.
- TRANSACTIONAL BOUNDARY: ONE `OutputPublish` commit/abort session spanning the
  whole mixed job (generated tables + masked tables), so a failure anywhere aborts
  the whole publish - no partial commit, no post-publish fallback. (Generated
  tables are resident, written to the sink alongside the streamed masked tables
  under the same session.)
- PARENT COMPLETENESS: the generated parent is resident and complete when the
  masked child reads it as an FK source (no streaming-parent hazard here, since
  generation is resident) - this is why F2 is tractable without F1.

## 4. Byte-identity (the tractable part)

Mixed output must be byte-identical to the current full-frame mixed oracle. This
holds because: (a) generation is the UNCHANGED resident path; (b) the mask half
runs the bounded Rust routes, already proven byte-identical to the full-frame
oracle by Phase D + D-audit (the no-legacy compute audit). So F2's byte-identity
is inherited, not newly established. Gate: the existing mixed/FK parity suites +
a forced-mixed-route test asserting the mask tables took the bounded route (not
full-frame) AND the output equals the full-frame oracle exactly.

## 5. Acceptance

- Byte-identical to the full-frame mixed oracle across the admitted matrix
  (independent mixed + one gen->mask FK edge, int and string keys), asserted with
  the mask route actually taken (route evidence / driver_by_table), exact
  schema+values+order.
- Decline path: composite / OOC-mixed / disqualifying-settings shapes keep the
  full-frame route with a coded reason (no silent change); a test asserts each.
- Transactional: a failure injected in the mask step aborts the whole job's
  publish (no partial commit of generated or masked tables); abort test.
- Generated-parent -> masked-child: the masked child's FK output matches the
  full-frame oracle (orphan/RI preserved), with the resident generated parent as
  the source (reuse F0's `mixed_fk_generated_parent` shape).
- No regression to pure-generate or pure-mask routing. ci-mirror clean (ruff +
  mypy on `src/decoy_engine`); companion + no-companion suites green; if any
  `execution/physical/` module changes, the physical-seam byte-identical sentry
  applies (shadow modules); if an `execution/` module outside `physical/` changes,
  add it to `permitted_non_physical` with rationale.
- Platform side (if the mixed route surfaces through the platform job path): the
  A3 contract / routing tests stay green; a sibling platform slice activates it
  with Cam go (mirror D2c/D5 pattern) - engine route first.

## 6. Risk

The admission promotion (shadow predicate -> shared production admission) must not
widen what runs bounded beyond the proven subset (guarded by the decline tests +
the shadow predicate being the single source of truth). The transactional boundary
across resident-generated + streamed-masked tables is the main new wiring (guarded
by the abort test). Byte-identity is inherited from Phase D, low risk. Expect
plan-gate scrutiny on the admission-predicate sharing and the one-publish-session
composition.

## 7. Gate chain

Codex plan-gate (this doc) -> build (Sonnet) -> ci-mirror + mixed/FK parity suites
+ forced-mixed-route + abort tests -> dennis -> Codex final -> merge on green +
Slack. DOCUMENT: roadmap + shipped-log, program plan F2 done, engine graph refresh.
Prerequisite reminder: the composite in-process repeatability test (F0 reviewers'
follow-up) is tied to F3, not F2 - defer with F3.
