Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev,
architecture, performance, testing, verification

# Phase F re-baseline: one generation oracle, then F1 + F3

Supersedes the byte-parity-to-F0 contract that parked the F1 rev-3 plan
(`docs/plans/2026-10-16-phase-f-f1-bounded-generation.md`) and bounded F3
(`docs/plans/2026-10-16-phase-f-program.md` §F3). Cam re-sequenced Phase F on
2026-10-17 (platform `docs/backlog/v2-candidate-plan.md` "Phase F re-sequence")
and chose the re-baseline direction on 2026-10-18, with three design calls
settled the same day (see §1). F0's contract, witness and comparator are reused
unchanged; only the frozen golden is re-based.

This is a production generation change. The guards are reproducibility,
route-independence, and distribution sanity, not equivalence to today's resident
bytes.

## 1. The contract (whole-PLAN, the one decision)

Today F0 froze the whole-table generation output as the golden, and F1/F3 had to
match it byte-for-byte. That exact-seed / exact-schema constraint repeatedly
failed the plan-gate because streamed and compiled-kernel generation cannot
reproduce the old resident draws bit-for-bit. Re-baseline replaces the guarantee:

- **OLD:** streamed/compiled output == today's resident output, byte-for-byte.
- **NEW:** generation output is REPRODUCIBLE and ROUTE-INDEPENDENT. For a fixed
  (seed, config, engine minor version), every run and every route emits identical
  bytes. That output is re-frozen as the new golden. It need not equal the
  pre-re-baseline resident bytes.

Three calls settled with Cam (2026-10-18, recorded in
`memory/decoy-c4-chunked-date-shift-state.md` cont-7):

- **(A) One golden, not two.** Resident and streamed collapse to a single
  route-independent deterministic output, mirroring masking's cross-route
  byte-identity (the D-audit result). A job's generated data is identical whether
  it streamed to a sink or stayed resident. Achieved by construction (§2), not by
  freezing two outputs to match.
- **(B) Aggressive F3.** Pooled, categorical and sequence SELECTION move onto the
  compiled kernel and re-freeze the emitted values. Distribution stays uniform
  over the pool. Faker custom providers, formulas and statistical stay Python.
- **(C) Contract pinned to the engine minor version.** Within a minor version,
  (seed, config) gives identical bytes. The golden may be re-frozen across minor
  versions while pre-1.0 (matches the pre-GA no-locking stance). The pinned minor
  version is recorded in the golden manifest.

## 2. Architecture: unify on one generation algorithm (decision A)

The route-independence guarantee is met by making resident generation a CONSUMER
of the streamed per-batch algorithm, not a second algorithm kept in sync.

- Keep F1's `generate_tables_streamed(plan, *, sink, budget_bytes=None, ...)` as
  the single generation core: whole-plan admission, persistent per-column RNG
  threaded by the global row offset (F1 rev-3 §4, unchanged and reused), enforced
  byte budget (F1 rev-3 §3, unchanged), bounded transactional sink (F1 rev-3 §5,
  unchanged).
- `generate_tables` (the resident entrypoint at `generation/_plan_entry.py` ->
  `generation/synthesize.py`) becomes: run the SAME per-batch core into an
  in-memory accumulating sink, then concatenate per table. For ADMITTED plans the
  resident result is the batch core materialized; there is one code path, so
  resident and streamed are byte-identical by construction.
- NON-ADMITTED plans (families or schemas the batch core declines, §3) keep the
  existing whole-table resident implementation. For those, resident IS the golden
  (nothing streamed to diverge from). The admission matrix (§3) is therefore also
  the boundary between "unified batch core" and "legacy whole-table", and every
  column lands in exactly one.
- Mixed generate-then-mask (`_pipeline_generate_mask.py`) consumes the resident
  result as today; because resident now equals streamed for admitted plans, mixed
  jobs inherit the re-baselined values with no separate freeze.

Net: no "two goldens to reconcile" state exists at any point. The change from
F1 rev-3 is that resident is re-pointed at the batch core for admitted plans
(rev-3 left resident byte-UNCHANGED and required streamed to match it; that is the
exact inversion re-baseline removes).

## 3. F1 under re-baseline: widened admission

The batch core admits a column when it can produce DETERMINISTIC, REPRODUCIBLE
output under persistent per-batch state. Byte-equivalence to the old whole-column
draw is no longer required, so families previously declined only for draw-order
divergence become eligible once a persistent-state design exists for them.

Admitted now (F1 rev-3 set, unchanged): sequence, categorical, faker (per-row and
pooled), null_mask, reference without cardinality repair.

Newly in scope under re-baseline, each requiring a persistent cross-batch state
design before admission (sequenced as sub-slices, §7):
- **grouped_cumcount / grouped series / walk:** admit by carrying per-group
  running state (counter, last value, RNG) across batches, keyed by the group key,
  with the group-state store charged against the byte budget (O(distinct groups);
  decline if the reservation does not fit, same discipline as the reference pool).
- **windowed_date:** admit by carrying the window anchor/offset state across
  batches.
- **reference WITH min/max_per_parent repair:** admit by carrying the per-parent
  assignment counters across batches (bounded by distinct parents, budget-charged).

Still declined (genuinely not streamable-deterministic, re-baseline does not
change this): formula / referenced_formula (whole-column pandas coercion),
derived_aggregate, statistical, and `determinism=="fresh"` (os.urandom,
non-reproducible by definition). Custom / value-inferred Faker providers: admit
ONLY with a probe-derived declared schema re-frozen into the golden; otherwise
decline (schema inference stays value-dependent). Deliver the TOTAL admission
matrix over every generator kind with its reason (F1 rev-3 §2 obligation, carried
forward and extended to the new families).

The byte budget (F1 rev-3 §3), the `bounded_write_batches` sink marker (§5), and
the honest O(parent-keys) / O(groups) reservation-and-decline bounds all carry
forward verbatim.

## 4. F3 under re-baseline: aggressive kernel selection (decision B)

F0's inventory finding holds: pure generation draws from seeded NumPy / Python
RNG, while the compiled index kernel derives indices via HMAC. These are different
algorithms, so adopting the kernel CHANGES emitted values. Re-baseline permits
that change and re-freezes the result.

- Produce the generator-to-algorithm inventory FIRST (F0/F3 obligation): for
  sequence, categorical, pooled-faker and composite selection
  (`composite/_person.py`, `composite/_name_email.py`, selection via `_sampler.py`),
  state which already ride the shared kernel and which adopt it under re-baseline.
- Move pooled, categorical and sequence SELECTION to default to the compiled HMAC
  kernel. The selection must be uniform over the pool / category set (verified,
  §5) so the distribution is preserved; only the specific drawn values change.
- POOL CONSTRUCTION stays Python (the program plan's constraint): the kernel
  selects indices, it does not build the Faker pool. Faker custom providers,
  formulas and statistical stay Python.
- Admitted columns record the compiled backend via the F0 witness; the Python
  reference stays reachable only for non-admitted / companion-absent cells.

## 5. The re-freeze procedure (the heart of the safety story)

Re-basing a golden is a deliberate, reviewed step, never an auto-overwrite.

1. **Regenerate** every affected F0 golden fixture with the re-baseline engine
   (unified batch core + F3 kernel selection), under the frozen seeds.
2. **Diff review (distribution sanity, not byte diff):** for each re-frozen
   column assert the DISTRIBUTION is preserved against the old golden, within
   declared tolerances: category frequencies, pool-value coverage, null rate, min
   /max / cardinality, and row count. A value-level byte diff is EXPECTED and is
   not the gate; an unexplained distribution shift IS a stop.
3. **Determinism re-run:** generate each fixture TWICE (and once per route:
   streamed and resident) and assert byte-identical, proving the new golden is
   reproducible and route-independent before it is frozen.
4. **Freeze with a manifest:** commit the new goldens with a manifest recording
   the engine minor version, the seeds, and the re-freeze date. The manifest is
   the (C) contract's machine-checkable anchor.
5. The re-freeze is one reviewed commit (its own sub-slice, §7), gated like any
   other change; the distribution-sanity harness ships with it.

## 6. Acceptance

- **Route-independence (decision A):** for every admitted fixture, streamed output
  == resident output, byte-for-byte, reassembled across SEVERAL IRREGULAR
  partitions (including pooled-threshold-crossing, all-null and zero-row decline
  cases). This replaces F1 rev-3's "streamed == old oracle" assertion.
- **Reproducibility (contract):** every fixture generated twice is byte-identical;
  the golden manifest's pinned minor version matches the running engine.
- **Distribution sanity (re-freeze):** the §5 step-2 harness passes for every
  re-frozen column; an intentional perturbation fixture makes it fail (non-vacuous).
- **Decline path:** a plan with each declining family / schema / budget / sink
  case returns `mode="resident"` with the right reason and the SINK UNTOUCHED
  (zero sink writes); its resident output is the legacy whole-table output and is
  itself reproducible.
- **Bounded RSS, non-vacuous:** the generation bench worker
  (`scripts/fk-scale/_supervisor.run_worker`) streams into a
  `ParquetTransactionalSink` at TWO tiers (5M + 10M) under the same `budget_bytes`
  and frozen batch size, asserts EXTERNAL peak RSS under a predeclared ceiling and
  not materially growing tier-to-tier, committed rows == n, exercising a pooled
  column, a (generated-parent) reference column, and one newly-admitted grouped
  family.
- **F3 selection:** admitted columns record the compiled backend (F0 witness); a
  uniformity test confirms the kernel selection is uniform over the pool /
  categories; the Python reference is proven reachable only for non-admitted /
  companion-absent cells; measured speedup on a pooled-generation job.
- **Transactional abort:** failure injected at a later table, writer-close, and
  commit each aborts with no partial commit, staging removed, original error
  preserved (F1 rev-3, carried forward).
- **Pinned-input + pre-provider validation** preserved on the unified entrypoint.
- **ci-mirror clean** (ruff + mypy on `src/decoy_engine`); companion and
  no-companion green. If the physical-seam sentry flags any changed `execution/`
  module, add it to `permitted_non_physical` with rationale (per the standing
  sentry rule).

## 7. Slice order

Each slice is independently gated (plan-gate already covers the program; each
build slice gets ci-mirror + suites + dennis + Codex final before merge).

1. **FR0 - unify + re-freeze core.** Re-point resident at the batch core for the
   CURRENT admitted set (no new families, no F3 yet), add the §5 re-freeze
   procedure and distribution-sanity harness, re-freeze the affected goldens, and
   land route-independence acceptance (§6 items 1-4). This is the contract change
   in isolation; it must be green before anything widens.
2. **FR1 - widen F1 admission.** Add the newly-eligible families (§3) one sub-slice
   at a time (grouped_cumcount/series/walk; then windowed_date; then
   reference-with-repair), each with its persistent cross-batch state design, byte
   budget charge, and admission-matrix entry. Re-freeze the newly-admitted
   fixtures per §5.
3. **FR3 - aggressive kernel selection.** The inventory, then kernel default for
   pooled / categorical / sequence selection, uniformity proof, witness, re-freeze
   (§4, §5). Runs after FR0 (needs the one-golden core) and may overlap FR1.
4. **FR-capacity hook.** Confirm the generation capacity slice (program §F4) rides
   the re-baselined core; no new freeze, just the bounded tiers.

Follow-ups (own slices, unchanged from F1 rev-3): bounded parent-key store (remove
the O(parent-keys) reference limit); bounded two-pass for still-declined families;
the composite in-process repeatability test both F0 reviewers flagged (land BEFORE
FR3 touches composite state).

## 8. Risk + gate chain

Risks: (a) a re-freeze that silently masks a real regression - guarded by the §5
distribution-sanity gate plus the determinism re-run, not a byte diff; (b) a
mis-admitted new family whose cross-batch state is not truly prefix-stable -
guarded by the route-independence assertion across irregular partitions; (c) F3
kernel selection that skews the distribution - guarded by the uniformity test.
Correct-by-construction otherwise: one algorithm feeds both routes, so they cannot
diverge.

Gate chain: Codex plan-gate (this doc) -> build each slice (Sonnet) -> ci-mirror +
suites + the generation bench + re-freeze harness -> dennis -> Codex final ->
merge on green + Slack. DOCUMENT per slice: roadmap + shipped-log, program plan
F1/F3 status, engine graph refresh.

cam
