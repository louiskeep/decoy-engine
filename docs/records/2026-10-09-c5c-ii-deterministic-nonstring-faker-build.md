# C5c-ii deterministic Faker over non-string sources: build record

Status: record

Date: 2026-10-09. Plan: `docs/plans/2026-10-09-c5c-ii-deterministic-nonstring-faker.md` (rev 4, section 10 authoritative for the chunked route; dennis GO + Codex re-gate GO). Branch `feat/c5c-ii-deterministic-nonstring-faker` off engine main `69a7741f`. Built tests-first by an Opus build agent. Gates (dennis/Codex) are run by the orchestrator, not here.

## What shipped

The native routes (chunked dispatcher + unified full-frame) now admit DETERMINISTIC Faker (and the `allow_collisions: true` alias) over `bool`, signed-integer and unsigned-integer sources, output byte-identical to the pandas oracle within each route. No new Rust: the deterministic draw already keys through the compiled `derive_index_batch` kernel, which canonicalizes int/bool via `_canonicalize_source`; both the oracle (`PoolSampler._deterministic` -> `_derive_pool_indices`, feeding the kernel a `pa.Array.from_pandas`) and the native kernel reach that same kernel over the same physical Arrow type, so they agree on the draw key exactly when the pandas conversion is an identity on the source value. The whole slice is therefore ADMISSION + an output-type pin + tests.

Admitted families: `bool`, `int8..int64`, `uint8..uint64`. NOT float (`_canonicalize_source` hard-errors on float) and NOT temporal (Arrow `-2**63` -> pandas `NaT` sentinel hazard), both deferred.

### Change sites

| Area | Change |
|---|---|
| Closed metadata-shape classifier | `native/_faker_deterministic_admission.py` (new): `metadata_shape_admits` (the two allowlisted shapes), `classify_deterministic_nonstring_faker`, `is_effective_deterministic_faker[_column]`, `C5cIiAdmissionContext`. Strict JSON parse (rejects duplicate keys / NaN / non-UTF-8); checks BOTH `pandas_type` and `numpy_type` against the physical-type table. |
| Producer stream guarantee | `_chunked_input.py`: `_FixedSchemaChunks` + `fixed_schema_chunks_from_resident`; the resident and lazy (`rechunk`) branches of `open_input` now return the producer. `_pipeline_auto_chunk._run_dispatcher` feeds the resident producer. |
| Capture + context threading | `native/_chunked_entry._run_chunked` captures `chunks.source_schema` before `_oracle_preflight`, verifies it equals the first chunk metadata-inclusive, and threads a `C5cIiAdmissionContext` into `plan_native_route`. |
| Chunked dispatch branch | `native/_dispatch.py:426-452`: the deterministic-non-string rejection is replaced by the new faker branch, which calls the classifier and admits or declines with `faker_conversion_schema_not_guaranteed` / `faker_conversion_metadata_not_allowlisted`. |
| Unified route | `_operator_registry.py`: `DETERMINISTIC_FAKER_SOURCE_TYPES` + the faker spec's `deterministic_resident_types`. `_unified_slice_resident_types.deterministic_resident_types` + the `_unified_slice_admission` domain gate. `physical/_shadow_bindings.py`: the non-positional faker binding passes `extra_source_types=DETERMINISTIC_FAKER_SOURCE_TYPES` to `_faker_pool_bindable`. |
| Degenerate string pin (option A) | `_faker_degenerate_pin.py` (new): the pin-set predicate (config + source-family, `when:`/FK-child excluded) and the retype helper (cast + `b"pandas"` metadata rewrite). Wired into the two `_chunked_schema_rule` string-pin construction sites and the three `from_pandas` sites (`_pandas_adapter`, `_sequential`, `_unified_slice_evidence`). |
| Docs/census | CHANGELOG `[Unreleased]`, compatibility-contract ROUTE-OUTPUT-CONTRACT, module-size census bumps (`_chunked_entry` 615->632, `_pandas_adapter` 686->691, `_sequential` 652->665, `_unified_slice` new 607). |

### Admitted vs declined shapes (chunked route)

Admitted (native) only when BOTH the stream is a `_FixedSchemaChunks` whose first chunk matches its captured schema metadata-inclusive AND the schema metadata is one of:
1. metadata absent (schema + every field), no extension type, plain bool/int/uint target; or
2. plain pyarrow `b"pandas"` metadata, an identity column mapping (one entry per physical field, in order, `name == field_name`, `metadata: null`, `pandas_type`+`numpy_type` matching the physical-type table), one of the two accepted index layouts, optional `attributes == {}`.

Declined to the oracle (output unchanged): any ordinary iterable (no guarantee); nullable `Int64`/`UInt64`/`boolean`; `bool[pyarrow]`/int-Arrow dtypes; `Float64`; StringDtype; category; a `numpy_type`-only drift on a physical int64; mismatched width/signedness; unknown metadata keys; nonempty field metadata; stored/named/MultiIndex; malformed/duplicate-key JSON; float/temporal/decimal/... source families.

The unified route is independent: it admits by the `cheap_admission` value-identical round trip, so a pandas nullable `Int64`/`UInt64` ADMITS on unified but DECLINES on the conservative chunked allowlist. Intentional (section 10 clarification).

## Red-before-green evidence

- The C5c-i decline tests `test_4_deterministic_faker_over_a_non_string_source_*` (chunked line 259, unified line 156) failed after the admission flip: the chunked int/uint/bool cases reported `faker_conversion_schema_not_guaranteed` from an ordinary list instead of the old `faker_source_type_not_string`, and the unified int/uint/bool cases activated the lane (the poisoned oracle no longer ran). Both were then split: the ordinary-iterable / float cases still decline; the trusted-producer / value-identical cases admit.
- The metadata classifier unit tests (`tests/native/test_c5c_ii_metadata_allowlist.py`, 35 cases) were written before the classifier existed as a consumer and drove its shape.

## Judgment calls (for gate scrutiny)

1. Module homes: `_FixedSchemaChunks` lives in `_chunked_input.py` (with `rechunk`/`_slices`, the reconstruction primitives it owns) so there is no generic public certifier and no import cycle; recognition is by the concrete type (`isinstance`), never a duck-typed `.source_schema`. The degenerate-pin helper lives in `execution/` (not `native/`) because the full-frame oracle (`_pandas_adapter`, `_sequential`) consumes it.
2. Pin membership is deliberately independent of native admission / the stream guarantee (section 4.3): the full-frame oracle and unified routes pin an admitted deterministic-Faker column's degenerate output even when the chunked route would decline it, so option A holds everywhere and the flag-on/flag-off D9 parity passes.
3. Two pin-set entry points share one predicate: `deterministic_faker_pin_columns` (config, for the chunked schema rule + unified coordinator) and `deterministic_faker_pin_columns_from_plan` (plan seeds + relationship graph, for the full-frame adapter/sequential which work from the compiled plan). FK child key columns are excluded in both.
4. The provider allowlist is route-asymmetric and unchanged by this slice: the unified faker binding still requires the C1 allowlist (`person_first_name`, `person_last_name`) via `_faker_pool_bindable`; the chunked route relies on the shared `_resolve_admitted_pools` string-pool check (`faker_provider_output_not_string`) for M7, as it already did for deterministic Faker over string. Both test providers are allowlisted.
5. `_unified_slice.py` cast: the pre-existing `inputs.caller_sources.get(...) is not candidate.source` identity check narrows `candidate.source`'s static type to a union for the rest of the function, so the new `candidate.source.schema` read is `cast`-ed back to `pa.Table`.
6. OOC adapter (`physical/drivers/_chunked.py:121`) left unaccelerated by design (arbitrary iterable -> declines up-front), per the section 10 clarification.

## Test coverage

- `tests/native/test_c5c_ii_metadata_allowlist.py`: the closed allowlist (shapes, numpy_type-only decline, rejection matrix, boundary families).
- `tests/native/test_c5c_ii_chunked_deterministic.py`: admit + within-route parity (families x nulls, pandas metadata shape 2, integer boundaries >2**53 / >2**63 / uint64 max), same-data-different-provenance, degenerate string pin, metadata-drift declines.
- `tests/native/test_c5c_ii_producer_guarantee.py`: producer schema guarantee (metadata + field metadata inclusive, re-iterable, not duck-typed), two-chunk per-chunk metadata-drift declines before output with the native masker poisoned.
- `tests/physical/test_c5c_i_unified_positional.py`: unified admit (bool/int/uint + nullable Int64/UInt64), degenerate-pin D9 parity, float declines.
- Splits of the C5c-i chunked/unified deterministic-decline tests.

## Gate-scrutiny checklist

- Confirm the closed allowlist is complete and conservative (nothing outside it makes the oracle and the kernel disagree on a key); especially the `numpy_type`-only check and the index-layout shapes.
- Confirm the producer guarantee cannot be forged (concrete-type recognition) and that the capture-before-preflight + first-chunk metadata-inclusive match is the only path to `C5cIiAdmissionContext`.
- Confirm the degenerate pin fires only on genuinely empty/all-null admitted columns, never a `when:`-bearing or FK-override column, and that it does not depend on `native_admitted`.
- Confirm the unified-vs-chunked nullable-integer asymmetry is the intended, documented behavior.
