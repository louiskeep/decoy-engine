# C5c-ii: deterministic Faker over non-string sources on the native routes

Status: plan (rev 4, Codex-authored chunked-route remediation in §10; escalate-to-plan). Rev-3 §4.1 superseded by §10. Phase C, Rust-engine program.
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


---

## 10. Rev 4 — Codex-authored chunked-route remediation (escalate-to-plan)

Authored by Codex (gpt-6-astra) 2026-10-09 per escalate-to-plan after the 3-round cap.
AUTHORITATIVE for the chunked route; SUPERSEDES rev-3 §4.1 and the §7 'Conversion-free
allowlist controls' bullet. Option A, the bool/int/uint scope, §4.2 unified guard, and §4.3
degenerate pin are unchanged. Next: dennis gates this, then Codex re-gates, then build.

### 4.1 Chunked native — schema-aware admission with a producer-enforced stream guarantee

C5c-ii admits deterministic Faker over bool/int/uint only when **both** the conversion classifier and the stream-guarantee check pass. Admission remains one decision for the whole table, made before any masking or output. A failure of either check declines to the existing oracle route.

A `pa.Field` is insufficient: pandas reconstructs dtypes and column identities from schema-level `b'pandas'` metadata. Likewise, the first chunk’s schema cannot establish what an arbitrary iterable will yield later.

This section replaces the field-only chunked classifier described in §3. Option A, the bool/int/uint target scope, the unified-route guard, and existing config/provider/pool requirements remain unchanged.

**Conversion classifier inputs and result**

Add a private classifier taking:

- The effective Faker work node and its existing config eligibility.
- The **complete original source `pa.Schema`**, including schema metadata and every field’s metadata.
- The target column’s name and ordinal. Require unique schema names and an exact name/ordinal match.
- The existing execution facts needed to establish that this node samples the original column: stock adapter, effective scalar Faker writer, and no earlier writer of the target.

The classifier returns a prepared conversion classification or a coded decline reason. It must not reconstruct a one-field schema, inspect output metadata, infer types from the profile, or run a sample conversion to establish admission.

The target must have exactly one of the existing bool, signed-integer or unsigned-integer physical types. Arrow extension types and extension storage masquerading through field metadata do not qualify.

**Closed metadata-shape allowlist**

Accept only the following two schema-metadata shapes. An unrecognized shape declines; it is not repaired, stripped, or interpreted optimistically.

1. **Metadata absent.**

   Schema metadata is `None` or empty, every field’s metadata is `None` or empty, and no field is an Arrow extension type. The target is a plain bool/int/uint field.

2. **Plain pandas metadata with an identity column mapping.**

   Schema metadata contains exactly `b'pandas'`; all field metadata is absent or empty. Parse the bytes as UTF-8 JSON using the standard JSON parser, rejecting duplicate object keys, invalid JSON and nonstandard numeric constants.

   The decoded object has exactly these required keys:

   ```text
   index_columns, column_indexes, columns, creator, pandas_version
   ```

   It may additionally contain `attributes`, but only with the value `{}`. No other keys qualify.

   Require:

   - `creator` is exactly an object with `library == "pyarrow"` and a nonempty string `version`.
   - `pandas_version` is a nonempty string.
   - Version strings are provenance, not evidence of safe conversion. The structural checks below determine admission.
   - `columns` contains exactly one entry per physical source field, in source-schema order.
   - Every entry has exactly `name`, `field_name`, `pandas_type`, `numpy_type`, and `metadata`.
   - Both `name` and `field_name` equal that physical field’s string name. No aliases, duplicate entries, omitted fields, stale entries or non-string labels qualify.
   - Every column entry has `metadata: null`.
   - Each entry matches this explicit physical-type table:

   | Physical Arrow type | `pandas_type` | `numpy_type` |
   |---|---|---|
   | `bool` | `"bool"` | `"bool"` |
   | `int8`, `int16`, `int32`, `int64` | Exact corresponding lowercase type name | Same |
   | `uint8`, `uint16`, `uint32`, `uint64` | Exact corresponding lowercase type name | Same |
   | `float32`, `float64` | Exact corresponding lowercase type name | Same |
   | `string`, `large_string` | `"unicode"` | `"object"` |

   The float and string rows permit ordinary companion columns; they do **not** expand the C5c-ii Faker target domain.

   Accept exactly two index-layout combinations:

   - `index_columns == []` and `column_indexes == []`; or
   - `index_columns` contains one object with exactly
     `{"kind": "range", "name": null, "start": 0, "stop": N, "step": 1}`,
     where `N` is a nonnegative integer, excluding booleans, and
     `column_indexes` contains exactly:

     ```json
     {
       "name": null,
       "field_name": null,
       "pandas_type": "unicode",
       "numpy_type": "object",
       "metadata": {"encoding": "UTF-8"}
     }
     ```

   Do not require `N` to equal each chunk’s length: resident slices preserve the original RangeIndex metadata. No stored index field, named/custom index or MultiIndex shape qualifies.

This deliberately declines pandas nullable extension metadata (`Int64`, `UInt64`, `boolean`), Arrow-backed pandas dtypes (`bool[pyarrow]`, integer Arrow dtypes), `Float64`, StringDtype, categorical metadata, mismatched widths or signedness, unknown metadata keys, and other unlisted shapes. Some declined shapes may be safe; proving additional shapes is outside this slice.

In particular, physical `int64` with a schema-level column entry declaring `numpy_type: "bool[pyarrow]"` declines even when its `pa.Field` has no metadata. The classifier checks **both** `pandas_type` and `numpy_type`; checking only the former is insufficient.

The conservative parser is an admission predicate, not a new public input validator. A parse or shape failure returns a decline. The oracle retains its existing behavior for that input.

**Null handling: prove every chunk shape rather than infer stream statistics**

Admission does not depend on the first chunk’s null count. For each accepted metadata shape, prove all cases:

| Target/chunk shape | Oracle sampling input |
|---|---|
| Bool, with or without nulls | Exact boolean values; missing positions match Arrow validity |
| Integer with no nulls | Exact integer values |
| Integer with both valid values and nulls, converted to NumPy `float64` | C5c-a supplies the original Arrow integers at the Faker sampling boundary |
| Integer preserved by the existing protected-column conversion | Exact integer values |
| Empty or all-null bool/integer | No valid draw keys; Option A determines degenerate output typing |

C5c-a’s actual condition is physical integer, converted frame dtype exactly NumPy `float64`, and `0 < null_count < length`. Preserve that condition and its no-earlier-writer requirement.

`null_count` comes from the **current source column array**, not its field: `_exact_int_faker._widened_int_column` reads `source.column(column).null_count` and the column length. Registration already runs on each oracle chunk through `PandasExecutionAdapter`. No new stream-wide null statistics, footer requirement or eager scan is needed for this admission design.

A later chunk acquiring nulls therefore does not change admission. It enters another already-proven case. This does not admit a later physically `null`-typed column: guaranteed producers preserve the concrete source type even for all-null chunks.

**Complete stream-schema guarantee**

Introduce a private source abstraction, `_FixedSchemaChunks`, whose read-only `source_schema` is tied to the stream it produces.

Its invariant is:

```python
chunk.schema.equals(source_schema, check_metadata=True)
```

for every emitted table, including empty tables if the producer emits them.

This covers field order, names, physical types, nullability, field metadata, and **all schema metadata, including the complete raw `b'pandas` value**. Neither physical-type equality nor equality of stored-index field names is sufficient. Arrow exposes metadata-inclusive schema equality explicitly. [Arrow Schema API](https://arrow.apache.org/docs/python/generated/pyarrow.Schema.html#pyarrow.Schema.equals)

Only two internal construction paths may produce this abstraction:

1. **Resident table slices.** Capture the actual resident `pa.Table` and its schema, and produce chunks solely through that table’s `slice`. Use this for both resident-output auto-chunk execution and resident input with streamed output.
2. **Fixed-schema reconstructed batches.** Capture the opened batch source’s schema and use the existing `rechunk` construction, which builds every emitted table through `pa.Table.from_batches(..., schema=captured_schema)` and then slices it.

The second path guarantees the schema of the tables the dispatcher and oracle actually receive. It does not assert that arbitrary incoming batch metadata is identical. Reconstruction under the captured schema is already the lazy-input path’s behavior; retain it.

Do not add a public `fixed_schema=True` flag, accept a duck-typed `.schema` attribute, or provide a generic “certify this table iterable” constructor. The private abstraction owns the producer; its supported factories do not pair arbitrary table iterables with an asserted schema. Recognition uses the concrete internal type, not a structural protocol or subclass override.

Retain lazy-source footer checks, row-count checks and handle closure. The wrapper must forward `close()` so it does not interrupt `InputChunks.close()` or `_guarded` cleanup.

**Wiring and enforcement points**

All locations below refer to HEAD `1a007f38`; paths are under `src/decoy_engine/execution/`.

- **Resident output:** `_pipeline_auto_chunk.py:149` currently produces resident slices, and `_run_dispatcher` passes them to `run_mask_chunked` at `:265–267`. Replace that input with the private resident-slice producer.
- **Resident input with streamed output:** `_chunked_input.py:348–373`, specifically the resident `InputChunks` construction at `:371`, must retain the same producer guarantee.
- **Lazy fixed-schema input:** `_chunked_input.py:214–237` reconstructs chunks with an explicit schema at `:231` and `:237`. `_open_lazy` wires this through `_guarded` at `:330–332` and `_chain` at `:345`. Preserve the guarantee on the resulting `InputChunks.chunks`, including across first-chunk priming and chaining.
- **Streamed dispatcher call:** `_pipeline_auto_chunk.py:315–317` passes that guaranteed `inputs.chunks` object to `run_mask_chunked`.

Capture the private producer and its schema at the start of `_chunked_entry._run_chunked`, **before** `_oracle_preflight` at `native/_chunked_entry.py:425` converts the input to an iterator and wraps it. `_oracle_preflight` currently consumes the first chunk at `_chunked_oracle.py:114–115`; preserve that consumption pattern.

After ordinary preflight, verify that `state.first.schema` equals the captured schema with metadata enabled. A missing guarantee or failed first-schema match cannot admit C5c-ii. Do not infer a guarantee from `state.first`.

Thread a typed admission context containing the captured schema/guarantee through the existing call to `plan_native_route` at `native/_chunked_entry.py:451–460`. Default the context to absent for existing callers.

The authoritative new Faker branch belongs in `native/_dispatch.py:426–452`, replacing the deterministic non-string rejection only when all C5c-ii conditions pass. It invokes the schema-aware classifier before companion probes or native pool execution. Callers without the context cannot obtain this new admission, including schema-less admission-only callers.

Use distinct coded decline reasons, for example:

```text
faker_conversion_schema_not_guaranteed:<column>
faker_conversion_metadata_not_allowlisted:<column>
```

Keep existing type/config declines for sources outside the new domain. A declined column downgrades the whole table using the existing mechanism; `_chunked_entry.py:506–518` remains the oracle branch.

**Arbitrary iterable behavior**

A caller supplying a list, generator, iterator, or other ordinary `Iterable[pa.Table]` has no producer guarantee. If the table needs C5c-ii admission, decline it **up-front**, even when the first chunk has an allowlisted schema and even when all chunks happen to match.

Up-front means after the existing eager preflight and first-chunk pull, but before companion execution, masking, vault writes or yielding output. Do not read the rest of the iterable to establish a guarantee.

Pass the original first chunk and remaining iterator to the oracle. Do not normalize, strip or replace their input metadata. Consequently, a later chunk with different pandas dtype metadata retains its own oracle conversion and draw keys.

Do not strengthen `validate_chunk_schema` at `native/_chunk_schema.py:84` for this feature. Its existing checks, installed by `_chunked_entry.py:450`, continue on both routes. The new guarantee is established **at the producer, before this validator**, and consumed during admission. It is not supplied by the validator.

There is no later-chunk admission check, route switch or new metadata-drift error. Arbitrary metadata-drifting inputs that are currently accepted remain oracle inputs. Other strategies and existing string/positional Faker admissions do not acquire this new guarantee requirement.

**Relationship to Option A**

The stream guarantee is a backend-admission requirement. It must not make the agreed degenerate string pin depend on `decision.native_admitted`. Preserve §4.3’s effective-writer, degenerate-output and `when:` exclusions on both routes, including otherwise pin-eligible work that declines because the stream has no guarantee.

Keep shared config/family/pin facts separate from the additional chunked conversion proof in the prepared classification. Consumers must not independently recreate a field-only classifier.

**Bounded scope and design rationale**

This slice safely accelerates allowlisted bool/int/uint targets from resident slices and fixed-schema reconstructed batches. It does not accelerate those targets from arbitrary table iterables or unlisted metadata shapes. Those cases remain on the oracle.

The design reuses existing slice/reconstruction producers, the standard JSON parser, Arrow schema comparison and C5c-a’s sampling boundary. Arrow documents both metadata-driven pandas reconstruction and default nullable-integer widening. [Arrow pandas integration](https://arrow.apache.org/docs/python/pandas.html#nullable-types)

The private producer abstraction exists because iterator wrapping currently erases the provenance needed for admission. Its invariant belongs to the producer; the conversion allowlist belongs to one classifier. No buffering of the complete input or per-chunk pandas conversion is added to native admission.

### §7 — replacement and additional chunked acceptance tests

Replace the existing “Conversion-free allowlist controls” bullet and qualify chunked positive-admission tests with the following cases.

1. **Accepted metadata shapes and actual native activation.**

   Exercise every bool/int/uint target width through both trusted producers, using metadata absent and each accepted pandas index-layout shape. Include accepted envelopes with `attributes` absent and `{}`. Include ordinary float/string companion columns matching the metadata table.

   Assert native activation explicitly and compare against the forced oracle using the same produced tables. Compare values, schema, pandas metadata, warnings and row errors; compare non-timing evidence semantically, with backend differences asserted separately.

2. **Schema-level reconstruction regression.**

   Use physical `int64 [1, 2]` with transplanted schema-level `bool[pyarrow]` metadata and empty field metadata. Assert metadata-classifier decline even on a guaranteed resident source. Verify the reported reproducer’s differing native/oracle indices (`[216, 50]` versus `[161, 161]`, under its fixed seed/pool fixture), and assert dispatcher output equals the oracle.

   Include a case changing only `numpy_type` while leaving `pandas_type` physically matching. It must decline.

3. **Closed metadata-shape rejection matrix.**

   Independently reject:

   - Nullable pandas and Arrow-backed extension dtype declarations, `Float64`, StringDtype and category.
   - Mismatched integer width or signedness.
   - Unknown schema metadata keys and nonempty field metadata, including extension markers.
   - Malformed UTF-8/JSON, duplicate JSON keys, unknown object keys and wrong member types.
   - Missing, duplicate, reordered or stale column entries; renamed `name`/`field_name`; duplicate physical names.
   - Stored indexes, named/custom RangeIndexes, MultiIndexes and unlisted column-index metadata.
   - Unlisted physical-type/metadata pairs and nonempty `attributes`.

   Classifier tests assert a decline rather than a new validation exception. End-to-end cases accepted by the current oracle must still produce its result. Cases the existing preflight/oracle rejects retain that existing failure behavior.

4. **Producer guarantee covers the complete schema.**

   For resident slices and fixed-schema reconstructed batches, assert metadata-inclusive equality to the captured schema for every emitted chunk. Include schema metadata and field metadata in producer-level fixtures, regardless of whether admission later declines those fixtures.

   For reconstructed batches, vary incoming batch metadata while keeping compatible physical arrays. Assert that every emitted table has the explicitly supplied reconstruction schema and that native/oracle comparison uses those reconstructed tables.

   Exercise resident output, resident input with streamed output, lazy streamed input, and closure on exhaustion, early close and failure.

5. **Same data, different provenance.**

   A trusted resident-slice source with allowlisted metadata activates native execution. An ordinary list or generator yielding the exact same tables declines C5c-ii and executes the oracle.

   A caller-defined iterable exposing `.schema`, `.source_schema` or a claimed fixed-schema flag remains unguaranteed. Existing direct `plan_native_route` calls without the new context cannot admit deterministic non-string Faker.

6. **Per-chunk pandas-metadata drift declines before output.**

   Retain the reported two-chunk fixture: matching physical `int64` fields, first-chunk allowlisted metadata, second-chunk metadata reconstructing booleans. Establish that the existing `validate_chunk_schema` accepts the pair.

   Pass it as an ordinary iterable. At dispatcher return, assert the route is already oracle and only the existing first-chunk preflight pull has occurred. Poison the native masker/index invocation. Consuming both chunks must succeed and reproduce the oracle outputs:

   ```text
   chunk 1: ["Katherine", "Samuel"]
   chunk 2: ["Diane", "Diane"]
   ```

   Pin the original seed/provider/pool fixture. Assert no new metadata-drift exception, no native prefix and no metadata normalization before oracle conversion. Repeat with metadata removal/addition and with an empty or all-null leading chunk.

7. **Null-pattern independence and exact integers.**

   For each admitted integer width and bool, vary chunks among null-free, mixed-null, all-null and typed-empty, including leading/interior/trailing positions. The guarantee and native decision remain unchanged.

   Cover signed boundaries, integers above `2**53`, unsigned values above `2**63`, and `uint64` maximum. Confirm that C5c-a reads the actual current array’s null count and supplies exact values for mixed-null widened integers. First-chunk null statistics must not decide later-chunk admission.

   Retain Option A assertions on native and oracle output, including unguaranteed iterable fallback, and retain all `when:`/effective-writer negative controls.

8. **Regression updates and mutation coverage.**

   Split the existing C5c-i deterministic-numeric decline expectation at `tests/native/test_c5c_i_chunked_positional.py:259`: the trusted allowlisted case now admits; the ordinary-iterable case continues to decline. Do not blanket-flip every direct chunked call to admission.

   Kill mutations that remove the producer-guarantee requirement, infer it from chunk 1, ignore schema-level metadata or `numpy_type`, accept unknown metadata shapes, lose the guarantee during priming/chaining, omit metadata-inclusive schema equality, or allow guarantee absence to disable the Option A pin. Preserve existing physical-drift and stored-index error tests unchanged.

### §10 clarifications (dennis gate, 2 LOW, folded)

- **OOC / arbitrary-iterable chunked adapter stays unaccelerated (intentional).** The out-of-core
  `NativeOrOracleChunkedAdapter.run` (`execution/physical/drivers/_chunked.py:121`) forwards an
  arbitrary `Iterable[pa.Table]` to `run_mask_chunked`; it is NOT a producer-wrap site. That is
  correct: no stream guarantee -> C5c-ii declines up-front -> oracle. A builder must NOT wrap it to
  force admission; it is out of this slice's bounded scope.
- **Route-dependent admission for nullable Int64/UInt64 (intentional, cross-ref §4.2).** A pandas
  nullable `Int64`/`UInt64` column round-trips exactly, so §4.2 ADMITS it on the unified full-frame
  route, but §10's conservative chunked allowlist DECLINES it (it is an extension metadata shape).
  So the same column accelerates when the job is small (unified) and declines to the oracle when
  large (chunked). This asymmetry is intentional and safe (§10: "some declined shapes may be safe;
  proving additional shapes is outside this slice"), not a §10 bug.
