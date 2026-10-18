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
- STRING fast-path: applies to EVERY admitted mixed job whose FK keys are strings (not just
  the F4 bench; the bench is representative because the `sequence` generator always emits
  strings, but any string-keyed mixed job takes this path). Write the resolved Arrow STRING
  array back DIRECTLY via pyarrow, NO `to_pylist`, NO pandas round-trip. This is the 100M
  memory win for string-keyed jobs.
- INTEGER fast-path: KEEP the existing `resolved.to_pylist()` -> `_write_back_fk_column`
  path unchanged. Integers are not the mixed-route 100M bottleneck (sequence keys are
  strings), and the integer value-dependent dtype (int32->int64 widening, uint64 edges) is
  exactly the fragile case the resolve plan-gate flagged; reproducing it in Arrow is not
  worth the risk for a non-bottleneck path. Document this split explicitly.

STRING write-back dtype (PINNED rule, fold P2-3; Codex probed PyArrow 24 and matched oracle
IPC bytes in all 20 string differential cases with exactly this):
- empty OR all-null (null_count == len, incl. length 0) -> `pa.nulls(length)` (the typed
  null array). Do NOT cast to `null` type via pc.cast: `large_string -> null` raises
  ArrowNotImplementedError (probed). `pa.nulls(length)` is the supported, byte-matching path.
- populated -> cast the resolved array to `pa.string()` (this covers large_string INPUT ->
  string OUTPUT, matching the oracle's pandas-object round-trip; probed byte-identical).
- The resolved array already holds the right VALUES (from index_in/take/if_else over string
  inputs); only its type is normalized by the above. No to_pylist, no pandas.
- CRITICAL byte-identity: the oracle produces its string column via a pandas object round
  trip. The Arrow-native result MUST produce identical CANONICAL Arrow IPC bytes (schema +
  values, using F2's metadata/chunk normalization). This is enforced by the acceptance
  (next section), not left to "confirm against the oracle": the rule above is pinned, and
  any case that still diverges STOPS the build.

Shared production + shadow via the one function (do not fork). WARN / FAIL / REMAP paths
unchanged (they act on the resolved array + orphan mask before write-back, already Arrow).

## 4. Acceptance

- tests/parity/test_phase_f_f2_mixed_parity.py + _sweep.py byte-identical +
  warning_signature-identical (string keys exercise the new path; integer keys exercise the
  unchanged path). No test changed to pass.
- The build's differential suite (tests/unit/execution/test_arrow_fk_resolve_differential.py,
  105 cases) stays green, INCLUDING the string dtype cases (all-null string, large_string,
  nonempty) now produced by the Arrow-native write-back. ADD a CANONICAL IPC byte comparison
  to the string differential cases (fold P2-3): compare the Arrow-native write-back output to
  the oracle output as normalized IPC bytes (F2's metadata/chunk normalization), not just
  value equality, so a string-vs-large_string or null-type divergence is caught. Cover
  empty, all-null, large_string input, and nonempty.
- Shadow parity + de10 lossless typing green.
- MEMORY (fold P2-4): re-run the F4 10M mixed tier (string FK, 2M/8M, 1% orphan) under the
  external supervisor. The <= 1000 MB figure is an EMPIRICAL target to verify, not a derived
  guarantee (removing the Python list removes ~600MB of the measured 1906MB, which predicts
  ~1300MB; the string output column + Arrow concat may add back, so measure). ACCEPTANCE:
  measure + assert whatever ceiling the run demonstrates, and it MUST be meaningfully below
  the 1906MB to_pylist result AND the 2009MB baseline. If it does not drop meaningfully,
  STOP and report (the Python list was supposed to be the dominant transient). Record the
  number; set the asserted ceiling to the observed peak rounded up with a small margin.
- ci-mirror clean; pytest-one both venvs (companion present + absent).

## 5. Risk

The one real risk is string-dtype byte-identity between the Arrow-native write and the
oracle's pandas-object round-trip. Mitigated by: (a) keeping the integer path on the proven
to_pylist write-back (no int dtype risk); (b) the PINNED rule (§3: pa.nulls for empty/all
null, cast to string otherwise) which Codex probed byte-identical on all 20 string cases;
(c) the canonical-IPC differential acceptance + F2 parity/sweep; (d) STOP-on-divergence.

FALLBACK (fold P1), pre-approved plan B with a COLUMN-WIDE dtype contract: if the pinned
Arrow rule ever diverges for a case, use a CHUNKED to_pylist write-back:
1. Decide the column-level output dtype ONCE from the WHOLE resolved array BEFORE chunking
   (empty/all-null -> pa.nulls; populated -> string). The dtype is a column decision, not a
   per-chunk one.
2. Iterate fixed-size batches: to_pylist(batch) -> build that batch AS the pre-decided dtype
   -> accumulate; the Python list is bounded to one batch. Concat the typed batches.
3. Every batch uses the SAME step-1 dtype (no per-chunk drift), so the concatenated column
   byte-matches the oracle while memory stays bounded to batch_size.
Not expected to be needed for strings (the pinned rule is probed byte-identical); it exists
so the builder has a specified fallback rather than improvising.

## 6. Gate chain

Codex plan-gate (this doc) -> build (Sonnet) -> ci-mirror + F2 parity/sweep + differential
(incl. canonical-IPC string cases) + shadow + de10 + the 10M memory measure+assert, both
venvs -> dennis -> Codex final ->
merge (blocked by the auto-mode classifier -> Cam clears the push) -> then the GCP 100M
mixed run. DOCUMENT: CHANGELOG + records note; refresh the engine graphify graph post-merge.
