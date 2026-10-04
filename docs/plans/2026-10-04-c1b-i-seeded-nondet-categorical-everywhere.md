Status: plan (revision 3: Codex plan-gate round 2 folded, author = Opus). Awaiting Codex plan-gate round 3.

Date: 2026-10-04. Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C, slice **C1b-i: "Non-deterministic categorical becomes reproducible (seeded, position-keyed); determinism metadata becomes truthful; ALL routing behavior is held constant."** Branch `feat/c1b-seeded-nondet-categorical` off engine main `c4ea0f37`. Risk **R2**.

**Central principle (Codex round 2, BLOCKER 1).** In this engine the "unseeded" label is NOT passive metadata: it is a LIVE ROUTE GATE in several places (multi-table split admission; out-of-core compatibility; native admission; the chunked veto). So C1b-i must do two separable things and keep them separate:
1. **Determinism metadata becomes truthful:** the draw is now seeded + position-keyed, so every place that CLASSIFIES it as unseeded is corrected to "seeded".
2. **Route safety is held constant:** every route whose positional implementation is deferred to C1b-ii (multi-table split, out-of-core, native kernel, chunked native) stays closed, now via HONEST temporary vetoes ("position-keyed implementation deferred"), NOT via the false "unseeded" label. C1b-i changes NO routing outcome: the exact set of jobs that ran whole-frame before still run whole-frame, now reproducibly.

C1b-ii later removes the temporary route vetoes as it implements each positional route.

Revision 3 note (Codex round 2: 1 BLOCKER, 2 HIGH, 1 MEDIUM): reframed around metadata-vs-route-safety (BLOCKER 1); multi-table split keeps its no-split behavior via a new truthfully-named veto `POSITION_KEYED_CATEGORICAL_SPLIT_DEFERRED` (replacing the categorical entry in `UNSEEDED_RANDOM_STRATEGIES`); out-of-core keeps rejecting under a truthful "positional deferred" reason (its impl still raises for deterministic=False); native admission is EXPLICITLY FROZEN (prepare_categorical + physical operator keep rejecting deterministic=False) (HIGH 2); expanded the stale-prose inventory and softened "do not touch _chunked_categorical.py" to "do not alter its predicate/code/outcome, prose only" (HIGH 3); corrected the nested wording to whole-frame leaf-ordinal-from-zero (MEDIUM 4); key encoding uses the PUBLIC `decoy_engine.kernel.encode_int` re-export (round-2 open-Q1).

**Owner decisions (Cam):** Option A (reproducible); split into C1b-i / C1b-ii; nested = flattened-leaf-position. **Research-first:** reuse `derive_index` + the `windowed_date` position-keyed pattern; no new primitive in C1b-i.

## 1. Goal and scope

Replace non-deterministic categorical's unseeded whole-column draw (`_categorical.py:214-231`) with a seeded, position-keyed draw, make the engine's determinism metadata truthful (seeded), and hold every route's admission outcome constant.

New draw (non-null row at global index `g = ctx.row_offset + local_index`; whole-frame uses `row_offset = 0`, so `g` is the positional index):
- Uniform: `idx = derive_index(ctx.mask_key, plan.namespace, encode_int(g), pool_size=len(categories))`.
- Weighted: `bucket = derive_index(ctx.mask_key, plan.namespace, encode_int(g), pool_size=_WEIGHTED_CDF_RES)` then shared CDF `bisect.bisect_right(cdf, bucket)` (clamp).
- Nulls positional (restored from the source null mask).
- `encode_int` = the PUBLIC `decoy_engine.kernel.encode_int` re-export (`kernel/_canonicalize.py:8`), the SAME canonical integer encoding `derive_index_batch` applies to an integer column (round-2 open-Q1 confirmed Python == Rust). So when C1b-ii feeds an int global-index column to the batch kernel, its output is byte-identical to this oracle output with no rework. Do not introduce a new private encoder.

**One global contract (round-1 B2): no unseeded categorical path remains.** Every non-deterministic categorical draw is seeded. Native-inadmissible shapes (numeric/non-string categories, non-string source) run the SEEDED oracle. Missing namespace fails (`categorical_requires_namespace`). `from_profile` stays unavailable to chunked (unchanged).

**Routing held constant (the split boundary).** C1b-i opens NO new route. These stay closed, each under a truthful "positional implementation deferred to C1b-ii" reason:
- Multi-table split (section 4.3).
- Out-of-core (section 4.4).
- Native kernel / full-frame physical admission (section 4.5, frozen).
- Chunked native route (the existing veto, section 4.6; predicate/code/outcome unchanged, prose corrected).

OUT of scope (-> C1b-ii): removing any of those temporary vetoes, the native integer-key batch path, oracle-chunked `row_offset` activation, `when:`/FK hazard gates, chunked output-type pin, chunked evidence.

## 2. Established facts (engine HEAD c4ea0f37; probe + Codex round 2)

- Unseeded branch `_categorical.py:214-231`; deterministic template `:180-213`; `derive_index` accepts arbitrary bytes (`determinism/_derive.py:278-310`); windowed_date precedent `transforms/windowed_date.py:216-219`.
- Public encoder: `decoy_engine.kernel.encode_int` (`kernel/_canonicalize.py:8`) over `generation/pool/_canonicalize.py:44` (Python) == `decoy-engine-native/src/canonicalize.rs:71,190` (Rust); the reference batch path canonicalizes each value before scalar `derive_index` (`native/_index_ext.py:290`).
- `ctx.row_offset` default 0 (`_adapter.py:189,198`); whole-frame uses 0.
- **LIVE ROUTE GATES keyed on the "unseeded" label (round-2 BLOCKER 1 / open-Q4):**
  - Multi-table split: `UNSEEDED_RANDOM_STRATEGIES` includes categorical (`_pipeline_multi_table.py:58`); `unseeded_random_nodes()` reports non-det categorical (`:78`); `_job_gates_hold()` requires an empty result to permit splitting (`:97`). Removing categorical here WOULD allow sibling-table splitting.
  - Out-of-core: `out_of_core/_compat.py:300` rejects this variant; that verdict controls routing (`_pipeline_routing.py:419,504`); the OOC categorical impl RAISES for `deterministic=False` (`out_of_core/_mask_group_b.py:357`) -- so removing only the compat rejection routes into an unsupported impl.
  - Chunk routing is independent: `check_chunked_compatibility` veto at `_chunked.py:310` (the protocol-provider classification does NOT by itself make it chunk-eligible, round-2 open-Q2).
- **Native admission deliberately closed to non-det (round-2 HIGH 2):** `prepare_categorical` rejects `deterministic=False` (`native/_categorical_prepared.py:39`); physical operator asserts deterministic/source-keyed (`physical/_shadow_operators.py:210`); pinned by `tests/physical/test_shadow_categorical.py:200,213`.
- **Stale "unseeded" prose/metadata to correct (round-2 HIGH 3, expanded inventory):** `_determinism_protocol.py:179-194`; `_draw_site_providers.py:815-848,899-902`; `native/_capabilities.py:214`; `native/_categorical_prepared.py:42`; `physical/_plan.py:96`; `physical/_shadow_operators.py:213`; `_chunked_categorical.py:6,62`; `docs/native/draw-site-inventory.md:208,491`; `tests/native/test_determinism_protocol.py:339-351`; `tests/physical/test_shadow_categorical.py:200`.
- **Nested (round-2 MEDIUM 4):** `_strategies/_nested.py:361,375,404,427` collects leaves in deterministic outer-row/deepest-first order, builds a fresh RangeIndex frame, passes the original ctx unchanged (`row_offset` is the outer invocation's, default 0). There is NO separate flattened `row_offset`; reproducibility holds because nested is whole-frame and the leaf ordinal starts at zero.
- **Old-contract tests:** `test_shuffle_categorical.py:212-221` (two-runs-differ); `test_categorical_weighted.py:433-454,510-531` (numpy-RNG-impl pins).

## 3. Semantics decision (round-1 confirmed correct)

Seeded, position-keyed via the global row index using `derive_index` + the shared integer CDF (round 1: `bisect_right` == native `searchsorted(side="right")`; distribution CDF-quantized to 1,000,000). Output independent of source value except nullness: reproducible, not value-joinable. `when:`/FK hazards do not arise in C1b-i (no chunking); they are C1b-ii.

## 4. Implementation

1. **Oracle strategy (`_categorical.py`).** Replace the unseeded else-branch (`:214-231`) with the seeded position-keyed draw (section 1): `g = ctx.row_offset + i`, `derive_index(ctx.mask_key, plan.namespace, encode_int(g), pool_size=...)` + (weighted) shared CDF. Import `encode_int` from `decoy_engine.kernel`. Require `plan.namespace`. Nulls positional. Rewrite the module docstring `:6-10`.
2. **Determinism-protocol metadata (truthful).** `_determinism_protocol.py:179-194` + `_draw_site_providers.py:815-848,899-902`: reclassify this draw site as SEEDED (entropy-root = mask_key/seed), partitionable by row position, consistent with windowed_date. Update `native/_capabilities.py:214` and the durable `docs/native/draw-site-inventory.md:208,491`. This is metadata only; it does NOT by itself open any route (round-2 open-Q2).
3. **Multi-table split: hold behavior via an honest veto (BLOCKER 1).** In `_pipeline_multi_table.py`: REMOVE categorical from `UNSEEDED_RANDOM_STRATEGIES` (it is no longer unseeded) and ADD a new truthfully-named route veto `POSITION_KEYED_CATEGORICAL_SPLIT_DEFERRED` (or extend `unseeded_random_nodes`/`_job_gates_hold` with a parallel "positional-deferred" set) so a table containing non-det categorical STILL does not split, and sibling tables still do not start splitting. Net split behavior is IDENTICAL to today. C1b-ii removes this veto when it implements the split path.
4. **Out-of-core: keep rejecting under a truthful reason (BLOCKER 1).** `out_of_core/_compat.py:300`: keep the rejection; change its REASON text from "unseeded" to "position-keyed implementation deferred (C1b-ii)". Do NOT admit it (the impl `_mask_group_b.py:357` still raises for `deterministic=False`). Routing (`_pipeline_routing.py:419,504`) outcome unchanged. C1b-ii implements the OOC positional path and lifts this.
5. **Native admission FROZEN (HIGH 2).** Keep `prepare_categorical(deterministic=False)` native-inadmissible (`native/_categorical_prepared.py:39`) and the physical operator's deterministic/source-keyed assertion (`physical/_shadow_operators.py:210`). Correct their prose ("unseeded" -> "position-keyed variant not implemented by the source-keyed native operator") at `_categorical_prepared.py:42`, `physical/_plan.py:96`, `physical/_shadow_operators.py:213`. Behavior unchanged.
6. **Chunked veto: predicate/code/outcome UNCHANGED, prose only (HIGH 3).** Do NOT alter the predicate, error code `categorical_nondeterministic_not_chunk_safe`, or admission outcome in `_chunked_categorical.py` / `_chunked.py:310`. DO update its docstring/error-message prose (`_chunked_categorical.py:6,62`) from "unseeded vector" to "position-keyed; chunked implementation deferred to C1b-ii". (This narrows the earlier absolute "do not touch".)
7. **Docs:** CHANGELOG + compatibility-contract (determinism-contract change on the whole-frame path). Barry pass with exact-HEAD receipt. Roadmap + RECENTLY-SHIPPED on merge.

## 5. Acceptance tests (written before implementation; red-before recorded)

1. **Reproducibility (whole-frame).** Same job/seed/data -> byte-identical across runs (uniform + weighted); different mask_key/namespace -> different output (KAT vectors, frozen literal expected indices, round-2 note on M8).
2. **Position-keyed, not value-keyed (KAT-pinned).** (a) same value, two positions -> can differ; (b) two different values, same index -> same category; (c) exact output == direct scalar `derive_index(mask_key, ns, encode_int(g), pool_size)` with FROZEN LITERAL expected indices (not recomputed by the helper under test); (d) null-position pin: replacing a null at position g with a non-null does not change later rows.
3. **Key-encoding forward-compat.** `encode_int(g)` equals `decoy_engine.kernel.encode_int(g)` and the bytes the batch kernel canonicalizes an int column to (so C1b-ii matches). Direct equality.
4. **Distribution.** Frequencies match weights/uniform within tolerance (retargeted skew/validity tests).
5. **Determinism-mode separation.** Deterministic categorical unchanged (C1 bytes); non-det now seeded.
6. **Determinism-protocol metadata.** `test_determinism_protocol.py:339-351` updated to assert SEEDED/partitionable; provider registry reports seeded; `native/_capabilities.py` consistent.
7. **ROUTE-REGRESSION (BLOCKER 1, the split-boundary guard).** With a non-det categorical present: (a) multi-table does NOT split (same as today) and sibling tables do not split; (b) out-of-core compatibility STILL rejects it; (c) automatic AND explicit out-of-core routes do NOT admit it; (d) the chunked error code `categorical_nondeterministic_not_chunk_safe` is unchanged; (e) native admission (full-frame physical) STILL rejects deterministic=False (the HIGH-2 no-widening pin; keep `test_shadow_categorical.py:200,213`, reword only). Each asserts the exact reason/code.
8. **Nested (MEDIUM 4).** Whole-frame nested categorical is reproducible using the flattened leaf ordinal from zero; test multiple leaves per outer row, sparse unmatched rows, and null leaves. Record that chunked nested global-leaf offsets are out of scope.
9. **Multi-table + out-of-core sentries migrated.** The sentries/fixtures assert seeded determinism metadata AND the deferred-route veto (not "unseeded"); out-of-core group-B parity reflects the seeded contract where it runs whole-frame.
10. **Old-contract migration.** Inventory (section 2 + an `rg` sweep) every test asserting the old unseeded/numpy-RNG behavior; migrate to the seeded contract; no test weakened/skipped/xfailed.

Record red-before and green-after.

## 6. Risk, rollback, docs, gates

- **Risk R2.** Operator determinism-contract change + truthful reclassification, with ALL routing outcomes held constant via honest temporary vetoes. Chief risk (round-2 BLOCKER 1): accidentally opening a deferred route; the route-regression tests (5.7) + the frozen native admission (5.7e) guard it. Secondary: an internally inconsistent half-reclassification; the inventory (section 2) + metadata test (5.6) guard it.
- **Rollback:** revert the branch. No persisted state.
- **Clean-env check:** `ci-mirror` at the final gate.
- **Gates:** Codex plan-gate -> Sonnet tests-first build -> dennis -> Codex final -> merge on local gates with Cam's go. Rebase onto latest main before merge.

## 7. Resolved design decisions (Codex round-2 answers folded)

1. **Key encoding:** use the public `decoy_engine.kernel.encode_int` (`kernel/_canonicalize.py:8`); Python == Rust `encode_int`; it is what the batch kernel applies, so C1b-ii is byte-identical. No new private encoder.
2. **Classification vs routing:** the protocol provider classification does NOT make the operator chunk-eligible (chunk routing uses `check_chunked_compatibility`, veto `_chunked.py:310`). BUT the multi-table "unseeded" set and the OOC compat verdict ARE live route gates; C1b-i replaces them with honest "positional-deferred" vetoes so routing is unchanged.
3. **Nested:** flattened-leaf keying is reproducible for the whole-frame path (deterministic leaf order, zero-based leaf ordinal); no outer-row coupling; do NOT claim a general flattened `row_offset`. Chunked nested offsets out of scope.
4. **Other routing consumers:** multi-table split + OOC compat change behavior unless gated -> gated here with temporary honest vetoes + route-regression tests (5.7). Native physical admission stays closed. C1b-ii removes the temporary vetoes as it implements each positional route.
