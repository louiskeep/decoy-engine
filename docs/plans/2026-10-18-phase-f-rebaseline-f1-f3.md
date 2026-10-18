Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev,
architecture, performance, testing, verification

# Phase F re-baseline: one generation oracle, then F1 + F3

Supersedes the byte-parity-to-F0 contract that parked the F1 rev-3 plan
(`docs/plans/2026-10-16-phase-f-f1-bounded-generation.md`, recovered from engine
commit `d2a80544`) and bounded F3 (`docs/plans/2026-10-16-phase-f-program.md`
§F3). Cam re-sequenced Phase F on 2026-10-17 (platform
`docs/backlog/v2-candidate-plan.md` "Phase F re-sequence") and chose the
re-baseline direction on 2026-10-18, with three design calls settled the same day
(§1). F0's contract, witness and comparator are reused unchanged; only the frozen
golden is re-based.

Rev-2 folds the Codex plan-gate (NO-GO, 9 P1 + 1 P2). The central correction: the
generated VALUES are chosen by semantic eligibility alone, never by the execution
environment (§2). This is a production generation change; the guards are
reproducibility, route-independence, semantic integrity, and bounded memory, not
equivalence to today's resident bytes.

## 1. The contract (whole-PLAN, the one decision)

Today F0 froze the whole-table generation output as the golden, and F1/F3 had to
match it byte-for-byte. That exact-seed / exact-schema constraint repeatedly
failed the plan-gate because streamed and compiled-kernel generation cannot
reproduce the old resident draws bit-for-bit. Re-baseline replaces the guarantee:

- **OLD:** streamed/compiled output == today's resident output, byte-for-byte.
- **NEW:** generation output is REPRODUCIBLE and ROUTE-INDEPENDENT. For a fixed
  PINNED INPUT ENVELOPE, every run and every route emits identical bytes, and that
  output is re-frozen as the new golden. It need not equal the pre-re-baseline
  resident bytes.

**Pinned input envelope (plan-gate #8).** Reproducibility is asserted only over an
explicit, complete input set: `(job seed, config, parent key sets, pinned inputs
[statistical_specs / provider snapshots], derive_key, per-column locale, engine
minor version)`. Two guarantees follow and are tested separately:
- Repeatability: same envelope, two FRESH PROCESSES, identical bytes (not merely
  two in-process runs).
- Route-independence: same envelope, resident route == streamed route, identical
  bytes.
EXCLUDED by definition: `determinism=="fresh"` columns (deliberate `os.urandom`);
they are never asserted reproducible and never re-frozen. Clock-dependent admitted
date providers are asserted only under a controlled clock, or they decline.

Three calls settled with Cam (2026-10-18, recorded in
`memory/decoy-c4-chunked-date-shift-state.md` cont-7):
- **(A) One golden, not two.** Resident and streamed collapse to a single
  route-independent deterministic output, mirroring masking's cross-route
  byte-identity (the D-audit result). Achieved by construction (§2).
- **(B) Aggressive F3.** Pooled, categorical and sequence SELECTION move onto the
  compiled kernel and re-freeze emitted values, distribution-preserving per the
  per-mode rules in §4. Faker custom providers, formulas and statistical stay
  Python.
- **(C) Contract pinned to the engine minor version.** Within a minor version, the
  envelope gives identical bytes. All value-changing re-baseline slices target ONE
  unreleased minor and land atomically (§7); the golden may be re-frozen across
  minor versions while pre-1.0.

## 2. Architecture: semantic eligibility vs streaming feasibility (plan-gate #1)

Two independent decisions, never conflated:

1. **Semantic eligibility (chooses the VALUES).** Decided from the plan + minor-
   version contract ALONE. A column is eligible when its generator can produce the
   deterministic re-baselined output under persistent per-batch state (§3). This
   decision is byte-deterministic and does not read the budget, the sink, or the
   host. For a plan where every column is eligible, the VALUES are fixed, period.
2. **Streaming feasibility (chooses the EXECUTION MODE, never the values).** Given
   eligible values, decide HOW to materialize them:
   - bounded sink present + byte budget fits -> stream batches to the sink;
   - else -> accumulate the SAME batches in memory (resident) and concat;
   - if even resident accumulation would exceed the hard ceiling -> FAIL EXPLICITLY
     (a declared `GenerationBudgetExceeded`), NEVER silently emit different values.

So `generate_tables_streamed(plan, *, sink, budget_bytes=None, ...)` is the single
core (whole-plan admission, persistent per-column RNG threaded by global row
offset [F1 rev-3 §4], enforced byte budget [§3 here]). `generate_tables` runs the
SAME core into an in-memory accumulating sink and concats. The resident
accumulating sink is an EXPLICIT exemption from the `bounded_write_batches`
requirement (it is the in-memory mode, not a Parquet sink); its memory is charged
and bounded by §3, and it is stated plainly as OUTSIDE the bounded-OUTPUT
guarantee (output is the whole table in RAM by design).

**Legacy whole-table generation runs ONLY for semantically INELIGIBLE columns.**
Eligibility is whole-plan: a plan with any ineligible column takes the legacy path
for that plan (F1 rev-3 §1 whole-plan rule, kept), and legacy output is itself the
golden for those columns. The admitted-vs-legacy boundary is therefore a pure
function of the plan, and every column lands in exactly one path regardless of
host. Mixed generate-then-mask (`_pipeline_generate_mask.py`) consumes the
resident result as today; because resident == streamed for eligible plans, mixed
jobs inherit the re-baselined values with no separate freeze.

## 3. F1 under re-baseline: admission, honestly bounded

Admitted now (F1 rev-3 set, prefix-stable, unchanged): sequence, categorical,
faker (per-row and pooled), null_mask, reference WITHOUT cardinality repair. Under
the old contract these could not STREAM (streamed draws differ from the old
whole-column draw); re-baseline lets them stream because the streamed output is
now the golden. That is the bulk of the near-term win and it needs no new family
design.

**Newly admitted in FR1 (one family, specced exactly):**
- **windowed_date:** admit by REUSING its existing global `row_offset` support
  (`transforms/windowed_date.py:182`), not an invented "anchor/offset state."
  Spec the anchor derivation, null handling, and out-of-window error behavior
  explicitly; the window offset is computed relative to each row's own anchor.

**Deferred to their OWN follow-up slices (plan-gate #2, #3), NOT admitted here:**
- **grouped_cumcount / grouped series / walk:** these sort by group + `order_by`
  then restore original positions (`transforms/grouped_series.py:218`, `:257`). A
  later batch can contain an earlier sort key, so arrival-order counters assign
  the wrong ranks and a shared-but-wrong core passes every partition-equality
  test. They REQUIRE a bounded sort / two-pass design before admission. Stay
  declined until that slice lands.
- **reference WITH min/max_per_parent repair:** existing repair revisits earlier
  assignments to satisfy deficits and remove excess (`generators/columns.py:554`);
  running counters cannot repair already-published rows. Requires an online
  allocation algorithm (known total child count, remaining deficits, remaining
  capacity, deterministic selection, defined impossible-constraint behavior, and a
  defined before-or-after-null-injection rule) before admission. Stay declined.

**Still declined (unchanged):** formula / referenced_formula (whole-column pandas
coercion), derived_aggregate, statistical, `determinism=="fresh"`. Custom /
value-inferred Faker providers: admit ONLY when the provider supplies an EXPLICIT
declared schema AND a deterministic-output contract; a probe is diagnostic
evidence only, never a type guarantee (plan-gate #5: a provider can return strings
during the probe and another type later, and probing a stateful provider changes
its subsequent output). Otherwise decline. Deliver the TOTAL admission matrix over
every generator kind with its reason.

**Memory, actually enforced (plan-gate #6).** The byte budget (F1 rev-3 §3) is
kept AND hardened:
- Preflight RESERVATION uses conservative upper bounds, not benchmark-friendly
  estimates: parent-key pools by parent `row_count` upper bound; any per-group
  store (when grouped families later land) by a conservative group-count and
  key-width bound plus actual per-group RNG/state bytes; Faker width from a bound
  that covers variable-width values, not just a short probe, or the column
  declines.
- All simultaneously-live stores are summed over the run lifetime alongside batch
  buffers and the sink/accumulator copy.
- Decline-to-resident happens BEFORE execution. Once batches are being produced,
  exceeding the reservation does NOT silently fall back; it aborts transactionally
  (`GenerationBudgetExceeded`), staging removed. Post-start fallback is forbidden
  because it would change the execution mode mid-run.

## 4. F3 under re-baseline: kernel selection, per generator semantics (plan-gate #4)

F0's inventory finding holds: pure generation draws from seeded NumPy / Python
RNG, the compiled kernel derives indices via HMAC; adopting the kernel changes
emitted values. Re-baseline permits the change and re-freezes, but ONLY where the
generator's SEMANTICS are selection-from-a-set. The inventory must classify each
generator by mode, because a blanket "uniform HMAC selection" would corrupt two
supported shapes:
- **Arithmetic sequence** (`generation/synthesize.py:306`): value is
  `format(start + global_row * step)`; it is NOT pool selection. Uniform selection
  would destroy ordering and uniqueness. Preserve the arithmetic via an
  appropriate compiled arithmetic op over the global row index, not a kernel draw.
- **Weighted categorical** (`generation/synthesize.py:359`): a configured 90/10
  split must stay 90/10. Uniform HMAC selection would flatten it to 50/50. Either
  define a weight-preserving kernel mapping (HMAC -> weighted index) or keep
  weighted cells on Python; uniform-only cells may adopt the uniform kernel.
- **Uniform pooled selection** and uniform categorical: adopt the compiled HMAC
  kernel (the genuine win).
- Pool/category CARDINALITY modes are enumerated and each gets an explicit
  admit-or-decline.
POOL CONSTRUCTION stays Python; the kernel selects indices, it does not build the
Faker pool. Faker custom providers, formulas and statistical stay Python. Admitted
columns record the compiled backend via the F0 witness; a uniformity / weight test
(§6) proves the distribution is preserved; the Python reference stays reachable
only for non-admitted / companion-absent cells.

## 5. The re-freeze procedure + SEMANTIC gates (plan-gate #7)

Re-basing a golden is a deliberate, reviewed, atomic step, never an auto-overwrite.
Marginal distribution checks are NECESSARY but NOT SUFFICIENT: permuting a grouped
result preserves its histogram while breaking `order_by`; independently permuting
composite fields preserves each marginal while breaking name/email coherence; FK
membership and per-parent constraints are invisible to row-count and min/max. So
the gate adds independent SEMANTIC assertions that two runs of the same broken
algorithm cannot pass.

1. **Regenerate** every affected golden with the re-baseline engine under the
   pinned envelope.
2. **Semantic gate (the real protection):** for each re-frozen table assert, as
   applicable: FK membership (every child key in the parent set), parent-child
   cardinality constraints, composite coherence (name/email and person fields
   consistent), sequence rules (order + uniqueness + step), grouped ordering/steps
   (once grouped families land), window offsets relative to each row's anchor,
   null propagation, and marginal distribution within declared tolerances against
   the CONFIGURED distribution (not merely one old sample). Non-vacuity is proven
   with mutants that preserve marginals but break a relation (permuted group,
   permuted composite field, dropped FK member) AND a frequency-bias mutant; each
   must make the gate fail.
3. **Repeatability + route-independence re-run:** regenerate twice in fresh
   processes and once per route; assert byte-identical before freezing.
4. **Freeze with a manifest** recording the engine minor version, the envelope
   digest, and the re-freeze date (the (C) anchor).
5. The re-freeze and its semantic harness ship as one reviewed, atomic commit with
   the implementation (§7).

## 6. Acceptance

- **Route-independence (A):** every eligible fixture, streamed == resident,
  byte-for-byte, reassembled across SEVERAL IRREGULAR partitions (pooled-threshold-
  crossing, all-null, zero-row). Property-based (Hypothesis) over partition
  layouts, not only hand-picked splits.
- **Repeatability (contract):** every fixture regenerated in a FRESH PROCESS is
  byte-identical; an interleaved-run test (two generations advancing together)
  confirms no shared mutable state; the manifest's pinned minor version matches the
  running engine.
- **Fresh exclusion:** a `determinism=="fresh"` fixture is asserted NON-reproducible
  and is never re-frozen (guards against over-broad reproducibility claims).
- **Semantic re-freeze gate (§5):** passes for every re-frozen table; the
  relation-breaking and frequency-bias mutants each fail it.
- **Decline path:** a plan with each declining family / schema / provider case
  returns `mode="resident"` with the right reason and the SINK UNTOUCHED (zero sink
  writes); its resident output is the legacy whole-table output and is itself
  reproducible. A budget-too-small-even-for-resident case raises
  `GenerationBudgetExceeded` and does NOT emit alternate values.
- **Bounded RSS, non-vacuous:** the generation bench worker
  (`scripts/fk-scale/_supervisor.run_worker`) streams into a
  `ParquetTransactionalSink` at TWO tiers (5M + 10M) under the same budget and
  frozen batch size, asserts EXTERNAL peak RSS under a predeclared ceiling and not
  materially growing tier-to-tier, committed rows == n. A UNIQUE-group / long-key /
  wide-value stress case confirms the reservation declines or aborts instead of
  overrunning.
- **F3 selection:** admitted columns record the compiled backend (F0 witness); a
  uniformity test for uniform cells AND a weight-preservation test for weighted
  categorical; a sequence test for order/uniqueness/formatting; a consumed-output
  backend mutant proves the compiled RESULT (not merely the call) supplied the
  output; the Python reference proven reachable only for non-admitted /
  companion-absent cells; measured speedup on a pooled-generation job.
- **Mixed-path (plan-gate #10):** a production generate-then-mask test through
  final stitching and publication (generated-parent / masked-child AND a
  `pregenerated` execution), comparing CANONICAL ARROW values AND schema metadata,
  not serialized Parquet bytes (mask output wins ties during stitching, so
  changing `generate_tables` alone is not sufficient proof).
- **Transactional abort:** failure at a later table, writer-close, and commit each
  abort with no partial commit, staging removed, original error preserved.
- **Pinned-input + pre-provider validation** preserved on the unified entrypoint.
- **ci-mirror clean** (ruff + mypy on `src/decoy_engine`); companion and
  no-companion green. Physical-seam sentry: add any flagged `execution/` module to
  `permitted_non_physical` with rationale.

## 7. Slice order + minor-version atomicity (plan-gate #9)

All value-changing slices target ONE unreleased minor version; each re-freeze
lands as an atomic merge unit (implementation + fixtures + manifest + semantic
gates together). No intermediate merge may leave the implementation and the golden
disagreeing. Overlapping golden updates are serialized and the gates re-run against
the exact combined artifact.

1. **FR0 - unify + contract + re-freeze core.** The §2 semantic-vs-feasibility
   split; resident re-pointed at the batch core for the CURRENT eligible set (no
   new families, no F3); the §5 re-freeze procedure and semantic harness; the
   pinned envelope + fresh exclusion; route-independence + repeatability
   acceptance. Contract change in isolation, green before anything widens.
2. **FR1 - widen F1 admission: windowed_date only** (specced per §3), re-frozen
   per §5. Grouped families and reference-with-repair stay DEFERRED to their own
   design slices (§3).
3. **FR3 - kernel selection** per the §4 per-mode inventory (arithmetic sequence
   preserved, weighted preserved or Python, uniform adopted), uniformity + weight
   proofs, witness, re-freeze. After FR0; may overlap FR1 with serialized golden
   updates.
4. **FR-capacity hook.** Confirm the generation capacity slice (program §F4) rides
   the re-baselined core; bounded tiers, no new freeze.

Follow-ups (own slices): bounded sort / two-pass for grouped families; online
allocation for reference-with-repair; bounded parent-key store (remove the
O(parent-keys) reference limit); the composite in-process repeatability test both
F0 reviewers flagged (land BEFORE FR3 touches composite state). Restore or
immutably link the inherited F1 rev-3 doc (engine `d2a80544`) so its reused
obligations stay reviewable.

## 8. Risk + gate chain

Risks: (a) a re-freeze that silently masks a real regression - guarded by the §5
SEMANTIC gates + mutants, not a byte diff or marginals alone; (b) an environment-
dependent value divergence - removed by construction via the §2 semantic-vs-
feasibility split; (c) a mis-admitted family whose cross-batch state is not truly
prefix-stable - guarded by route-independence across irregular partitions, and by
deferring the families that need a two-pass / allocation design; (d) F3 selection
that skews a distribution - guarded by the per-mode uniformity / weight tests;
(e) an unbounded run - guarded by conservative preflight reservation + transactional
abort (no post-start fallback).

Gate chain: Codex plan-gate (this doc) -> build each slice (Sonnet) -> ci-mirror +
suites + the generation bench + semantic re-freeze harness -> dennis -> Codex
final -> merge on green + Slack. DOCUMENT per slice: roadmap + shipped-log,
program plan F1/F3 status, engine graph refresh.

cam
