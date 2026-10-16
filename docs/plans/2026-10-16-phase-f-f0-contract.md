Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, testing, verification, feature-dev

# Phase F slice F0: generation parity contract + executed-backend witness

Tests-only. First Phase F slice, per the program plan
`docs/plans/2026-10-16-phase-f-program.md` §3 (F0). No production (`src/`) change.
It establishes the oracle, invariants, and witness harness that F1 (bounded
generation) and F3 (Rust generation selection) are built and gated against.

## 1. Why F0 first

F1 will make generation produce rows in bounded batches instead of whole tables,
and F3 will route some generator selection through the compiled kernel. Both can
silently change output (partitioned generation can flip pool admission; a kernel
swap can change values). F0 freezes what "unchanged" means BEFORE those slices,
so each is gated against a fixed oracle + explicit invariants + a backend witness.

## 2. Scope boundary (what F0 can and cannot test)

Generation today (`generation/_plan_entry.py:55` `generate_tables` ->
`generation/synthesize.py`) produces WHOLE Arrow tables; there is no partitioned
generation yet (that is F1). So F0 CANNOT test resumable/partitioned generation,
and slicing an already-generated table would not prove it (Codex program-plan-gate
note). F0 therefore freezes:
1. the whole-table parity ORACLE (the fixed output F1 must reproduce), and
2. the INVARIANTS F1's partitioned generation must preserve, expressed as
   properties of the current whole-table path, and
3. the executed-backend WITNESS harness (for F3 to assert against).
The partition-invariance cases themselves (irregular batch partitions producing
byte-identical output) are F1's ACCEPTANCE, run against F0's frozen oracle - F0
provides the oracle + the documented invariants, not a partitioned run.

## 3. Deliverables (tests-only)

The builder DERIVES the coverage from the actual generation dispatch (as D-audit
derived its matrix from admission constants), names the executable entry point +
valid config for every fixture, and checks in the expected outputs. No guessing
from this plan's illustrative lists.

### 3.1 Whole-table generate-parity oracle + contract
A frozen, seed-fixed parity baseline whose expected outputs are CHECKED IN as
canonical digests (or small expected tables) captured from `d11097f9`, with pinned
inputs and the relevant dependency versions - NOT regenerated at runtime (Codex
plan-gate P2). For a given (config, seed), the named entry point produces an exact
output pinned by CANONICAL ARROW equality: schema (names, types, field metadata,
nullability), column/row order, values, and null-vs-NaN distinguished - while
accepting equivalent Arrow chunk layouts. "Byte parity" = canonical Arrow values +
schema, NOT serialized artifact bytes. Name the entry point per fixture: whole
`generate_tables` (`generation/_plan_entry.py:55`) for end-to-end cases; the
composite samplers (`composite/_person.py`, `_name_email.py`) as SEPARATE sampler
contracts where that is the surface. Do NOT point a fixture at a surface it does
not actually exercise (Codex plan-gate P1-4). This is the oracle F1 reproduces and
F3 must not perturb.

### 3.2 Supported/deferred coverage matrix + the invariants F1 must preserve
Build an explicit SUPPORTED/DEFERRED matrix with a concrete whole-table fixture
per admitted generator/dependency family, derived from the dispatch. Families to
classify (supported-and-pinned, or deferred-with-reason): sequence, categorical,
pooled-faker, live-faker, composite, formula / referenced-formula (post-pass
ordering), derived_aggregate, conditional sampling, statistical / DP-fit columns
(pinned statistical artifacts), grouped/windowed, and partial-null / all-null
masks. For each, pin the behavior F1 must preserve. Invariants to pin explicitly:
- Pool admission is a function of the individual TABLE's `row_count`
  (`generation/_faker_pool.py:239`) - NOT a job-wide sum (Codex plan-gate P1-4
  correction). Pin that pooled-vs-live faker selection is decided by that table's
  total row_count, so F1 computes admission on the table total, never per-batch.
- Pool IDENTITY stable for (provider, config, namespace, job_seed)
  (`generation/pool/_identity.py`); pin the ordered pool, resolved pool_size,
  locale/provider resolution, kwargs, and the build-seed vs selection-seed
  separation.
- Parent-pool ordering + repair, and formula/reference POST-PASS ordering: pin so
  a reordered or re-read F1 would diverge.
- RNG progression / global row ordinals as a function of (seed, global row
  position), not batch-local.
- generated-parent -> masked-child FK coupling: `generate_tables` alone cannot
  establish it; either add a `run_pipeline` contract fixture for that boundary OR
  explicitly DEFER it to a later gate (F2) in the matrix. F1 must preserve or
  explicitly decline every family the matrix does not cover.

### 3.3 Executed-backend witness harness
A per-column witness that classifies the ACTUAL selection backend among the full
set the generation path uses today (Codex plan-gate P1-1): NumPy selection, live
provider (Faker call), compiled index selection (`generation/pool/_sampler.py:49`),
Python index reference (`_sampler.py:135-157`), and no-selection. It must
POSITIVELY observe today's actual path per column (negative/mixed backend baseline
is fine - no slice yet claims compiled as the admitted default; F3 will). Make it
NON-VACUOUS (Codex plan-gate P1-2): a marking-index witness whose marked selected
values appear in the final column, plus mutants that MUST trip it (discarded
result, recompute, wrong-column attribution). Test the assertion logic WITHOUT the
companion (stand-in), and include a real compiled-execution observation WITH the
companion; label stand-in evidence separately. Separate the lower-level sampler
coverage from end-to-end generation coverage, and state which F3 route connects
them. Model on D-audit's marking-kernel witness.

## 4. Acceptance

- All new tests pass against current main (`d11097f9`): companion venv
  (`~/.cache/decoy-d1a-abi3-venv/bin/python` via `~/bin/pytest-one`) and the
  ci-mirror (no-companion) venv for the companion-optional assertion-logic tests.
- Expected outputs are CHECKED IN (digests/tables from d11097f9), inputs + dep
  versions pinned, no runtime regeneration of expectations.
- Non-vacuity, enforced: each invariant (3.2) is pinned by a test that FAILS if
  the property is violated, via a mutation through the EXERCISED PRODUCTION SEAM
  (e.g. simulate per-batch/per-table-sum admission, a reset row index, a reordered
  pool - without changing production code). The comparator is itself tested against
  changed values, row order, null-vs-NaN, types, nullability, and metadata, while
  accepting equivalent Arrow chunk layouts. The witness mutants (3.3) trip it.
- No `src/` change; ruff + mypy clean on the new tests.
- The matrix states, per family, supported-and-pinned vs deferred-with-reason, and
  the test module documents how F1 reuses this oracle (partitioned output across
  irregular partitions vs the frozen oracle) and how F3 asserts the witness.

## 5. Risk

Low (tests-only). The main risk is a VACUOUS contract - a parity baseline that
would not actually catch a partitioned-generation divergence, or a witness that
cannot distinguish compiled vs reference. Mitigated by the non-vacuity requirement
in §4 and by Codex plan-gate review of the exact invariant tests.

## 6. Gate chain

Codex plan-gate (this doc) -> build (Sonnet) -> ci-mirror + companion suites ->
dennis -> Codex final -> merge on green + Slack. DOCUMENT: run-state ledger;
program plan F0 noted done; engine graph refresh.

cam
