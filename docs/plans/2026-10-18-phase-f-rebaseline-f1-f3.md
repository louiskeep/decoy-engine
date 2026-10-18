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

Rev-4 folds three Codex plan-gate rounds (r1 NO-GO 9 P1 + 1 P2; r2 NO-GO 2 P1 + 1
P2; r3 NO-GO 1 P1 + 1 P2). r1/r2 findings and r3's #8 are resolved. Rev-4 closes
r3's last two: the unbounded-allocation ESCAPE through the resident/legacy path (a
column lacking a defensible bound is now rejected on EVERY path before provider
invocation, and the bench supervisor is explicitly containment, not production
enforcement, §3), and the windowed_date null/error behavior is now stated as an
INTENTIONAL semantic change applied to both paths, not a false preservation claim
(§3). The central correction from r1 stands: generated VALUES are chosen by semantic
eligibility alone, never by the execution environment (§2). This is a production
generation change; the guards are reproducibility, route-independence, semantic
integrity, and enforced bounded memory, not equivalence to today's resident bytes.

NOTE: rev-4 was authored at the 3-round review cap. The remaining fixes are
unambiguous engineering corrections (no product or multi-option call), so they are
applied here; a confirming gate round is Cam's to authorize rather than run
automatically.

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
[statistical_specs / provider snapshots], derive_key, RESOLVED effective per-column
locale [including `instance_default_locale`], controlled-clock inputs for any
admitted date provider, engine minor version)`. Two guarantees follow and are
tested separately:
- Repeatability: same envelope, two FRESH PROCESSES, identical bytes (not merely
  two in-process runs).
- Route-independence: same envelope, resident route == streamed route, identical
  bytes.

**Reproducibility eligibility is a THIRD, independent axis (plan-gate #8),** distinct
from semantic eligibility (§2) and streaming feasibility (§2). A column is
reproducibility-EXCLUDED when its output is not a deterministic function of the
envelope, and exclusion propagates over the DEPENDENCY CLOSURE:
- Directly excluded: `determinism=="fresh"` (deliberate `os.urandom`); a custom
  provider WITHOUT a deterministic-output contract (a pinned callable snapshot does
  not freeze the provider's mutable state, clock reads, or external randomness); a
  clock-dependent provider run WITHOUT a controlled clock.
- Transitively excluded: any column (reference, formula, referenced_formula,
  derived_aggregate, composite) whose inputs include an excluded column.
- The exclusion applies REGARDLESS of path: a legacy-executed (semantically
  ineligible) column that is also reproducibility-excluded is still excluded.
Excluded columns are NEVER re-frozen into a golden and NEVER carry a reproducibility
or route-independence assertion. Everything else (the reproducibility-eligible set)
is re-frozen and asserted. A clock-dependent date provider is reproducibility-
eligible ONLY under a controlled clock recorded in the envelope; otherwise it is
excluded.

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

**Newly admitted in FR1 (one family, fully specced below, plan-gate #3):**
- **windowed_date**, admitted ONLY under this exact scope (anything outside it
  declines). The design, not an instruction to design:
  - *Anchor availability.* The anchor is a SAME-TABLE column that is itself
    reproducibility-eligible and ordered BEFORE the windowed column in the plan, so
    both co-compute within the same batch from the same global row offset. A
    cross-table or later-ordered anchor declines (no sibling-input seam is built
    here; that is a follow-up).
  - *Supported anchor types (AMENDED 2026-10-18, FR1 build):* a parseable STRING
    anchor (the realistic case), plus `date32` and `timestamp` (tz-naive). Other
    types decline. RATIONALE for the amendment: no generated column the core admits
    produces `date32`/`timestamp` (sequence and faker are `string`, categorical is
    string/bool/int64/float64, reference copies one of those, windowed_date itself is
    `string`), so a `date32`/`timestamp`-only rule would never fire; and the
    `WindowedDateError` "unparseable anchor" rule below only has meaning for a string
    anchor (a typed date cannot be unparseable). The common real shape is a date
    windowed off a string date column, e.g. an event date off a str `date_of_birth`.
    A string anchor is parsed, but NOT by unrestricted `pd.Timestamp` (plan-gate
    HIGH, 2026-10-18): raw `pd.Timestamp` accepts wall-clock forms like `"now"` /
    `"today"` and timezone-bearing / ambiguous strings, which would make output
    non-reproducible and route-dependent. REQUIRED: accept only an ABSOLUTE,
    UNAMBIGUOUS, timezone-naive string in a fixed grammar (full numeric ISO date
    `YYYY-MM-DD` or ISO datetime, no timezone, no relative/partial/locale forms),
    VALIDATE the string against that grammar BEFORE parsing, and reject every other
    form identically on both paths. The existing `apply_windowed_date` conversion and
    string formatting (`transforms/windowed_date.py`) carries over for the accepted
    grammar; the offset is computed relative to each row's own anchor via the existing
    global `row_offset`.
  - *Output schema:* `string`, matching the current formatter. No inference.
  - *Null / invalid anchor (INTENTIONAL SEMANTIC CHANGE, plan-gate #3).* Today's
    `apply_windowed_date` (`transforms/windowed_date.py:224`) unconditionally
    converts, samples, and formats, with no null branch and no error wrapper. FR1
    deliberately CHANGES that: a genuine NULL anchor (an actual null cell, not a
    string) PROPAGATES to a null output (deterministic, no draw consumed for that
    row). A non-null anchor that fails the grammar above, parses to `NaT` (including
    `""` and `"NaT"`, which `pd.Timestamp` turns into `NaT`), or whose date falls
    outside the declared SUPPORTED RANGE or overflows during the window arithmetic or
    formatting RAISES a declared `WindowedDateError` (fail-closed, not a silent clamp).
    A successful parse is NOT sufficient (plan-gate MEDIUM): distinguish an actual null
    from a non-null string that parses to `NaT`, and declare a stable supported date
    range so overflow is caught deterministically. All of this lives in the shared
    `apply_windowed_date` so BOTH the unified batch core AND the legacy whole-table
    path behave identically (sharing the helper is necessary but the parity must still
    be TESTED, not assumed). It lands under the one re-baseline minor (§7) and is
    re-frozen with the golden.
  - *Derivation namespace:* the offset RNG is keyed by the existing per-column
    namespace plus the global row index (unchanged from the resident path), so
    streamed == resident by construction.
  - *Compatibility assertions:* the admission matrix records windowed_date as a
    CHANGED-semantics family; acceptance (§6) asserts the new null/error behavior on
    both paths and the pre/post diff is captured in the re-freeze manifest.

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

**Memory enforcement mechanism (plan-gate #6).** The byte budget is not just an
admission estimate; it is a run-scoped ledger that gates every bounded allocation
BEFORE it happens, backed by a runtime RSS ceiling.

- *Budget owner:* a run-scoped `GenerationBudgetLedger` created from the resolved
  ceiling (`execution/out_of_core/_budget.resolve_budget` +
  `detect_effective_memory_bytes`). It owns the remaining-bytes counter for the
  whole generation run and is the single place memory is charged and released.
- *Reserve-before-allocate lifecycle.* Each bounded STAGE calls
  `ledger.reserve(stage, conservative_upper_bound)` and only allocates AFTER the
  reservation succeeds; on success the bytes stay charged for the stage's lifetime
  and are released when it ends. The bounded stages, each charged before it runs:
  (1) Faker pool CONSTRUCTION; (2) parent-key pool build; (3) per-batch Arrow
  conversion buffer (batch_rows x per-column width, plus the Python-list and
  null-copy overhead); (4) the resident ACCUMULATOR (whole-table footprint) or the
  one-row-group sink buffer. Overlapping live buffers (Python + Arrow + conversion +
  sink/accumulator) are summed; a stage whose reservation would exceed the remaining
  budget REFUSES before allocating.
- *No defensible bound = rejected on EVERY path, before provider invocation
  (plan-gate #6, r3).* A column that cannot produce a conservative upper bound (a
  Faker provider whose value width is not boundable, a parent pool of unknown size)
  is NOT merely declined from streaming while legacy runs it on an estimate. Under a
  bounded ceiling there is NO estimate-based escape: such a column makes the whole
  plan fail the bounded contract at admission with a declared
  `GenerationUnboundedError`, before the provider is ever called. The only paths that
  execute are ones where every column has a defensible bound. (An unbounded ceiling,
  i.e. no `budget_bytes`, is a separate explicit mode that carries no bounded
  guarantee and is never auto-selected for a bounded request.)
- *Resident and legacy use a DEFENSIBLE BOUND, not an estimate.* Resident
  accumulation and legacy whole-table generation both charge a conservative
  whole-table UPPER BOUND through the ledger and are rejected before allocation if it
  does not fit, or if any column lacks such a bound (previous bullet). "Estimate" is
  not sufficient: a bound that can be exceeded by real data is not a bound. The
  resident path is exempt only from the bounded-OUTPUT guarantee (output is a whole
  table by design), NOT from the ceiling or the defensible-bound requirement.
- *The benchmark supervisor is containment, not production enforcement.* The
  supervisor RSS kill (`scripts/fk-scale/_supervisor`, `--rss-kill-mb`) is an
  external benchmark process that polls RSS and SIGKILLs; it does not protect
  ordinary library calls, guarantee pre-OOM termination, or run transactional
  cleanup. Production enforcement is the preflight defensible-bound rejection above,
  on every path. The supervisor is measurement and a bench-only safety net, not the
  memory guarantee.
- *No post-start fallback.* Once batches are being produced, a reservation breach
  aborts transactionally (`GenerationBudgetExceeded`, staging removed); it does NOT
  switch execution mode mid-run, because that would change neither the values nor
  the mode silently.

## 4. F3 under re-baseline: kernel selection, per generator semantics (plan-gate #4)

> **SHELVED 2026-10-18 (Cam), measured regression.** FR3 was built to spec and is
> correct, but a measurement showed the compiled HMAC index kernel (~670 ns/row on
> one thread, ~194 ns/row on four) is far more expensive than the vectorized RNG it
> replaces for GENERATION (NumPy `integers` ~3 ns/row, `random.choices` ~100-130
> ns/row). Every adopted column regressed (categorical uniform 0.09 s -> 0.73 s at
> 1M; a mixed 6-column job 3.55 s -> 7.24 s); only weighted-reference got faster
> (it had replaced a per-row Python loop). The kernel wins for MASKING (per-row
> Python HMAC), not generation. FR0 already made generation route-independent, so
> the kernel adds no correctness gain here, and the selection draw is not the
> generation bottleneck (Faker calls and pool construction are). DECISION: do not
> adopt the compiled kernel for generation selection. The full generator-to-algorithm
> inventory and the per-column measurements are preserved on the parked branch
> `feat/phase-f-fr3-kernel-select` @ `c417c105` (not merged) and
> `docs/plans/2026-10-18-phase-f-fr3-selection-inventory.md` there. If a faster
> route-independent selection is ever wanted, the backlog tracks the alternatives
> (NumPy Philox counter-RNG; narrow weighted-reference-only adoption; a non-crypto
> Rust position-index op). Phase F continues with F4 then F-audit on the FR0 + FR1
> substrate. The section below is the shelved original design, kept for the record.

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
- **Reproducibility exclusion closure (plan-gate #8):** a `determinism=="fresh"`
  fixture, a STATEFUL custom-provider column that declines a deterministic contract,
  and a reference/formula column DEPENDENT on either are each asserted
  NON-reproducible and never re-frozen, on BOTH the streamed and legacy paths. A
  negative test confirms none of them carries a reproducibility or route-independence
  assertion. A resolved-locale test (including `instance_default_locale`) and a
  controlled-clock date-provider test confirm those inputs are in the envelope.
- **Semantic re-freeze gate (§5):** passes for every re-frozen table; the
  relation-breaking and frequency-bias mutants each fail it.
- **Decline path:** a plan with each declining family / schema / provider case
  returns `mode="resident"` with the right reason and the SINK UNTOUCHED (zero sink
  writes); its resident output is the legacy whole-table output, and is asserted
  reproducible ONLY for its reproducibility-eligible columns (excluded columns per
  the §1 closure carry no such assertion). A budget-too-small-even-for-resident case
  raises `GenerationBudgetExceeded` and does NOT emit alternate values.
- **Bounded RSS, non-vacuous:** the generation bench worker
  (`scripts/fk-scale/_supervisor.run_worker`) streams into a
  `ParquetTransactionalSink` at TWO tiers (5M + 10M) under the same budget and
  frozen batch size, asserts EXTERNAL peak RSS under a predeclared ceiling and not
  materially growing tier-to-tier, committed rows == n.
- **Reserve-before-allocate enforcement (plan-gate #6), DIRECT LIBRARY, not via the
  bench supervisor:** a per-stage test forces the ledger near-exhaustion and asserts
  the REFUSAL happens BEFORE the disallowed allocation at EACH bounded stage (Faker
  pool construction, parent-key pool build, batch Arrow conversion, resident
  accumulation). An unbounded-width provider and an unknown-size parent pool each
  raise `GenerationUnboundedError` at admission on EVERY path (streamed, resident,
  legacy) with the provider NEVER invoked and nothing written (assert zero provider
  calls and zero sink/file writes). A resident/legacy-too-big case raises
  `GenerationBudgetExceeded` without materializing past the ceiling. These are
  in-process library assertions; the bench supervisor RSS kill is covered separately
  as a bench-only containment check, not as the memory guarantee.
- **windowed_date (FR1, plan-gate #3):** cross-partition route-independence with the
  anchor and windowed columns co-computed in one batch; a null-anchor row yields a
  null output and consumes no draw (verified by downstream determinism); an
  unparseable / out-of-range anchor raises `WindowedDateError`; a cross-table or
  later-ordered anchor DECLINES at admission.
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
3. **FR3 - kernel selection: SHELVED 2026-10-18 (measured regression, see the §4
   banner).** Original design (now shelved): per the §4 per-mode inventory (arithmetic
   sequence preserved, weighted preserved or Python, uniform adopted), uniformity +
   weight proofs, witness, re-freeze. After FR0; may overlap FR1 with serialized golden
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
