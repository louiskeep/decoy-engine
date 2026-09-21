---
Status: plan
---

# Native-ize the World B pool sampler's per-row index derivation

> Roadmap: generation fast-path follow-up (task #11), Stage C-adjacent. Author: Opus.
> Native-throughput family: the same `derive_index_batch` drop-in that made Phase 2 pool selection
> ~13x. FPE / masking are untouched.

## FRAME
`generation/pool/_sampler.py` maps a canonicalized source value to a pool slot with a **per-row**
`derive_index(seed, namespace, source=canonical, pool_size)` loop (`_derive.py:278`), in two places:
- **scalar** deterministic sample (`_sampler.py:206-244`): materializes source to a list + null mask,
  then `for i, value: idx = derive_index(...); output[i] = pool_values[idx]`, nulls preserved as `pd.NA`.
- **composite bundle** (`sample_bundle`, `_sampler.py:303-397`): ONE index per row derived from the
  canonicalized source, shared across all bundle columns (`for i: idx = derive_index(...); for j,c:
  per_col[c].append(pool_values_c[idx])`).

The masking + Faker-generation paths already replaced this exact per-row loop with the compiled
**`derive_index_batch(values, *, mask_key, namespace, pool_size, native_threads)`** kernel
(`execution/native/_index_ext.py`; reference `_ReferenceIndexDerivation` + compiled `_CompiledIndexKernel`,
byte-parity-proven by the index KAT in `decoy-engine-native/vectors/` + `docs/native/derive-index-contract.md`).
The canonical reference call site is `execution/native/_chunk_masking.py:98-135` (call -> validate
uint64/len/null-positions/bounds -> gather). `derive_index` is the irreducible per-row cost the sampler's
own docstring (`_sampler.py:211-212`) names; batching it is the whole win.

**The load-bearing invariant: byte-identical output.** `derive_index_batch(canonicalize(v_i))` must equal
`derive_index(seed, namespace, canonicalize(v_i), pool_size)` for every row i, and the null/gather result
must match the current per-row output exactly (golden test-flight + the sampler's parity tests are the proof).

## The one real risk: canonicalization must match
The per-row path canonicalizes with `_canonicalize_source(value)` BEFORE `derive_index`. The batch kernel
canonicalizes internally over the Arrow array. **These two canonicalizations must be provably identical**
or the indices diverge. Before any swap: establish (a property test) that for a representative value corpus
(str/int/float/bool/None/edge unicode), `derive_index_batch(pa.array(values), mask_key=seed, namespace=ns,
pool_size=k)` returns, per element, exactly `derive_index(seed, ns, _canonicalize_source(v), pool_size=k)`.
If the kernel expects PRE-canonicalized input (it may not canonicalize), pass a pre-canonicalized Arrow
array built with the SAME `_canonicalize_source`. Resolve this empirically in DEVELOP D0 before writing the
swap; it decides the whole approach. (The masking/faker paths already rely on this equivalence, so it holds
for their inputs; World B must confirm it for the sampler's own value domain + canonicalization.)

## DEVELOP
- **D0 (spike, gates the rest): canonicalization + per-element parity.** Property test:
  `derive_index_batch` == per-row `derive_index` over the sampler's value corpus, deciding whether to pass
  raw or pre-canonicalized values. No production edit until green.
- **D1 scalar sampler.** Replace the `_sampler.py:206-244` loop: build the source Arrow array, call
  `derive_index_batch(..., mask_key=seed, namespace=namespace, pool_size=pool.size)`, then gather
  `pool_values[idx]` with POSITIONAL null preservation. Validation (P2a -- ADAPT, do not copy the
  masking gather wholesale): reuse the kernel-result checks in spirit (exact `pa.uint64`, expected
  length, null positions equal the source's, idx < pool_size before gather, mapping the coded index
  errors via `_translate_compiled_index_kernel_error`), but the sampler returns a pandas `Series` with
  `pd.NA` nulls, not Arrow -- so ALSO assert the output preserves Series length + row order + the
  observable dtype/null representation the legacy per-row path produced. Keep the exact
  `GenerationError` codes + the `len(source)!=n` contract raise.
- **D2 composite bundle.** Same for `sample_bundle` (`:303-397`): ONE `derive_index_batch` over the shared
  canonicalized source, gather each bundle column by the shared idx array. The cross-column alignment (one
  index shared) is preserved by construction (single idx array).
- **D3 kernel selection + fallback.** Reuse the same compiled-or-reference selection the masking path uses
  (companion present -> compiled; absent -> `_ReferenceIndexDerivation`, byte-identical, slower). The sampler
  must stay correct + byte-identical with the companion ABSENT (the "supported, correct, slower" contract).
- **D4 non-deterministic mode.** UNCHANGED (it does not use `derive_index`); only the deterministic path is
  batched. Confirm the non-det branch is untouched.

## VERIFY (acceptance)
1. D0 per-element parity property test green (the gate).
2. Byte-identical (P2b -- the existing sampler tests are thin AND the visible `_faker_pool.py` caller
   uses `deterministic=False`, so golden tests may not exercise this path; the differential tests below
   are the real proof). Add explicit LEGACY-ORACLE differential tests (new batch path vs the current
   per-row path, asserting equal output) for scalar AND multi-column bundle sampling, covering:
   - admitted raw inputs: strings incl. NFC/NFD unicode, bools, signed/unsigned + numpy ints,
     timezone-aware timestamps;
   - duplicate values; empty / all-null / mixed null positions; pool_size 1 and varied pool sizes;
   - unsupported values raising the EXACT current error codes; the filtered/admitted fallback behavior;
   - compiled-present vs companion-absent/reference vs the legacy per-row output (all three equal);
   - bundle tuple integrity: every row's emitted columns come from ONE shared pool index/tuple;
   - validation-failure injection (wrong dtype, wrong length, mismatched null mask, out-of-bounds idx).
   Existing `pool/_sampler` tests + test-flight fingerprints must also stay green/unchanged.
3. Companion-ABSENT run is byte-identical to companion-PRESENT (reference == compiled).
4. Non-deterministic mode unchanged.
5. ruff + mypy clean; module-size sentry ok.
6. A quick local throughput check at ~50k-100k rows (sampler is generation-side, small tiers run locally)
   showing the batch path is faster than the loop; the WIN is the point, but PARITY is the gate.

## Out of scope
- FPE, masking, the Faker-pool generation path (already batched), the N_THRESHOLD crossover (task #9).
- Any change to the derivation math or the KAT.

## Gates
FRAME (done) -> PLAN (this) -> Codex plan-gate -> D0 spike (parity, gates the build) -> build D1-D4 ->
dennis -> Codex FINAL -> CI (both substrates). Byte-identity is the hard gate throughout.
