# Mutation grading: `execution/_sequential.py` -- substrate bar 75%

TQ substrate sweep (branch `tq/substrate-sweep`), FULL-TRIAGE grade by
`scripts/tq_mutate.py` with default survived-bucket re-adjudication (finding #16
RESOLVED). `_sequential.py` is the SEQUENTIAL FK execution route:
`run_sequential` masks an FK-related job one table at a time in FK-topological
order (byte parity to the full-frame path), with quarantine row-error routing,
source-snapshot eviction, a streaming transactional sink, key-error bookkeeping
+ FK cascade, and group-anchor / top-code pre-mask snapshots. `table_topo_order`
is the Kahn table sort it relies on; `_has_transactional_write_contract` probes a
sink's write/commit/abort shape. Not crypto/RI, so the bar is **75% of LOGIC
mutants**.

## Numbers

**Killed 369/417 = 88.49% LOGIC (tool-native, 0 unresolved). 48 survivors: 29
proven equivalent + 16 accepted non-contract survivors (8 timing + 8 message
prose, see Taxonomy) + 3 precisely-characterized residual (see below).** Above the
75% bar (measured max(55.40 + 15, 75) = 75%). This module is NOT 0-residual: 3
mutants (131/137/141) are killable-but-deferred and documented as such.

TAXONOMY (Codex batch gate, honest labeling). Of the 45 non-residual survivors, 16
are killable but the sweep deliberately does not kill them (accepted non-contract),
NOT proven equivalent: the 8 timing mutants (102/145/147/149/230/231/232/234 --
`boundary_conversion_ms` arithmetic, killable via a controlled `time.perf_counter`
clock, see finding #18) and the 8 message-prose mutants (guardrail 28/30/31,
`relationship_cycle` 51-55). The other 29 ARE proven equivalent (cache-eviction
timing, invariant-conditioned no-ops, redundant sorts).

Graded at **`--jobs 1`** (deterministic). This selection carries a WALL-CLOCK
assertion (`test_result_reports_conversion_and_timings` pins
`boundary_conversion_ms > 0`), so a `--jobs 6` run flakes: it false-survived
`run_sequential` mut_146 (`conversion_ms += -> -=`, a genuine kill -- the sign flip
makes the timing negative) under concurrent-worker timing perturbation. mut_146
dies standalone and under the full single-threaded selection; the jobs=1 grade is
369/417, the jobs=6 flake was 368/417. See tq-findings #18.

Baseline (5 existing integration/unit files): 231/417 = 55.40%, 186 survived --
the dedicated coverage under-pins `run_sequential`'s quarantine / key-error /
sink / snapshot branches and `table_topo_order`'s Kahn logic. Full triage added
two kill files and adjudicated all 186 survivors: **138 additional kills**, 45
equivalent, 3 residual.

| Function | Killed by the sweep | Equivalent | Residual |
|---|---|---|---|
| `run_sequential` | 120 | 38 | 3 |
| `table_topo_order` | 14 | 7 | 0 |
| `_has_transactional_write_contract` | 4 | 0 | 0 |

## Kills

### `table_topo_order` (14) + `_has_transactional_write_contract` (4) -- `test_sequential_helper_kills.py`
Direct-call tests over a stub plan (`seed_envelope.per_table` only) + hand-built
`RelationshipGraph`: parent-before-child ordering (overriding seed order), a
two-parent child's indegree ACCUMULATION, the ready-queue position tiebreak
(`key=None`), the self-FK skip (a `==`->`!=` that fabricates a false cycle; the
`continue`->`break`), an edge-only table added once by NAME (`seen.add`/`append`
None), and the `relationship_cycle` coded error. The sink probe: a partial sink
(missing write / commit / abort) is not transactional, and the no-default
`getattr(..., "commit"/"abort")` raises instead of returning False.

### `run_sequential` (120) -- `test_sequential_run_kills.py`
End-to-end via `PandasExecutionAdapter.run_sequential`, asserting observable
machine fields: quarantine config parsing + the fail-closed no-output_path
guardrail (both operands of `q_enabled AND q_triggers`, incl. an
enabled-but-no-triggers job that must RUN), the fail-loud/quarantine per-table
row-error classification + `counts_by_trigger` accumulation, the key-error
bookkeeping + FK cascade (cross-table AND self-ref), the transactional-sink
commit/abort protocol (pre-existing-file refuse, committed-table alias refuse,
post-commit publish failure does not abort), group-anchor pre-mask snapshots
(`snapshot_missing` on a wrong key), keyed-mask wiring, byte parity across the FK
chain + a diamond, and the packaged `ExecutionResult` fields (row_errors,
quality_metrics, timings, boundary_conversion_ms `>0` and `<1000ms`).

## Non-residual survivors (45): 29 proven equivalent + 16 accepted non-contract

Each verified to survive the full 7-file selection standalone (rc 0). The first two
groups (27) + the two redundant sorts in the last group (2) = 29 PROVEN EQUIVALENT
(no test can kill them). The TIMING group (8) and the two prose bullets minus the
redundant sorts (8) = 16 ACCEPTED NON-CONTRACT (killable, deliberately not killed).

- **Memory / cache-eviction only (17)** -- proven equivalent. -- survive a full `.equals` byte-parity
  across the FK chain AND a diamond; they change only WHEN in-memory caches free,
  never output or errors: `run_sequential` 68, 69, 70 (remaining-consumer setup),
  268-277, 279 (source-snapshot / parent-map / group-anchor eviction blocks), 281,
  282, 284 (`pop` no-default / absent-key, where the key is always present by
  construction).
- **Redundant / no-op under reachable inputs (10):** `run_sequential` 6, 8
  (`bool(get("enabled", None))` == `bool(False)` when the key is present), 79
  (`relationship_graph=None` not read on this path -- graph threaded explicitly),
  80 (`namespace_registry=None` read only by the composite handler, not on this
  route), 112 (`_committed=None` used only as `not _committed`), 152 (dedup
  `and`->`or` whose delta re-copies an already-snapshotted equal value; the
  col-absent branch is compile-unreachable), 164, 166 (`.get(table, None/())`
  default only for a zero-work table, none exist), 226, 228 (`preserve_index`
  None vs False -- a default RangeIndex stores only as metadata, values identical).
- **Telemetry timing, ACCEPTED NON-CONTRACT (8):** `run_sequential` 102, 145, 147,
  149, 230, 231, 232, 234 -- `boundary_conversion_ms` arithmetic perturbations. The
  test suite's wall-clock assertion does not pin them, but a controlled
  `time.perf_counter` clock WOULD kill them (finding #18); they are deliberately
  left as accepted non-contract (telemetry, no product contract), NOT equivalent.
  The ABSOLUTE-clock leaks (`+t0`/`+t1`, mut 148/233) DID escape and are killed.
- **Message prose, ACCEPTED NON-CONTRACT (8) + redundant sort, EQUIVALENT (2):**
  `run_sequential` 28, 30, 31 (the no-output_path guardrail's explanatory sentence
  -- a plain `ValueError`, so the type is the machine field and the identifying key
  `output_path` is asserted; the prose is left as accepted non-contract, killable
  via full-message-equality); `table_topo_order` 51-55 (the `relationship_cycle`
  message prose, code pinned, accepted non-contract). The two EQUIVALENT here are
  35, 37 (the children-sort `key`, redundant -- the ready-queue re-sorts by
  position every iteration so child insertion order never surfaces).

## Residual (3) -- precisely characterized, killable, deferred

`run_sequential` 131, 137, 141: the lossless-int-typing column set at load
(`group_anchor_cols | top_code_cols`) -- mut 131 flips `|` -> `&`
(union -> intersection, dropping the top_code contribution), 137/141 null the
`.get(table)` key to `.get(None)`. A no-op for string / plain columns (the frame loads
byte-identically, which is why the string-anchor group_by test cannot reach
them). KILLABLE only with a null-bearing int64 group-anchor / top-code column
carrying a value >= 2**53, where dropping it from lossless typing lets pandas
widen it to float64 -- collapsing two entities whose keys differ only beyond
float64 precision onto one date-shift offset (group_by) or rounding the rendered
integer (top_code). Deferred, not attempted: the kill hinges on exact float64
integer-rounding at 2**53, an intricate and fragile fixture for 3 of 417 mutants.
The recipe is recorded here; flagged for a decision (build the >=2**53 lossless
fixture, or accept as a documented above-bar residual). Not a product bug -- the
lossless typing IS applied for these columns in the real code.

## Candidate findings

None. No mutation exposed a wrong quarantine verdict, a wrong sink commit/abort
outcome, a wrong FK-topo order, a leaked errored key, or a chunked/full-frame
byte divergence.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `src/decoy_engine/execution/_sequential.py`
and the test selection to the SEVEN files: `test_sequential_eviction.py`,
`test_transactional_sink.py`, `test_de10_fk_lossless_typing.py`,
`test_de03_output_projection.py`, `test_quarantine_row_errors.py` (the last under
`tests/unit/`), `test_sequential_helper_kills.py`, and
`test_sequential_run_kills.py`; then `rm -rf mutants && python scripts/tq_mutate.py
--run`. `source_paths` stays at the package root.
