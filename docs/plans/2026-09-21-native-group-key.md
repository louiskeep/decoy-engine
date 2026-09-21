# PLAN — native `group_key` operator

Status: plan
Author: Opus (top-tier, per the guides/plans standing rule). Codex cross-reviews as plan-gate.
Roadmap: native operator breadth (S-port slate #2). Cam-approved 2026-09-21 ("push ahead" after the FRAME
survey showed group_key needs a new kernel path -- it is an M, not the S the measurement implied). Base:
engine main `6420ccfa` (post bucket_perturb #173). FRAME survey: agent af09ce384 (2026-09-21).

## Why (measured) + the headline complication
group_key runs the old way at ~311s/100M (a Python for-loop). It is row-local, static-typed, zero-
diagnostic. BUT unlike bucket_perturb/categorical it **cannot reuse an existing kernel byte-identically**:
the oracle hashes RAW `str(value).encode("utf-8")` (`transforms/group_key.py:170`) with NO canonicalizer,
while `derive_batch`/`derive_index_batch` canonicalize INSIDE the Rust kernel (`batch.rs:233,384`
`canonicalize_row`: NFC-normalize strings, length-prefix ints, special-encode bool/date). So a native
group_key needs a **canonicalization-free hex derivation path** (new Rust). Byte-identical output is the
hard gate.

## FRAME — the goal
Put `group_key` on the native full-frame lane, output BYTE-IDENTICAL to the pandas oracle
(`transforms/group_key.py` / `execution/_strategies/_group_key.py`).

### What group_key computes (parity target)
Per row: `hex_key = derive(job_seed, namespace, str(<sibling group_by cell>).encode("utf-8"))[:length//2].hex()`;
result = `prefix + hex_key` (`group_key.py:168-174`). Key facts:
- **Keys on a SIBLING column** (`config.group_by`), NOT the target's own value (`group_key.py:161,168`).
  Rows sharing a group value get the same key. The result is written to the TARGET column
  (`_group_key.py:68`).
- **Namespace is SYNTHESIZED** `f"group_key/{target_column}"` (`_group_key.py:66`), NOT `plan.namespace`.
- **No canonicalization** -- raw `str(value).encode()`. `derive` = HKDF-SHA256+HMAC (`_derive.py:231`),
  draw family `source_keyed_hmac` (`_determinism_protocol.py:258`), same envelope as `hash`.
- Output: `pa.string()` hex (first `length//2` bytes -> hex chars == first `length` hex chars, `length`
  even) + `prefix`. Nulls NEVER pass through: a null sibling cell -> `str(None)="None"` -> a key
  (`group_key.py:170`). So group_key CAN NEVER emit an all-null column; its only degenerate shape is EMPTY.
- Config (`GroupKeyConfig`, `group_key.py:70-130`): `group_by` (required str), `length` (even int
  `[8,64]`, default 16), `prefix` (str, default ""). Always deterministic; no non-det mode; namespace
  always present (synthesized).

## Design

### The two-part kernel (Python stringify + new canonicalize-free Rust derive)
The `str(value)` and the derivation split cleanly so the new Rust surface is minimal:
1. **Python stringifies the sibling column, vectorized, byte-exact to the oracle's per-row `str()`.** The
   oracle does `str(raw_val)` per row (`group_key.py:170`). Native reproduces it via a VECTORIZED stringify
   of the group_by column that is PROVEN equal to element-wise `str()` for every ADMITTED sibling type
   (string/large_string/int*/bool/date/timestamp). A B0-style spike measures `astype(str)`/`pc.cast` vs
   per-row `str()` per admitted type and the plan uses whichever is byte-exact (fallback: an explicit
   vectorized formatter per type). Result: a `pa.string()` array of the raw stringified sibling values.
   Nulls become `"None"` (matching `str(None)`), so the output has no nulls.
2. **New Rust path: canonicalize-FREE hex derivation.** Add a `canonicalize=False` mode to the existing
   `derive_batch` machinery (preferred -- reuses the shared pool, `py.detach`, threads-clamp, KAT
   infra), or a sibling `derive_hex_raw_batch`. It computes `derive(mask_key, namespace,
   value.utf8_bytes)[:truncate_bytes].hex()` per row over a `pa.string()` array WITHOUT `canonicalize_row`
   (raw utf8 bytes straight to `derive`). Byte-parity target: `derive(seed, ns, s.encode())[:length//2].hex()`.
   Truncation: pass `truncate = length` hex chars (== `length//2` bytes, since `length` is even). ABI
   bump + a KAT vector for the raw path + a Rust parity test vs the reference.
3. **Python prepends `prefix`** to the hex column (vectorized `pc.binary_join`/string concat).

### v1 SCOPE (decline outside the proven envelope)
- **Sibling group_by column SAFE-TYPE gate:** admit only `{string, large_string, int*, bool, date,
  timestamp}` (reuse `group_by_type_is_safe`, `_chunked_group_key.py:95-115`); **exclude float + decimal**
  (str()/canonicalization + the 0.0/-0.0 collision trap). Non-safe sibling type declines to the oracle.
- **The group_by column must be RESIDENT** in the source table / batch; admission confirms it, else decline.
- **FULL-FRAME route only** (chunked declines -- group_key is sibling-keyed, deliberately kept out of the
  chunk-safe set, `_chunked_group_key.py:85`; the sibling-cell-identity guarantee is not met by eager
  per-chunk emit).
- Always deterministic; synthesized namespace; no non-det/namespace gates needed.

### Output typing (B0, but simpler than bucket_perturb)
group_key builds a FRESH string column (like hash) and never emits nulls, so its ONLY degenerate shape is
EMPTY. It belongs in `_TOKENIZING_STRATEGIES` (empty -> float64, else -> string; the all-null branch is
dead for it). NOT `_NULL_ON_EMPTY` (that exists only because bucket_perturb passes its source series
through). PIN with an empty-frame golden asserting the oracle's `df[col]=[]` inferred type (structural
evidence says float64 via the hash precedent, but MEASURE + assert at the ExecutionResult boundary; do not
assume).

### The 9-seam checklist (+ the SIBLING-COLUMN plumbing, the genuinely new part)
1. `native/_requirements.py`: add `group_key` to `NATIVE_KERNEL_STRATEGIES` + new `group_key_config_
   rejection` (group_by present; length even/in-range; the group_by column's RESIDENT type in the safe
   set via `group_by_type_is_safe`).
2. `native/_plan.py` eligibility: a `group_key` branch that resolves + checks the GROUP_BY column (not the
   target); reject if absent/unsafe-typed.
3. `native/_dispatch.py`: full-frame-only -- add to `CHUNKED_ROUTE_VETOED_STRATEGIES` + veto; positive
   oracle-route test.
4. `native/_chunk_masking.py`: N/A v1 (declined at #3).
5. `physical/_plan.py` `ExecutionBinding`: NEW `group_key_group_by: str|None`, `group_key_length`,
   `group_key_prefix`, resolved output schema. `KeyBinding` with SYNTHESIZED `namespace=f"group_key/{col}"`.
5b. `physical/_shadow_bindings.py`: `SLICE_STRATEGIES` + `OPERATOR_ID_BY_STRATEGY` (`group_key` ->
   `native_group_key`) + a binding branch populating the fields; **`input_schema` from the GROUP_BY
   column's resident type, not the target's** (`_shadow_bindings.py:189`).
6. `physical/_shadow_operators.py`: `native_group_key` branch + `_GROUP_KEY` const; **`run_operator`
   gains access to the GROUP_BY array** (pass the batch or a second array arg) -- the one new signature
   change vs the prior two ports. Fail-closed asserts.
7. `physical/_shadow_coordinator.py`: (a) the per-node loop (`:380`) feeds `batch.column(group_by)` as the
   derivation input while writing to `column`; (b) add `group_key` to `_TOKENIZING_STRATEGIES` (pending
   the empty golden); (c) crypto-companion load (like hash) -- NOT the index kernel.
8. `execution/_unified_slice_admission.py`: `ALLOWED_OPERATOR_IDS` + `_COMPANION_DEPENDENT_OPERATOR_IDS`
   (crypto companion, like hash) + `_ADMITTED_RESIDENT_TYPES["group_key"]` gating the **group_by
   (sibling) column** against the safe set + confirming residency.
9. `native/_capabilities.py`: ALREADY PRESENT (`_Ortho(True,False,False,False,True)`) -- VERIFY only.

## Acceptance tests (byte-parity is the merge gate)
1. **Byte-identity (HARD), value AND field type**, native == oracle, at both boundaries, across sibling
   types {string (incl. non-NFC/decomposed-unicode, dup, empty-string), int, bool, date, timestamp};
   varied `length` (8/16/64) + `prefix`; group cardinality (all-same, all-distinct, mixed); null sibling
   cells (-> "None" key); single-row; empty; the same-group-same-key invariant.
2. **Canonicalization-free proof:** a differential vs a raw-Python `derive(seed, ns, str(v).encode())[:n].hex()`
   over a corpus that INCLUDES a non-NFC unicode string, an int, a bool, a date -- these are exactly where
   the canonicalizing `derive_batch` would DIVERGE; the new raw kernel must match the oracle, not
   `derive_batch`. (A regression asserting native != a canonicalizing derivation on those inputs, to prove
   the raw path is actually taken.)
3. **Stringify parity:** the vectorized sibling stringify == element-wise `str()` per admitted type (B0
   spike corpus).
4. **KAT** for the new raw-hex kernel + the group_key operator.
5. **Admission declines (positive):** float/decimal sibling declines; a missing/non-resident group_by
   declines; chunked declines; full-frame with a safe-typed resident sibling EXECUTES native.
6. **Empty output type** golden (float64 per the tokenizing rule) at the ExecutionResult boundary.
7. **Companion-absent:** native-path tests `@skipif(not companion.ok)`; production declines cleanly (the
   bucket_perturb/categorical CI lesson).

## Build order (single PR)
B0 stringify + empty-type spikes -> the canonicalize-free Rust kernel path + KAT + Rust parity test ->
`ExecutionBinding` fields + `KeyBinding` synth namespace -> `group_key_config_rejection` + admission
(safe-type + residency + full-frame gates) -> `native_group_key` kernel (stringify + raw-derive + prefix)
-> the sibling-column plumbing (binding input_schema, coordinator loop, run_operator signature) -> the
9-seam registration -> byte-parity + canonicalization-free + stringify + KAT + decline + empty + companion-
absent tests -> dennis -> Codex FINAL -> CI (companion present + absent) -> merge -> DOCUMENT. Rebuilds the
native crate (new Rust path) -- unlike bucket_perturb/categorical.

## Risks + rollback
- **Canonicalization mismatch (the whole reason for the new kernel):** the raw path must NOT canonicalize;
  proven by test #2's non-NFC/int/bool differential. HIGHEST risk.
- **Vectorized stringify != per-row `str()`** for some type/edge (NaT, timestamp formatting, numpy scalar
  repr): B0 spike + test #3; fall back to an explicit per-type formatter if `astype(str)`/`cast` diverges.
- **Sibling residency/plumbing:** the group_by array must reach the operator; admission confirms residency,
  the coordinator loop feeds it; fail-closed if absent.
- **Empty typing:** golden-pinned, not assumed.
- **Rollback:** additive behind admission; any un-admitted shape (unsafe sibling, non-resident, chunked)
  declines to the pandas oracle (fail-closed). New Rust path is additive (existing derive_batch unchanged).

## Gates
FRAME (survey done) -> PLAN (this) -> Codex plan-gate -> build -> dennis -> Codex FINAL -> CI -> merge ->
DOCUMENT. Then faker-full-frame (the S-slate's remaining true-S cheap win).
