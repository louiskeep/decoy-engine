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

**Killed 372/417 = 89.21% LOGIC (tool re-grade reported 373; conservatively
adjusted to 372, -1 for the flaky timing kill -- see GRADE-RUN NOTE; 0 unresolved).
45 survivors: 29
proven equivalent + 16 accepted non-contract survivors (8 timing + 8 message
prose, see Taxonomy) + 0 residual.** Above the 75% bar (measured max(55.40 + 15,
75) = 75%). **RESIDUAL RESOLVED 2026-07-28:** the 3 formerly-deferred mutants
(131/137/141) are now KILLED by `test_sequential_lossless_residual_kills.py` (an
exact >= 2**53 float-precision fixture -- see "Residual: resolved" below), so this
module IS now 0-residual.

GRADE-RUN NOTE (honest count): the re-grade tool run reported 373/417 this session,
one above the 372 attributable here. The extra is timing mutant 231 (a
`boundary_conversion_ms` arithmetic mutant), which mutmut's in-process runner
flakily caught via the wall-clock assertion THIS run -- standalone it survives, and
the new residual-kill tests do NOT kill it. Per finding #18 that is a
non-deterministic timing catch, not a stable kill, so the stable count keeps 231 an
ACCEPTED NON-CONTRACT timing survivor and reports 372, not 373. (Killing 231
stably would need a controlled `perf_counter` clock, the deliberately-declined
approach.)

TAXONOMY (Codex batch gate, honest labeling). Of the 45 non-residual survivors, 16
are killable but the sweep deliberately does not kill them (accepted non-contract),
NOT proven equivalent: the 8 timing mutants (102/145/147/149/230/231/232/234 --
`boundary_conversion_ms` arithmetic, killable via a controlled `time.perf_counter`
clock, see finding #18) and the 8 message-prose mutants (guardrail 28/30/31,
`relationship_cycle` 51-55). The other 29 ARE proven equivalent (cache-eviction
timing, invariant-conditioned no-ops, redundant sorts).

Graded at **`--jobs 1`** (a MITIGATION, not a determinism guarantee -- jobs=1
removes concurrent-worker contention but a real-clock assertion is still exposed to
scheduler / GC jitter; see finding #18). This selection carries a WALL-CLOCK
assertion (`test_result_reports_conversion_and_timings` pins
`boundary_conversion_ms > 0`), so a `--jobs 6` run flakes: it false-survived
`run_sequential` mut_146 (`conversion_ms += -> -=`, a genuine kill -- the sign flip
makes the timing negative) under concurrent-worker timing perturbation. mut_146
dies standalone and under the full single-threaded selection; the jobs=1 grade is
372/417 (369 before the residual kills landed); the earlier jobs=6 flake dropped
mut_146 for a 368 read. See tq-findings #18.

Baseline (5 existing integration/unit files): 231/417 = 55.40%, 186 survived --
the dedicated coverage under-pins `run_sequential`'s quarantine / key-error /
sink / snapshot branches and `table_topo_order`'s Kahn logic. Full triage added
three kill files and adjudicated all 186 survivors: **141 additional kills**, 29
proven equivalent, 16 accepted non-contract, 0 residual.

| Function | Killed by the sweep | Proven equiv | Accepted non-contract | Residual |
|---|---|---|---|---|
| `run_sequential` | 123 | 27 | 11 (8 timing + 3 prose) | 0 |
| `table_topo_order` | 14 | 2 | 5 (prose) | 0 |
| `_has_transactional_write_contract` | 4 | 0 | 0 | 0 |

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

Each verified to survive the full 8-file selection standalone (rc 0). The first two
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
  left as accepted non-contract, NOT equivalent -- `boundary_conversion_ms` is a
  public `ExecutionResult` field, but its exact timing ARITHMETIC is scoped OUT of
  the TQ contract (a scope decision, same as the pandas-adapter ledger).
  The ABSOLUTE-clock leaks (`+t0`/`+t1`, mut 148/233) DID escape and are killed.
- **Message prose, ACCEPTED NON-CONTRACT (8) + redundant sort, EQUIVALENT (2):**
  `run_sequential` 28, 30, 31 (the no-output_path guardrail's explanatory sentence
  -- a plain `ValueError`, so the type is the machine field and the identifying key
  `output_path` is asserted; the prose is left as accepted non-contract, killable
  via full-message-equality); `table_topo_order` 51-55 (the `relationship_cycle`
  message prose, code pinned, accepted non-contract). The two EQUIVALENT here are
  35, 37 (the children-sort `key`, redundant -- the ready-queue re-sorts by
  position every iteration so child insertion order never surfaces).

## Residual: resolved (2026-07-28) -- 131/137/141 now KILLED

`run_sequential` 131, 137, 141 targeted the lossless-int-typing column set at load
(`fk_columns | group_anchor_cols.get(table) | top_code_cols.get(table)`) -- mut 131
flips the second `|` -> `&` (union -> intersection, dropping the top_code
contribution), 137/141 null the `.get(table)` key to `.get(None)` (dropping the
group-anchor / top_code column). Each drops a column from lossless typing, so a
null-bearing int64 column widens to float64, which cannot represent every integer
past 2**53 exactly and rounds the non-representable ones (e.g. 2**53 + 1 -> 2**53).

Previously deferred as an intricate fixture; now KILLED by
`test_sequential_lossless_residual_kills.py` (2 tests through `run_sequential`):
- **top_code (kills 131 + 141):** a null-bearing int64 `top_code` column carrying
  an in-range `-(2**53 + 1)` renders its EXACT decimal string; under the mutant the
  widened float rounds it to `-(2**53)` and a different integer is emitted.
- **date_shift group_by (kills 137):** a null-bearing int64 `group_by` anchor with
  two ids differing only beyond float64 precision (`2**53` vs `2**53 + 1`); under
  the mutant they collapse to one double and `_canonicalize_source` fails closed on
  the float (S5 ban), so the distinct-shift outcome cannot be produced.

Discrimination proven by manual per-mutant simulation (drop each contribution ->
the matching test fails) and confirmed by the mutmut re-grade (all 3 absent from
the survivor set; module 372/417 stable after the -1 flaky-231 adjustment, 0
residual). Never a product bug --
lossless typing IS applied for these columns in the real code; the tests lock it.

## Candidate findings

None. No mutation exposed a wrong quarantine verdict, a wrong sink commit/abort
outcome, a wrong FK-topo order, a leaked errored key, or a chunked/full-frame
byte divergence.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `src/decoy_engine/execution/_sequential.py`
and the test selection to the EIGHT files: `test_sequential_eviction.py`,
`test_transactional_sink.py`, `test_de10_fk_lossless_typing.py`,
`test_de03_output_projection.py`, `test_quarantine_row_errors.py` (the last under
`tests/unit/`), `test_sequential_helper_kills.py`, `test_sequential_run_kills.py`,
and `test_sequential_lossless_residual_kills.py`; then `rm -rf mutants && python
scripts/tq_mutate.py --run --jobs 1` (jobs=1 -- the selection carries a wall-clock
assertion, finding #18). `source_paths` stays at the package root.
