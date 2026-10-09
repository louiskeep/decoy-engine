# C5c-ii: deterministic Faker over non-string sources on the native routes

Status: plan (rev 2, folds Codex plan-gate round 1). Phase C, Rust-engine program.
Branch `feat/c5c-ii-deterministic-nonstring-faker` off engine main `69a7741f`.
Follows C5c-i (#220, positional Faker over non-string) and C5c-a (#221, oracle
sampling-boundary fix for deterministic Faker over nullable ints).

Rules consulted: 00-universal, risk-and-exceptions, development-loop, feature-dev, testing,
architecture, api-and-compatibility, performance, documentation.

Rev 2 (Codex round 1, REVISE, option A upheld): drop tz-timestamps (sentinel hazard, H3); the
pin classifier uses config AND the Arrow source schema, not config alone (H1); the degenerate
string retype is applied as a scoped step AFTER `pa.Table.from_pandas`, which otherwise erases it
(H2); chunked admission is defined against the oracle's effective sampling input, not a type-family
copy of the unified guard (H4); determinism is effective (includes the `allow_collisions` alias,
M6); real pool-content validation for the new pinned set (M7); the nullable-int matrix and the
acceptance contract are made exact (M5, M8).

## 1. Why / scope

The native routes (unified full-frame + chunked native) decline a deterministic Faker column over
a non-string source, forcing its table off the native path. C5c-i opened POSITIONAL Faker over
bool/int/uint/float; C5c-a fixed the ORACLE so deterministic Faker over nullable ints works (exact
integers at the Faker sampling boundary, frame untouched). C5c-ii opens the NATIVE routes to
**deterministic** Faker (string-output provider) over **bool, signed integers, and unsigned
integers only**. 

**tz-aware timestamps are OUT this slice (rev 2, H3):** Arrow `-2**63` in `timestamp[ns, tz]`
becomes pandas `NaT`, which the oracle masks as missing while the compiled kernel treats as a
valid timestamp (probe: `[-2**63,0,None]` -> pandas missing `[T,F,T]`, compiled indices
`[84,1,None]`), so native could emit a value where the oracle emits null. Timestamps need their
own proven sentinel/range/tz design; deferred with the other temporal families.

## 2. Decision (Cam 2026-10-09): option A — pin the degenerate output to `string` on both routes

Pin an admitted deterministic-Faker column's degenerate (all-null / empty) output to `string` on
BOTH the oracle and the native routes, as positional Faker and C1 categorical already do. Pre-GA
output-type change for the empty/all-null case only; value-bearing columns unchanged. The
string-output provider guarantees string values, so `string` is the honest type and pandas
`double`/`null` were empty/all-null inference artifacts.

## 3. Admission classifier (config + source schema; effective determinism)

One classifier decides both native admission and pin membership, to keep them in lockstep. It
takes the column config AND the original Arrow source field, and returns admitted iff ALL hold:

- **Effective determinism (M6):** `deterministic: true` OR the `allow_collisions: true` alias
  (`_seed_envelope.py:203` compiles the alias to deterministic REUSE). Mutually exclusive with
  the positional set, which `is_positional_faker_entry()` already excludes both of; no precedence
  rule is invented.
- cardinality/namespace/pool preconditions as positional requires them.
- **Source family (H1):** the Arrow source field is `bool`, a signed int (`int8..int64`) or an
  unsigned int (`uint8..uint64`). Decimal, float, temporal, dictionary, binary, nested, null and
  string sources are NOT admitted. This needs the SOURCE SCHEMA; a config-only test cannot tell an
  admitted int from an excluded decimal, so the classifier is given the field, not inferred from
  masked output or profile dtype.
- **String-output provider with validated pool (M7):** mirror `reject_nonstring_positional_pools`
  (`native/_chunk_masking.py:228`) with a deterministic-pool content validator that runs on the
  actual pool values for exactly the new admitted set, on both chunked legs before output and on
  the full-frame path. A custom registry overriding an allowlisted provider with non-string values
  fails closed for the admitted column ONLY; non-admitted provider behavior is unchanged (no new
  blanket rejection of oracle-routed jobs).

The admitted datatype families are listed as explicit Arrow datatype instances in the registry
(`_operator_registry.py`, `_unified_slice_resident_types.py`), per C5c-i precedent.

## 4. Routes

### 4.1 Chunked native (admit against the oracle's effective sampling input, H4)

C5c-a supplies exact Arrow integers to the sampler ONLY when the frame column widened to `float64`
around nulls; other metadata-driven conversions (e.g. a `StringDtype`-tagged int column) remain
authoritative oracle behavior and would change the derived keys (probe: raw `int64 [1,2]` keys
`[807,524]` vs the `StringDtype`-reconstructed `["1","2"]` keys `[682,377]`). So chunked admission
is defined against the **oracle's effective per-chunk sampling input**: admit only when the
compiled kernel's key for each cell equals the oracle's key, which holds for the C5c-a exact-integer
exception and plain null-free int/uint/bool, and decline any conversion that changes canonical keys
or missingness. The check reads raw per-chunk metadata BEFORE normalization, on every chunk
(including later ones), not a copy of the unified round-trip guard.

### 4.2 Unified full-frame (keep the guard; exact nullable-int matrix, M5)

Keep `_unified_slice_admission.py:375-399` intact. The correct matrix:
- default-conversion mixed null/non-null integers (widen to `float64`): **decline** (guard rejects).
- pandas nullable `Int64`/`UInt64` columns that round-trip exactly: **admit** (they pass the guard).
- typed empty/all-null columns: bypass the value comparison at `:391`, so admission is by family.
Tests assert native activation for the admit cases and the existing decline for default widening.

### 4.3 The degenerate string pin through the WHOLE output path (H2)

The pin cannot be set only in assembly: the unified reconstruction does
`frame[col] = masked_col.to_pylist()` then `pa.Table.from_pandas(frame)`
(`_unified_slice_evidence.py:214-217`), and `from_pandas` infers `null`/`double` from the Python
objects, erasing an Arrow `string` pin (probe-confirmed). So the degenerate retype is a scoped step
applied AFTER `from_pandas`, re-casting exactly the admitted-classifier columns whose output came
back `null`/non-`string` to `string`, at all three from_pandas sites:
`_unified_slice_evidence.py:217`, `_pandas_adapter.py:336`, `_sequential.py:471`. Plus the two
`_chunked_schema_rule` string-pin sites for the chunked legs, and the oracle degenerate pin so the
chunked oracle leg matches native. The working frame's dtype during execution is NOT changed (only
the final output construction), so other readers are untouched. Matching pandas-string metadata is
defined for the retyped columns.

### 4.4 What does NOT change (C5c-a lesson)

Shared frame, `when:`/derived/case_when readers, group_by siblings, pandas metadata during
execution, positional-Faker outcomes (C5c-i), deterministic draw values (C5c-a). The fix stays at
admission + the final-output typing + pool validation.

## 5. Build order (tests-first)

1. Acceptance tests (§7) first. 2. The admission classifier (config + source field + effective
determinism + pool validation). 3. The degenerate pin at the three from_pandas sites + the two
chunked-schema-rule sites + the oracle. 4. Gates: full faker/native/parity/unified suites green;
C5c-i/C5c-a suites byte-identical; the pre-GA degenerate change recorded. 5. Docs: CHANGELOG pre-GA
note with the migration line (empty/all-null admitted deterministic-Faker int/uint/bool column:
`double`/`null` -> `string`), compatibility-contract ROUTE-OUTPUT-CONTRACT update, build record,
roadmap row.

## 6. Failure modes

- Non-string pool under an allowlisted provider (custom registry override): fails closed for the
  admitted column only; non-admitted columns unchanged (M7).
- Mixed null/non-null default-widened int on the UNIFIED route: declines (guard), runs chunked/oracle.
- A source family outside bool/int/uint: declines on both routes.
- Companion absent: deterministic selection has its pure-Python oracle (`_index_ext.py`); the column
  runs byte-identical, its table just not accelerated. Every positive parity test asserts native
  activation (or a poisoned fallback), so a silent fallback cannot masquerade as a pass.

## 7. Acceptance tests (byte parity within a route; pre-GA degenerate change pinned; M8)

- **Native activation vs poisoned fallback:** every admitted positive case asserts the native path
  actually ran (poison the Python path / instrument), never a silent oracle fallback counted as a
  pass.
- **Within-route parity:** native == oracle on schema + pandas metadata + values + warnings + row
  errors + non-timing evidence, for deterministic Faker over bool, int8-int64, uint8-uint64
  (string-output provider), including the C5c-a nullable-int chunked case and nullable `Int64`/
  `UInt64` on the unified route.
- **Degenerate pin (the §2 change):** all-null and empty admitted columns emit `string` on the
  oracle, both chunked legs (resident AND streamed output), and the unified leg. Chunk-count
  coverage places an empty/all-null chunk in the LEADING, INTERIOR and TRAILING position, and 1-vs-N
  split gives identical Arrow type.
- **Negative scope controls:** degenerate output is UNCHANGED for other strategies, excluded Faker
  families (decimal/float/temporal/...), string-source deterministic Faker, positional Faker, and
  `when:` cases. The oracle pin touches only the admitted set (proven by these).
- **Key/boundary:** compiled == reference selection at integer boundaries incl. values `> 2**53`
  and unsigned `> 2**63`; the H4 key-equivalence declines (StringDtype-tagged int) decline.
- **Cross-route:** expected route differences pinned per ROUTE-OUTPUT-CONTRACT, not asserted equal
  whole-table. **Regressions:** C5c-i and C5c-a outcomes byte-identical. **Mutation** on the
  classifier (determinism alias, source-family, pool validation), the pin-set membership, and the
  degenerate retype at each from_pandas site.

## 8. Open points for the Codex plan gate (round 2)

- Confirm the post-`from_pandas` retype is the right mechanism at all three sites vs carrying an
  authoritative dtype into reconstruction (the empty-table path already carries one via
  `_assemble_column`; is the all-null-in-nonempty-table case covered by the same seam?).
- Confirm the chunked effective-sampling-input admission (H4) is checkable from per-chunk metadata
  alone, without materializing the oracle's conversion.
- Confirm bool/int/uint is the right rev-2 scope and timestamps are cleanly deferred.
