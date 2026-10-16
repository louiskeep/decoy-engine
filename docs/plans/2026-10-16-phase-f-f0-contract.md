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

### 3.1 Whole-table generate-parity oracle + contract
A frozen, seed-fixed parity baseline over a representative generation config
(sequence, categorical, pooled-faker, composite, and a formula/statistical column
for coverage): for a given (config, seed), `generate_tables` produces an exact
output, pinned by CANONICAL ARROW equality - schema (names, types, field
metadata, nullability), column/row order, and values (null-vs-NaN distinguished).
Define "byte parity" explicitly as canonical Arrow values + schema (NOT serialized
artifact bytes). This is the oracle F1 reproduces and F3 must not perturb.

### 3.2 Documented + test-pinned invariants F1 must preserve
Each is a property of the current path, asserted now so F1 cannot regress it
silently:
- Pool admission is a function of TOTAL n (`generation/_faker_pool.py:239`): pin
  that pooled-vs-live faker selection is decided by the whole-job row count, so
  F1 must compute admission on total n, never per-batch. Test: a config/size that
  is pooled at total n stays pooled (document the boundary).
- Pool IDENTITY is stable for a given (provider, config, namespace, job_seed)
  (`generation/pool/_identity.py`): same identity -> same pool.
- RNG progression / global row ordinals: the per-generator seeding + row indexing
  is a function of (job_seed, global row position), not batch-local - pin the
  determinism so a batched F1 that reset indices per batch would diverge from the
  oracle.
- Schema + late schema discovery and empty / all-null column shapes are part of
  the frozen output (reproduce the current shapes exactly).

### 3.3 Executed-backend witness harness
A test harness that observes, per generated column, whether its value SELECTION
ran on the compiled index kernel (`generation/pool/_sampler.py:49`) or the Python
reference (`_sampler.py:135-157`). Today the backend baseline is NEGATIVE / mixed
(no slice yet claims the compiled path as the admitted default); the witness
exists so F3 can later assert "admitted pooled/deterministic columns ran compiled,
the reference ran only for non-admitted/companion-absent cells." Model it on
D-audit's executed-backend witness, companion-optional where possible.

## 4. Acceptance

- All new tests pass against current main (`d11097f9`), companion venv
  (`~/.cache/decoy-d1a-abi3-venv/bin/python` via `~/bin/pytest-one`) and, where
  the witness is companion-optional, the ci-mirror (no-companion) venv.
- The parity oracle is DETERMINISTIC (two runs identical) and the invariants
  (3.2) are each pinned by a test that would FAIL if the property were violated
  (non-vacuous - e.g. a test that simulates per-batch admission and shows it would
  diverge, without changing production).
- No `src/` change; ruff + mypy clean on the new tests.
- Document in the test module how F1 reuses this oracle (run F1's partitioned
  output across irregular partitions against it) and how F3 uses the witness.

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
