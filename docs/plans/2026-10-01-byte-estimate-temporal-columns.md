# Byte-estimate routing: price columns by Arrow type, not "not numpy means string"

Status: plan

Date: 2026-10-01. Branch `fix/byte-estimate-temporal-columns`, off engine main `0bb196fa`. Evidence probes (read-only, session scratchpad): `b2r4/b2r4_tb2.py` + `.out` (original repro), `bet/cat_probe.py` + `cat_probe.out` / `cat_probe_nonull.out` (67-type catalogue), `bet/resident_probe.out` (pandas resident cost per type), `bet/pin_probe.out` (current values pinned below), `bet/misc_probe.out` (pyarrow kernel support).

## Problem

With the default `use_byte_estimate_routing=True`, `run_pipeline` raises `AttributeError: 'datetime.date' object has no attribute 'encode'` for a mask table with a date column once the routing signals run. By default that is any table at or above `AUTO_CHUNK_THRESHOLD_DEFAULT` (100,000 rows), so real production jobs with dates fail. `use_byte_estimate_routing=False` runs the same jobs fine.

Trace: `execution/_pipeline.py:441` -> `_pipeline_routing_signals.py:553` (`routing_signals`) -> `_transforms_admission.py:312` (`admission_signals`) -> `_pipeline_routing_signals.py:338` (`byte_estimate_full_frame_fits`) -> `_mem_estimate_schema.table_size_spec_from_profile` -> `sample_average_string_bytes` (`len(v.as_py().encode("utf-8"))`).

Root cause. `table_size_spec_from_profile` decides a column's width class from `ColumnProfile.dtype`, which is the pandas dtype label (`canonical_dtype_label`). It treats every label not in `_FIXED_WIDTH_DTYPE_BYTES` as a string and samples it. pandas reports many non-string Arrow types as `object` or as a label the table does not list, so they reach the string sampler or the `ColumnSizeSpec` "unrecognized dtype" `ValueError`. The defect is the binary split "numpy fixed label, else string". The 2026-09 nullable-extension fix (`boolean`/`Int64`/`Float64` added to the table) patched one instance of the same class by adding labels; adding more labels would only move the crash to the next type.

The catalogue probe shows the scope is wider than temporal types. Through `run_pipeline` with byte-estimate on, 27 catalogue types crash today (all run with byte-estimate off):

| Profile label | Arrow types | Failure |
|---|---|---|
| `object` | date32, date64, time32[s], time32[ms], time64[us] | `AttributeError` in the sampler |
| `datetime64[ns, <tz>]` | timestamp s/ms/us/ns with tz | sampler (resident) or `ValueError` unrecognized dtype (lazy) |
| `timedelta64[s/ms/us]` | duration s/ms/us | same |
| `float16` | halffloat | same |
| `object` | decimal32/64/128/256, binary, large_binary, binary_view, fixed_size_binary, uuid (ext), bool with nulls | `AttributeError` in the sampler |
| `object` | string_view with nulls | `ArrowNotImplementedError` (`drop_null` has no view kernel) |
| `category` | dictionary of string / large_string / binary | `ValueError` unrecognized dtype |

Naive timestamps, `duration[ns]`, ints, floats, bool without nulls, string, large_string, json (ext), bool8, opaque, null type and dictionary of int work today. time64[ns] and the nested types are refused earlier by the profiler (separate issue, see Known issues).

The prepared-table adapter `table_size_spec_from_table` already reads Arrow types (`_arrow_size_label`) and does not crash, but it prices on Arrow storage width, which undercounts the resident form the estimator models (below).

## Guarantee and non-goals

Guarantee: with default knobs, the byte estimate never raises for a column whose type the profiler accepts. Every column is either priced from a fixed per-cell cost, priced from a sampled byte length (string/binary family only), or marked UNPRICEABLE (routes bounded, the existing §3.5 rule). String-column estimates and every route decision that worked before stay identical, except the intended changes listed under Design.

Non-goals: the profiler refusals (time64[ns] with sub-microsecond values, nested types), the advisory disk preflight (`out_of_core/_spill_estimate.py`), generate-table pricing, any change to the K constants or the route rules.

## Design

### What the estimator prices

The estimator's basis is the resident pandas form a full_frame run materializes: a string cell costs its payload plus a 57-byte pointer and `str` header (`_STR_OBJECT_OVERHEAD_BYTES`). Arrow storage width is the wrong number for types pandas holds as Python objects. Measured with `to_pandas()` defaults (pyarrow 24, CPython 3.10, `resident_probe.out`):

| Arrow type | Arrow bytes/row | pandas dtype | Resident bytes/row |
|---|---|---|---|
| date32 / date64 | 4 / 8 | object (`datetime.date`) | 40 |
| time32 / time64 | 4 / 8 | object (`datetime.time`) | 48 |
| decimal128 / decimal256 | 16 / 32 | object (`Decimal`) | 112 |
| timestamp (any unit, tz or naive) | 8 | datetime64 | 8 |
| duration (any unit) | 8 | timedelta64 | 8 |
| int8 with nulls | 1 | float64 | 8 |

So "price as fixed width" here means a fixed per-cell cost read off the Arrow type with no sampling, and that cost is the resident cost: numpy itemsize for types pandas holds natively, pointer plus a measured Python object size for types it holds as objects. Pricing date32 at 4 bytes would undercount the full_frame peak by 10x, the unsafe direction. The Arrow storage widths from the bug report (date32 4, date64 8, time32 4, time64 8, timestamp 8, duration 8, decimal by byte width) stay recorded in the classifier table for reference and for the disk-width follow-up.

### One classifier, keyed on the Arrow type

New module `execution/_mem_estimate_arrow.py` (keeps `_mem_estimate_schema.py` and `_mem_estimate.py` under the 600-LOC cap) with one function:

`classify_column(arrow_type: pa.DataType, *, has_nulls: bool) -> ArrowSizeClass`

`ArrowSizeClass` is one of: `Fixed(label)` (a label in `_FIXED_WIDTH_DTYPE_BYTES`), `Declared(width_bytes)` (variable-width `object` with an exact known payload, no sampling), `Sampled(decoded_type)` (string or binary family, sampled), `Unpriceable(reason)`. Rules, applied after unwrapping extension types to their storage type, dictionary to its value type and run_end_encoded to its value type:

- int/uint: itemsize label (`int8`..`uint64`); with nulls `float64` (pandas widens to float64; this is today's profile label, so the estimate is unchanged).
- bool: `bool` (1); with nulls a new label `pyobject[bool]` = 8 (object array of the `True`/`False`/`None` singletons, pointer only).
- float16/32/64: new label `float16` = 2, `float32`, `float64`.
- timestamp, any unit, with or without tz: `datetime64[ns]` (8). duration, any unit: `timedelta64[ns]` (8).
- date32, date64: new label `pyobject[date]` = 40. time32, time64: new label `pyobject[time]` = 48. decimal32/64/128/256: new label `pyobject[decimal]` = 112. Each is `8 + sys.getsizeof(<value>)`, measured; `Decimal` measured 104 at 7, 18, 38 and 76 digits.
- string, large_string, string_view: `Sampled`. binary, large_binary, binary_view: `Sampled` (a `bytes` cell costs payload + 41, so pricing it as a string at payload + 57 over-prices by 16 bytes, the safe direction).
- fixed_size_binary[n] (and uuid through its `fixed_size_binary(16)` storage): `Declared(n)`. The payload is exact from the type; same 16-byte safe over-price.
- dictionary: classify the value type. A string dictionary samples its decoded values. This over-prices a resident `category` column, which is the safe direction, and the masked output is decoded anyway.
- null type: `Declared(0.0)` (today's value: profile `object`, sampled width 0.0).
- list, large_list, list_view, large_list_view, fixed_size_list (incl. fixed_shape_tensor storage), struct, map, dense/sparse union, month_day_nano_interval, and any type not named above: `Unpriceable("<arrow type> has no resident-size model")`. That is an explicit, documented estimate (route bounded), never a crash and never a guess.

New labels go into `_FIXED_WIDTH_DTYPE_BYTES` with a comment giving the measurement. `_VARIABLE_WIDTH_DTYPES` is unchanged.

### Sampler

`sample_average_string_bytes` becomes vectorized and type-checked: cast view types to `large_string`/`large_binary`, decode dictionary and run-end-encoded arrays, then `pc.sum(pc.binary_length(arr)) / (len - null_count)`. `binary_length` is the UTF-8 byte length for string arrays (parity checked on non-ASCII input in `misc_probe.out`), so string values are unchanged. Any other type raises `TypeError` naming the Arrow type, so a future misroute fails loudly with the type in the message instead of an `AttributeError` on one value. This also removes the per-value Python loop, which today runs over every resident row.

### Where the Arrow type comes from

- Resident table: the sample column's own type, `has_nulls` from its `null_count`. The resident type wins over the profile label when they disagree (it is what actually gets materialized). This also removes the documented workaround at `tests/physical/test_unified_slice_input_formats.py` ~625.
- Lazy table (`LazySource`): the Parquet footer schema (`LazySource.schema()`, metadata only, no row read), `has_nulls` from `ColumnProfile.null_count`. `Sampled` columns stay UNPRICEABLE, as today.
- No Arrow type at all (table absent from `caller_sources`): keep the profile label only when it is in `_FIXED_WIDTH_DTYPE_BYTES`, else UNPRICEABLE. Never sample without a type, never raise on an unknown label.

### Callers (survey; all change together)

| Caller | Today | Change |
|---|---|---|
| `_pipeline_routing_signals.byte_estimate_full_frame_fits` (~305-315) | profile adapter + resident sample | pass Arrow types (resident or footer) |
| `_pipeline_routing_signals.resolve_probe_recovery` (~430-447) | profile adapter for the `raw_data_bytes` skip gate; resident only | same adapter, resident types |
| `_transforms_admission._specs` (~141-153) | profile adapter for unprepared tables, `table_size_spec_from_table` for prepared | both go through `classify_column` |
| `_pipeline_routing_signals._resident_column_arrays` (~240) | `{}` for lazy | add a sibling `_column_arrow_types` returning footer types for lazy tables |
| `_mem_estimate_schema.table_size_spec_from_table` | `_arrow_size_label`, storage width | replaced by `classify_column`; `_arrow_size_label` deleted |
| `_mem_estimate_schema.table_size_spec_from_generate_table` | config-only, no sampler; not wired into routing (comment at `_pipeline_routing_signals.py` ~289) | none (no sampler, no profile label); see Known issues |
| `out_of_core/_spill_estimate._source_disk_width_bytes` | `is_fixed_width_dtype` on profile labels; date/tz/duration fall to `max_length` or the free-text ceiling | none in this fix (advisory, over-estimates, cannot crash); follow-up |

`table_size_spec_from_profile` keeps its keyword signature (`declared_widths`, `sample`) and gains `arrow_types: Mapping[str, pa.DataType] | None = None`, so the spies in `tests/unit/execution/test_pipeline_routing_route_kills.py` keep working. `declared_widths` still wins over everything.

### Intended estimate changes (the only ones)

1. Types that crashed now get an estimate (the 27 types above). No previous value to preserve.
2. Lazy tables: date, time, decimal, bool-with-nulls and fixed_size_binary columns that were UNPRICEABLE (profile label `object`, no sample) become priced from the footer type. Lazy string and binary columns stay UNPRICEABLE. A lazy table whose only unpriceable columns were these can now route by byte estimate instead of always bounded. This is a routing change for a path that worked; it is the correct result of the root-cause fix, and AT6 pins it. If Cam wants zero lazy-route movement, the fallback is to keep `Fixed` results on lazy tables UNPRICEABLE for object-resident labels only; that keeps two code paths and is not recommended.
3. Prepared tables (`table_size_spec_from_table`): date32 4 -> 40, date64 8 -> 40, time 4/8 -> 48, int-with-nulls storage width -> 8, bool-with-nulls 1 -> 8; binary, decimal, dictionary, view and extension columns go from UNPRICEABLE to priced. All increases or unpriceable-to-priced; none lowers an estimate. For the pin table below, prepared `raw_data_bytes` moves 27,790 -> 29,190, equal to the profile adapter's figure.

## Acceptance tests (written first, red before the fix)

New file `tests/unit/execution/test_mem_estimate_arrow_types.py` plus one end-to-end file `tests/integration/test_byte_estimate_arrow_types_e2e.py`. Each test records its red-before failure in the commit message of the test-first commit.

1. Temporal end-to-end, default knobs. For each of date32, date64, time32[s], time32[ms], time64[us], timestamp[s/ms/us/ns, tz="+05:30"], timestamp[us, tz="UTC"], duration[s/ms/us]: a single mask table of 100,000 rows (the default threshold) with a hashed string column and the temporal column as passthrough, `run_pipeline` with no routing keyword arguments completes, and its output equals the `use_byte_estimate_routing=False` run byte for byte. Red before: `AttributeError` (dates, times, tz) or `ValueError` (duration non-ns).
2. Temporal end-to-end where the signal is read: the `_fk_pure_mask_config` shape from `test_byte_estimate_routing.py` with a date32 and a tz timestamp column added to the parent, explicit `out_of_core_budget_bytes=1 GB`. Completes with `route_reason == "byte_estimate_full_frame_fits"`. Red before: `AttributeError`.
3. Catalogue, unit: for every entry in `tests/native/_rev9_type_catalogue.py`, with and without a null, `classify_column` returns the class pinned in a table in the test (one row per type, matching the Design rules) and building the `ColumnSizeSpec` does not raise. The test fails if the catalogue gains a type the table does not list, so a new pyarrow type cannot slip through unclassified.
4. Catalogue, end-to-end: for every catalogue entry the profiler accepts, `run_pipeline` with the column as passthrough and `auto_chunk_threshold_rows=10` completes with byte-estimate on and matches the byte-estimate-off output. Parquet-writable types go through a file source; the rest (month_day_nano_interval, dictionary of string_view, run-end-encoded) through a resident source only. Red before: the 27 types listed in Problem.
5. String estimates unchanged (pins from `pin_probe.out`, current main): `sample_average_string_bytes` gives 2.2 for the cycle `["a","bb","ccc","a","dddd"]`, 3.75 for `["héllo","日本",None,"x","yz"]`, 4.0 and 0.0 for the two existing `test_mem_estimate.py` cases, 6.0 for the catalogue json column with a null, 0.0 for the null-type column, and the same values for the large_string and string_view forms. The 200-row pin table (`s`, `u`, `i8`, `i8n`, `f`, `ts`, `b`) prices at `raw_data_bytes == 29190` through the profile adapter, with per-column labels `object, object, int8, float64, float64, datetime64[ns], bool`.
6. Lazy path: `byte_estimate_full_frame_fits` on a `LazySource` table with a tz timestamp, a duration[s] and a dictionary-of-string column does not raise (red before: `ValueError` unrecognized dtype). A lazy string column is still UNPRICEABLE (the existing `test_lazy_table_with_variable_width_column_is_unpriceable` passes unchanged). A lazy table of an int64 and a date32 column now returns a priced fit (intended change 2).
7. Prepared path: `table_size_spec_from_table` on the pin table gives 29,190 (was 27,790) and date32 prices as `pyobject[date]` (intended change 3).
8. Sampler guard: `sample_average_string_bytes` on an int64, date32 or decimal128 array raises `TypeError` whose message names the Arrow type.
9. Routing unchanged: `test_byte_estimate_routing.py`, `test_probe_routing.py`, `test_pipeline_routing*.py`, `test_mem_estimate.py`, `test_mem_estimate_kills.py`, `test_auto_chunk_routing.py`, `test_composite_routing.py` and `test_out_of_core_routing.py` pass unmodified, including the pinned end-to-end decisions (`full_frame` / `byte_estimate_full_frame_fits` under a 1 GB budget; `sequential` / `pure_mask_fk` with the flag off; the tight-budget bounded route; the width-flip test).
10. `tests/physical/test_unified_slice_input_formats.py` ~633: the mismatched-resident-type case runs with the default `use_byte_estimate_routing=True` and passes; the workaround comment is removed. Red before: the sampler error on the int64 resident column.

## Known issues (not fixed here)

- time64[ns] with non-microsecond values is refused by the profiler (`profile/_readers.py:175`, `ArrowInvalid: Value 7 has non-zero nanoseconds`) under both flag values. Separate bug, separate plan.
- Nested types (list family, struct, map, union) and fixed_shape_tensor are refused by the pandas profile walk before routing. This fix classifies them UNPRICEABLE so they are safe if the profiler ever admits them.
- The routing signals are computed for every job above the threshold, but only relationship-bearing pure-mask jobs under `auto` read them (`admission_signals` docstring). An estimator defect therefore kills jobs that never use the estimate. Making the signals lazy is a worthwhile follow-up, but it would also hide estimator defects from single-table tests, so it does not replace this fix.
- `_spill_estimate._source_disk_width_bytes` prices date, time, tz and non-ns duration columns at `max_length` or the free-text ceiling. Advisory only (warns, never rejects) and over-estimates; a follow-up can reuse `classify_column` with the Arrow storage widths recorded in it.
- Generate tables: `table_size_spec_from_generate_table` is not wired into routing yet. When it is, typed generators such as `windowed_date` should price through the same classifier on their declared output Arrow type instead of staying UNPRICEABLE.
- The `pyobject[*]` costs are CPython object sizes measured on 3.10 x86-64. The test for each label computes `8 + sys.getsizeof(value)` at test time and asserts the table value is not below it, so an interpreter change that grows an object fails the suite instead of silently under-pricing.

## Gates

Plan: Codex plan-gate (top-tier authored). Build: Sonnet builder, tests first; ruff check, ruff format --check, mypy on changed files. Before merge: coverage and mutation on the changed units (`_mem_estimate_arrow.py`, the two adapters, the sampler, `_column_arrow_types`) per the pre-merge testing rule; the classifier is a free function so mutmut grades it. Review: dennis, then Codex final. Test runs go through `~/bin/pytest-one`. No GCP run needed: the change is estimator input classification, and the route rules and K constants are untouched.
