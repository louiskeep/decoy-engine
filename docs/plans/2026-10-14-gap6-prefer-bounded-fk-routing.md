Status: plan (Phase D / audit GAP6. rev 3, Codex plan-gate rev1+rev2 REVISE folded (cycle BLOCKER, OOC-selection, perf, and the schema-aware no-new-failing-job retention on both admission paths covering decimal + uint64 FK-key reject classes). Stop auto-routing a bounded-eligible FK job to the full-frame oracle just because it "fits" RAM; prefer the measured-faster bounded route. HOLD build + merge for Cam: the diverted-output envelope exceeds the NFC-only shift Cam pre-approved -- see the DECISION NEEDED block.)

# Prefer the bounded route over the full-frame oracle for RAM-fitting FK jobs

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`; engine `CLAUDE.md`. Grounding: the 2026-10-14 bottleneck audit (GAP6: `auto` routes a RAM-fitting relationship (FK) job to the full-frame pandas oracle, measured ~2583s/10M, 4.3x over the 600s bar) + the 2026-10-14 local crossover measurement (below). Base: engine `origin/main` @ `9ef26c9f`.

## DECISION NEEDED FROM CAM (why this is held, not built yet)

Cam pre-approved "prefer the bounded route, accepting the documented native-vs-Python NFC output shift." The Codex plan gate found that is NOT the whole shift. The byte-estimate OOC-selection branch (`_pipeline_routing.py:505`) diverts ANY out-of-core-COMPATIBLE acyclic fitting FK job to OUT-OF-CORE (not sequential), regardless of size. OOC output carries differences BEYOND NFC, all documented in `tests/parity/SEMANTIC_DIFFERENCES.md` but NOT covered by Cam's NFC approval:
- missing pandas schema metadata; preserved float NaN vs oracle null; different empty-child and all-null FK schemas;
- a documented dtype case where the oracle SUCCEEDS but OOC REJECTS (a job that works on full-frame today would fail after diversion -- a functional regression, not a byte diff);
- error-contract differences (e.g. a composite-chain REMAP failure reports `sequential_composite_fk_chain_unsupported` instead of `orphan_remap_parent_missing`).

So diverting fitting FK jobs is a broader compatibility change than the NFC envelope. **Cam's options (pick before build):**
- **(A) Accept the broader bounded envelope** for diverted jobs (the documented OOC + SEQ differences), AND keep full-frame for the specific OOC-reject-dtype shape so no working job starts failing. Biggest win.
- **(B) Divert to SEQUENTIAL only** (keep full-frame for OOC-compatible fitting jobs): stays closest to the NFC-only envelope, but a large share of fitting FK jobs are OOC-compatible, so the realized win is much smaller.
- **(C) Divert to both, but only where the bounded route provably produces output within an agreed envelope**, retaining full-frame for anything outside it (needs the envelope defined).

This plan is written for **(A)** as the recommended path, with the OOC-reject shape retained on full-frame. If Cam picks B or C the acceptance matrix narrows accordingly. HOLD build until Cam picks.

## 0. Why + the measurement

`decide_execution_route` (`_pipeline_routing.py`) already prefers a bounded route over full-frame for an `auto` relationship-bearing pure-mask job, EXCEPT two returns that divert a bounded-eligible job to full-frame when a byte estimate (`:484-485` `"byte_estimate_full_frame_fits"`) or a probe (`:497-498` `"probe_recovered_full_frame"`) says full-frame "fits" RAM, on the assumption full-frame is faster when it fits. A local 3-table FK chain (width 15, hash), full-frame vs sequential:

| rows/table | full-frame wall / peak RSS | sequential wall / peak RSS | sequential advantage |
|---|---|---|---|
| 50k | 80.1s / 1085 MB | 13.8s / 652 MB | 5.8x faster, 1.7x less memory |
| 100k | 172.3s / 1935 MB | 26.9s / 1040 MB | 6.4x faster, 1.9x less memory |
| 250k | (not run) | 67.6s / 2084 MB | |
| 500k | 800.5s / 8187 MB | 130.3s / 3858 MB | 6.1x faster, 2.1x less memory |

Sequential wins at every measured tier (no crossover); full-frame OOMs by ~1M rows/table. The measurement is SEQUENTIAL vs full-frame on a hash chain at 50k-500k. It does NOT by itself establish that OUT-OF-CORE is faster than full-frame for a tiny fitting job, nor that every strategy widens the gap (see MED-4 scope note in §6). Full-frame's slowness is the strategy-agnostic per-cell oracle, so the direction is expected to hold, but the headline claim is narrowed to what was measured.

## 1. The load-bearing subtlety (do not regress small cyclic FK jobs) -- Codex BLOCKER

A cross-table FK cycle (A->B->A, acyclic column graph) is bounded-INELIGIBLE (the sequential route cannot order it; out-of-core excludes it). CRITICAL: `_sequential_eligible` (`:177`) checks job SHAPE only and does NOT inspect the graph, so a pure-mask cycle returns `eligible=True`; the cycle is flagged separately as `cyclic=True` (`_has_cross_table_fk_cycle`), and `out_of_core_ready=False`. So `eligible or out_of_core_ready` is TRUE for a cycle -- the rev-1 predicate would have sent a small cyclic fitting job (which runs on full-frame today) to the bounded fallback, which REJECTS it (`fk_full_frame_oom_risk_rejected`): the exact regression this plan exists to prevent (Codex reproduced it).

**Correct predicate: `bounded_eligible = eligible and not cyclic`.** (Since `out_of_core_ready` already requires `eligible`, this is the true union of the bounded branches.) A non-cyclic eligible fitting FK job diverts to bounded; a cyclic fitting FK job keeps full-frame exactly as today.

## 2. The code change (minimal, in `decide_execution_route`)

- Compute `bounded_eligible = eligible and not cyclic` (`eligible` at `:402`, `cyclic` at `:411`).
- `:484`: `if full_frame_fits_estimate is True and not bounded_eligible: return "full_frame", "byte_estimate_full_frame_fits"`.
- `:497`: `if use_probe_routing and probe_recovers_full_frame is True and not bounded_eligible: return "full_frame", "probe_recovered_full_frame"`.
- A `bounded_eligible` job now falls through to the EXISTING bounded-by-eligibility block. That block (`:505`) selects OUT-OF-CORE for `eligible and not cyclic and out_of_core_compatible` (reason `byte_estimate_bounded_out_of_core`), regardless of the row threshold, and SEQUENTIAL for an eligible acyclic job that is not out-of-core-compatible. The plan does NOT change that block or the OOC-vs-SEQ choice; it only stops the full-frame diversion ahead of it.
- **No-new-failing-job retention guard (Codex rev2 HIGH-1 + HIGH-2), schema-aware, on BOTH admission paths.** A fitting FK job that would divert to OUT-OF-CORE must not become a failing job where the oracle succeeds. `out_of_core_compatible` inspects the plan/work/graph, NOT source schemas, so it does NOT exclude the oracle-success/OOC-reject dtype classes; a guard keyed only on the decimal fixture is insufficient. Documented OOC-reject-but-oracle-success classes to cover: a `decimal` FK-key dtype (`tests/parity/test_phase_d_d0_baseline.py:277`) AND a `uint64` FK key even at small values (`tests/unit/execution/test_de10_fk_lossless_typing.py:250`); the build must audit the OTHER OOC dtype-rejection branches for any further classes and cover them too.
  - Compute a schema-aware signal (new routing input derived from the source schema, since compatibility does not examine schemas) that is true when a fitting FK job carries an OOC-reject-but-oracle-success dtype class.
  - Apply the retention on BOTH full-frame admission paths: the `full_frame_fits_estimate is True` branch (`:484`) AND the `probe_recovers_full_frame is True` branch (`:497`) -- a probe-recovered job in a reject-shape would otherwise divert to OOC and fail. Each path keeps its own reason (`byte_estimate_full_frame_fits` / `probe_recovered_full_frame`).
  - Preference for those shapes: divert to SEQUENTIAL when the job is SEQ-eligible AND SEQ actually handles the dtype (recovers the speed win with no regression); retain FULL-FRAME only when SEQ also cannot match the oracle for that shape. The build must verify SEQ's handling of each dtype class (decimal, uint64, and any others found) rather than assume it; default to full-frame retention for any class SEQ does not provably handle. Do NOT globally change `out_of_core_compatible` to reject these (that would redirect already-bounded jobs to SEQ and change their current behavior).
- No change to the explicit `execution_mode` escape hatches, the lazy-transform branch, the reject-before-read, or the `use_byte_estimate_routing=False` rollback path. Keep the rollback knobs.

## 3. Behavior / parity contract (three populations)

- **Route unchanged -> byte-identical + route-identical to base:** non-FK jobs, generate+mask jobs, explicit `execution_mode` jobs, already-bounded (non-fitting) FK jobs, cyclic FK jobs (small -> still full-frame; large -> still rejected), the oracle-success/OOC-reject dtype shape (retained on full-frame under option A), and the `use_byte_estimate_routing=False` rollback path.
- **Diverted full-frame -> SEQUENTIAL** (eligible, acyclic, NOT out-of-core-compatible, fitting): output vs old full-frame differs only by the documented native-vs-Python NFC envelope on hash/categorical/bucket_perturb; byte-identical for NFC-agnostic strategies (fpe/redact/truncate/passthrough/date_shift/faker/text_redact). FK RI: matched child keys resolve to a masked parent; PRESERVE/WARN/REMAP orphans follow the documented orphan contract (an orphan does not necessarily reference an emitted parent). SEQ error-contract codes may differ from full-frame for composite-chain REMAP (documented).
- **Diverted full-frame -> OUT-OF-CORE** (eligible, acyclic, out-of-core-compatible, fitting): output vs old full-frame carries the documented OOC differences (missing pandas metadata, NaN-vs-null, empty-child/all-null FK schema differences) in addition to the NFC envelope. This is the population that EXCEEDS Cam's NFC approval (the DECISION NEEDED block).

## 4. Acceptance matrix (for option A; narrows for B/C)

- **T1 divert targets the correct bounded route (Codex HIGH-2):** an eligible, acyclic, out-of-core-COMPATIBLE fitting FK job (including a small one, e.g. 10 rows) now returns `("out_of_core", "byte_estimate_bounded_out_of_core")` not full-frame; an eligible, acyclic, NOT-out-of-core-compatible fitting FK job returns `("sequential", ...)`. Assert route + reason for both.
- **T2 small cyclic FK NOT regressed (Codex BLOCKER):** a small cross-table-cycle FK job with an acyclic column graph (assert `eligible=True` AND `cyclic=True`), estimate fits, STILL returns `("full_frame", "byte_estimate_full_frame_fits")` and runs; same for the probe-recovers case (T6).
- **T3 large non-bounded-eligible still rejects:** a large cyclic FK job still raises `fk_full_frame_oom_risk_rejected`; a large non-fitting non-eligible job unchanged. Byte-rejection requires estimate-not-fitting AND no successful probe (not "large" alone).
- **T4 route-unchanged classes byte-identical:** non-FK, generate+mask, each explicit `execution_mode`, already-bounded FK, and the `use_byte_estimate_routing=False` rollback path -- same route + byte-identical output vs base.
- **T5a diverted->SEQ envelope:** NFC-only vs old full-frame (byte-identical for NFC-agnostic strategies incl. fpe/redact/truncate/passthrough/date_shift/faker/text_redact; documented NFC envelope for hash/categorical/bucket), FK RI preserved, with a negative control (an injected real difference must fail).
- **T5b diverted->OOC envelope:** output matches the OOC route's own strict baseline (the documented OOC differences), FK RI preserved, empty-child/all-null/boundary-date cases covered; a negative control. This is the matrix Cam's option-A approval would cover.
- **T5c no-new-failing-job (both paths, both dtype classes):** for a `decimal` FK-key job AND a `uint64` FK-key job (and any other OOC-reject class the build finds), when it fits full-frame via the ESTIMATE path AND when it is recovered via the PROBE path, it still completes (either retained on full-frame with its original reason, or diverted to SEQ if SEQ provably handles the dtype) -- never diverted to an OOC reject. Real success-vs-rejection fixtures for both admission paths; a mutant that drops the guard on either path, or covers only the decimal class, must be killed.
- **T6 probe path gated identically:** bounded-eligible probe-fit job diverts to bounded; cyclic probe-fit job stays full-frame.
- **T7 reason inventory (broad):** update every assertion of `byte_estimate_full_frame_fits` / `probe_recovered_full_frame` for a DIVERTED FK shape across the byte-estimate/probe tests, transform-admission, Arrow-integration, and the Phase-D faker/fpe/composite/text-redact route tests, AND the physical compiler reason handling -- to the new bounded route; RETAIN those reasons' assertions for the cyclic-fitting and other bounded-ineligible cases (the intended contract change, not a weakening).

## 5. Failure modes (reproduce, do not smooth)

- A small cyclic FK job regressing from full-frame to a reject -> T2 (the primary risk; the `not cyclic` term is load-bearing).
- A fitting job diverted to OOC that the oracle would have completed but OOC rejects -> T5c (no new failing job).
- A non-FK / generate / explicit-mode / rollback job changing route or output -> T4.
- A diverted->SEQ job differing by more than the NFC envelope, or a diverted->OOC job differing from the OOC baseline, or broken FK RI -> T5a/T5b.

## 6. Verification + gates

- abi-3 companion venv for the SEQ/OOC paths; ci-mirror otherwise. One test process via `~/bin/pytest-one`; PYTHONPATH `<worktree>/src:<worktree>`. Routing-decision tests are pure-Python; the parity runs use the companion.
- T1-T7 pass; full existing routing suite green except the intended T7 reason updates. No masking-path test weakened.
- Coverage + mutation on the changed lines: a mutant removing the `not cyclic` term must be killed by T2; a mutant removing the whole `not bounded_eligible` guard must be killed by T1; a mutant dropping the T5c retain-full-frame guard must be killed by T5c.
- Module size: `_pipeline_routing.py` is 599 LOC (at goal) -- a couple of lines + one local; confirm <=600 or add a census entry at the exact LOC.
- Perf (Codex MED-4, narrowed): the measured claim is SEQUENTIAL 5.8-6.4x over full-frame on the hash chain at 50k-500k. Do NOT claim OOC is faster for tiny jobs or that all strategies widen it. Add a routing-level witness that the fast bounded route is chosen for the fitting shapes, plus one small real run per diverted route (SEQ and OOC) confirming it completes faster than the full-frame arm. No 10M rerun needed.
- Gate chain: Opus VERIFY -> dennis -> Codex final. **Do NOT merge, and do NOT start the build until Cam picks A/B/C** (the diverted-output envelope exceeds his NFC pre-approval). Once he picks: build to the chosen scope, gate, post diff + results to Slack, HOLD merge for his final look.

## 7. Done when

- [ ] Cam has chosen A/B/C for the diverted-output envelope.
- [ ] T1-T7 green for the chosen scope (divert to the correct bounded route; small-cyclic NOT regressed; large-cyclic rejects; route-unchanged byte-identical; SEQ + OOC diverted envelopes; no-new-failing-job; probe gated; reason inventory updated); full routing suite green.
- [ ] Changed lines coverage + mutation-proven (incl. the cycle-exclusion and retain-full-frame mutants); module within size policy.
- [ ] Perf witness recorded (narrowed claim); diff + results posted to Slack; HELD for Cam's merge go.
- [ ] On merge: confirm main CI green, ROADMAP + RECENTLY-SHIPPED updated (GAP6 closed), graphify refresh (subject to the keyless blocker), Slack recap.

cam
