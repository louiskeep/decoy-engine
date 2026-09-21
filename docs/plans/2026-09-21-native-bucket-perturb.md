# PLAN — native `bucket_perturb` operator

Status: plan
Author: Opus (top-tier, per the guides/plans standing rule). Codex cross-reviews as plan-gate.
Roadmap: native operator breadth (post-Phase 5, S-port slate). Cam-approved 2026-09-21 (the measured
cheap-win slate: bucket_perturb -> group_key -> faker-full-frame). Base: engine main AFTER the categorical
PR (#171) merges (this branch rebases onto that main before build, to reuse categorical's seam plumbing
and avoid seam-file conflicts). FRAME survey: agent ae0609b3 (2026-09-21).

## Why (measured)
bucket_perturb is the single worst old-way masking cost: **~2.5h / 100M rows** (91 µs/row, single-thread),
dominated by an `.iloc` per-row loop that does per-row `derive()` + `strftime` (`transforms/bucket_perturb.py:164-171`).
It is row-local, static-typed, and **zero-diagnostic** (no quarantine wall) -- a clean native port that
reuses the existing byte-pinned `derive_index_batch` kernel (no new Rust). Byte-identical output is the
hard gate.

## FRAME — the goal
Put `bucket_perturb` on the native full-frame lane so a job masking a date column with it runs on the
streaming Arrow path (multithreaded, flat RSS) instead of the per-row pandas oracle, with output
BYTE-IDENTICAL to the current oracle.

### What bucket_perturb computes (parity target, `transforms/bucket_perturb.py`)
Per non-null, parseable row: parse the date (`pd.to_datetime(format=fmt)`), snap to a deterministic
position within its time bucket, strftime back. `_perturb_date` (`:94-120`):
`bucket_start, bucket_size = _bucket_start_and_size(date, bucket)`; `digest = derive(job_seed, namespace,
_canonicalize_source(value_str))`; `offset = int.from_bytes(digest[:8], "big") % bucket_size`; return
`bucket_start + timedelta(days=offset)`, then `strftime(fmt)`. Null and parse-failed rows are PASSED
THROUGH UNCHANGED (`:165-166`, a `continue`) -- no raise, no quarantine, no diagnostic.
- Buckets = `{"week","month","quarter"}` (`:47`). **Bucket size VARIES per row:** week=7 always; month=
  `calendar.monthrange` (28/29/30/31, leap Feb); quarter = 90/91/92 (leap Q1). This is the central design
  fact.
- The offset math `int.from_bytes(digest[:8],"big") % pool_size` is BYTE-IDENTICAL to `derive_index`
  (`determinism/_derive.py:278-304`). Canonicalizer is the SAME function the kernels use
  (`kernel/_canonicalize.py:9` re-exports `generation/pool/_canonicalize._canonicalize_source`) -- parity-
  safe for string sources (the handler canonicalizes `series.astype(str)`, always the string branch).

### v1 SCOPE (decline everything outside the clean parity envelope to the oracle)
- **STRING source only** (`pa.string()`). Non-string declines to the oracle. (Matches the chunked oracle
  route's own source gate `_chunked_bucket_perturb.py:74`; keeps `astype(str)` an identity so
  canonicalization matches byte-for-byte.)
- **Explicit `date_format` only.** A bucket_perturb node WITHOUT `date_format` (autodetect) declines to
  the oracle. Rationale: autodetect (`date_shift._detect_format`, a `head(200)` whole-column prepass,
  `date_shift.py:78`) is ORDER-DEPENDENT -- a chunk/subset can pick a different format than the full
  column -- so it is a parity hazard; the chunked oracle route already refuses it
  (`_chunked_bucket_perturb.py:77-90`). Native mirrors that. (Whole-column format-detect prepass on the
  native lane is a later slice.)
- **FULL-FRAME route only.** The chunked/OOC route DECLINES bucket_perturb to the oracle (add to
  `CHUNKED_ROUTE_VETOED_STRATEGIES`), same reason as categorical: the eager per-chunk emit cannot resolve
  the data-dependent output type (below).
- Always deterministic (keyed by `derive(job_seed=mask_key, namespace, canon(value))`); no non-det mode
  to gate.

### Non-goals (v1)
- No autodetect `date_format` on native (declines to oracle) -- deferred to a whole-column format-detect
  prepass slice.
- No chunked/OOC native bucket_perturb (declines) -- deferred (eager-emit data-dependent-type problem).
- No non-string sources (decline).

## Established methodology
Reuses the shipped native-operator pattern (the categorical plan
`docs/plans/2026-09-21-phase5-native-operator-expansion.md`, Track B): the same admission/binding/
coordinator seams, the same `derive_index_batch` kernel + shared canonicalizer, the same B0 output-typing
reconciliation. The keyed derivation is HKDF-SHA256 (`derive`) reduced by first-8-bytes-uint64-mod
(`derive_index`) -- established, KAT-pinned (`native/_index_ext.py:65-71`).

## Design

### The native kernel `native_bucket_perturb` (new `native/_bucket_perturb_ext.py`)
Reuses `derive_index_batch` -- NO new Rust. Given a `pa.string()` array + resolved `bucket` + resolved
`date_format` + mask_key + namespace:
1. **Parse** (vectorized): `pc`/pandas `to_datetime(values, format=date_format, errors="coerce")`. Build
   the null mask (source null) + parse-fail mask (coerced NaT & not source-null). These rows pass through
   UNCHANGED (see step 6).
2. **Bucket start + size per row** (vectorized): from the parsed dates, compute `bucket_start` and
   `bucket_size` reproducing `_bucket_start_and_size` (`:53-91`) EXACTLY -- week (Monday snap, size 7),
   month (first-of-month, `monthrange` size), quarter (`_QUARTER_START_MONTH`, day-count size). Leap years
   included.
3. **Offset via group-by-bucket-size + `derive_index_batch`** (reuses the kernel; pool_size is scalar, so
   group the parseable rows by their distinct `bucket_size` -- week: 1 group (7); month: <=4 groups
   (28/29/30/31); quarter: <=3 groups (90/91/92) -- call `derive_index_batch(group_values, mask_key,
   namespace, pool_size=size)` per distinct size, and order-preservingly SCATTER each group's uint64
   offsets back to row positions). The kernel canonicalizes internally (shared canonicalizer) -- byte-
   parity to `_perturb_date`'s `int.from_bytes(digest[:8],"big") % bucket_size`.
4. **Perturbed date** (vectorized): `perturbed = bucket_start + offset` days.
5. **Format** (vectorized): `strftime(date_format)` reproducing the oracle's `perturbed.strftime(fmt)`
   exactly (same format, same pandas/Python strftime semantics).
6. **Pass-through** null + parse-fail rows UNCHANGED (the ORIGINAL string value, not re-null/re-format) --
   `_canonicalize`-free; matches `:165-166`.
Output type is resolved at final assembly (below), not forced.

### Output typing (B0-style, reuse categorical's solution)
Data-dependent, exactly like categorical: empty output -> `pa.float64()` (the tokenizing default,
`_shadow_coordinator.py:190`); all-null / all-parse-fail-with-null -> `pa.null()`; populated -> `pa.string()`.
A B0 spike MEASURES the oracle's end-to-end field type for {empty, all-null, all-parse-fail, mixed,
populated} x {week, month, quarter} through the real unified-slice output path; native reproduces it via
`_TOKENIZING_STRATEGIES` (per-batch `pa.string()` then whole-column final normalization at
`_shadow_coordinator.py:172,190`); byte-parity (value AND field type) asserted at the `ExecutionResult`
boundary. Do NOT assume a rule -- match B0. (A fully-unparseable string column passes originals through
-> stays `pa.string()`; confirm in B0.)

### The 9-seam checklist (bucket_perturb is absent from every native seam; mirror categorical)
1. `native/_requirements.py`: add `bucket_perturb` to `NATIVE_KERNEL_STRATEGIES` (`:126`) + new
   `bucket_perturb_config_rejection` (bucket in {week,month,quarter}; namespace present; STRING source;
   `date_format` PRESENT -- reject autodetect to the oracle).
2. `native/_plan.py` eligibility (`:283-317`): add a `bucket_perturb` branch calling the rejection fn;
   confirm it does not ride any other operator's exception.
3. `native/_dispatch.py`: add `bucket_perturb` to `CHUNKED_ROUTE_VETOED_STRATEGIES` (`_requirements.py:136`)
   + the explicit chunked veto (`_static_route_decision`, `_dispatch.py:221`) -> declines to oracle; a
   positive oracle-route test.
4. `native/_chunk_masking.py`: NO chunked branch in v1 (declined at #3; deferred slice).
5. `physical/_plan.py` `ExecutionBinding`: add `bucket_perturb_bucket`, `bucket_perturb_date_format`,
   resolved output schema fields.
5b. `physical/_shadow_bindings.py`: `SLICE_STRATEGIES` + `OPERATOR_ID_BY_STRATEGY` (`bucket_perturb` ->
   `native_bucket_perturb`) + a binding branch populating the new fields.
6. `physical/_shadow_operators.py`: `run_operator` branch + `_BUCKET_PERTURB` const -> calls
   `native_bucket_perturb`.
7. `physical/_shadow_coordinator.py`: (a) index-kernel loading for bucket_perturb bindings (`needs_index`,
   `:355-357`); (b) add to `_TOKENIZING_STRATEGIES`; (c) final-assembly output-type resolution (B0).
8. `execution/_unified_slice_admission.py`: `ALLOWED_OPERATOR_IDS` (`:79`) + `_ADMITTED_RESIDENT_TYPES`
   (`:101`) -> `bucket_perturb: frozenset({pa.string()})`.
9. `native/_capabilities.py:195`: ALREADY PRESENT (`_Ortho(True,False,False,False,True)`, zero-diagnostic)
   -- VERIFY only, no edit.

## Acceptance tests (byte-parity is the merge gate; defined now)
1. **Byte-identity (HARD), value AND Arrow field type, at both the coordinator and the unified-slice
   `ExecutionResult` boundary**, native == pandas oracle, across:
   - buckets {week, month, quarter};
   - **leap-year edges**: Feb 2024 (29), Feb 2023 (28), Q1 2024 (91) vs Q1 2023 (90), month/quarter
     boundary dates (1st, last day);
   - nulls, parse-failures (unparseable strings pass through UNCHANGED), mixed null+parse-fail+valid;
   - duplicate + unicode source values; single-row; empty; all-null; all-parse-fail;
   - the full-column output-type per B0 (empty/all-null/populated).
2. **Group-by-size scatter correctness**: a month column spanning 28/29/30/31-day months, and a quarter
   column spanning 90/91/92, produce offsets identical to per-row `derive_index(pool_size=bucket_size)` --
   assert the scatter preserves row order + null positions.
3. **`derive_index_batch(pool_size=size) == _perturb_date` offset** differential over a value corpus per
   bucket size (byte-parity of the keyed offset, incl. the shared canonicalizer).
4. **Admission declines (positive assertions)**: a bucket_perturb column WITHOUT `date_format` declines to
   the oracle; a NON-STRING source declines; the CHUNKED route declines; full-frame WITH explicit
   date_format + string source EXECUTES native (route evidence).
5. **KAT**: a bucket_perturb KAT vector (fixed config+seed corpus per bucket) guarding drift.
6. **strftime round-trip**: several `date_format`s (`%Y-%m-%d`, `%m/%d/%Y`, `%Y%m%d`, ...) produce
   byte-identical formatted output.

## Build order (single PR, categorical-style)
B0 oracle-typing spike -> `ExecutionBinding` fields -> `bucket_perturb_config_rejection` + admission
(string+explicit-format+full-frame gates) -> the `native_bucket_perturb` kernel (parse + bucket math +
group-by-size derive_index_batch + scatter + strftime + pass-through) -> the 9-seam registration ->
byte-parity + group-scatter + KAT + decline + strftime tests -> dennis -> Codex FINAL -> CI (incl. the
companion-ABSENT substrate(pandas) leg: guard native-path tests with `@skipif(not companion.ok)`, per the
categorical CI lesson) -> merge. No GCP needed (byte-parity is local; a GCP throughput reconfirm is
optional follow-up).

## Risks + rollback (from the survey's risk register)
- **Variable bucket size (highest)**: month/quarter sizes are per-row (leap years). The group-by-size +
  per-size `derive_index_batch` must reproduce `_bucket_start_and_size` exactly. Mitigated by test #2 +
  leap-edge cases in #1.
- **Canonicalization**: safe ONLY for string sources (`astype(str)` identity); the string-source gate
  enforces it. A non-string source diverges by chunk -> declined (gate #8 + rejection #1).
- **Data-dependent output type**: the categorical B0 problem; same solution + `ExecutionResult`-boundary
  assertion.
- **format-detect order-dependence**: sidestepped by admitting explicit-`date_format` only (autodetect
  declines).
- **Rollback**: additive behind admission; any un-admitted shape declines to the proven pandas oracle
  (fail-closed). No frozen-surface paths touched.

## Gates
FRAME (survey done) -> PLAN (this) -> Codex plan-gate -> build (after #171 merges; rebase onto that main)
-> dennis -> Codex FINAL -> CI -> merge -> DOCUMENT (roadmap native-breadth entry + shipped-log). Then
group_key, then faker-full-frame (the rest of the S-slate).
