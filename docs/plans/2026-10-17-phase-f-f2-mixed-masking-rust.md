Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, architecture, performance, testing, verification

# Phase F slice F2: mixed generate+mask with the masking on Rust

Per program plan §3 (F2) + the Cam 2026-10-17 re-sequence (F2 now; F1/F3 held for
a re-baselined generation oracle). Generation stays RESIDENT (unchanged); the MASK
half of a mixed job runs on the bounded Rust routes. Rev-2 folds the Codex
plan-gate NO-GO (3 P1 + 2 P2): the key correction is that F2's byte-identity is
PARTLY inherited (the mask kernels, from Phase D/D-audit) and PARTLY NEW (the
generate->bounded-mask FK COMPOSITION), which F2 must verify, not assume.

## 1. Problem + goal

Today any generate table disqualifies the bounded routes, so mixed jobs run
full-frame/pandas (`execution/_pipeline_routing.py:216-217`; orchestration
`_pipeline_generate_mask.py:73`, stitch `_stitch.py`). A native mixed path exists
but is SHADOW-ONLY/inert (`execution/physical/_shadow_mixed.py dispatch_mixed`),
reusing the native mask loop + the shadow FK resolver
(`_shadow_fk.build_fk_dispatch`/`resolve_admitted_fk_node`). Goal: a PRODUCTION
mixed route - mask tables on the bounded Rust routes, generation resident, one
transactional publish, byte-identical to the full-frame mixed oracle.

## 2. Two-stage shared admission (plan-gate #2/#3)

Extract a PRODUCTION-NEUTRAL SEMANTIC admission CORE out of `_shadow_mixed` (the
shadow wrapper keeps its shadow-only inertness checks; the production route keeps
its own execution/output restrictions). The core is the single source of truth and
is TWO-STAGE:
- STATIC (pre-generation, shape): the admitted mixed shapes - INDEPENDENT mixed (no
  generate->mask FK edge) AND COUPLED mixed (EXACTLY ONE generate->mask FK edge,
  single non-composite int/string key); complete relationship graph; admitted
  generation shapes; every downstream mask node admissible on a bounded route; no
  job-level validators/quarantine/vault/fidelity/mask_secret/source-loader.
  (Resolves the r1 "exactly one edge vs independent" contradiction: independent =
  zero crossing edges; coupled = exactly one; both admitted, each stated.)
  OUT_OF_CORE mask driver in a mixed job is an UNCONDITIONAL F2 decline.
- RUNTIME (post-generation, materialized-output, before ANY sink write): across
  ALL generated tables (incl. independent mixed), check the actual produced output
  - no nulls/NaN/nested in a coupling key, actual parent-key Arrow type supported
  (int/string), etc. On a RUNTIME decline, REUSE the already-generated resident
  outputs in the full-frame mask path (do NOT regenerate), coded reason.
Name the mixed dispatch point and how it interacts with BOTH routing layers
(`_pipeline_routing` static gate + the post-generation runtime gate), the explicit
execution modes, and the kill switch.

## 3. The generate->bounded-mask FK bridge (plan-gate #1 — the NEW part)

The bounded masking entry (`_pipeline_auto_chunk.run_auto_chunk` / the OOC route)
today resolves FK from file/source inputs; it cannot directly consume a RESIDENT
generated parent as the FK context. F2 specifies a PRODUCTION FK BRIDGE that feeds
the resident generated parent's key column(s) as the parent context into the
bounded masking route's FK resolution, REUSING the shadow resolver's semantics
(`_shadow_fk`: parent-key map, matched-child inheritance via `resolve_fk_keys`,
orphan EXCLUDE/CASCADE/policy precedence, REMAP integer write-back). Define: the
parent-context interface, how it threads into chunked-payload masking per chunk,
and the orphan/REMAP/PRESERVE/WARN/FAIL behavior. This COMPOSITION is new and must
be verified (not inherited).

## 4. Byte-identity: inherited kernels + newly-verified composition (plan-gate #1)

The mask kernels are byte-identical to the full-frame oracle (Phase D + D-audit).
NEW and to be verified: the generate(resident)->bounded-mask FK composition equals
the full-frame mixed oracle. Gate against the full-frame mixed oracle with the mask
route actually taken AND actual BACKEND execution asserted (a chunked route label
alone can hide oracle execution - assert the compiled/native backend ran, per the
D-audit witness pattern).

## 5. Acceptance

- Byte-identical to the full-frame mixed oracle across the admitted matrix:
  independent mixed, and coupled one-FK-edge with int AND string keys; spanning
  MULTIPLE chunks; nullable/all-null child keys; integers above 2**53; and the
  PRESERVE/WARN/FAIL/REMAP orphan behaviors. Assert the bounded route taken AND the
  native backend executed (not just the route label).
- Two-stage admission tests: static declines (composite, OOC-mixed, disqualifying
  settings) keep full-frame with a coded reason; RUNTIME declines (nulls/NaN/nested
  in a coupling key, unsupported actual parent-key type) fall back to full-frame
  masking REUSING the already-generated outputs (assert no regeneration), coded
  reason.
- Transactional boundary: open ONE OutputPublish session BEFORE the first generated
  write; exactly one final commit; no subordinate commit. Inject failure AFTER a
  generated table AND at least one masked batch are staged, plus generated-write
  and commit failures: assert no published target, staging cleaned, no
  retry/fallback, and (success case) both table sets published atomically.
- No regression to pure-generate / pure-mask routing.
- ci-mirror clean (ruff + mypy on `src/decoy_engine`); companion + no-companion
  suites green.
- Platform: A3 contract / routing tests green; a sibling platform activation slice
  (Cam go) mirrors D2c/D5 - engine route first.

## 6. Sentry handling (plan-gate #5 — two distinct gates)

Put the SHARED semantic admission core + the FK bridge helpers OUTSIDE
`execution/physical/` (shadow code imports them), so the physical-seam
IMPORT-DISCONNECTION check and the shadow INERTNESS tests stay intact. Any changed
`execution/` module outside `physical/` gets a justified `permitted_non_physical`
diff-exception with rationale; changed `physical/` modules fall under the
byte-identical seam sentry (shadow). Keep the two gates separate (do not conflate
the import-boundary gate with the byte-identical-diff gate).

## 7. Risk

The NEW composition (resident generated parent -> bounded-mask FK resolution) is
the real risk; mitigated by reusing the shadow resolver semantics + the
oracle-parity matrix (multi-chunk, nullable, big-int, orphan policies) + asserting
actual backend execution. Admission-core extraction must not widen the bounded
subset (guarded by the decline tests + the core being the single source of truth).
The one-publish-session over resident + streamed tables is new wiring (guarded by
the abort matrix). Scope deferrals (nullable/NaN coupling, composite, 100M mixed
via F1) are explicit.

## 8. Gate chain

Codex plan-gate (this doc) -> build (Sonnet) -> ci-mirror + mixed/FK parity +
forced-route/backend + admission-decline + abort tests -> dennis -> Codex final ->
merge on green + Slack. DOCUMENT: roadmap + shipped-log, program plan F2 done,
engine graph refresh.
