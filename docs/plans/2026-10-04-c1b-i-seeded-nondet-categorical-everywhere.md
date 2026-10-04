Status: plan (revision 2: Codex plan-gate round 1 folded + SPLIT into C1b-i / C1b-ii, author = Opus). Awaiting Codex plan-gate round 2.

Date: 2026-10-04. Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C, slice **C1b-i: "Non-deterministic categorical becomes reproducible (seeded, position-keyed) on every path, and is reclassified as seeded."** Branch `feat/c1b-seeded-nondet-categorical` off engine main `c4ea0f37`. Risk **R2** (changes one operator's observable determinism contract; no routing change, no destructive side effect).

**Split rationale (after Codex round-1 REVISE).** Round 1 confirmed the position-keyed semantics are correct but showed the full change is cross-cutting (it touches the engine's determinism-classification machinery, nested, multi-table sentries, out-of-core compat, AND the chunked fast-path admission + native kernel). Bundling the sensitive reclassification with the fast-path admission is one oversized, high-blast-radius PR. So it is split:
- **C1b-i (THIS plan):** redefine the non-deterministic categorical draw to seeded + position-keyed EVERYWHERE it runs today (the whole-frame / oracle path), switch weighted selection to the shared CDF, require a namespace, and RECLASSIFY the operator as seeded across the engine (determinism protocol, draw-site providers, nested, multi-table sentries, out-of-core compat, old-contract tests). No routing change: the chunked-route veto stays, so non-deterministic categorical keeps running on the whole-frame path, now reproducibly. Independently valuable (reproducible jobs) and the prerequisite for C1b-ii.
- **C1b-ii (next plan, deferred):** lift the chunked veto for the seeded-admissible case; admit seeded positional categorical to the chunked route (oracle-chunked `row_offset` keying + the native batch-kernel integer-key path + route-aware native admission); add the `when:` / FK hazard gates (only relevant once it runs chunked); honest chunked evidence; the chunked output-type pin. Folds round-1 findings B1, B3, B4, H5, H7.

**Owner decisions (Cam, 2026-10-04):** Option A (make it reproducible; "allowing determinism is always better than not offering it"). Split into two slices and nested = flattened-leaf-position keying: decided autonomously under the loop's autonomous-operation mandate after Cam twice re-launched the loop with these as open engineering/packaging calls; both are veto-able.

**Research-first:** the seeded position-keyed shape is the EXISTING `windowed_date` mechanism (`transforms/windowed_date.py:216-219`, `derive(seed, ns, index.to_bytes(8,"big"))`). C1b-i reuses `derive_index` (no new primitive). (C1b-ii will need the batch-kernel variant; see round-1 finding B1, deferred there.)

## 1. Goal and scope

Non-deterministic categorical today draws a whole-column vector from an **unseeded** `np.random.default_rng()` (`_categorical.py:214-231`): non-reproducible by design, and the engine's determinism machinery classifies this draw site as unseeded/entropy-root-none. C1b-i replaces that with a **seeded, position-keyed** draw and makes the whole engine agree it is now seeded.

New draw (for each non-null row at global index `g = ctx.row_offset + local_index`; whole-frame uses the default `row_offset = 0`, so `g` is the positional index):
- Uniform: `idx = derive_index(ctx.mask_key, plan.namespace, _key_bytes(g), pool_size=len(categories))` -> `categories[idx]`.
- Weighted: `bucket = derive_index(ctx.mask_key, plan.namespace, _key_bytes(g), pool_size=_WEIGHTED_CDF_RES)` then the shared CDF `bisect.bisect_right(cdf, bucket)` (clamp), identical to the deterministic branch (`_categorical.py:196-213`).
- Nulls preserved positionally (restored from the source null mask).

**Key encoding (round-1 B1, forward-compatible with C1b-ii).** `_key_bytes(g)` is the engine's CANONICAL INTEGER encoding of `g` (the same encoding the compiled batch kernel applies to an integer column: Python `generation/pool/_canonicalize.py:44-59`, Rust `canonicalize.rs:202-213`), NOT raw `g.to_bytes(8,"big")`. C1b-i runs only the scalar `derive_index` (which accepts arbitrary bytes), but it deliberately uses the canonical-integer encoding so that when C1b-ii feeds an integer global-index column to `derive_index_batch`, the native output is byte-identical to this oracle output with no rework. (`g.to_bytes(8,"big")` was my round-1 detail, not Cam's decision; Cam approved "position-keyed", so the encoding is an implementation choice.)

This keys on row position, not source value: same value at different rows can differ; different values at the same index get the same category. Reproducible, not value-joinable (privacy distinction preserved).

**One global contract (round-1 B2): NO unseeded categorical path remains.** Every non-deterministic categorical draw becomes seeded + position-keyed, on every execution path. Native-inadmissible shapes (numeric/non-string categories, non-string source) still run on the pandas oracle, now SEEDED (not unseeded). Missing namespace fails explicitly (`categorical_requires_namespace`). `from_profile` stays unavailable to chunked execution (profile inputs are chunk-dependent; unchanged).

In scope: the operator-semantics change + the engine-wide reclassification. OUT of scope (-> C1b-ii): lifting the chunked veto, the native kernel/admission, oracle-chunked `row_offset` activation on the chunked route, `when:`/FK hazard gates, chunked output-type pin, chunked evidence. The chunked preflight veto `categorical_nondeterministic_not_chunk_safe` STAYS in C1b-i (so non-det categorical still runs only whole-frame, now seeded); C1b-ii lifts and retires it.

## 2. Established facts (engine HEAD c4ea0f37; from the C1b design probe)

- Unseeded branch: `_categorical.py:214-231` (`default_rng()` no seed, whole-column `rng.integers`/`rng.choice(p=)`, nulls via `na_mask`, numpy `p=` normalization not the CDF). Module docstring `:6-10` documents the unseeded contract (to rewrite).
- Deterministic template: `_categorical.py:180-213` (`derive_index(mask_key, namespace, _canonicalize_source(value), pool_size)`; weighted via `_build_cdf` + `bisect_right`; `_WEIGHTED_CDF_RES=1_000_000`).
- `derive_index(seed, namespace, source, *, pool_size)` accepts arbitrary `source` bytes (`determinism/_derive.py:278-310`). windowed_date precedent: `transforms/windowed_date.py:216-219`.
- Canonical integer encoding (what C1b-ii's batch kernel uses, so C1b-i must match): `generation/pool/_canonicalize.py:44-59` (Python), `decoy-engine-native/src/canonicalize.rs:202-213` (Rust) -- length-prefixed signed int encoding; round 1 confirmed Python and Rust agree.
- `ctx.row_offset` exists (`_adapter.py:198`, default 0); whole-frame uses 0. (Its chunked-route activation is C1b-ii; C1b-i's formula reads `ctx.row_offset` so it is already correct when C1b-ii turns the chunked route on.)
- **Determinism-protocol classification to update (round-1 H6):** this draw site is registered UNSEEDED: `execution/native/_determinism_protocol.py:179-194` (entropy-root `none`, unpartitionable, unseeded) and `execution/native/_draw_site_providers.py:815-848,899-902` (registered as an unseeded provider). Asserted by `tests/native/test_determinism_protocol.py:339-351`. C1b-i reclassifies: entropy-root = the mask_key/seed, partitionable by row position, seeded.
- **Nested (round-1 H6):** `_strategies/_nested.py:404-429` invokes the categorical handler over flattened leaves. With the handler seeded, nested categorical becomes seeded too, keyed by FLATTENED-LEAF position (Cam's decision). Document; test.
- **Multi-table sentries (round-1 H6):** `tests/unit/execution/test_multi_table_gates.py:464-536,552-650`, `test_multi_table_units.py:37-56`, `_multi_table_sentry_fixtures.py:34-69` classify/fixture non-det categorical as unseeded/unreproducible. Update to seeded.
- **Out-of-core compat (round-1 H6):** `execution/out_of_core/_compat.py:300-310` + `tests/parity/test_out_of_core_group_b_parity.py:392-401` retain the unseeded rationale. Update.
- **Old-contract tests (round-1 H6, expanded):** `tests/unit/execution/test_shuffle_categorical.py:212-221` (two-runs-differ), `tests/unit/execution/test_categorical_weighted.py:433-454,510-531` (pin the numpy RNG impl + normalization path), `tests/native/test_determinism_protocol.py:339-351`. Migrate to the seeded contract; keep validity/distribution assertions, replace RNG-implementation assertions.

## 3. Semantics decision (round-1 confirmed correct)

Seeded, position-keyed via the global row index using `derive_index` + the shared CDF, mirroring windowed_date. Weighted selection switches from numpy `p=` to the integer CDF `bisect_right` (round 1 confirmed: for identical integer buckets `bisect_right` == `searchsorted(side="right")`, so this is also what makes C1b-ii's native path match; distribution is CDF-quantized to resolution 1,000,000, same as deterministic weighted). Output independent of source value except nullness: reproducible, not value-joinable. The `when:` and FK hazards do NOT arise in C1b-i (no chunking: whole-frame enumeration == physical positions); they are gated in C1b-ii when chunked execution is enabled.

## 4. Implementation

1. **Oracle strategy (`_categorical.py`).** Replace the unseeded else-branch (`:214-231`) with the seeded position-keyed draw (section 1): per non-null row at `g = ctx.row_offset + i`, `derive_index(ctx.mask_key, plan.namespace, _key_bytes(g), pool_size=...)` + (weighted) shared CDF `bisect_right`. Add `_key_bytes(g)` = canonical-integer encoding (reuse `generation/pool/_canonicalize.py`). Require `plan.namespace` (raise `categorical_requires_namespace`). Nulls positional. Rewrite the module docstring `:6-10` to the new reproducible position-keyed contract.
2. **Determinism-protocol reclassification.** `_determinism_protocol.py:179-194`: this draw site is seeded (entropy-root = mask_key/seed), partitionable by row position. `_draw_site_providers.py:815-848,899-902`: re-register as a seeded provider. Keep the classification honest and consistent with how windowed_date is classified (it is the position-keyed precedent).
3. **Nested.** Confirm `_nested.py:404-429` routes through the updated handler so nested categorical is seeded, keyed by flattened-leaf position (ctx.row_offset within the flattened frame). Add a one-line docstring note.
4. **Multi-table sentries + out-of-core compat.** Update `_multi_table_sentry_fixtures.py` + the multi-table gate/unit classifications and `out_of_core/_compat.py:300-310` so non-det categorical is treated as seeded/reproducible. These are classification/sentry updates, not behavior changes beyond the seeded draw.
5. **Keep the chunked veto.** Do NOT touch `_chunked_categorical.py` / `_chunked.py:322-324,341` (the veto stays; non-det categorical remains whole-frame-only). C1b-ii lifts it.
6. **Docs:** CHANGELOG + compatibility-contract (the determinism-contract change: non-det categorical is now reproducible/position-keyed on the whole-frame path). Barry pass with exact-HEAD receipt. Roadmap + RECENTLY-SHIPPED on merge.
7. **Stale comments:** the `_categorical.py` module docstring; any "unseeded"/"two runs differ" comment; the determinism-protocol docstrings that call this site unseeded.

## 5. Acceptance tests (written before implementation; red-before recorded)

1. **Reproducibility (whole-frame).** Same job + seed + data -> byte-identical output across runs (uniform + weighted). Different `mask_key` or `namespace` -> different output. (Use KAT vectors with known-distinct expected indices, round-1 M8, not generic inequality.)
2. **Position-keyed, NOT value-keyed (KAT-pinned, round-1 M8).** (a) The same source value at two positions can map to different categories. (b) Two different source values at the same index map to the SAME category. (c) Exact output equals direct scalar `derive_index(mask_key, ns, _key_bytes(g), pool_size)` for pinned vectors. (d) Null-position pin: replacing a null at position `g` with a non-null value does NOT change later rows (proves nulls occupy positions; enumeration does not compress to non-null ordinals).
3. **Forward-compatibility of the key encoding.** Assert `_key_bytes(g)` equals the bytes the canonical integer encoder produces for `g` (so C1b-ii's batch kernel over an integer column will match). A direct equality test against `generation/pool/_canonicalize` output.
4. **Distribution.** Frequencies match configured weights within tolerance (weighted) / uniform (unweighted), over a large column -- the existing skew/validity tests retargeted to the seeded path.
5. **Determinism-mode separation.** Deterministic (value-keyed) categorical unchanged (C1 bytes); non-det now seeded position-keyed. Both in one config.
6. **Determinism-protocol reclassification.** `test_determinism_protocol.py` asserts this draw site is now SEEDED/partitionable (update the `:339-351` assertions); the provider registry reports it seeded.
7. **Nested.** Nested categorical is reproducible, keyed by flattened-leaf position (same leaf position -> same category across runs; distinct from outer-row identity).
8. **Multi-table + out-of-core.** The migrated sentries/fixtures assert seeded/reproducible classification; out-of-core group-B parity reflects the seeded contract.
9. **Still vetoed from chunked (unchanged routing).** A non-det categorical in a chunked job still fails preflight with `categorical_nondeterministic_not_chunk_safe` (C1b-i does NOT lift it). Pin this so the split boundary is explicit.
10. **Old-contract migration (round-1 H6).** Inventory every test asserting the old unseeded "two runs differ" / numpy-RNG-impl behavior (section 2 list + an `rg` sweep); migrate to the seeded contract. No test weakened/skipped/xfailed.

Record red-before (fails on current main: unseeded) and green-after.

## 6. Risk, rollback, docs, gates

- **Risk R2.** Operator determinism-contract change (owner-approved) + engine-wide reclassification; no routing change, no destructive side effect. Chief risk: an incomplete reclassification leaving the engine internally inconsistent (some module still treats it unseeded) -- the determinism-protocol test (5.6) + the sentry/out-of-core updates (5.8) + the `rg` sweep (5.10) guard against that.
- **Rollback:** revert the branch; non-det categorical returns to unseeded. No persisted state.
- **Clean-env check:** run `ci-mirror` (clean CI-pinned venv: ruff + mypy + the changed tests) at the final gate.
- **Gates:** Codex plan-gate (this doc) -> Sonnet tests-first build (red-before) -> dennis -> Codex final -> merge on local gates with Cam's go. Rebase onto latest main before merge (avoid stale base).

## 7. Open questions for the plan-gate

1. Is the canonical-integer key encoding (`_key_bytes`) exactly what `derive_index_batch` will apply to an int64/uint64 column, so C1b-ii is byte-identical with no oracle rework? Confirm the Python encoder (`generation/pool/_canonicalize.py:44-59`) is the one the scalar path should reuse and that it matches the Rust `encode_int`.
2. Is reclassifying the draw-site provider (seeded, partitionable) self-consistent with how the determinism protocol consumes that classification elsewhere (any downstream that would now treat non-det categorical as partition-safe in a way that is wrong for the still-vetoed-from-chunked state)? Confirm C1b-i's reclassification does not accidentally imply chunk-admissibility before C1b-ii.
3. Nested: is flattened-leaf position the right identity, and does `_nested.py` pass a `ctx.row_offset` (or equivalent) that makes leaf position well-defined and reproducible? Confirm no outer-row coupling is required for reproducibility.
4. Does any consumer of the seeded/unseeded classification (multi-table split decisions, out-of-core) change BEHAVIOR when non-det categorical flips to seeded, beyond classification (e.g. does a seeded column now become eligible for a split path it should still avoid until C1b-ii)? Enumerate and gate.
