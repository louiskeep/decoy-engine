# C5c-ii: deterministic Faker over non-string sources on the native routes

Status: plan (rev 3, folds Codex plan-gate round 2). Phase C, Rust-engine program.
Branch `feat/c5c-ii-deterministic-nonstring-faker` off engine main `69a7741f`.
Follows C5c-i (#220, positional Faker over non-string) and C5c-a (#221, oracle
sampling-boundary fix for deterministic Faker over nullable ints).

Rules consulted: 00-universal, risk-and-exceptions, development-loop, feature-dev, testing,
architecture, api-and-compatibility, performance, documentation.

Rev 2 (Codex round 1) folded H1/H2/H3/M5/M6/M7 and most of M8. Rev 3 (Codex round 2, REVISE,
option A upheld; Cam 2026-10-09 "push conservative rev-3") resolves the two remaining HIGHs, both
on the chunked-route admission, by making admission CONSERVATIVE and UP-FRONT:
- **r2-H1 (metadata-driven keys):** config + an Arrow field cannot prove the oracle and the kernel
  derive the same draw key, because pandas/extension metadata reconstructs different values (an
  `int64` tagged `bool[pyarrow]` -> bools; `Float64` -> floats). Rev 3 replaces "decline
  key-changing conversions" with a CONVERSION-FREE ALLOWLIST: admit only a plain physical
  int/uint/bool field with no reconstructing metadata (plus the C5c-a exact-integer null case);
  decline everything else to the oracle.
- **r2-H2 (per-chunk route commitment):** the dispatcher picks one route before yielding any chunk,
  so "decline a later chunk" is not expressible. Rev 3 decides admission ONCE from the column's
  fixed stream field, before any chunk, so there is no later-chunk transition.
- **r2-M3:** the string pin fires only on a genuinely degenerate output (`null_count == num_rows`
  or empty) AND excludes `when:`-bearing columns, scoped to the effective Faker work node.
- **r2-M4:** the C5c-i decline assertions that now flip to admit are enumerated; evidence is
  compared semantically with the expected native-vs-oracle activation difference asserted
  separately, not by literal whole-evidence equality.

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

### 4.1 Chunked native — conversion-free up-front allowlist (resolves r2-H1 + r2-H2)

Admission is decided ONCE from the column's stream field, before any chunk is yielded (the
dispatcher commits to a route up-front; there is no later-chunk decline, r2-H2). A deterministic
Faker column is chunk-admissible iff its field is CONVERSION-FREE, meaning the oracle's per-chunk
`to_pandas` sampling input is provably the identical physical value the native kernel reads:

- physical Arrow type is a plain `bool`, `int8..int64`, or `uint8..uint64`, AND
- the field carries NO reconstructing metadata: no Arrow extension type, and no pandas field
  metadata (`b'pandas'`) that declares a different logical dtype (no `bool[pyarrow]`, `Float64`,
  `Int64`/`UInt64` extension, `StringDtype`, category, etc.). A plain physical int/uint/bool with
  absent or physically-matching pandas metadata is admitted; anything else is NOT.

The one allowed conversion is the C5c-a case: an integer column with nulls that pandas widens to
`float64`, where C5c-a already supplies the exact integers to BOTH the oracle sampler and the
kernel, so keys match. That case is detectable up-front (int physical type, `null_count > 0`).

Everything outside this allowlist (any extension/reconstructing metadata, any non-admitted family)
DECLINES to the oracle, unchanged from today. The allowlist is conservative by construction: an
unknown or mismatched metadata shape declines rather than guessing key-equivalence (probe r2-H1:
`int64` tagged `bool[pyarrow]` gives native keys `[194,193]` vs oracle `[388,388]` — exactly the
kind of column this allowlist refuses). If the stream cannot guarantee one fixed field for the
column across chunks, the column declines (no mixed-metadata stream reaches the kernel).

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
applied AFTER `from_pandas`, at all three from_pandas sites (`_unified_slice_evidence.py:217`,
`_pandas_adapter.py:336`, `_sequential.py:471`), plus the two `_chunked_schema_rule` string-pin
sites and the oracle degenerate pin so the chunked oracle leg matches native.

**The retype fires only on a genuinely degenerate output (r2-M3):** a column is retyped to `string`
iff it is an admitted deterministic-Faker column AND its output `null_count == num_rows` (all-null)
or the table is empty, AND it is NOT `when:`-bearing (a `when:` predicate can leave value-bearing
cells, so such a column is never pinned — probe r2-M3: a zero-selected `when:` over int `[7,8]`
must stay `[7,8]`, not `["7","8"]`). Pin membership is the effective Faker work node, excluding
FK-resolution overrides or other writers whose output the Faker pool does not guarantee. The
source-schema facts the classifier needs are captured BEFORE sequential execution deletes `src`.
A cast alone does not fix pandas metadata (probe: empty keeps `pandas_type: float64`, all-null
keeps `pandas_type: empty`), so the retype explicitly sets the string pandas metadata for exactly
the retyped columns and leaves unrelated metadata untouched. The working frame's dtype during
execution is NOT changed (only final output construction), so other readers are untouched.

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
  whole-table.
- **Regressions with explicit exceptions (r2-M4):** C5c-i POSITIONAL outputs and C5c-a deterministic
  DRAW VALUES stay byte-identical. The C5c-i assertions that a deterministic numeric column DECLINES
  now flip to admit and are enumerated and updated (`tests/native/test_c5c_i_chunked_positional.py:259`,
  `tests/physical/test_c5c_i_unified_positional.py:156`); the excluded-family / string-source /
  positional / `when:` decline controls are preserved. Evidence is compared SEMANTICALLY (values,
  warnings, row errors, schema/metadata), with the expected native-vs-oracle backend/activation
  difference asserted separately, not by literal whole-evidence equality.
- **Conversion-free allowlist controls (r2-H1):** a plain int/uint/bool admits and activates native;
  a column tagged `bool[pyarrow]`, `Float64`/`Int64` extension, `StringDtype`, category, or any
  non-physically-matching pandas metadata DECLINES to the oracle (each a pinned case). The C5c-a
  null-int float64-widen case admits.
- **Mutation** on the classifier (determinism alias, source-family, the conversion-free metadata
  check), the pin-set membership (degenerate `null_count==num_rows` + `when:` exclusion), and the
  degenerate retype at each from_pandas site.

## 8. Open points for the Codex plan gate (round 3)

- Confirm the conversion-free allowlist (§4.1) is decidable from the column's stream field alone
  and that a plain physical int/uint/bool with absent-or-physically-matching pandas metadata is the
  complete safe set (plus the C5c-a null-int widen case); i.e. nothing outside it can make the
  oracle and the kernel disagree on a key.
- Confirm the up-front single-route decision fully retires the later-chunk-decline problem (r2-H2),
  given the stream carries one fixed field per column.
- Confirm the degenerate-only + `when:`-excluded pin (§4.3) plus the explicit pandas-metadata set
  is complete, and that the retype touches only genuinely all-null/empty admitted columns.
