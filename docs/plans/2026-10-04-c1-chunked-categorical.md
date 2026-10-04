Status: plan (revision 1)

Date: 2026-10-04. Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C, slice **C1: "Chunked dispatcher: categorical (deterministic)."** Branch `feat/c1-chunked-categorical` off engine main `f8c8bf36`. Risk **R2-R3**: it moves the deterministic `categorical` operator from the pandas-oracle fallback onto the native chunked route for every chunked job that masks a categorical column, and it pins that operator's chunked output type (today data-dependent per chunk). Merges only with Cam's go, after Codex plan-gate, Sonnet build, dennis, Codex final. Merged on local gates while GitHub Actions is out of minutes (self-hosted runner sweep later).

## 1. Goal and scope

Deterministic `categorical` currently never runs on the chunked route: it is in `CHUNKED_ROUTE_VETOED_STRATEGIES` (`execution/native/_requirements.py:151`), so any table carrying a categorical column is vetoed whole to the pandas oracle (every sibling column on that table, native-capable or not, runs on the oracle too). C1 makes deterministic categorical a first-class chunked-native operator: it executes per chunk through the compiled kernel that already exists, with byte parity against the pandas oracle, and the table is no longer forced off the native route by a categorical column.

In scope: deterministic categorical only (`is_deterministic_categorical` true: `deterministic` or `allow_collisions`, with a namespace and all-string categories, per `execution/native/_operator_config_rejections.py:44-86`).

Out of scope (explicit): non-deterministic categorical (unseeded whole-column RNG) is slice **C1b** and stays rejected here by `categorical_not_deterministic`. No change to the full-frame / unified-slice route except the output-type pinning in section 4, which is deliberately shared. No other vetoed operator (bucket_perturb C2, group_key C3, date_shift C4) changes; they remain the oracle-forcing fixtures' target is migrated off categorical (section 5).

## 2. Established facts (from the engine, HEAD f8c8bf36)

- **The kernel exists and is already parity-tested full-frame.** `native_categorical` (`execution/native/_categorical_ext.py:38-123`) wraps `IndexDerivationKernel.derive_index_batch` plus a NumPy gather, and the weighted path does `np.searchsorted(cdf, bucket, side="right")` with a clamp. It runs full-frame via `execution/physical/_shadow_operators.py:228-243` and is covered by `tests/native/test_categorical_ext.py`, `test_derive_index_kat.py`, and the full-frame parity suite `tests/physical/test_shadow_categorical.py`. C1 builds NO new Rust.
- **Deterministic categorical is row-local; no cross-chunk state.** Each row maps from its own canonicalized source value plus `(mask_key, namespace)` via `derive_index` (`_strategies/_categorical.py:~187`); nulls map to None. Cross-chunk consistency is therefore automatic (`execution/native/_capabilities.py:214` marks it row-local, `output_type_is_static`-adjacent). No prepass, no sibling state. This is what makes C1 the tractable first Phase C operator.
- **The one hard problem is the data-dependent output TYPE.** Categorical's output type depends on contents: empty -> float64, all-null -> null, otherwise string (`execution/native/_shadow_assembly.py:20-53`, `categorical` in `_TOKENIZING_STRATEGIES`). The full-frame route resolves this once at `assemble_column` over the whole column. The chunked route has no assembly point: `_native_route` (`execution/native/_chunked_entry.py:155-263`) emits each chunk eagerly, and the native kernel pins `pa.string()` per batch (`_categorical_ext.py` docstring). Today the oracle chunked route yields per-chunk-content types, and `tests/native/test_chunked_entry_values_schema.py:474` (`test_categorical_on_the_oracle_route_keeps_per_chunk_types`) is an explicit CHARACTERIZATION test that documents this as provisional "until a later phase pins them." C1 is that phase.
- **The veto is mirrored in four sites** that must move together: `_requirements.py:151` (the set), `execution/native/_dispatch.py:238` (`_static_route_decision` emits `categorical_not_native_chunked_route:<col>`), `execution/native/_chunked_evidence.py:45` (plans `PANDAS_ORACLE` backend), and `execution/native/_phase3_eligibility.py:116-122`.
- **Preflight loads the index kernel only for faker today** (`_dispatch.py` ~`:422-430`, gated on `any(n.strategy == "faker" ...)`), downgrading with `index_extension_unavailable` otherwise. Categorical needs the same kernel (`KernelAvailability.index`, `execution/native/_companion_status.py:~353`).
- **`_mask_chunk_native` has no categorical branch** (`execution/native/_chunk_masking.py:~160-230`); its `else` asserts. It handles passthrough, redact, truncate, hash, faker. Categorical's compile-time inputs on the full-frame path are `binding.categorical_categories` and `binding.categorical_cdf` (`_shadow_operators.py:228-238`); on the chunked path these come from `ColumnSeed.provider_config`, built once per column (not per chunk).
- **Several tests use categorical as the "force the oracle" fixture** (`test_unconfigured_passthrough.py:523,569`; the `FORCE` helpers; `test_chunked_entry_values_schema.py:235`). Once categorical is native-chunked, those fixtures need a still-vetoed operator (bucket_perturb) to keep forcing the oracle.

## 3. The output-type decision (the plan's core choice)

**Decision: on the chunked route, deterministic categorical output is pinned to `pa.string()` for BOTH the native and the oracle leg, and the characterization test is updated to assert the pinned type.** Rationale:

- Deterministic categorical's config gate requires all-string categories (`categorical_categories_not_all_string`), so the operator's intrinsic output is a string column. The empty->float64 / all-null->null shapes are pandas round-trip inference artifacts, not a property of the operator.
- A column's Arrow type must not depend on which chunk a row landed in. Per-chunk content-dependent typing (today's oracle behavior) is the provisional state the characterization test flagged for pinning. Pinning to string makes the chunked output type stable and chunk-count invariant, which the program's parity bar (byte parity including Arrow field type across chunk sizes) requires.
- Pinning both legs to string keeps native == oracle on the chunked route (the parity contract) regardless of chunk shape (empty, all-null, ragged).

Mechanism: add `categorical` to the pinned-string set in `execution/native/_chunked_schema_rule.py` (today pins hash, truncate, redact to string, passthrough to source). Because `_chunked_schema_rule` applies on BOTH chunked legs, this pins the oracle leg too, so `test_chunked_entry_values_schema.py:474` is rewritten from "keeps per-chunk types" to "pinned to string on both legs" (a deliberate, plan-sanctioned update of a characterization test, recorded in the build record; it is not a weakening, it is the pinning that test invited).

**Cross-route note (scoped, not a blocker):** the full-frame / unified-slice route keeps its whole-column `assemble_column` behavior (an all-null categorical column is `null` type full-frame, `string` chunked). This is a pre-existing route-dependent type difference for the degenerate all-null column only; aligning full-frame to pin string as well is a candidate follow-up but is OUT OF SCOPE for C1 (C1 does not change full-frame output). The build record states this explicitly. Acceptance tests assert the chunked native-vs-oracle parity, which is what C1 owns.

## 4. Implementation

1. **Remove the veto** at all four mirrored sites (`_requirements.py:151`, `_dispatch.py:238`, `_chunked_evidence.py:45`, `_phase3_eligibility.py:116-122`). Deterministic categorical becomes chunk-eligible; non-deterministic stays rejected by the config gate (`categorical_not_deterministic`), so the veto removal must be conditioned on `is_deterministic_categorical` / the compiled `ColumnSeed.deterministic`, not a blanket un-veto. A non-deterministic categorical column still vetoes its table to the oracle (preserve that, with a reason code `categorical_nondeterministic_not_native_chunked_route:<col>` distinct from the old one, so C1b can later lift it).
2. **Add the categorical branch to `_mask_chunk_native`** (`_chunk_masking.py`): resolve categories + cdf once per column from `ColumnSeed.provider_config` (reuse `_build_cdf`), hold them on the per-column handler state, and call `native_categorical(index_kernel, ...)` per chunk. Nulls restore positionally (the kernel already null-masks).
3. **Load the index kernel in preflight for categorical**, not only faker (`_dispatch.py` ~`:422-430`): widen the guard to `any(n.strategy in {"faker", "categorical"} ...)`; keep the `index_extension_unavailable` downgrade (a companion-absent run reroutes categorical to the oracle, same as today).
4. **Pin the output type** per section 3 (`_chunked_schema_rule.py`), both legs.
5. **Evidence/backend label** (`_chunked_evidence.py` `_planned_backend`): categorical on the native leg reports the native backend label its kernel warrants (align with how hash reports; confirm against the evidence tests, `test_chunked_entry_evidence.py`). The oracle leg (companion-absent, or non-deterministic) still reports `pandas_oracle`.
6. **Migrate the oracle-forcing test fixtures** off categorical to a still-vetoed operator (bucket_perturb) so `test_unconfigured_passthrough.py` and the `FORCE` helpers keep exercising the oracle path.

Module-size: touched files stay under the engine's cap; no module should cross it (check `_chunk_masking.py`, `_dispatch.py`).

## 5. Acceptance tests (written before implementation; red-before recorded)

Parity is the contract: native chunked == pandas-oracle chunked, byte-for-byte on values AND Arrow field type AND metadata, row/column order, warnings, errors, and evidence. Extend `tests/native/_chunked_entry_support.py` (it already has a deterministic `categorical(name)` fixture) and the `test_chunked_entry_*` suite.

1. **Parity matrix.** Deterministic categorical (uniform and weighted) over: every supported source dtype (string, int-with-nulls, and the canonicalization edge cases), chunk shapes {empty, all-null, single-row, ragged last chunk, a chunk that is all-null beside one that has values}, and several chunk sizes (e.g. 1, 7, 50_000), at native_threads 1 and 4. Assert native == oracle per chunk and across the reassembled column (values + Arrow type + metadata). Covers the pinned-string type on both legs.
2. **Determinism across chunks.** The same source value appearing in different chunks maps to the same category; the mapping equals the full-frame deterministic result for the same `(mask_key, namespace)`.
3. **Output-type pinning.** An all-null chunk and an empty chunk both yield a `string` column on the native AND oracle legs (the rewritten `test_chunked_entry_values_schema.py:474`). Chunk-count invariance: the same data split 1 way vs N ways yields the identical Arrow type.
4. **Veto now lifts for deterministic, holds for non-deterministic.** A deterministic categorical column lets its table run native (sibling redact/hash columns run native too, not forced to the oracle); a non-deterministic categorical column still vetoes the table with the new distinct reason code. Update `test_chunked_entry_evidence.py:254-258` (was `categorical_not_native_chunked_route:c`, backends `c: pandas_oracle`) to the native backend + lifted veto for the deterministic case.
5. **Companion-absent.** With no compiled companion, categorical reroutes to the oracle (gated by `@NEEDS_COMPANION` where the native result is asserted; the oracle-leg parity assertions run on both).
6. **Config-gate rejections unchanged** (the eight `categorical_*` codes still fire for bad configs on the chunked route).
7. **Thread-count invariance** (`tests/native/test_thread_clamp_parity.py` pattern) and GIL-release behavior unaffected.

The full-frame categorical suite (`tests/physical/test_shadow_categorical.py`) must stay green unchanged except its `test_chunked_route_declines_categorical` seam (`:148-167`), which is updated to reflect that deterministic categorical is now admitted on the chunked route (non-deterministic still declines).

## 6. Risk, rollback, gates

- Risk R2-R3: changes the production route of every chunked job with a categorical column, and pins an output type. Rollback: deterministic categorical can be re-added to `CHUNKED_ROUTE_VETOED_STRATEGIES` (one set + its three mirrors) to restore the oracle route; the pinned type is a separate, smaller revert.
- No bar is loosened. The parity bar is byte-identity native-vs-oracle on the chunked route.
- Gates: Codex plan-gate (this doc) before build; Sonnet builds tests-first; dennis adversarial; Codex final. Per the review-rounds rules.
- Build order: (1) acceptance tests red-before; (2) veto removal conditioned on determinism + the new reason code; (3) `_mask_chunk_native` categorical branch + preflight kernel load; (4) output-type pinning + the characterization-test rewrite; (5) evidence labels + fixture migration; (6) green, mutation substitute on the new branch logic, build record.

## 7. Open questions for the plan-gate

- Output-type pinning (section 3): confirm pinning categorical to string on BOTH chunked legs (and rewriting the characterization test) is preferred over emulating the oracle's per-chunk content-types on the native leg. The plan chooses pinning for type stability + chunk-count invariance; the alternative preserves today's oracle per-chunk behavior but keeps a content-dependent type.
- Cross-route difference (all-null categorical column: full-frame `null` vs chunked `string`): confirm leaving full-frame unchanged in C1 (candidate follow-up) is acceptable, or whether C1 should also pin full-frame for cross-route consistency.
- The distinct non-deterministic reason code: confirm the name and that keeping non-deterministic vetoed here (C1b lifts it) is right.
