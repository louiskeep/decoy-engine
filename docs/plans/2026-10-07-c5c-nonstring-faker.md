Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, testing, observability-and-resilience, code-review.

# C5c-i: positional Faker over numeric and boolean sources

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C. Branch `feat/c5c-nonstring-faker` off engine main `eaa623ca`. Risk R2. Facts come from a code survey (2026-10-07) and Codex plan-gate round 1; each is cited so the gate can check it.

**Rev 2 re-scope (Codex round 1: 5 HIGH).** Rev 1 bundled three changes:
- an oracle fix for nullable integers under deterministic Faker;
- positional Faker over every type;
- deterministic Faker over several types.

Codex showed that each needs more design than one slice can carry. The split is now:
- **C5c-i (this plan):** positional (non-deterministic REUSE) Faker over integer, unsigned integer, boolean and floating-point sources only.
- **C5c-a (next, own plan):** Cam's oracle fix. Deterministic Faker over integers with nulls must work. Per Codex, it is done at the Faker sampling boundary, with exact integer values supplied to the sampler. The shared frame, predicates and other readers are left alone.
- **C5c-ii (after C5c-a):** deterministic Faker over bool, integers and tz-aware timestamps. It includes an explicit design for the degenerate (all-null and empty) output types.
- **Later:** temporal sources (timestamp, date, time, duration), decimal, binary, nested, float16, the null type and dictionary sources.

## 1. Goal and scope

Positional Faker reads only the null mask of its source (`_strategies/_faker.py:97-110`); the values are ignored. It runs natively today only over string sources. This slice admits integer, unsigned integer, bool and float sources on both native routes, where the native null mask provably equals pandas' `isna` after the oracle's own conversion.

**In:** positional Faker over int8-int64, uint8-uint64, bool, float32 and float64.

**Out:**
- every other source family (listed above);
- deterministic Faker over any non-string source (C5c-a, C5c-ii);
- any change to the oracle or to unified admission's round-trip check.

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

**Added from Codex round 1:**
- **Unified admission round trip.** `_unified_slice_admission.py:375-399` declines any column whose pandas round trip is not type- and value-identical to the source. An int column with nulls becomes `double`, and a float column with real NaN values has them become nulls, so both fail. This slice does NOT change that check, so on the unified route those cases keep declining. Only null-free int and uint, bool and NaN-free float columns can reach the unified binder.
- **Registry types.** The operator registry stores exact datatype instances (`_operator_registry.py:76, 137`; `_unified_slice_resident_types.py:16-34`). The admitted families are therefore listed as explicit instances, not as a predicate over arbitrary types.
- **Temporal sentinels.** Arrow-valid `-2**63` in a timestamp or duration becomes pandas `NaT`, and `time64` can fail pandas conversion outright. That is why temporal families are out of scope.
- **Degenerate deterministic output.** All-null and empty deterministic Faker chunks produce `null` and `double` on the oracle against `string` natively, and the joiners reject `double` beside `string`. Positional Faker is unaffected, because its chunked output is pinned to `string` by config (`native/_chunked_schema_rule.py:38-44, 110-123, 257`). This is why deterministic Faker is out of scope.

## 3. Decisions

**3a. Admitted families.** Positional Faker over int8-int64, uint8-uint64, bool, float32 and float64 is admitted:
- on the chunked native route (`native/_dispatch.py:426-446`, a per-variant admitted-type check);
- on the unified route (`physical/_shadow_bindings.py` `positional_faker_bindable`, plus the registry entry).

Deterministic Faker keeps its string-only checks unchanged.

**3b. Null mask equals pandas `isna`.**
- The native positional step's null mask (`_operator_step.py:~173`, which uses `col.is_valid()` today) becomes `is_valid AND NOT is_nan` for float32 and float64. For int, uint and bool, `is_valid` already equals pandas `isna`, because pandas has no other null sentinel for these after conversion: an int with nulls becomes float NaN at exactly the null positions, and a bool with nulls becomes object None.
- One helper computes this mask, and the chunked and unified paths both call it.

**3c. Unified route.** The binder admits the families from 3a. The existing round-trip gate still decides whether a candidate reaches it, so int-with-nulls and NaN-bearing floats decline there. Test 5 pins that.

**3d. Logs.** No data values; the log sentry passes unchanged.

## 4. Acceptance tests (written first; never weakened)

Differential = lane-on against an explicit lane-off run, comparing:
- output tables byte-equal (schema and `b"pandas"` metadata);
- warnings and row errors;
- metrics minus timings and the activation leaf.

Admitted cases poison the oracle fallback, so a silent reroute fails the test.

1. **Chunked, every admitted family:**
   - int8-int64 and uint8-uint64 at their boundaries, with and without nulls;
   - bool with and without nulls;
   - float32 and float64 with nulls, real NaN values, `inf` and `-0.0`;
   - several chunks, nulls across chunk boundaries, an all-null chunk, an empty chunk.

   Output equals the chunked oracle and whole-frame (position-keyed draws).
2. **NaN is null:** a float source whose only "missing" values are Arrow-valid NaN gives null output at exactly those rows, matching the oracle.
3. **Unified, admitted cases:** null-free int and uint, bool with and without nulls (if the round trip passes; the builder pins which), and NaN-free float with nulls. Several batches. Activation asserted.
4. **Declines unchanged:**
   - deterministic Faker over every non-string family still declines on both routes;
   - positional Faker over timestamp, date32, date64, time64, duration, decimal128, decimal256, binary, a list, a struct, float16, the null type and dictionary still declines.

   Each case's outcome (output or error) equals lane-off.
5. **Unified round-trip declines:** positional Faker over int with nulls, and over float with real NaN values, declines on the unified route with the existing reason, and its output equals lane-off.
6. **Old decline tests:** the tests that pinned "non-string declines" (`tests/native/test_dispatch_faker.py`, `test_chunked_nondet_faker_admission.py`, `test_chunked_nondet_faker_auto_route.py`, `tests/physical/test_unified_slice_faker.py`, and the registry snapshot tests) change ONLY for the families admitted here, and only for the positional variant. The record lists every changed assertion and why.
7. **Testflight:** if any fingerprint moves, STOP and report.
8. **Sentries.** A perf record (positional Faker over int64, 1M rows, both routes). Mutation on the mask helper and the admission checks.

## 5. Failure modes

| Risk | Closed by |
|---|---|
| The native null mask differs from pandas | 3b rule; tests 1-2 cover NaN, inf and -0.0 |
| A non-admitted family slips through | Explicit type lists; test 4 |
| The unified route admits a column its round trip rejects | The gate is unchanged; test 5 |
| An old decline test is silently dropped | Test 6 rule |

Rollback: revert the merge commit.

## 6. Review log

- **Codex plan gate, round 1: REVISE** (5 HIGH). Rev 2 re-scopes rather than patching:
  - **HIGH 1 ("only failing jobs change" is false) and HIGH 2 (incomplete conversion sites):** the oracle fix moves to its own slice, C5c-a, designed at the sampling boundary as Codex proposed.
  - **HIGH 3 (unified round trip and registry):** the round-trip gate is kept and pinned (test 5); the admitted families are explicit registry instances.
  - **HIGH 4 (temporal NaT and conversion failures):** temporal families are out of scope.
  - **HIGH 5 (degenerate deterministic output):** deterministic Faker is out of scope until C5c-ii, which must design it explicitly.
