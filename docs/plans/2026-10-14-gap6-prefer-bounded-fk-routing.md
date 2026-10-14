Status: plan (Phase D / audit GAP6. DRAFT rev 1, pre Codex plan-gate. Stop auto-routing a bounded-eligible FK job to the full-frame oracle just because it "fits" RAM; prefer the measured-faster bounded route. HOLD merge for Cam.)

# Prefer the bounded route over the full-frame oracle for RAM-fitting FK jobs

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`; engine `CLAUDE.md`. Grounding: the 2026-10-14 bottleneck audit (GAP6: `auto` routes a RAM-fitting relationship (FK) job to the full-frame pandas oracle, measured ~2583s/10M, 4.3x over the 600s bar) + the 2026-10-14 local crossover measurement (below). Base: engine `origin/main` @ `9ef26c9f`. **Cam pre-authorized the direction** (prefer the bounded route above a measured cutoff, accepting the documented native-vs-Python output shift for the diverted jobs); this plan supplies the measured cutoff. **HOLD the merge for Cam's review** (it changes `auto`-mode default routing).

## 0. Why + the measurement

`decide_execution_route` (`_pipeline_routing.py`) already prefers a bounded route (out-of-core, else sequential) over full-frame for an `auto` relationship-bearing pure-mask job, EXCEPT two returns that divert a bounded-eligible job to full-frame when a byte estimate or a probe says full-frame "fits" RAM:
- `:484-485` `if full_frame_fits_estimate is True: return "full_frame", "byte_estimate_full_frame_fits"`
- `:497-498` `if use_probe_routing and probe_recovers_full_frame is True: return "full_frame", "probe_recovered_full_frame"`

Both exist on the assumption that **full-frame is faster than the bounded route when it fits**. The GAP6 measurement disproves that assumption. A local 3-table FK chain (width 15, hash), full-frame vs sequential:

| rows/table | full-frame wall / peak RSS | sequential wall / peak RSS | sequential advantage |
|---|---|---|---|
| 50k | 80.1s / 1085 MB | 13.8s / 652 MB | 5.8x faster, 1.7x less memory |
| 100k | 172.3s / 1935 MB | 26.9s / 1040 MB | 6.4x faster, 1.9x less memory |
| 250k | (not run) | 67.6s / 2084 MB | |
| 500k | 800.5s / 8187 MB | 130.3s / 3858 MB | 6.1x faster, 2.1x less memory |

Sequential wins at every measured tier (no crossover), and full-frame OOMs by ~1M rows/table (needs ~16 GB) while sequential stays bounded. Full-frame's slowness is the strategy-agnostic per-cell pandas oracle, so other strategies only widen the gap. So diverting a bounded-eligible FK job to full-frame "because it fits" is strictly worse on both speed and memory.

**This slice gates those two returns on `not bounded_eligible`:** a bounded-eligible FK job falls through to the existing bounded-by-eligibility logic (out-of-core when `out_of_core_ready`, else sequential); a bounded-INELIGIBLE job keeps the full-frame-if-fits behavior unchanged.

## 1. The load-bearing subtlety (do not regress small cyclic FK jobs)

A cross-table FK cycle (A->B->A with an acyclic column graph) cannot be ordered for the sequential route and is excluded from out-of-core, so it is bounded-INELIGIBLE. Today a SMALL cyclic FK job that fits full-frame runs successfully precisely BECAUSE of the `full_frame_fits_estimate is True` return being removed here. Removing that return unconditionally would send a small cyclic job to the bounded-by-eligibility fallback, which rejects it (`fk_full_frame_oom_risk_rejected`) -- a functional regression of a job that works today.

Therefore the gate is **`full_frame_fits_estimate is True AND not bounded_eligible`** (and likewise for the probe return), where `bounded_eligible = eligible or out_of_core_ready` (sequential-eligible, or out-of-core-ready). A bounded-eligible job diverts to bounded; a bounded-ineligible-but-fitting job (small cyclic) keeps full-frame exactly as today. Large cyclic jobs still reject as today.

## 2. The code change (minimal, in `decide_execution_route`)

- Compute `bounded_eligible = eligible or out_of_core_ready` (both already computed at `:402`/`:419`).
- `:484`: `if full_frame_fits_estimate is True and not bounded_eligible: return "full_frame", "byte_estimate_full_frame_fits"` (unchanged reason/behavior for the bounded-ineligible case).
- `:497`: `if use_probe_routing and probe_recovers_full_frame is True and not bounded_eligible: return "full_frame", "probe_recovered_full_frame"`.
- A bounded-eligible job now falls through to the existing bounded-by-eligibility block (`:499+`): out-of-core when `out_of_core_ready`, else sequential. No change to that block, to the OOC-vs-SEQ choice, to the reject-before-read, to the explicit `execution_mode` escape hatches, to the lazy-transform branch, or to the row-count rollback path (`use_byte_estimate_routing=False`).
- Keep `use_byte_estimate_routing` / `use_probe_routing` as-is (rollback knobs); this gate is additive within the byte-estimate-in-scope block.

No new module, no kernel, no change to any masking path. The only behavior change is which route a bounded-eligible RAM-fitting FK job takes.

## 3. Behavior / parity contract

This is a routing change, NOT a byte-identity slice. Two classes:
- **Route unchanged -> output byte-identical:** non-FK jobs, generate+mask jobs, explicit `execution_mode` jobs, already-bounded FK jobs (didn't fit full-frame), cyclic FK jobs (small -> still full-frame; large -> still rejected), and the `use_byte_estimate_routing=False` rollback path. All must be byte-for-byte and route-for-route unchanged.
- **Route changed (bounded-eligible FK job that previously fit full-frame) -> now bounded:** output changes ONLY by the already-documented, already-accepted native-vs-Python NFC divergence on `hash`/`categorical`/`bucket_perturb` (the Unicode-version normalization difference recorded in `docs/strategies.md` + the compatibility contract). For NFC-agnostic strategies (`fpe`, `redact`, `truncate`, `passthrough`, `date_shift`, `faker`, `text_redact`) the diverted job's output is byte-identical to its old full-frame output. The slice must PROVE the diverted output differs from the old full-frame output ONLY by that documented envelope, never by a real divergence, and that FK referential integrity is preserved.

## 4. Acceptance matrix

- **T1 divert happens + faster route chosen:** a bounded-eligible (sequential-eligible, below the OOC threshold) FK job whose full-frame estimate fits now returns `("sequential", ...)` not `("full_frame", "byte_estimate_full_frame_fits")`. Assert the route + reason changed; assert (via the routing decision, and a small real run) the sequential route runs. Include an OOC-ready (large, compatible) fitting FK job that now returns `("out_of_core", ...)` instead of full-frame.
- **T2 small cyclic FK NOT regressed (the load-bearing guard):** a small cross-table-cycle FK job that fits full-frame STILL returns `("full_frame", "byte_estimate_full_frame_fits")` and runs successfully (not rejected). This is the regression this plan is built to avoid.
- **T3 large cyclic FK still rejects:** a large cyclic FK job still raises `fk_full_frame_oom_risk_rejected` (unchanged).
- **T4 route-unchanged classes byte-identical:** a non-FK job, a generate+mask job, an explicit `execution_mode=full_frame|sequential|out_of_core` job, and an already-bounded (non-fitting) FK job each take the SAME route and produce byte-identical output to base `origin/main`. Also the `use_byte_estimate_routing=False` rollback path is unchanged.
- **T5 diverted-output parity envelope:** for a diverted bounded-eligible FK job, the new (sequential) output vs the old (full-frame) output is byte-identical for NFC-agnostic strategies (fpe/redact/truncate/passthrough/date_shift) and differs ONLY by the documented NFC envelope for hash/categorical/bucket_perturb; FK referential integrity holds (every masked child key resolves to a masked parent key). Prove it is NOT a real divergence (a negative control: an injected real difference must fail the check).
- **T6 probe-recovery path gated too:** a bounded-eligible FK job that the probe would have recovered to full-frame now goes bounded; a bounded-ineligible (small cyclic) probe-fit job still returns full-frame.
- **T7 routing-reason inventory:** enumerate every test that asserts `byte_estimate_full_frame_fits` / `probe_recovered_full_frame` for an FK job and update it to the new bounded route (the intended contract change, not a weakened assertion); pin that those reasons STILL fire for the cyclic-fitting case.

## 5. Failure modes (reproduce, do not smooth)

- A small cyclic FK job regressing from full-frame to a reject -> T2 (the primary risk of this change).
- A non-FK / generate / explicit-mode job's route or output changing -> T4 (the gate must be scoped to the byte-estimate-in-scope relationship-bearing block only).
- A diverted job's output differing from full-frame by more than the NFC envelope, or breaking FK RI -> T5 (that would be a real regression, not an accepted shift).
- The OOC-ready case accidentally going to sequential (or vice-versa) -> T1 (the bounded-by-eligibility choice must be unchanged).

## 6. Verification + gates

- abi-3 `~/.cache/decoy-d1a-abi3-venv/bin/python` (companion, for the SEQ/OOC native paths); ci-mirror `~/.cache/decoy-ci-mirror-venv/bin/python`. One test process via `~/bin/pytest-one`; PYTHONPATH `<worktree>/src:<worktree>`. Routing-decision tests are pure-Python (no companion needed); the diverted-output parity run uses the companion.
- T1-T7 pass; the full existing routing suite (`test_pipeline_routing.py` and friends) stays green except the intended reason-change updates (T7). No masking-path test changes.
- Coverage + mutation on the changed lines (the two gated returns + the `bounded_eligible` term); a mutant that drops the `not bounded_eligible` guard must be killed by T1; a mutant that drops it on the cyclic side must be killed by T2.
- Module size: `_pipeline_routing.py` is 599 LOC (at the 600 goal) -- the change is a couple of lines + one local; confirm it stays <=600 (or add a census entry at the exact LOC if it crosses).
- Perf witness: cite the measured 5.8-6.4x sequential advantage; add a routing-level assertion that the fast route is chosen for the fitting FK shape (a full 10M perf run is not required here -- the win is the route choice, and the SEQ/full-frame per-route numbers are already measured).
- Gate chain: Opus VERIFY (diff + routing tests + the parity-envelope proof) -> dennis -> Codex final. **Do NOT merge:** build to merge-ready, post the diff + results to Slack, and HOLD for Cam's review (it changes `auto`-mode default routing with a documented output shift). Cam's pre-approval covers the direction; the merge is his call.

## 7. Done when

- [ ] T1-T7 green (divert to bounded + faster route; small-cyclic NOT regressed; large-cyclic still rejects; route-unchanged classes byte-identical; diverted-output NFC-envelope-only + FK RI; probe path gated; reason inventory updated); full routing suite green.
- [ ] Changed lines coverage + mutation-proven; module within size policy.
- [ ] Diff + results posted to Slack; slice HELD for Cam's review (not merged).
- [ ] On Cam's go: merge, confirm main CI green, ROADMAP + RECENTLY-SHIPPED updated (GAP6 closed), graphify refresh (subject to the keyless blocker), Slack recap.

cam
