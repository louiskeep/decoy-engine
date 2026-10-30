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
1. **Python stringifies the sibling column via pandas `Series.astype(str)` -- the ONE proven formatter
   (Codex P1-1).** The oracle does `str(raw_val)` per row (`group_key.py:170`). `pc.cast` is DEMONSTRABLY
   WRONG (`"1"` vs the oracle's pandas `"1.0"` for nullable int, `"true"` vs `"True"` for bool, preserves
   nulls instead of `"None"`, tz `-0500` vs `-05:00`); a local experiment showed pandas
   `Series.astype(str)` on the effective sibling values MATCHES the oracle for string, nullable int, bool,
   date, timestamp, and tz timestamp. So native converts the group_by column to a pandas Series and applies
   `.astype(str)` (the same path the oracle's frame takes), giving a `pa.string()` array where nulls become
   `"None"`. The B0 spike PINS this formatter's corpus (per admitted type), the pandas/Arrow VERSIONS,
   null/NaT behavior, timestamp unit + timezone coverage, and NON-NFC string preservation, tested at the
   actual execution boundary; a type is NOT admitted until `astype(str)` is proven byte-exact for it.
2. **New Rust path: a DISTINCT canonicalize-FREE hex kernel (NOT a flag on derive_batch) (Codex P0-2).**
   Add a SEPARATE entry point `derive_hex_raw_batch` (its own pyfunction/protocol + loader), leaving the
   canonicalizing `derive_batch` contract SEMANTICALLY UNCHANGED. It reuses the shared pool / `py.detach`
   / thread-clamp but SKIPS `canonicalize_row` -- raw utf8 bytes straight to `derive`:
   `derive(mask_key, namespace, value.utf8_bytes)` then hex-encode then truncate. Byte-parity target:
   `derive(seed, ns, s.encode()).hex()[:HEX_CHARS]`.
   - **Truncation unit is unambiguous (Codex P1-3): `hex_chars` (= `length`), converted to bytes in ONE
     place** (`output_bytes = hex_chars // 2`, `length` even). The ABI contract names `hex_chars` so no
     implementation can double it.
   - **Full companion integration (Codex P0-2):** `native_companion_status()` / the loader must
     capability-DETECT the raw symbol and run its OWN type-and-byte KAT at loader init AND in the status
     probe (today they validate only `derive_batch`/`derive_index_batch`). A MISSING or KAT-mismatched raw
     symbol is a clean ORACLE DECLINE (never a hard failure). ABI bump.
3. **Python prepends `prefix`** to the hex column (vectorized `pc.binary_join`/string concat).

### v1 SCOPE (decline outside the proven envelope)
- **DECLINE the order-dependent case (Codex P0-1).** The oracle reads `df[group_by]` at group_key's
  execution point, and the pandas adapter mutates the frame in ordered-node sequence -- so if an EARLIER
  node masked `group_by`, the oracle keys on the ALREADY-MASKED value, not the source
  (`test_group_key_chunked.py:1406`). v1 does NOT model that dependency: admission DECLINES to the oracle
  whenever the `group_by` column is itself a masked (non-passthrough) node in the plan -- native admits
  ONLY when `group_by` is an unmasked/passthrough column, where `batch.column(group_by)` (the original
  source value) equals what the oracle reads. Modeling the effective-input dependency is a later slice.
  Test group_key with `group_by` masked before it (declines) and unmasked (native).
- **Sibling group_by column SAFE-TYPE gate:** admit only `{string, int64, bool}` in v1 (final-gate MEDIUM
  narrowing). The stringify-safe set is wider (`group_by_type_is_safe`: string/large_string/int*/bool/date/
  timestamp, float+decimal excluded), and the operator masks all of it byte-identically, BUT the sibling
  must be an unmasked PASSTHROUGH node and passthrough's production resident set is exactly
  `{string, int64, bool}` (`_unified_slice_admission._ADMITTED_RESIDENT_TYPES`) -- so a large_string/int32/
  uint64/date/timestamp sibling could never activate end-to-end anyway; admitting it would over-advertise.
  `group_key_sibling_type_admitted` is therefore the exact `{string, int64, bool}` set. Dictionary is
  excluded (Codex P1-2: the exact-type resident map can't express the recursive predicate). Extending
  passthrough's resident set + end-to-end coverage for the wider set is a later slice. The type check runs
  against the SIBLING resident type, not the target schema.
- **Resident-plumbing (Codex P1-2):** `resident_contract_admission()` today looks up `source.schema.field(
  TARGET)` and requires an input-schema field of the target name -- a binding whose input is `group_by`
  would FAIL/raise, not decline. Add an EXPLICIT sibling-input field to the binding and special-case its
  safe-predicate + residency validation BEFORE the target-name schema lookup, so a missing/unsafe sibling
  is a clean DECLINE.
- **FULL-FRAME route only** (chunked declines -- group_key is sibling-keyed, deliberately kept out of the
  chunk-safe set, `_chunked_group_key.py:85`).
- Always deterministic; synthesized namespace; no non-det/namespace gates needed.

### Output typing (B0, but simpler than bucket_perturb)
group_key builds a FRESH string column (like hash) and never emits nulls, so its ONLY degenerate shape is
EMPTY. It belongs in `_TOKENIZING_STRATEGIES` (empty -> float64, else -> string; the all-null branch is
dead for it). NOT `_NULL_ON_EMPTY` (that exists only because bucket_perturb passes its source series
through). PIN with an empty-frame golden asserting the oracle's `df[col]=[]` inferred type (structural
evidence says float64 via the hash precedent, but MEASURE + assert at the ExecutionResult boundary; do not
assume).

### The seam checklist (+ the raw-kernel loader seam and the SIBLING-COLUMN plumbing -- the new parts)
0. **Raw-kernel loader + companion-status (Codex P0-2, NEW):** the Rust `derive_hex_raw_batch` pyfunction;
   a Python protocol/wrapper + loader for it; and its integration into `native_companion_status()` +
   loader-init so the raw symbol is capability-detected and its own type-and-byte KAT runs at both points.
   A missing / KAT-mismatched raw symbol -> group_key cleanly DECLINES to the oracle. `derive_batch`
   untouched.
1. `native/_requirements.py`: add `group_key` to `NATIVE_KERNEL_STRATEGIES` + new `group_key_config_
   rejection` (group_by present; length even/in-range; the group_by column's RESIDENT type in the safe
   set via `group_by_type_is_safe` EXCLUDING dictionaries; the group_by column NOT itself masked by an
   earlier node -- order-dependence decline).
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
5. **Admission declines (positive):** float/decimal sibling declines; a DICTIONARY-typed sibling declines
   (v1); a missing/non-resident group_by declines; **group_by masked by an EARLIER node declines
   (order-dependence), while an unmasked group_by EXECUTES native** (Codex P0-1 -- parity tested both
   ways); chunked declines; full-frame with a safe-typed, unmasked, resident sibling EXECUTES native.
   Also assert an ALL-NULL sibling column yields NON-NULL string keys (`str(None)="None"`), NOT a null
   column.
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
