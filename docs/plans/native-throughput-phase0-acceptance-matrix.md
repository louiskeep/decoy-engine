Status: draft for plan-review GO

# Phase 0 Task 0.4: Acceptance matrix

Defines correctness before any implementation changes, for the Execution
Consolidation and Native Throughput program. Every planned failure has an
observable expected result. The pinned pandas full-frame path is the oracle
throughout. No frozen target may be weakened to make a result pass (plan §6.7).
Existing gate assets are reused, not re-derived: `tests/parity/native/test_phase2_gate.py`
(the frozen W2 correctness/route/perf gate and its `_assert_gate_parity`),
`test_e2e_certification.py` (scattered-null property + 100M sampled slice), and
the committed baseline harness under `scripts/native-baseline/`.

## 1. Byte / logical parity (against the oracle)

| Axis | Cases | Expected | Where |
|---|---|---|---|
| Values | every admitted strategy (hash/redact/truncate/passthrough; Phase 2 adds deterministic faker) over the frozen W2 columns | byte-identical to oracle | `_assert_gate_parity` |
| Types | utf8, large_utf8, int (all widths), bool, timestamp-with-tz as hash inputs | identical Arrow type + values; unsupported input type rejected at eligibility, not executed | `test_phase2_gate` type set + Task 1.1 per-type |
| Nulls | whole-column all-null, no nulls, and scattered nulls at arbitrary row positions across every nullable column, at varying chunk boundaries | null-in / null-out preserved position-for-position | `test_e2e_certification::...scattered_nulls` (Hypothesis) |
| Row order | output row order equals input row order | identical, independent of batching/threads | gate + Task 1.5 order assembly |
| Warnings | warning set + global warning indices | identical to oracle | gate parity extended per plan §3 (global-index fix) |
| Row errors | first-failure row + coded reason | same first failure as oracle; parallel completion order does not change it | Task 1.5 exit, plan §9.4 |
| Determinism | same output across processes, across batch sizes (1/5/11/50k), across thread counts (1/2/4/8) | byte-identical | Task 1.5 / 2.2 exit gates |

## 2. Batch-size x thread-count matrix

Batch sizes {1, 5, 11, 50000} crossed with native thread counts {1, 2, 4, 8}.
Every cell must produce byte-identical output to the 1-thread / oracle result.
Thread count must not affect any output byte (plan §9.4). Applied to keyed hash
(Phase 1) and `derive_index_batch` (Phase 2).

## 3. Companion-present / companion-absent

| State | Expected | Where |
|---|---|---|
| companion present, ABI-compatible | native route admitted; `compiled_kernel_executed=true`; kernel call counters > 0 | route-proof (plan §6.6) |
| companion absent (portable install) | preflight records `native_unavailable`, uses the approved oracle/fallback; no silent partial exec | plan §9.2, Task 3.1 |
| companion ABI-incompatible | preflight reroutes to oracle (portable) or refuses worker startup (official image); coded reason | plan §9.2, Task 3.2 |
| official production image, companion missing | worker startup fails (installation failure), does not accept jobs degraded | plan §9.2, Task 3.2 exit |

## 4. Packaging / clean-install

Per supported target (see host/OS matrix in Task 0.1 §7.5): clean source install
WITHOUT the companion imports and runs the oracle; clean wheel install WITH the
companion imports the correct ABI and runs a parity smoke test; exact core-to-
companion version rule enforced; x86-64 and ARM64 images both build/install/import.
(Task 3.1 / 3.2 gates.)

## 5. Controlled failures

| Failure | Injected how | Expected observable |
|---|---|---|
| Native panic | forced panic in kernel over a batch | coded engine error, no partial array crosses the boundary; table not restarted on oracle after staging began | plan §9.3 |
| ABI mismatch | stale/corrupt companion | worker startup rejection (official) or preflight reroute (portable), coded reason | plan §9.2 |
| Schema drift | input Arrow schema != physical-plan schema | preflight/operator coded rejection before output | plan §8, §9.1 |
| Disk exhaustion | fill spill dir mid-run | coded error, no corrupt/partial publish; atomic sink leaves prior state intact | inventory risk E2; plan §9.3 |
| Cancellation | cancel mid-batch | no partial publish; clean teardown | plan §9.3 |

## 6. Mutation targets (changed security + routing units)

Mutation grading (no unreviewed value/type/error survivor) on the units each slice
changes:
- Phase 1: `decoy-engine-native/src/derive.rs` (hkdf_key, derive), `src/batch.rs`
  (derive_array, the new cached-key context, Rayon range assembly). Security-
  sensitive: full value/type/error coverage required (crypto bar).
- Phase 2: `derive_index` contract + `derive_index_batch`; `src/decoy_engine/generation/pool/_sampler.py`.
- Phase 3+: route-selection units the physical-plan compiler subsumes
  (`_pipeline_routing.py`, `_route_policy.py`, `native/_dispatch.py`, and the
  platform eligibility twins named in the Task 0.3 inventory §B).

## 7. Inventory-driven acceptance additions (from Task 0.3)

The route inventory surfaced contract risks the matrix must also pin:
- E1 (HIGH): the `admission_fk.py` docstring claiming the OOC route is unreachable
  is stale (it IS reachable via `v2_orchestrator.py:303`). Correct the contract doc;
  add a test asserting the OOC route is reachable from the platform runner.
- E2 (HIGH): publication atomicity has two owners (engine sink vs caller-materialize).
  The §8.3 unified coordinator must absorb both; acceptance for any migrated slice
  includes an atomic-publish + crash-before-commit test on BOTH prior owners' paths.
- B1: route eligibility decided twice (engine vs platform twins). The physical-plan
  compiler's shadow-mode gate (Task 4.3/4.4) must match every current route decision
  in the acceptance corpus before it owns the decision.

## 9. Phase 1 parallel-execution criteria (Phase 0 gate additions, Codex)

Binding on Tasks 1.4-1.6 (GIL release + Rayon), beyond the batch x thread parity
matrix in §2:

| Criterion | Case | Expected |
|---|---|---|
| GIL-release proof | a Python sentinel thread runs while the native kernel computes a large batch | the sentinel makes observable progress during the compute interval (proves `Python::detach`; concurrent-call tests alone do not) |
| Multi-error arbitration | failures placed in DIFFERENT Rayon ranges and batches | the reported first error is the minimum global row index, never task-completion order |
| Worker panic | panic injected INSIDE a Rayon worker | coded engine error, no partial array crosses the boundary |
| Threaded scratch | per-batch transient scratch at threads {1,2,4,8}, frozen 50k batch | <= 2x input Arrow bytes, excluding the returned output buffer, at every thread count |
| Precedence x threads | empty / all-null / wrong-key / empty-namespace (Task 1.2) re-run across thread counts | identical coded behavior regardless of thread count |
| Pool ownership | one long-lived Rayon pool, explicit lifetime, aggregate bound across concurrent jobs | no pool constructed per 50k batch; thread count never exceeds the approved budget |
| Small-batch non-regression | batches {1,5,11} x threads {1,2,4,8} | native wall does not regress vs 1-thread (thread-pool spin-up must not dominate tiny batches) |
| Per-tier non-regression floor | every W2 tier, native vs oracle | native wall <= oracle wall at that tier (Task 1.6 asserts this, not just the absolute 600 s) |

Mutation bar (crypto + routing units): "zero non-equivalent value / type / error /
arbitration survivors"; each equivalent or unreachable mutant is documented
individually, not left as an unreviewed survivor.

## 8. Exit gate
This matrix passes plan-review GO with all BLOCKER/HIGH findings closed (dennis +
Codex). It is the correctness contract Phases 1-6 build against.
