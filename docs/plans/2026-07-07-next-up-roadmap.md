# Next-Up Development Roadmap

**Written:** 2026-07-07. **Purpose:** the ordered list of what we complete next
when we return to development. Captures the in-flight 100M-row scaling sprints
(SC1-SC6) at their current status, then four cross-repo follow-on items Cam
named for after the scaling program.

This is a planning/tracking doc, not an implementation guide. Each sprint below
already has (or will get) its own `docs/plans/…-implementation-guide.md`. The
canonical detail for the scaling sprints lives in
`docs/plans/2026-07-06-100m-row-scaling-program.md` (PR #33).

Related roadmaps: `docs/job-performance-sprints.md` (P0-P10 / R1-R3 runtime
performance), `docs/relationships-memory-scaling.md` (the memory model this
program removes the ceiling from).

---

## Part A - Current sprints: 100M-row FK scaling program (SC0-SC6)

The goal: FK (multi-table, relationship-preserving) jobs to 100M+ rows on a
32 GB box. Full-frame FK is memory-bound and OOMs near ~9M rows/table
(~27M total); the out-of-core DuckDB route (bounded RAM regardless of
cardinality) is the only path that completes 100M. This program lands that
route, auto-routes to it, widens its strategy surface, and adds pre-flight OOM
prevention. Full spec + GATE-1 decisions: the 100M program doc (PR #33).

| Sprint | What | Status |
|--------|------|--------|
| **SC0** | Land the routing spine (`run_pipeline` substrate selection + auto-chunk + `_planner.classify_job` + P0 perf gates) | **DONE** - landed via engine PR #31 (engine main 0.3.0) |
| **SC1** | Land the out-of-core FK runner as an **opt-in, unwired sibling** (`run_fk_out_of_core`, DuckDB-gated, budgeted); initial strategy set `hash/redact/truncate/passthrough`; parity harness | **DONE** - merged 2026-07-09 via engine PR #34 (fail-closed hardening pass, dennis APPROVE 0 BLOCKER/0 HIGH). See "SC1 status" below. |
| **SC2** | Wire auto-routing: the live router selects out-of-core for eligible large FK jobs; ineligible-but-large jobs reroute-to-sequential or reject-before-read with a reason (never silent OOM) | **DONE** - part 1 (CF1/CF2/CF3 hardening) merged via PR #37; part 2 (the auto-routing wire) merged 2026-07-09 via PR #38 (dennis re-review APPROVE, 0 BLOCKER/0 HIGH). See "SC2 status" below. |
| **SC3** | Widen out-of-core to Group (b) strategies (`fpe`, `text_redact`, `date_shift`, `bucketize` + conditional `faker`/`categorical`), each with byte-parity vs full-frame | **DONE (branch `sc3/widen-oocgroup-b`)** - `fpe`, `text_redact`, `categorical` (deterministic) ported with proven byte-parity; `date_shift`, `bucketize`, `faker` are documented fail-closed routing MISSes. M1 carry-forward test added. See "SC3 status" below. |
| **SC4** | Widen out-of-core to Group (c) strategies (`text_mask`, `code_set`, `bucket_perturb` with conditional config shapes; `geo_generalize`, `formula`, `derived`, `nested` documented fail-closed) - in v1 critical path per Cam | **DONE** - merged 2026-07-09 via engine PR #43 (SC4 + remediation commit), dennis APPROVE 0 BLOCKER/0 HIGH/1 MEDIUM/3 LOW. See "SC4 status" below. |
| **SC5** | Platform Sprint E: peak-MB estimator + admission gate (measure-only default; over-hard-ceiling reject before read; reroute OOC-eligible jobs to streaming). OOM **prevention**. | **DONE** - merged 2026-07-09 via engine PR #40 (public eligibility-query export) + platform PR #20 (FK-aware admission discount), dennis APPROVE after one fix round. **Surfaced a significant gap: the engine's out-of-core route is not reachable through the platform's job runner today for any config shape** - see "SC5 status" below. |
| **SC6** | Validate 100M on GCP (32 GB) with the built `scripts/gcp-bench/engine-bench.sh` battery; commit the run that backs the "100M+ on 32 GB" claim | **QUEUED** - needs gcloud auth + spend confirmation. Overlaps Part B item 1. |

**Critical path to a shippable, auto-routed, OOM-safe 100M:**
SC0 → SC1 → SC2 → SC3 → SC4 → SC6, with SC5 built in parallel (measure-only) and
calibrated from SC6's early baseline. SC4 (GA-completeness: Group c strategies)
landed 2026-07-09.

### SC1 status - DONE (merged 2026-07-09, PR #34)

The prove-or-reject fail-closed hardening pass Cam specced on 2026-07-07 landed
as 3 independently-reviewable commits: (1) `_mask.py` truncate raw-leak backstop
- invalid config now raises the oracle's `StrategyError` codes instead of
falling back to `passthrough_array`; (2) `_batch_join.py`/`_join.py` int/float
FK-key sink-parity - a representability-guarded round-trip cast replaces the
unsafe-cast crash, failing closed with `out_of_core_fk_key_dtype_unsupported`
when a key can't survive the cast losslessly; (3) `_compat.py` - the audit this
pass required turned up a bonus gap (multi-table FK-cycle admission that
crashed `_table_order` uncoded), now gated fail-closed pre-runner.

dennis adversarial review: **APPROVE, 0 BLOCKER / 0 HIGH / 1 MEDIUM / 2 LOW.**
Full mirror gates green (ruff, format, mypy, regression-gate 5680 passed/36
skipped/1 pre-existing unrelated env failure, parity harness, memory sentinel).
The MEDIUM did not block the SC1 merge (fails closed, opt-in/unwired route,
zero live blast radius today) but **is a hard precondition for SC2** - see CF3
below.

**Carry-forwards tracked into SC2 (must be resolved before the route is wired):**
- **CF1** - chunked-FK self-masking gate reachability: re-review before relaxing
  `_planner._chunked_rejection` or the platform gate.
  **RE-CONFIRMED (SC2 part 1, 2026-07-09):** still holds. `git show --stat`
  confirms SC1 (`c14aa7e`) did not touch `_planner.py`, so the engine guard
  (`_chunked_rejection`'s `if config.get("relationships")` short-circuit, which
  skips `check_chunked_compatibility` entirely) is unchanged. The relaxed
  FK-child admission remains reachable only by a direct caller of
  `check_chunked_compatibility` / `run_mask_pipeline_chunked` with an FK config,
  of which none exists in engine or platform. No code change. Pinned by
  `test_execution_planner.py::...::test_fk_relationships_keep_chunked_self_masking_gate_unreachable`
  so a future SC2/SC3 relaxation cannot silently make it reachable. (The second
  guard, platform `plan_streaming_route`, lives in decoy-platform and is
  unchanged by SC1; its re-review is recorded here for the cross-repo record.)
- **CF2** - `out_of_core/_batch_join.py` composite partial-null orphan parity
  divergence (documented, module docstring lines ~40-47): gate or explicitly
  accept before SC2 wires the route.
  **RESOLVED → GATED (SC2 part 1, 2026-07-09):** the docstring divergence does
  not reproduce in the canonical `composite_fk_group` shape (proven oracle-parity
  by a 400-trial fuzz + pinned tests across orphans/partial-nulls/all policies).
  It only reproduces when composite-FK child columns are masked as independent
  `scalar` nodes (double-masking): the oracle keeps the scalar-masked value on
  orphans while out-of-core preserved the **raw** value - a raw-value leak.
  Orphans can't be excluded statically, so per the fail-closed default that
  shape is now gated (`out_of_core_composite_fk_scalar_child_unsupported`); the
  parity-proven group shape and single-column scalar children stay admitted.
- **CF3** (dennis SC1 review) - intra-batch int-beyond-2^53 + float FK output
  crashed with an **uncoded** `ArrowInvalid` at `_join.py:304`
  (`_append_output_batch`). The gate admits float-parent/int-child FK edges with
  no dtype check (`_compat.py` `_check_edge`); a batch mixing a matched float
  value with an orphan int key beyond float-precision hit `pa.array(...,
  from_pandas=True)` before `cast_fk_chunk` (CF3's sibling fix from SC1) was
  ever reached. Production-reachable on a gate-admitted config, not just a
  parity-harness gap.
  **RESOLVED (SC2 part 1, 2026-07-09):** chose option (b) - guarded
  `_append_output_batch` the same way `cast_fk_chunk` guards the cross-batch
  cast (the compat gate never sees source dtypes/value ranges, so it can't
  reject this at admission time), raising the coded
  `out_of_core_fk_key_dtype_unsupported` fail-closed rejection instead of the
  raw crash. The pandas oracle's silent rounding on this same shape
  (`9007199254740993` → `9007199254740992.0`, not authoritative) is recorded in
  `tests/parity/SEMANTIC_DIFFERENCES.md`.

All three carry-forwards resolved on PR #37 (branch `sc2/carryforward-hardening`,
open); dennis review pending before merge and before the actual auto-routing
wiring (SC2 part 2) begins.

### SC2 status - DONE (part 1 PR #37 merged; part 2 PR #38 merged 2026-07-09)

**Part 1** (PR #37, merged): the CF1/CF2/CF3 fail-closed hardening above.

**Part 2** (PR #38, merged 2026-07-09): the actual auto-routing wire.

- **Reconciliation of the two routing mechanisms.** The repo had two: the
  planner's `classify_job` (whose `out_of_core_relationship` mode only ever
  recorded `RELATIONSHIP_ROUTE_DEFERRED` and fell through to `pandas_fallback`)
  and `_pipeline_routing.decide_execution_route` (the S2 mechanism that ALREADY
  decides sequential-vs-full_frame for pure-mask FK jobs and is the live surface
  `run_pipeline` early-returns on). Investigation confirmed `decide_execution_route`
  is the **sole live router** for FK jobs -- `classify_job`'s FK branch never
  routes. Decision: **extend `decide_execution_route`** with a third
  `out_of_core` route + a fail-closed reject-before-read; leave `classify_job`
  as the static EXPLAIN/chunked classifier but update its now-stale "FK stack on
  another branch" disposition to point at the live router (`PLANNER_ROUTING_ENABLED`
  stays `False`). Activating the planner's dormant FK branch as a second router
  was rejected: it would duplicate the live decision and drift.
- **Priority order** (`execution_mode="auto"`, pure-mask FK): **out_of_core**
  (`check_out_of_core_compatibility` admits AND largest mask table
  >= `out_of_core_threshold_rows`) > **sequential** (bounded but O(cardinality))
  > **full_frame**. A large relationship job that no bounded route can take
  (not out-of-core-eligible, disqualified from sequential) is **rejected before
  the mask step** with coded `ExecutionError` `fk_full_frame_oom_risk_rejected`,
  never left to a silent full-frame OOM. `execution_mode="out_of_core"` added as
  an explicit fail-closed force (mirrors `"sequential"`); `"full_frame"` still
  forces full-frame and bypasses the reject (operator escape hatch).
- **Thresholds** (per largest mask table, in `_planner`, kwarg-overridable for
  the SC5 estimator): `OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT = 5_000_000`,
  `FULL_FRAME_REJECT_ROWS_DEFAULT = 7_500_000`. Anchored to the documented
  full-frame FK memory model (`docs/relationships-memory-scaling.md` §6:
  `peak_RSS ~= 144 MB + 3.3 MB * rows/1000` for a 3-table width-16 hash chain,
  OOM near ~9M rows/table / ~30 GB on 32 GB). 5M projects to ~16.6 GB (half the
  32 GB box, past the measured 250k-1M out-of-core overhead-regression zone);
  7.5M to ~24.9 GB (~78%, the danger zone below the ~9M cliff with margin for
  wider payloads). Conservative interim constants; the memory-scaling doc is
  explicit that precise box+schema-calibrated MB prediction is SC5's job.
- **Strategy surface unchanged** (SC1's `hash/redact/truncate/passthrough`): an
  unsupported strategy is a routing MISS (stays sequential/full_frame), never a
  run failure. Widening is SC3/SC4.
- **Verification:** ruff / ruff format / mypy clean; full `tests/unit` +
  `tests/parity` green (new: `test_out_of_core_routing.py`,
  `test_out_of_core_routing_parity.py` proving eligible-large routes to
  out-of-core with byte-parity vs the oracle, ineligible-large rejects before
  read, ineligible-small unchanged); memory sentinel unchanged.
- **Module-size BLOCKER (dennis first pass) - RESOLVED.** `_pipeline.py` and
  `_pipeline_routing.py` both breached the ~600 LOC orchestration cap on the
  first fix attempt (653 / 773 LOC). Fixed via genuine decomposition, not an
  allowlist: `_pipeline_route_exec.py` (route executors, 324 LOC) and
  `_pipeline_finalize.py` (post-mask finalize: reproducibility stamps, BF1
  fidelity report, D8 validator/quarantine, 210 LOC) split out. Final sizes
  all ≤600, dennis re-review confirmed the split is a clean separation of
  concerns (decision logic vs execution vs finalize), not a mechanical dodge.

**Carry-forward into SC3 (tracked, not a blocker):**
- **M1** - the `source_loader` lazy-load branch in
  `_pipeline_route_exec.py` (~line 212, reached only via the
  `execution_mode="out_of_core"` forced escape hatch when sources are not yet
  resident) is dennis-verified correct - it resolves the same table set the
  sequential path loads (`table_topo_order(plan, graph)`), the static compat
  gate can't be invalidated by lazy-vs-resident sources, and a loader
  exception fails closed before any sink opens - but **no test exercises it**;
  every current forced-OOC test passes fully-resident sources. Fix: one test
  driving `run_pipeline(..., sources={}, execution_mode="out_of_core",
  source_loader=<fixture loader>)` asserting the loader is invoked for the
  full `table_topo_order` set and reaches byte-parity with the resident
  oracle. Do this before or alongside SC3 so the branch isn't shipped
  untested indefinitely.
  **RESOLVED (SC3, branch `sc3/widen-oocgroup-b`):** added
  `test_out_of_core_group_b_routing.py::test_m1_forced_out_of_core_lazy_source_loader`
  exactly as specced (loader invoked for the full `table_topo_order` set +
  byte-parity with the resident oracle). See "SC3 status" below.

### SC3 status - DONE (branch `sc3/widen-oocgroup-b`, PR pending)

Widened the out-of-core FK route's masked-column (payload) strategy surface from
SC1's `hash/redact/truncate/passthrough`. New per-value kernels live in
`src/decoy_engine/execution/out_of_core/_mask_group_b.py` (split out of `_mask.py`
to hold the ~600 LOC cap); dispatch wired in `_mask.py` (`mask_column` gained a
`column` kwarg for fpe's per-column tweak) and admission widened in `_compat.py`.

**Ported with proven byte-parity (3 of 6):**
- **`fpe`** - reuses `transforms.fpe.fpe_encrypt_value` + the `_fpe` handler's exact
  key model (`derive(job_seed, namespace, FPE_KEY_LABEL)`, column/`fpe_join_group`
  tweak). Per-value, chunks cleanly.
- **`text_redact`** - reuses `storm.detectors.iter_spans` + `_text_redact._splice`
  (incl. the NER model-version pin). Per-cell, chunks cleanly.
- **`categorical` (deterministic)** - reuses `derive_index` over the plan's category
  pool, incl. the weighted-CDF path. Source-conditioned per-value ("conditional"
  = the deterministic source→category remap). Non-deterministic categorical is a
  gate MISS (unseeded RNG has no cross-route parity).

**Scope of admission:** Group (b) strategies are admitted for **masked payload
columns only**. The FK **parent-key** surface (`_check_edge`) stays
`hash/redact/truncate/passthrough` - an FK edge keyed on a Group (b) strategy is a
fail-closed MISS (the join/remap key path is not ported for them). This is the
common case (fpe on an SSN payload, categorical on a status payload, etc.); a
Group (b) join key is rare and falls back to full-frame.

**Documented fail-closed routing MISSes (3 of 6):**
- **`faker`** (`out_of_core_faker_pool_unsupported`) - needs a registry-backed
  `ValuePool` + a cross-batch pool cache; the out-of-core mask kernel is
  deliberately registry-free (backend-neutral per-value). Only deterministic+REUSE
  mode is even parity-able (UNIQUE/MATCH/SCALE/non-deterministic carry cross-row or
  unseeded state). Porting it = threading the registry + a pool cache through the
  streaming rewrite path, a distinct capability beyond dispatch-widening.
- **`bucketize`** and **`date_shift`** (`out_of_core_row_error_strategy_unsupported`)
  - both record **per-value format errors** (uncoercible numeric / unparseable date)
  that the full-frame path removes via the D8 quarantine pass. The out-of-core route
  returns from `run_pipeline` **before** that pass (`_pipeline.py` early-return) and
  `run_fk_out_of_core` has no row-error/quarantine channel, so admitting them would
  make dirty data produce **route-dependent** output (OOC silently keeps the
  original - the exact per-value leak the honesty pack closed - while full-frame
  quarantines). `date_shift` additionally needs whole-column format detection that
  does not chunk. **Reviewer adjudication point:** the clean-data case IS byte-parity;
  the blocker is dirty data + the missing quarantine channel. The correct fix is to
  give the streaming OOC runner a row-error/quarantine channel (its own capability,
  recommend as SC3-followup / an SC4 precondition), after which both admit cleanly.

**M1 carry-forward - RESOLVED.** Added
`tests/parity/test_out_of_core_group_b_routing.py::test_m1_forced_out_of_core_lazy_source_loader`:
`run_pipeline(config, sources={}, execution_mode="out_of_core", source_loader=…)`
asserts the loader is invoked for exactly the `table_topo_order(plan, graph)` set
and the run reaches byte-parity with the resident full-frame oracle.

**Tests:** `tests/parity/test_out_of_core_group_b_parity.py` (adapter-boundary
parity for all 3 ports across orphan policies + a no-op guard + gate-MISS pins for
parent-key/non-deterministic/deferred shapes) and
`tests/parity/test_out_of_core_group_b_routing.py` (run_pipeline routes an fpe
payload to OOC with full-frame parity + M1). Mutation-verified (a shifted
categorical index fails parity). Full `tests/unit` + `tests/parity` green on the
CI-pinned stack (Python 3.10, pandas 2.3.3, numpy 2.2.6) - 0 failures; ruff /
ruff format / mypy / module-size sentry clean.

dennis adversarial review: **APPROVE, 0 BLOCKER / 0 HIGH / 1 MEDIUM / 3 LOW.**
Independently re-verified the fail-closed MISS gating is airtight (positive
whitelist in `_compat.py`, gate/runner enumeration proven identical, defense-in-depth
at runtime) and re-proved byte-parity via live mutation (categorical off-by-one and
fpe tweak-poisoning both correctly fail the parity suite). Endorsed the
defer-3-strategies call as the correct engineering decision. 2 LOW findings (dead
import, doc overstatement) fixed inline. Remaining findings tracked below rather
than blocking merge, per dennis's own "acceptable as carry-forward" framing.

**Carry-forward into SC4 (tracked, not a blocker):**
- **MEDIUM** - the parity suite pins the common config shapes (charset=digits,
  preserve_separators, default/label tokens, plain/weighted categorical) but not
  every live branch of the ported kernels: fpe `validate_luhn`/`checksum`/non-digit
  charsets/`fpe_join_group` tweak; text_redact NER path/detector-subset/non-string
  token; categorical `from_profile`. These reuse identical primitives so almost
  certainly correct today, but a future edit could diverge one branch without the
  parity suite catching it. Fix: extend `_PAYLOADS` with configs exercising each of
  these branches.
- **LOW** - `fpe_join_group` on an out-of-core payload column silently drops the
  full-frame `fpe_join_group_active` QualityWarning (informational only, no data/PII
  impact - `outputs` stay byte-identical). Documented as an accepted gap in
  `_mask_group_b.py`'s module docstring; wiring it needs a static per-column
  emission pass before `_stream_table`'s batch loop, mirroring how
  `orphan_fk_warning` aggregates today.

### SC4 status - DONE (engine PR #43, merged 2026-07-09)

Widened the out-of-core FK route's masked-column (payload) strategy surface from
SC3's `hash/redact/truncate/passthrough/fpe/text_redact/categorical`. New
per-value kernels for Group (c) live in
`src/decoy_engine/execution/out_of_core/_mask_group_c.py` (split out of `_mask.py`
to hold the ~600 LOC orchestration cap).

**Ported with proven byte-parity (3 of 7):**
- **`text_mask`** - reuses `transforms.text_mask.mask_cell` (HMAC-SHA256 keyed
  span masking, RFC 2104; the exact primitive `_strategies/_text_mask.TextMaskHandler`
  calls). Per-value, chunks cleanly, unconditionally admitted.
- **`code_set` (mask mode, no chapter_preserve)** - reuses
  `transforms.code_set.apply_code_set` (HMAC-SHA256-keyed modular selection over
  the code-sorted corpus). Mask mode is per-value; compat gate restricts admission
  to mask mode without chapter_preserve (gen mode threads a global row index the
  streaming kernel lacks; chapter_preserve records per-value errors the route
  cannot quarantine).
- **`bucket_perturb` (explicit date_format)** - reuses
  `transforms.bucket_perturb.apply_bucket_perturb` (HKDF-SHA256 keyed offset).
  Per-value once the strptime format is fixed; compat gate requires an explicit
  `date_format` so no whole-column format detection is needed.

**Documented fail-closed routing MISSes (4 of 7):**
- **`geo_generalize`** (`out_of_core_whole_column_aggregation_unsupported`) -
  k-anonymity cascade thresholds each row on whole-dataset counts, not batch-local.
- **`formula`** (`out_of_core_dynamic_output_type_unsupported`) - emits a value
  whose Arrow type is not determinable from the plan; carries an order-dependent
  RNG channel.
- **`derived`** (`out_of_core_dynamic_output_type_unsupported`) - emits a value
  whose Arrow type is not determinable; needs same-row sibling-column context.
- **`nested`** (`out_of_core_child_dispatch_unsupported`) - reuses the full pandas
  child-strategy dispatch (SCALAR_HANDLERS) plus per-cell JSON; porting needs the
  pandas handler stack per batch with child strategy statically bounded, beyond
  dispatch-widening.

**Scope of admission:** Group (c) strategies are admitted for **masked payload
columns only**. The FK **parent-key** surface stays `hash/redact/truncate/passthrough`
- an FK edge keyed on a Group (c) strategy is a fail-closed MISS (join/remap key path
not ported). This is consistent with SC3 Group (b) scoping.

**Additional change:** Fixed a stale `SUPPORTED_STRATEGIES` public export in
`src/decoy_engine/execution/out_of_core/_compat.py` that was hardcoded to the narrow
FK-parent-key set (`_INITIAL_SUPPORTED_STRATEGIES`) and never widened as SC3/SC4
landed, silently understating what's admitted to decoy-platform's cross-repo query
surface. Now correctly tracks `_SUPPORTED_WORK_STRATEGIES` (the full payload-admitted
set) with corrected docstrings distinguishing it from the separately-gated parent-key
surface at `_check_edge`. This surfaces as the re-exported `OUT_OF_CORE_SUPPORTED_STRATEGIES`
at `decoy_engine.execution` (SC5's platform query surface).

**Tests:** `tests/parity/test_out_of_core_group_c_parity.py` (adapter-boundary parity
for all 3 ported strategies across shapes + gate-MISS pins for deferred strategies) and
`tests/parity/test_out_of_core_group_c_routing.py` (run_pipeline routes ported Group (c)
with full-frame parity + carries M1 and SC3's live-branch coverage work forward).

dennis adversarial review: **APPROVE, 0 BLOCKER / 0 HIGH / 1 MEDIUM / 3 LOW.**
Independently verified fail-closed MISS gating is airtight and re-proved byte-parity.
MEDIUM: fixed the stale `SUPPORTED_STRATEGIES` export (see above); docstrings now
distinguish payload-admitted from parent-key-gated surfaces. 3 LOW coverage gaps
(all-null column, single-row batch, router-level null coverage) added as test
assertions. Full mirror gates green: 5941 tests (3 pre-existing unrelated failures),
ruff/mypy clean, regression-gate green.

**Carry-forwards tracked for SC6 (not blockers):**
- **MEDIUM (SC3 carry, resolved SC4)** - the parity suite now covers live branches
  added by SC4 (text_mask span variations, code_set config shapes, bucket_perturb
  format handling) in addition to SC3's prior coverage.
- **LOW** - quality-warning surface (QualityWarning emissions from `text_mask`'s
  per-detector strategy dispatch, `bucket_perturb`'s timezone inference) are
  intentionally suppressed in the out-of-core route as documented in
  `_mask_group_c.py` (informational only, data parity intact). Wiring them needs
  static per-column aggregation before batch streaming, deferred to a future
  capability enhancement (not a data-correctness issue).

### SC5 status - DONE (engine PR #40, platform PR #20, both merged 2026-07-09)

- **Engine side (PR #40):** thin, additive public re-export at
  `decoy_engine.execution` - `check_out_of_core_compatibility`,
  `OutOfCoreCompatibility`/`OutOfCoreRejection`, `OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT`,
  `FULL_FRAME_REJECT_ROWS_DEFAULT`, `OUT_OF_CORE_SUPPORTED_STRATEGIES`. Zero
  behavior change (identity-verified against the internals the live router
  already calls); additive-only per `docs/compatibility-contract.md` §4.1, no
  version bump needed.
- **Platform side (PR #20):** `api/jobs/admission_fk.py` extends the Sprint
  E/E2 pre-claim admission gate (`api/jobs/admission.py`) with a config-only,
  zero-read structural proxy for "pure-mask FK job that takes platform's
  bounded eviction route." Investigated calling the engine's real
  `check_out_of_core_compatibility` directly and rejected it:
  `decoy_engine/profile/_source.py::_load_file_source` does an unbounded
  full-file read regardless of sample size, which would defeat the point of
  a *pre-read* admission gate. The proxy checks config-visible facts only
  (relationships present, no generate+mask overlap, no validators, no vault
  columns, no self-referential FK edge) and applies a 0.75x multiplier
  discount (the low/conservative end of the measured 25-27% `run_sequential`
  peak-RSS reduction band, `docs/relationships-memory-scaling.md` §6.1).
  GATE-1 invariants (measure-only always runs, fail-open on every new error
  path, admin override untouched, non-FK estimation byte-for-byte unchanged)
  verified intact by dennis.

**Significant finding (dennis-verified, not just asserted):** decoy-platform's
own `v2_sequential.py` (`_should_use_sequential_relationship_path`) already
intercepts *every* pure-mask FK job before `run_pipeline` is ever called,
routing it through the engine's `run_sequential` table-by-table eviction
instead. The only relationship shape that *does* reach `run_pipeline`
(generate+mask FK) is disqualified from the engine's out-of-core route by the
engine's own eligibility rules (`_sequential_eligible`'s `has_generate_table`
check - out-of-core eligibility is a strict subset of sequential eligibility).
**Net effect: decoy-engine's SC1/SC2/SC3 out-of-core route is not reachable
through this platform's job runner today, for any config shape.** The SC5
admission discount is therefore pinned to the already-realized sequential-tier
number, not the engine's stronger but currently-unreachable out-of-core bound
- the honest number for what actually runs in production today.

**New follow-up surfaced (not started, not part of SC0-SC6):** wire platform
to route large, out-of-core-eligible FK jobs through the engine's DuckDB
route instead of always taking the sequential-eviction path first. Until this
lands, all of SC1/SC2/SC3's out-of-core engineering investment delivers zero
production value through this platform - it's real, tested, and byte-parity
proven at the engine level, but currently unreachable end-to-end. Flag for
Cam: this is arguably higher-priority than SC4/SC6 once the current program
finishes, since it's what makes SC1-SC3 actually matter in production.

**Cam's call (2026-07-09):** backlog this - finish SC3 → SC4 → SC6 on the
original critical path first, then scope/build the platform-wiring follow-up
as its own item once this program wraps and SC6's real GCP numbers exist to
design against, rather than inserting it ahead of SC4 now.

**Known accuracy gaps tracked for GATE-2** (dennis review, fixed where cheap,
tracked where not): the 25% discount is measured only on >=3-table FK chains
and may be optimistic for the more common 2-table parent-child shape; the
underlying `CSV_MEMORY_MULTIPLIER` (6.0x) is itself provisional against a
single 8.05x real sample. Both compound in the same direction (under-estimate
risk) and are mitigated today by measure-only-by-default + fail-open, but
must be closed before `admission_control_enabled` enforcement is turned on.

### Deliverables already shipped alongside this program
- **PR #33** - the 100M program doc (`docs/plans/2026-07-06-100m-row-scaling-program.md`).
- **PR #19 (platform)** - the GCP benchmark harness (`scripts/gcp-bench/`:
  `engine-bench.sh`, `remote-engine-bench.sh`, `BENCHMARK-QUESTIONS.md`). Ready
  to run once auth + spend are approved (this is Part B item 1's tooling).

---

## Part B - Next to complete once we return (Cam, 2026-07-07)

Four cross-repo items to complete after the scaling program. Ordered as listed.

### 1. GCP benchmark + testing
Run the real scale/memory/OOM battery on GCP and record the numbers. Tooling is
already built (platform PR #19 `scripts/gcp-bench/engine-bench.sh` - spins up a
32 GB `n2-standard-8`, ships the engine as a git bundle, runs the
`fk_memory_probe` battery B1-B5, tears down). This **is** SC6 plus the broader
battery that answers the original benchmark questions (largest job, concurrency,
OOM signature, 50M/100M wall-clock).
- **Blocked on run, not build:** no active gcloud account (`gcloud auth login`),
  a project, `--confirm-cost`, and the base image (`build-base-image.sh`).
  Building is free; running costs money. Benchmark against a **named ref**
  (post-SC1 branch, then post-SC2 main), not "the repo".
- **Depends on:** SC1 merged (for the opt-in OOC numbers), ideally SC2 (for the
  auto-routed numbers). Feeds SC5's estimator calibration.

### 2. Wire up UI to engine
Wire the platform Job Studio UI end-to-end against the now-complete backend
(the finish-open-ended program S1-S10 completed the half-built surfaces the UI
needs). Known gap to close: the fixed-width authoring fork (pipeline builder
still authors fixed-width via the legacy `FwDefinition`, so a UI pipeline-source
can't yet reference a saved `FixedWidthLayout` - API+engine layer works + tested,
UI picker deferred). See `decoy-platform` and the UI-redesign notes (function-box
"Job Studio" wizard).
- **Repo:** decoy-platform (+ decoy-web for the front end).

### 3. CLI testing
Build out test coverage for the decoy CLI (`docs/cli.md` surface). Verify the
CLI paths - chunked single-table route, config validation, typed-error behavior
- hold under the post-scaling engine. Establish the CLI as a first-class tested
surface, not just an incidental entrypoint.
- **Repo:** decoy CLI / engine.

### 4. Web UI/UX testing + remapping
Test and remap the decoy-web UI/UX: audit the current flows against the completed
backend + new Job Studio model, fix UX gaps, and remap navigation/IA where the
product has moved (e.g. surfaces added by S8-S10, the /proof page, quality
panel). Includes visual/interaction QA.
- **Repo:** decoy-web.

---

## Part C - External consultant review findings (2026-07-09)

An external architecture review (`docs/engine-consultant-findings-2026-07-09.md`,
Codex/gpt-5.5, reviewed at main `02b18cc`) surfaced 9 findings. Every finding
spot-checked against the code was accurate (F1, F3, F4, F8 independently
re-verified file:line by Claude before any of this was actioned). Status:

| Finding | Severity | What | Status |
|---------|----------|------|--------|
| **F1/F2** | HIGH | `run_pipeline()` profiles (eagerly, full pandas read) BEFORE route selection, so a huge file/S3/GCS-backed FK job can OOM during profiling before ever reaching the bounded-memory out-of-core route. **Confirms and generalizes the SC5 platform finding above** ("`_load_file_source` does an unbounded full-file read... would defeat the point of a pre-read admission gate") - that was platform working around this exact engine-level gap rather than it being fixed at the source. Note: the GCP bench harness (SC6 / this program) calls `run_fk_out_of_core`/`run_fk_sequential` directly, bypassing `profile_source()` entirely, so the 50M/100M benchmark numbers are NOT proof this is fixed - they validate the execution route, not the public `run_pipeline()` entrypoint most callers use. | **IN PROGRESS** - design doc being written (Opus tech-lead pass) at `docs/plans/2026-07-09-consultant-f1-f2-bounded-profiling.md`. Do not claim `run_pipeline()` is bounded-memory on the proof page until this lands. |
| F3 | MEDIUM | Execution routing decision surface is split across `classify_job()` (mostly inert, `PLANNER_ROUTING_ENABLED=False`) and `decide_execution_route()` (the actual live router) - drift risk as more strategies/substrates land. | Backlog. Real refactor (consolidate into one `ExecutionPlanner`), not a quick fix; scope as its own sprint after F1/F2 lands (touches the same modules). |
| F4 | MEDIUM | `SchemaInspector`/`LicenseVerifier` are public stub exports with no GA release gate. | **DONE** - `tests/sentry/test_ga_stub_exports.py` added; fails at GA flip unless resolved. Branch `sc7/consultant-f8-f4-cleanup`. |
| F5 | MEDIUM | Polars is not a full native execution substrate; many strategies port through pandas (`PandasStrategyPort`), which affects performance expectations if surfaced as "Polars substrate" without qualification. | Backlog. Needs a machine-readable per-strategy substrate matrix + telemetry distinguishing native/ported/fallback. |
| F6 | MEDIUM | Out-of-core is a well-gated strategy subset, not a general route; external callers could overestimate eligibility from strategy membership alone. | Backlog, but low-urgency - this accurately describes the deliberate SC1→SC3→SC4 incremental widening, not a design mistake. Worth a public admission API once the strategy surface stabilizes. |
| F7 | MEDIUM | No DB/SFTP sources/targets (file/S3/GCS only) - explicitly deferred in code comments already ("SFTP rides S18; DB rides V2.1"). | Backlog, already-known scope boundary, not new information. |
| F8 | LOW | Stale `decoy_engine.graph.*` mypy overrides for a removed module tree. | **DONE, and widened** - the sentry test written to guard this (`tests/sentry/test_mypy_override_targets.py`) found 13 MORE dangling overrides beyond the graph.* ones the review sampled (V1 `connectors`/`generators`/`masker`/`transforms`/`plan` modules deleted by the S9.5 bulk-delete, never cleaned from `pyproject.toml`). All removed, verified against `git log --diff-filter=D`, mypy still clean (327 files). Branch `sc7/consultant-f8-f4-cleanup`. |
| F9 | LOW | Module-size ratchet allowlist has 5 large files in risk-heavy areas (detection, quality reporting, training, plan checks). | Backlog - ratchet already prevents further growth (see `tests/sentry/test_module_size.py`); this is "decompose eventually," not urgent. Prioritize `plan/_checks.py` if picked up (compile checks are fail-closed-safety-central per the review). |

### SC7a status - F8 + F4 cleanup - DONE, branch `sc7/consultant-f8-f4-cleanup`

Two independent, low-risk, mechanical fixes bundled into one branch since both
are sentry-test additions with no behavior change:
- F8: removed 21 total dangling `[[tool.mypy.overrides]]` entries (7 `graph.*`
  from the review + 13 more the new sentry test found: `connectors.{csv_connector,
  database,factory,fixed_width}`, `generators.{generator,relationships}`,
  `internal.{large_file_processor,validator}`, `masker.masker`,
  `plan._registry_stub`, `transforms.{categorical,faker_based,hash,registry}`).
  Every removed entry verified against `git log --diff-filter=D` before deletion.
  New sentry: `tests/sentry/test_mypy_override_targets.py`.
- F4: new sentry `tests/sentry/test_ga_stub_exports.py` - pins the current
  `SchemaInspector`/`LicenseVerifier` stub registry pre-GA, fails hard at the
  `RELEASE_PHASE` flip to `"ga"` unless each entry is resolved and removed.

Verification: `mypy src/decoy_engine` clean (327 files), full `tests/sentry/`
suite green (1403 passed / 1 skipped), ruff + ruff format clean.

### SC7b status - F1/F2 bounded-memory profiling fix - IN PROGRESS

Design pass in flight (Opus tech-lead), scoped to F1/F2 only (not F3's broader
routing consolidation, even though they touch adjacent modules - kept separate
so the fix that actually matters for the proof-page claim isn't blocked on a
bigger refactor). See the design doc once written:
`docs/plans/2026-07-09-consultant-f1-f2-bounded-profiling.md`.

## Resume checklist

1. ~~**SC1** - execute the prove-or-reject hardening pass on PR #34~~ **DONE**,
   merged 2026-07-09.
2. ~~**SC2** - resolve CF1 + CF2 + CF3, wire auto-routing.~~ **DONE** (part 1
   PR #37; part 2 PR #38, both merged). M1 (untested lazy-loader branch)
   carried forward, see "SC2 status" above.
3. ~~**SC3 → SC4** - widen the strategy surface (parity-tested).~~ **DONE** (SC3
   branch `sc3/widen-oocgroup-b` merged; SC4 PR #43 merged 2026-07-09). M1 carried
   forward and resolved.
4. ~~**SC5** (parallel) - platform estimator + admission gate, measure-only.~~
   **DONE** (engine PR #40, platform PR #20, both merged). Surfaced a new,
   unscoped follow-up: wire platform to actually route large FK jobs through
   the engine's out-of-core execution mode - see "SC5 status" above.
5. **SC6 / Part B item 1** - GCP 100M benchmark run (needs auth + spend).
6. **Part B items 2-4** - UI-to-engine wiring, CLI testing, web UI/UX.
