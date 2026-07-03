# Sprint G: FK-Aware Subsetting Core (engine)

Source spec: `decoy-platform` `docs/backlog/capability-review-2026-07/sprint-g-fk-subsetting-core.md`
(read that for full current-code evidence + acceptance detail). Scoping input:
`decoy-platform` `docs/backlog/subsetting/00-overview.md`. This is the FINAL sprint of the
capability-review program (A-F shipped). Highest complexity/upkeep; the novel risk is SS3 (closure
engine) + SS4 (fan-out).

Repo/branch: **decoy-engine** `sprint-g/fk-subsetting` (worktree `/home/cam/vscode/sprint-g-fk-subsetting`,
off engine main 5fc939a). Build tier: **Fable** (novel closure engine; per the model-tiering
directive, Fable for Sprint G's closure engine). SS6 (CLI) is a thin follow-on in the `decoy` CLI
repo. SS7 (platform UI) is HELD (platform surfacing freeze; depends on A/B which shipped, but not
built here).

## GATE-1 (Cam, 2026-07-03) — resolved

1. **CSV: reject-with-guidance.** Parquet-only for subsetting. A subsetting job on non-Parquet input
   fails fast with a specific message ("convert to Parquet for subsetting"), NOT a degraded full-load.
2. **DB sources: deferred (out of v1).** File/batch (Parquet) only. Do NOT build a DB-source
   abstraction; do NOT design "for a future DB from day one."
3. **Fan-out policy:** upward parent-completeness **ON by default** (never emit a dangling FK) WITH a
   **mandatory dry-run/estimate before any real run** (explosion is always seen, never silent).
   Budget = **both** a total-output-row cap AND a per-table multiple-of-seed-size cap. On budget hit:
   **HARD FAIL before any materialization** (no partial Parquet written); NEVER truncate-and-flag
   (truncation re-introduces orphans). Per-edge traversal toggles (upward/downward/both/neither)
   supported.
4. **Engine memory-scaling dependency:** build SS1-SS4 (pure key-set/graph work) AND SS5
   (materialization) against the **current full-frame FK execution path**. Do NOT merge the unmerged
   `feat/fk-ri-memory-scaling` / `feat/option4-out-of-core` engine branches first; do NOT block on
   them. Accept today's full-frame memory ceiling for v1. Add an explicit design note in SS5 marking
   where sequential per-table eviction (`_sequential.py`, once merged) would plug in, so it composes
   cleanly later rather than being redesigned.
5. **Composite-key UX:** SS3 consumes the SAME composite-key tuple declaration the relationships
   surface already uses for masking (`PlanRelationship` / the `relationships` block). No new
   declaration surface.
6. **Package placement:** new **`decoy_engine/subset/`** subpackage behind a narrow entrypoint the
   platform/CLI call (CODEMAP: engine owns data semantics). Do NOT extend `plan/`.

## Scope — build SS1-SS5 (engine core) in THIS sprint

- **SS1 — FK-validity preflight.** Reuse `run_fk_validity`
  (`decoy_engine/validation/post/_checks/_fk_validity.py`) logic as a PRE-selection pass over the
  declared `relationships` + actual Parquet schemas/key columns. Fail closed on: column-type mismatch
  (`"007"` vs `7`), half-declared composite keys (tuple-length mismatch; partly guarded by
  `PlanRelationship.__post_init__`), and target columns absent from the file. Emit a structured
  `FkPreflightReport` (parity with `FkValidityReport`). Independently valuable (hardens linked masking
  too).
- **SS2 — Seed selection.** Deterministic sample / filter predicate / explicit key-list over root
  table(s). Deterministic sample MUST reuse the existing job seed (reproducible across runs), not a
  fresh draw. Output: initial per-table surviving-key set. Polars `filter`/`sample` over a lazy
  Parquet scan.
- **SS3 — Closure engine (THE NOVEL WORK, highest risk).** Downward (cascade, semi-join) + upward
  (parent-completeness) fixpoint walk over `PlanRelationship` edges from SS2's seed. Alternate
  down/up until no table's key set grows. **Cycle termination is a TESTED INVARIANT** (self-ref
  `employee.manager_id->employee` AND mutual A->B->A), asserting the visited-set/no-growth condition
  is exercised, not a timeout smoke test. PURE key-set computation, NO row-data I/O (that's SS5) — so
  SS3 is testable as a graph algorithm independent of substrate.
- **SS4 — Fan-out policy + dry-run estimate.** Per-edge traversal toggles; the both-caps budget from
  GATE-1 #3 with HARD-STOP-and-report on hit; a dry-run/estimate mode returning projected per-table
  row counts BEFORE materializing anything. Treat dry-run as a first-class deliverable (it is the
  entire subsetting UX in category tools), not a debug flag.
- **SS5 — Parquet materialization + subset manifest.** Semi-join each Parquet to its final surviving-
  key set; write filtered Parquet. Deterministic/order-independent given the key sets. Enforce
  subset-then-mask ordering AT THE CONFIG LEVEL (reject subset-after-mask). Evidence: a subset
  manifest (seed spec, edges traversed + direction, per-table input vs surviving row counts, fan-out
  outcome) reusing the existing evidence-manifest contract; NO raw key values in the manifest (counts/
  table-ids/edge-ids only). Design against full-frame FK execution (GATE-1 #4); note the eviction
  plug-in point.

**Follow-ons (NOT this build):** SS6 `decoy subset` CLI (thin wrapper, `decoy` CLI repo — dispatched
after engine-core review). SS7 platform UI (HELD).

## Non-goals
Live DB / in-DB / CDC / streaming; polymorphic FKs (no clean `PlanRelationship` representation —
document unsupported); CSV subsetting; automatic FK/schema inference; new masking semantics;
platform UI.

## Acceptance tests (from spec — all non-negotiable before promotion)
1. Referential completeness: 2% of `customers` (deterministic sample) -> every surviving `order`/
   `order_item` belongs to a surviving customer, nothing orphaned.
2. Self-reference AND mutual-cycle fixtures terminate via the visited-set/no-growth condition
   (explicitly exercised, not a timeout).
3. Fan-out budget hard-fails BEFORE any materialization (no partial Parquet), error names the
   edge/table that tripped it.
4. Preflight fails closed on: type-mismatch, half-declared composite, dangling target column — each
   naming the specific relationship + problem, before SS2/SS3 run.
5. Dry-run estimate per-table counts EQUAL the SS5 materialized per-table counts (no estimate drift).
6. Evidence manifest completeness: seed spec, edges+direction, per-table input/surviving counts,
   budget outcome; NO raw key values.
7. Subset-then-mask: masking runs only on the subset (row counts match the subset), FK-consistent
   masked-value propagation identical to a non-subsetted job over fewer rows.

## Core risk (from spec §5)
FK-integrity correctness IS the sprint. A missed upward pull -> dangling FK; an over-pull silently
defeats the "2%" promise. Both are worse than "no feature" (a subset that looks complete/small and
isn't). That is why SS1 (preflight) + SS4 (hard-stop budget) are mandatory, not optional.

## Process
DEVELOP (TDD; SS3 cycle-termination + SS4 hard-fail + SS1 fail-closed are the load-bearing tests) ->
SELF-CHECK (engine CI-gate mirror: ruff + ruff format --check + mypy + pytest + sphinx -W, no-extras
env — see the engine's own CI) -> dennis adversarial REVIEW (Opus; focus: closure correctness incl.
upward-completeness + cycles, budget hard-fail-not-truncate, preflight fail-closed, no raw keys in
manifest, subset-then-mask ordering enforced at config level, determinism) -> REMEDIATE -> barry docs
-> engine CI-gate green -> GATE-2 (Cam merge; engine merge is human-gated/unpushed per program) ->
then SS6 CLI follow-on. Baseline: engine plan tests green (167 confirmed).
