Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, testing, observability-and-resilience, code-review.

# C5c-i: Faker over non-string sources

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C. Branch `feat/c5c-nonstring-faker` off engine main `eaa623ca`. Risk R2: one oracle bug fix plus wider native admission on two routes. Facts come from a code survey on 2026-10-07 (session scratchpad report). Each fact is cited below so the gate can check it.

## 1. Goal and scope

Faker columns whose source is not a string run on the native routes where the oracle's result is chunk-stable and a native encoder already reproduces it. One oracle bug is fixed on the way.

**In:**
- **(A) Oracle fix (Cam, 2026-10-07: "make it work").** Deterministic Faker over an integer column with nulls works, instead of failing with `float_canonicalization_unsupported`.
- **(B) Positional (non-deterministic REUSE) Faker** over every non-dictionary source type, on both native routes.
- **(C) Deterministic Faker** over bool, signed and unsigned integers (nulls included, after A) and timezone-aware timestamps, on both native routes.

**Out (C5c-ii or later):**
- Deterministic Faker over `date32`/`date64` and decimal. The oracle supports them, but Rust has no encoder (`decoy-engine-native/src/canonicalize.rs:218-223`).
- Dictionary-encoded sources (kept declined).
- `large_string` on the unified route, unless it falls out for free.
- Float and naive-timestamp sources under deterministic Faker. The oracle raises for these by design (`generation/pool/_canonicalize.py:89`, `:101-114`), so they decline to the oracle, which raises the same error as today.

## 2. Established facts

**Oracle, deterministic** (`_strategies/_faker.py:117-130`, `generation/pool/_sampler.py:117-157, ~330-370`, `generation/pool/_canonicalize.py:62-119`):
- Nulls (`isna`) are masked. Each other value is canonicalized by type:
  - bool gives `\x01` or `\x00`;
  - int gives a length-prefixed two's complement;
  - float raises;
  - Decimal gives `str()`;
  - a tz-aware datetime gives UTC ISO;
  - a naive datetime raises;
  - a date gives ISO.
- **Integer with nulls:** the adapter's `to_pandas_fk_safe` (`_fk_keys.py:492-547`) protects only FK, group-anchor, top_code and group_key integer columns (`_pandas_adapter.py:209-218`). A plain Faker source with a null becomes float64, and canonicalization raises. A chunk WITHOUT a null stays int64 and succeeds, so the chunked oracle's result depends on chunk boundaries (`native/_dispatch.py:426-436` comment).
- `plan/_checks.py:36` rejects int-with-null at plan time for truncate, hash and categorical, but not for Faker.

**Oracle, positional** (`_faker.py:97-110`): reads only `source.isna()`. A NaN float, NaT, `pd.NA` or None is null. Source values are ignored.

**Oracle, write-back** (`_faker.py:109, 130`): the whole column is replaced by a list of str or None. `from_pandas` gives `string`, or `null` when all values are None.

**Native:**
- Deterministic Faker passes the Arrow column to `derive_index_batch` (`native/_operator_step.py:65-124`).
- `canonicalize.rs` (`:179-224, 262-277`) admits utf8, large_utf8, bool, int8-int64, uint8-uint64 and tz-aware timestamps. It byte-matches `_canonicalize_source` for these, pinned by `vectors/derive_index_kat.json` and `tests/native/test_derive_index_kat.py:143`.
- Positional Faker never reads source values. It restores nulls from `col.is_valid()` (`_operator_step.py:~173`). **Gap:** a float NaN is Arrow-valid but pandas-null.
- Output is always `pa.string()` (`_gather_pool_values`).

**Decline sites today:**
- chunked: `native/_dispatch.py:426-446` (`faker_source_type_not_string`);
- unified: `physical/_shadow_bindings.py:147-175` (`_faker_pool_bindable`) and `:197-209` (positional);
- the operator registry's `unified_resident_types=_STRING_ONLY` for Faker (`_operator_registry.py:76, 137`; enforced at `_unified_slice_admission.py:511`).

**Output pins:**
- Positional Faker's chunked output is pinned to `string` by config (`native/_chunked_schema_rule.py:38-44, 110-123, 257`).
- Deterministic Faker is not pinned. How the chunk joiner handles a deterministic string-source column today (native `string` against an oracle chunk of `null` or float64) applies unchanged, because the output never depends on the source type. The builder confirms this with a test, not by assumption.
- Unified: the output type is `string` for Faker (`physical/_requirements.py:295-315`). Reconstruction replaces the whole column (`_unified_slice_evidence.py`), which matches the oracle.

## 3. Decisions

**3a. Oracle fix (A).**
- In `PandasExecutionAdapter`'s conversion (`_pandas_adapter.py:209-218`), the protected set gains each DETERMINISTIC Faker target column whose Arrow type is an integer AND whose column has at least one null (`null_count > 0` on the table being converted).
- `to_pandas_fk_safe` then reads it as the matching nullable integer dtype (`Int64`, `UInt64` and so on), exact even above 2**53.
- The Faker handler's existing path then masks `pd.NA` and canonicalizes the values as Python ints, the same bytes as a null-free int64 column.
- **Why only columns that have nulls:** a null-free integer column already converts to int64 and works today. Protecting it would change the dtype that other readers of that column see in jobs that succeed now. Columns with nulls are exactly the ones whose jobs fail today, so only failing jobs change (Cam: a bug fix).
- **Chunked:** the same rule applies per chunk, so a chunk without nulls stays int64, a chunk with nulls becomes `Int64`, and both give identical keys. The chunked oracle becomes chunk-stable for this case.
- The compiled pool-index path (`_sampler.py` `pa.Array.from_pandas`) must accept the nullable dtype and give the same result as the reference path. Test 1.
- The same carve-out goes in the chunked oracle leg's conversion and any other place the adapter's protected set is rebuilt (the chunked carry's `adapter_fk_safe_columns`), so both legs agree. The builder lists every site in the record.

**3b. Positional Faker (B).**
- Both routes admit any source type except dictionary-encoded.
- The native null mask becomes "pandas null": `is_valid`, AND, for floating-point types, NOT NaN (`pc.is_nan`).
- Timestamps and dates have no NaN distinct from null in Arrow, and decimals cannot hold NaN. The builder verifies both with a test.

**3c. Deterministic Faker (C).**
- Both routes admit bool, int8-int64, uint8-uint64 (with or without nulls) and timestamp with a timezone.
- Float, naive timestamp, date, decimal and dictionary sources keep declining, with the existing code or a precise new one recorded in the record.
- The Rust encoder already covers the admitted types.

**3d. Registry and admission.**
- Faker's `unified_resident_types` widens to the admitted set, split by variant if the registry needs it.
- `_faker_pool_bindable` and `positional_faker_bindable` check the variant's admitted set.
- The chunked `faker_source_type_not_string` check becomes a per-variant admitted-type check.
- The existing all-null and empty-table behavior is unchanged.

**3e. Logs.** No data values are logged; the log sentry passes unchanged.

## 4. Acceptance tests (written first; never weakened)

Every differential compares lane-on against an explicit lane-off run with the fallback poisoned for admitted cases: output tables byte-equal (schema and `b"pandas"` metadata), warnings, row errors, and metrics minus timings and the activation leaf.

1. **Oracle fix:**
   - deterministic Faker over int64 with nulls, int32 with nulls, and uint64 with nulls including values above 2**63, completes;
   - each non-null value maps to the same fake value it gets in a null-free copy of the column;
   - nulls stay null;
   - values above 2**53 key exactly;
   - the compiled and reference pool-index paths agree;
   - a null-free int column's frame dtype is unchanged from main (it is not protected).
2. **Only failing jobs change:** a job with a null-free integer Faker source, plus a `when:` predicate and a group_key sibling reading that column, has byte-identical output to main.
3. **Chunked stability:** an int64 column with nulls in some chunks and not others gives the same output as one pass, on the chunked oracle and on the native route.
4. **Positional, every type:**
   - int, uint, float (with real NaN values and nulls), bool, date32, timestamp tz and naive, decimal;
   - both routes, several batches, nulls across boundaries;
   - native output equals the oracle, including NaN rows becoming null.
5. **Deterministic, admitted types:** bool, int8-int64 and uint8-uint64 (boundaries, with and without nulls) and tz-aware timestamps in all four units, on both routes.
6. **Declines:**
   - deterministic float, naive timestamp, date32, decimal and dictionary all decline;
   - the job's outcome (output or error) equals lane-off;
   - float and naive raise the oracle's error.
7. **The chunk joiner** for deterministic Faker over a non-string source with an all-null chunk and an empty chunk matches the oracle.
8. **The tests that pinned the old decline** (listed in the survey: `tests/native/test_dispatch_faker.py`, `test_chunked_nondet_faker_admission.py`, `test_chunked_nondet_faker_auto_route.py`, `tests/physical/test_unified_slice_faker.py` decline cases, plus the registry snapshot tests) are updated ONLY where the admitted set changed. A decline that still applies keeps its test. The record lists each change and why.
9. **Testflight:** if any fingerprint moves, STOP and report.
10. **Sentries;** a perf record at 1M rows (positional over int64, deterministic over int64 with nulls); mutation on the changed units.

## 5. Failure modes

| Risk | Closed by |
|---|---|
| The oracle fix changes jobs that work today | Protect only integer columns with nulls; test 2 |
| Native and oracle disagree on what counts as null | 3b NaN rule; test 4 |
| The chunked oracle and native disagree on a chunk with nulls | The same carve-out on both legs; test 3 |
| Large integers lose precision | Nullable dtypes are exact; test 1 values above 2**53 |
| A decline test is silently dropped | Test 8 rule and record list |

Rollback: revert the merge commit.
