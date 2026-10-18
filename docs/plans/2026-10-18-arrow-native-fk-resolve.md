Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, architecture, performance, testing, verification, use-established-methodology

# Arrow-native FK-key resolve for the F2 mixed route (100M fix)

Per Cam 2026-10-18 (fix-first for 100M). F4 measured the mixed route's FK-key resolve
as the dominant memory transient (~2GB at 10M child rows, 2-point-projected ~20GB at
100M -> would exceed the 8GiB ceiling). This replaces the per-row Python-object resolve
with a pyarrow-compute path, keeping output + warnings byte-identical, so 100M mixed
fits under 8GiB.

## 1. Problem (exact hotspot)

The whole Python-object transient is inside `resolve_generate_parent_fk_child`
(execution/_mixed_fk_bridge.py:75-97) + the `resolve_fk_keys` loop it calls
(execution/_fk_resolve.py:80-157):
- `_identity_parent_map` (bridge:100-116): `parent.to_pylist()` + 2 tuples + a dict
  entry PER PARENT ROW.
- `_child_key_tuples` (bridge:119-128): `child.to_pylist()` + one `(key,)` tuple PER
  CHILD ROW (the dominant transient over 10M-100M rows).
- `resolve_fk_keys` allocates its own `masked` list (N) + per-row dict `.get`.
- `_write_back_fk_column` (bridge:153-176): a whole-column pandas round-trip.
All scale O(child rows); nothing stays Arrow until the very end. The carrier
(_chunked_preresolved.py) + the mixed route (_pipeline_mixed_route.py `_build_carrier`)
are ALREADY Arrow; only the resolve that PRODUCES `.resolved` is the problem.

## 2. Goal

Produce the identical resolved FK `pa.Array` (+ identical warnings/errors) using
pyarrow compute over the two Arrow key columns, with no O(child-rows) Python-object
allocation. Byte-identical to the full-frame oracle AND to the shadow leg (both call
this same function - do NOT fork).

## 3. Design (pyarrow compute; reuse OOC precedents)

Rewrite `resolve_generate_parent_fk_child` to compute Arrow-natively for the
ADMITTED fast-path key types, keeping a fallback for the rest:
- Parent value_set: the parent output's key column, deduplicated keeping the LAST
  occurrence (today's dict is last-write-wins; `pc.index_in` returns the FIRST match,
  so build the value_set as last-wins-deduped parent keys, e.g. reverse+unique or a
  group-by-last, and carry the parent's preserved original values aligned to it).
- `indices = pc.index_in(child_keys, value_set=parent_keys_deduped)` -> per child row
  the matched parent index or null (no match / null child).
- `taken = pc.take(parent_values_deduped, indices)` -> matched parent preserved value.
- `orphan_mask = pc.and_(pc.is_null(indices), pc.is_valid(child_keys))` (a null child
  key is NOT an orphan). `orphan_count = pc.sum(orphan_mask).as_py()` -> PLAIN int.
- Resolved array (PRESERVE/WARN): `pc.if_else(orphan_mask, child_keys, taken)` (orphans
  keep source key, matches take parent value, nulls stay null). One Arrow array, no
  per-row Python.
- FAIL: if orphan_count>0 raise the EXACT `orphan_fk_violation` message
  resolve_fk_keys uses today. REMAP: if orphan_count>0 raise `orphan_remap_parent_missing`
  (_reject_remap's exact message). No-orphan: resolved == taken.
- WARN emission: reuse out_of_core/_join.py `orphan_fk_warning` (already byte-identical
  detail dict: code="orphan_fk", provider=edge.namespace, column=join(child_columns),
  detail{parent_table,parent_columns,child_table,child_columns,orphan_rows=int}). Emit
  ONE per edge only when WARN + orphans>0 (zero-orphan emits nothing).
- KEY-TYPE GATE (byte-identity safety): `fk_key_value` folds bool->int, whole-float->int,
  Decimal, NaN->null. Arrow equality != fk_key_value for those. So take the Arrow-native
  path ONLY when BOTH key columns are plain int (any width) or utf8/large_utf8 (where
  Arrow equality == fk_key_value); otherwise DELEGATE to the existing Python
  resolve_fk_keys path unchanged. (F2 already declines dirty/float/mixed shapes to
  full-frame, so the gate loses no admitted coverage; the Python path stays as the
  correctness-preserving fallback.)
- Write-back: replace `_write_back_fk_column`'s pandas round-trip with an Arrow-only
  cast to the SAME output type the oracle produces, modeled on out_of_core/_batch_join
  `_resolve_output_types` / `_cast_chunks` and _relation `_cast_masked_array`. PRESERVE
  the `out_of_core_fk_key_dtype_unsupported` (FK_KEY_DTYPE_UNSUPPORTED_CODE) rejection
  for the float+int>2**53 shape (lossless_fk_int_values). The resolved Arrow type MUST
  equal the oracle's exactly (no int32->int64 widening, no float64-on-null).

Shared by production + shadow: the function lives at execution level and `physical/
_shadow_fk.py::resolve_admitted_fk_node` delegates to it, so one rewrite covers both.

## 4. Acceptance (byte-identity is the bar)

- tests/parity/test_phase_f_f2_mixed_parity.py + _sweep.py ALL green, byte-identical
  (ipc_bytes) + warning_signature-identical to the oracle, unchanged: PRESERVE/WARN
  (one edge warning, orphan_rows==oracle count, plain int), FAIL (orphan_fk_violation),
  REMAP matched + orphan (orphan_remap_parent_missing), int keys > 2**53 (lossless
  typing), string + int keys, nullable keys, multi-chunk, streamed==resident. The
  dirty/float/when-reads-key/count-warning cases STILL DECLINE to full-frame (the gate
  must not newly admit them).
- Shadow parity (tests/physical shadow_fk/shadow_mixed) green (shared function).
- de10 lossless-typing regression (tests/unit/execution/test_de10_fk_lossless_typing.py)
  green.
- MEMORY: a test/bench showing the resolve no longer allocates O(child-rows) Python
  objects - re-run the F4 10M mixed tier and show the key-resolve transient dropped
  sharply vs the ~2GB baseline (the real proof the 100M fix works; exact 100M run is
  the separate GCP step).
- ci-mirror clean; pytest-one affected suites, companion present + absent.

## 5. Risk

The last-write-wins dedup is the subtle correctness point (index_in returns first
match) - must reproduce today's last-wins exactly; pin with a duplicate-parent-key
test. The key-type gate must be conservative (fall back to Python for anything not
provably Arrow-equality==fk_key_value) so no edge-case key silently diverges. The
Arrow-only write-back must match the oracle's exact dtype per policy (the parity IPC
byte check guards this). No new index_in/dictionary_encode exists in the repo yet; use
`import pyarrow.compute as pc` house style (or importlib with pc:Any if mypy stubs lag).

## 6. Gate chain

Codex plan-gate (this doc) -> build (Sonnet) -> ci-mirror + F2 parity/sweep + shadow +
de10 + the memory-drop check (10M), both venvs -> dennis -> Codex final -> merge (Codex
+ dennis green + local CI; GitHub CI ignored). Unblocks the GCP 100M mixed run (wire
bench_worker_mixed into the battery, then the one approved run). DOCUMENT: CHANGELOG +
records note.
