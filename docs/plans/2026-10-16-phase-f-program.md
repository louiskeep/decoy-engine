Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, architecture, performance, testing, verification, autonomous-operation

# Phase F program: generation and mixed jobs

Initial program plan. The parent program plan
(`docs/plans/2026-09-30-rust-engine-program.md`, §"Phase F") sketches four
slices; this expands them into gated build-slices with scope, risk, acceptance,
and sequencing, mirroring the Phase D program plan
(`docs/plans/2026-10-10-phase-d-fk-routes-program.md`). Each slice is its own
gated build (Codex plan-gate -> build -> dennis -> Codex final). Phase F is the
last phase of the Rust-engine program (A-F). It starts after Phase E's 100M
milestone; F4's scale slices inherit the bounded-memory discipline and must not
begin until the Phase E 100M run confirms the platform captures per-column Rust
evidence (ROADMAP Phase E row).

## 1. Goal, gap, scope

Program goal: fast, bounded-memory generation to 100M+ and mixed generate+mask
jobs with the masking on the Rust path, with the same no-silent-fallback
guarantee Phase D established for masking.

Current state (recon 2026-10-16):
- Generation (`generation/synthesize.py`, entry `generation/_plan_entry.py:55`
  `generate_tables`) is full-frame Python/NumPy/Faker: it materializes whole
  Arrow tables in memory, synthesizes per-row for faker/formula/reference, and
  does a post-pass formula re-read. This is the throughput + memory bottleneck.
- The pool value-SELECTION step SHARES code with the compiled Rust index kernel
  (`generation/pool/_sampler.py:49` `_compiled_index_kernel`, the same
  `derive_index_batch` masking uses) and has a byte-identical Python reference
  fallback (`_sampler.py:135-157`) when the companion is absent or a type is not
  admitted. The sampler code existing does NOT by itself prove that pure
  generation selection executes on the compiled path vs the Python reference at
  runtime (Codex plan-gate P2) - F0's executed-backend witness establishes which,
  and F3 widens only where it is parity-preserving. Pool CONSTRUCTION (calling
  Faker to fill the pool) stays Python.
- Mixed jobs (generate + mask in one job) always route full-frame/pandas in
  production today (`execution/_pipeline_routing.py:216-217`,
  `_pipeline_generate_mask.py:73`). A native/bounded mixed path is already built
  but SHADOW-ONLY / inert (`execution/physical/_shadow_mixed.py`,
  `_shadow_generation.py`, `_shadow_fk.py`), with a clearly-scoped admitted
  subset and coded declines.
- Distribution-integrity primitives exist (engine `quality/`, platform
  `api/quality/` + `GET /jobs/{id}/quality`) but the full measure -> persist ->
  improve -> gate system is largely unbuilt; it is a sizable V1 pillar, not a
  Phase F slice (see §6).

Scope boundary: Phase F is generation + mixed jobs. It reuses the Phase D
masking backends for the mask half (so the mask half's no-legacy guarantee is
already covered by D-audit, provided F2 routes through the admitted kernels).
Arbitrary/custom Faker providers, formulas, and statistical (DP-fit) columns
stay Python by design (the documented exceptions, mirroring D-audit's
code_set/NER classification).

## 2. The memory connection (why F1 and the 100M finding are linked)

The 100M masking run (Phase E) peaked ~29 GB regardless of an 8 GB budget: the
memory tracks rows x columns (total data), pointing to an O(total-data)
in-memory materialization outside the budgeted DuckDB path (profiling in flight).
Generation today has the SAME shape: `generate_tables` materializes whole tables.
So F1 (bounded generation + incremental sink) attacks the same class of problem
on the generate side, and whatever the 100M profiling finds (output
accumulation vs sorter whole-run materialization vs a full-table read) likely
informs F1's design and may share a fix. Treat the memory finding as a planning
input to F1; do not finalize F1's design until it lands. Note: diagnosing the
Phase E consumer is not itself evidence that the generation/mixed PRODUCTION path
is bounded - F1 and F4 must each demonstrate bounded memory on their own path.

## 3. Slices

Each is a gated build-slice. Sizes from the parent program plan.

### F0 - generation parity contract + executed-backend witness (tests-only, S)
Mirrors D0, and is the prerequisite that makes F1 and F3 safe to split. Freeze a
PARTITION-INVARIANT generate-parity contract, not just a single-shot byte check
(Codex plan-gate correction): the principal streaming hazard is that today's
generation semantics depend on the TOTAL job size, so naively calling the
generator per-batch would silently change output. The contract must freeze, and
prove invariant across multiple IRREGULAR batch partitions of the same job:
- total-job pool ADMISSION (pool admission depends on total `n` -
  `generation/_faker_pool.py:239`; a smaller batch could flip pooled faker to
  live faker),
- pool IDENTITY (same identity -> same pool across partitions),
- RNG PROGRESSION and global ROW ORDINALS (sequence/categorical/faker row indices
  must not reset per batch),
- results across pool-size THRESHOLD crossings and empty / all-null chunks,
- exact schema metadata and late schema discovery.
Define "byte parity" explicitly (canonical Arrow values + schema, not serialized
artifact bytes). Add a generation executed-backend WITNESS (does an admitted
pooled/deterministic column's selection record the compiled kernel vs the Python
reference?). No production change; seeds the F-audit (§5). Acceptance: the
contract + witness exist and pass against current main (negative backend baseline
where no kernel is yet claimed), including the multi-partition invariance cases;
no src change.

### F1 - bounded generation with an incremental output sink (L)
The enabling substrate. Today generation materializes whole Arrow tables; F1
makes it produce rows in bounded batches to the existing incremental sink
(B6a #192) with a declared memory ceiling, reusing the chunked-dispatcher /
streaming substrate masking already uses. Blocks 100M generation regardless of
kernel speed. Design input: the 100M memory finding (§2). F1's build plan must
NAME the bounded streaming entrypoint + result contract, its
resident-compatibility behavior (callers expecting a whole-table return), and
parent-state ownership (Codex plan-gate correction). Acceptance: generation
output byte-identical to the full-frame oracle (seed-fixed), satisfying F0's
partition-invariant contract; peak RSS within a DECLARED ABSOLUTE ceiling at a
mid tier (e.g. 10M) measured out-of-process; no post-pass full-table re-read for
the streamed path, or an explicit declined-with-reason for shapes that need one
(formula/reference-pass columns may require a bounded two-pass or a decline); the
streaming return + publication boundary specified, not implicit.

### F2 - mixed mask+generate and FK+generate, masking through Rust (L)
The inert shadow-mixed path (`_shadow_mixed.py`, `_shadow_generation.py`,
`_shadow_fk.py`) supplies the ADMISSION contract, the native mask COMPUTE (reuses
the per-node mask loop), and the PARITY machinery - but it is a shadow
(publication-impossible) harness, NOT an end-to-end BOUNDED mixed executor. So F2
is reuse of that machinery PLUS real production streaming integration (Codex
plan-gate correction); F1 alone does not make this route bounded. F2 sub-slices,
prioritized in this order:
- F2a: promote the admitted subset to a real production route (one transactional
  publication boundary across generate+mask; abort on late decline/failure; no
  fallback after publish).
- F2b: bounded mask INPUT/OUTPUT on the mixed route (the mask half must stream,
  not materialize - it depends on F1's bounded generate half AND the Phase E
  bounded-mask-memory outcome; a generated parent feeding a masked child needs
  bounded generated-parent FK-key state, not a whole-parent in RAM).
- F2c (lower priority): lift the highest-value declines - nullable/NaN generate
  output, then additional FK key types. Composite FK keys and an OOC mask driver
  stay declined-with-reason or V2.
Depends on F1. Acceptance: mixed output byte-identical to the full-frame oracle
across the admitted matrix; mask half proven on the admitted Rust kernels (reuses
D-audit's matrix); the generate->mask->sink path bounded at a mid tier with a
declared RSS ceiling; one transactional publish boundary with abort tests; every
non-admitted shape a coded decline, never a silent full-frame drop.

### F3 - Rust generation selection for pooled/deterministic generators (M)
NOT a trivial "widen the compiled path" (Codex plan-gate correction). The
compiled index kernel derives indices via HMAC (the MASKING selection
algorithm); pure generation today draws from seeded NumPy / Python RNG. Those
are different algorithms: substituting HMAC-derived indices for RNG draws would
change the emitted values and BREAK F0 parity, even if the distribution looked
similar. So F3 must FIRST produce a generator-to-algorithm inventory
(sequence / categorical / pooled-faker / composite, and what "composite" denotes
at the actual generation entrypoint - `composite/_person.py`,
`composite/_name_email.py`): for each, which already select via the shared kernel
parity-preservingly (pooled-faker + composite selection already ride
`_sampler.py` per the recon), which could adopt it WITHOUT changing their seeded
result, and which cannot (those stay Python). Only the parity-preserving set is
made to default to the compiled kernel. Arbitrary/custom Faker providers,
formulas, and statistical stay Python. F3 may run ALONGSIDE F1 only AFTER their
shared RNG / pool-state / row-offset contract is frozen (F0). Acceptance:
byte-identical to each generator's existing seeded output (not merely
distribution-similar); admitted columns record the compiled backend (F0
witness); the Python reference proven reachable only for
non-admitted/companion-absent cells; measured speedup on a pooled-generation
job.

### F4 - capacity gate for generation and mixed jobs at 1M / 10M / 100M (M)
The generation/mixed scale proof, paralleling Phase E's mask 100M milestone.
After F1, F2, F3 and their RELEASED artifacts. Inherits the bounded-memory
discipline (predeclared absolute RSS ceilings per slice; the program "Performance
and memory" rule). No uncapped run option (Codex plan-gate). Prerequisites before
the 100M tier: (a) bounded lower-tier (1M/10M) evidence first; (b) enforced
resource/abort limits; (c) PLATFORM evidence transport for generation +
generate->mask-seam backend/timing (net-new; Phase E establishes this for masking
only - it does not cover generation); (d) Phase E's own 100M evidence-capture
confirmation. Acceptance: generation + mixed jobs complete at 1M/10M/100M within
the declared memory ceilings, byte-parity preserved, with per-column Rust
evidence captured AND transported to the platform (not just the in-process
F-audit witness).

### F-audit - no-legacy generation compute audit (terminal Phase F slice)
Mirrors D-audit's FULL discipline (Codex plan-gate correction - not just a
matrix). A generation compute-admission matrix DERIVED FROM THE ACTUAL DISPATCH
PREDICATES (not observed dispositions), classifying EVERY generator
(sequence / categorical / pooled-faker / composite / live-faker / formula /
statistical) x pooled-vs-live x companion-present-vs-absent as compiled
index-selection vs dedicated-Python, asserting no admitted cell silently drops to
the Python reference when a kernel was claimed. Consumed-output witnesses with
discard-and-recompute mutants that MUST fail (the D-audit non-vacuity bar), zero
unintended-fallback assertions, and empty / all-null columns handled SEPARATELY
from the positive compiled-call fixtures (an empty column makes a compiled-call
witness vacuous). Test the PRODUCTION mixed route through the
generate -> mask -> sink seam, not just the generate side in isolation. The mixed
mask half cites D-audit's matrix; the F-audit adds the generate side + the seam.
NOTE: an F0 test spy proves backend locally but does NOT emit F4's platform-side
per-column evidence - production evidence emission + aggregation for generation
is its own build slice (F3/F4 scope, see §4/§7), not the audit's. Closes Phase F.
(Program rule: CI fails if a column admitted to a Rust backend records zero
compiled calls - the per-generation witness satisfies it.)

## 4. Sequencing

F0 (contract/witness) -> F1 (bounded generation) with F3 (kernel selection)
alongside -> F2 (mixed promotion + decline-lifting) -> F4 (capacity gate) ->
F-audit (closes Phase F). Prereqs: R4 done (#271; also gives the ShadowCoordinator
headroom F2's promotion needs). Phase F after Phase E. F4 gated on the Phase E
100M evidence-capture confirmation and the bounded-memory finding (§2).

## 5. Distribution-integrity (parallel V1 pillar, not a Phase F slice)

Sizable V1 pillar whose natural home is alongside Phase F (fidelity spans
generation + masking). Honest order (do not reorder): measure source-vs-output
fidelity -> persist as audit evidence -> improve strategies on material drift ->
warn/fail policy gates. Primitives exist (engine `quality/`; platform
`api/quality/` best-effort hook + `job_quality_reports` + `/quality`); the
improve + gate stages are largely unbuilt, measure/persist are partial
(single-source/target only, best-effort, UI gaps). Standing honesty rule: do not
lead with distribution-preserving claims until measurement + persistence +
evidence are real; no DP / synthetic-privacy / automatic-utility claims. This
needs its OWN program plan, not folding into F0-F4; flag it for placement when
Phase F's generation substrate (F1/F3) is in, since the "improve" stage edits
generation strategies. Not on the F0-F4 critical path.

## 6. Verification + gates (per slice)

Per-slice: the generate-parity byte/schema/order contract (F0) as regression;
the executed-backend witness; seed-fixed byte-parity to the full-frame oracle;
isolated-process peak-RSS with a declared absolute ceiling for F1/F4; ci-mirror
clean; the standard gate chain (Codex plan-gate -> build -> dennis -> Codex
final -> CI). DOCUMENT per slice: roadmap + shipped-log current, engine graph
refreshed.

## 7. Open items / decisions for Cam

- Confirm F0 (tests-only contract + witness) as the first slice, or fold the
  contract into F1.
- F2 decline-lifting priority order (which declines to lift first:
  nullable/NaN generate output vs additional FK key types) - a product call once
  F2 planning starts.
- Distribution-integrity program placement + its own plan (sizable; §5).
- F4 does NOT offer an uncapped 100M run (removed per Codex plan-gate): F4
  requires predeclared absolute ceilings, bounded lower-tier evidence first, and
  enforced resource/abort limits before advancing to 100M. A diagnosis of the
  Phase E memory finding is not evidence the generation/mixed production path is
  bounded - F4 must demonstrate it on its own path.
- PLATFORM EVIDENCE TRANSPORT for generation/mixed is a named prerequisite for
  F4 (Codex plan-gate): Phase E establishes per-column Rust evidence capture for
  MASKING; it does NOT establish that the platform captures/transports generation
  (and generate->mask-seam) backend + timing evidence. That plumbing is a build
  slice F4 depends on - confirm whether it rides F3 or is its own slice.

cam
