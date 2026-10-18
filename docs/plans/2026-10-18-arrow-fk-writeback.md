Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, architecture, performance, testing, verification, use-established-methodology

# Arrow-native write-back for the FK resolve (bound the 100M memory)

Increment on the merged-pending Arrow-native FK resolve (feat/arrow-native-fk-resolve).
The resolve itself is now Arrow-native and ~2x faster, but the F4 build measured that it
does NOT hit the 100M memory goal, because the write-back still materializes a full Python
list of the resolved values. This slice removes that for the case that matters.

## 1. Problem

`resolve_generate_parent_fk_child` (execution/_mixed_fk_bridge.py) now resolves keys with
pyarrow compute (index_in / take / if_else) -- cheap and bounded. But it then does
`resolved.to_pylist()` and hands that Python list to the UNCHANGED `_write_back_fk_column`
(a pandas round-trip). The build measured: the Arrow compute is ~0.5s; the write-back is
~8s and ~600MB at 10M. The mixed route's FK keys are STRINGS (the `sequence` generator
emits strings, parity-frozen to V1), so `to_pylist()` of N child-row strings dominates and
scales linearly: 10M -> peak RSS 1906MB; projected ~19GB at 100M (80M child strings), which
blows the 8GiB target. The resolve speedup is real but insufficient alone.

## 2. Goal

For the Arrow fast-path, write the resolved values back WITHOUT a full Python-list
materialization, byte-identical to the full-frame oracle and the shadow leg, so the STRING
mixed route fits the 100M 8GiB target. Integer keys keep the existing (correct) path.

## 3. Design: Arrow-native write-back for STRING keys; integer keys unchanged

SCOPE SPLIT (deliberate, risk-minimizing):
- STRING fast-path (the realistic mixed-route case): write the resolved Arrow STRING array
  back DIRECTLY via pyarrow, NO `to_pylist`, NO pandas round-trip. This is the entire 100M
  memory win, because the mixed route's keys are strings.
- INTEGER fast-path: KEEP the existing `resolved.to_pylist()` -> `_write_back_fk_column`
  path unchanged. Integers are not the mixed-route 100M bottleneck (sequence keys are
  strings), and the integer value-dependent dtype (int32->int64 widening, uint64 edges) is
  exactly the fragile case the resolve plan-gate flagged; reproducing it in Arrow is not
  worth the risk for a non-bottleneck path. Document this split explicitly.

STRING write-back dtype (must match the oracle BYTE-for-BYTE):
- The oracle's string FK output dtype is value-dependent (the build's differential tests
  pin: all-null string -> null type; large_string input -> string output; nonempty ->
  string). Reproduce it from the resolved Arrow array WITHOUT Python:
  - all-null (null_count == len) -> cast to the oracle's all-null string dtype (null type
    per the differential oracle).
  - otherwise -> cast to the oracle's nonempty string dtype (string / large_string per the
    oracle; confirm which via the differential reference, do not guess).
  Use `pc.cast` (or an Arrow array builder) directly on the resolved array. The resolved
  array is already the right VALUES (from index_in/take/if_else over string inputs); only
  its type needs to match the oracle's chosen string dtype.
- CRITICAL byte-identity check: the oracle path produces its string column via a pandas
  object round-trip. The Arrow-native cast MUST produce the identical Arrow IPC bytes
  (schema + values). This is the main risk; the differential suite + F2 parity/sweep are
  the guard. If any case diverges, the build STOPS and reports (do not weaken the oracle).

Shared production + shadow via the one function (do not fork). WARN / FAIL / REMAP paths
unchanged (they act on the resolved array + orphan mask before write-back, already Arrow).

## 4. Acceptance

- tests/parity/test_phase_f_f2_mixed_parity.py + _sweep.py byte-identical +
  warning_signature-identical (string keys exercise the new path; integer keys exercise the
  unchanged path). No test changed to pass.
- The build's differential suite (tests/unit/execution/test_arrow_fk_resolve_differential.py,
  105 cases) stays green, INCLUDING the string dtype cases (all-null string, large_string,
  nonempty) now produced by the Arrow-native write-back. Extend it if the string write-back
  adds observable dtype cases.
- Shadow parity + de10 lossless typing green.
- MEMORY: re-run the F4 10M mixed tier (string FK, 2M/8M, 1% orphan) under the external
  supervisor. ACCEPTANCE: total external peak RSS <= 1000 MB at 10M (vs 1906MB with the
  to_pylist write-back, and the F4 baseline 2009MB). Assert the numeric ceiling. The
  remaining memory should be the Arrow carrier + output column only, no O(child-rows)
  Python list. If it misses, STOP and report the number (do not move the target).
- ci-mirror clean; pytest-one both venvs (companion present + absent).

## 5. Risk

The one real risk is string-dtype byte-identity between the Arrow-native cast and the
oracle's pandas-object round-trip (string vs large_string, all-null -> null). Mitigated by:
(a) keeping the integer path on the proven to_pylist write-back (no int dtype risk); (b) the
105-case differential suite + F2 parity/sweep pinning exact dtype + IPC bytes; (c) STOP-on-
divergence. If the string cast cannot match the oracle exactly, fall back to a CHUNKED
to_pylist write-back (bounded batch size) as plan B -- still bounds memory, lower risk,
noted here so the builder has a pre-approved fallback rather than improvising.

## 6. Gate chain

Codex plan-gate (this doc) -> build (Sonnet) -> ci-mirror + F2 parity/sweep + differential
+ shadow + de10 + the 10M <=1000MB memory assertion, both venvs -> dennis -> Codex final ->
merge (blocked by the auto-mode classifier -> Cam clears the push) -> then the GCP 100M
mixed run. DOCUMENT: CHANGELOG + records note; refresh the engine graphify graph post-merge.
