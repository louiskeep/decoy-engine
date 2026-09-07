# P4 slice 5: `bucket_perturb` (explicit date_format) on the chunked route

Status: plan

> Part 2 Phase 4, chunked-route strategy port, slice 5. Follows slices 1-4
> (windowed_date, group_key, text_mask, code_set), all built, double-gated,
> mutation-ledgered, and held on `feat/native-phase3`. Design doc:
> `docs/plans/2026-08-31-part2-phase4-plan.md`. Ledger: auto-memory
> `decoy-engine-efficiency-plan.md`. Phase 4 merges once at the end after all
> testing; this slice is built and held, not merged. The template is the code_set
> slice (`docs/plans/2026-09-01-p4-slice4-code-set-chunked.md`); this one is
> simpler (no corpus, no evidence).

## Goal

Admit `bucket_perturb` with an explicit `date_format` to the memory-bounded
chunked route, so a job coarsening a date column no longer forces the whole
table resident on full-frame. Output must be byte-identical to the pandas
oracle for the admitted config shape, or the column is not admitted and falls
through to full-frame. The out-of-core route already admits `bucket_perturb`
for the same shape (`out_of_core/_compat.py:344`, requires an explicit
`date_format`); this chunked slice is a **stricter subset** of that admission.

## Why explicit-`date_format` is chunk-safe, and the one thing that is not

`apply_bucket_perturb` (`transforms/bucket_perturb.py:123`) parses each date
string with the resolved format, snaps it to a deterministic position within its
time bucket via `_perturb_date` = `derive(job_seed, namespace,
_canonicalize_source(value_str))`, and reformats with `strftime(fmt)`. With the
format fixed, this is a pure function of `(value, job_seed, namespace, bucket,
date_format)`: no `row_index`, no whole-column reduction, and null / unparseable
values are passed through per value. The output is always a string (the
reformatted date, or the original string on passthrough). So per-chunk masking
reproduces whole-column masking value-for-value, the chunk invariant.

The one input that is NOT chunk-safe is **autodetect**: when `date_format` is
absent, `fmt = date_format or _detect_format(series)`
(`transforms/bucket_perturb.py:150`) detects the format from the WHOLE series, a
cross-row reduction whose result can differ between a chunk and the full column.
So admission requires an explicit `date_format`, exactly as the OOC route does.

Two more properties, confirmed from the handler (`_strategies/_bucket_perturb.py`):

- **namespace is required** and the handler raises `bucket_perturb_requires_
  namespace` when `plan.namespace is None` (`:47`). The chunked route inherits
  that identical fail-closed raise, so a namespace-less config errors the same on
  both routes; no special admission handling is needed. The namespace is a
  per-column constant, so it does not vary across chunks.
- **No evidence / warnings.** `run` returns `(result, [])` (`:80`), so there is
  no per-column provenance to aggregate (unlike code_set's `code_set_corpora`).

## Scope: explicit date_format, chunk-stable string source, non-FK-key columns

Admit `bucket_perturb` only for the shape whose chunked output provably equals
the whole-column output:

- **explicit `date_format` only.** Autodetect (`_detect_format`) is a
  whole-column cross-row reduction. Reject a `bucket_perturb` column with no
  `date_format`.
- **chunk-stable string source (a deliberately-proven subset, not the exact
  safe boundary).** `apply_bucket_perturb` operates on date STRINGS
  (`series.astype(str)`, `pd.to_datetime(series, format=fmt)`). An integer source
  is the clear unsafe case: an Arrow `int64` chunk with nulls renders to pandas
  `float64`, turning `"20240101"` into `"20240101.0"`, and that promotion is
  chunk-boundary-dependent (the text_mask slice-3 hole). Arrow `date32`/`timestamp`
  sources may in fact convert chunk-stably, so string/large_string is admitted as
  the DELIBERATELY-PROVEN subset, not claimed to be the exact necessary boundary;
  admitting date/timestamp sources is a possible later widening once proven.
  Require a chunk-stable string / large_string source; reject otherwise.
- **non-FK-key columns, either orientation.** `bucket_perturb` is admitted as a
  CONDITIONAL strategy, NOT added to the flat `CHUNK_SAFE_STRATEGIES` (it is only
  safe with an explicit `date_format`). So `gate_fk_child_edges` rejects a
  `bucket_perturb` FK key edge (it inspects only flat `CHUNK_SAFE_STRATEGIES`),
  and a new `reject_bucket_perturb_fk_keys` gate rejects the chunked table when
  it participates as PARENT or CHILD in any FK edge with a `bucket_perturb` key
  column (the code_set precedent: the child-edge gate alone misses the
  parent-side case). FK self-masking for `bucket_perturb` is a deliberate
  follow-up.

## Admission mechanism (two layers, mirroring slices 3-4)

- **Config-shape (conditional predicate).** Add `bucket_perturb` to
  `CHUNK_CONDITIONAL_STRATEGIES` (`_chunked.py:164`, now `{faker, categorical,
  code_set}`) and a free function `bucket_perturb_conditional_failures(col_entry)`
  returning the `date_format`-absent failure reason (and any bucket-validity
  reason worth surfacing early), wired into `_conditional_admission_failures`.
  Mirror the OOC reason codes where practical, but do NOT promise identical
  codes (chunked is a stricter subset; see below).
- **Runtime source-dtype gate (schema layer, the text_mask/code_set trio).** A
  work-list-derived `bucket_perturb` column collector, a first-and-every-chunk
  schema gate for the manual `run_mask_pipeline_chunked` entry, and a
  whole-source schema rejection in `_planner._runtime_source_rejections` for the
  auto route. Mirror `code_set_source_columns` /
  `reject_unsafe_code_set_chunk_schema` / `unsafe_code_set_source_columns`.
- **`when:` gate.** Add `reject_bucket_perturb_when` (fail-closed), wired beside
  the sibling gates, unless the plan-gate proves `when:` is chunk-safe for a pure
  per-value strategy (a `when:` predicate can carry a pandas-eval whole-column
  reduction whose per-chunk evaluation selects different rows, the same hazard
  the other slices reject).

## Relation to the OOC route (stricter subset, not identical)

The OOC route gates only the explicit-`date_format` shape and admits non-string
Arrow sources (it reads values differently). This chunked slice additionally
requires a string source and rejects `when:` and FK-key participation. Parity is
asserted only on the shape BOTH routes admit; reason codes are route-specific and
not promised equal.

## Hazards to clear at the plan-gate (each prior slice hid one)

1. **Autodetect rejection** is fail-closed (no `date_format` -> not admitted).
2. **Source-dtype chunk-drift.** String-source gate at the schema layer (manual
   per-chunk + auto-route). Confirm the `pd.to_datetime(series, format=fmt,
   errors="coerce")` + `series.astype(str)` path is per-value chunk-stable for a
   string source, and that no whole-column datetime inference sneaks in with an
   explicit format.
3. **Output type + empty chunk (differs from code_set).** For a non-empty string
   source every emitted value is a string (strftime) or a passthrough source
   string, so the chunk is Arrow `string`. An EMPTY or all-null string chunk is
   pandas `object` -> Arrow `null` (NOT the code_set `double` hazard: code_set's
   empty `out=[]` inferred `float64`). `concat_masked_chunks` already promotes a
   `null` chunk losslessly to the non-null chunk's string type, and the pandas
   oracle emits `null` for an empty/all-null column too. So this slice does NOT
   force per-chunk string normalization (that would change the empty-table oracle
   output and break standalone-empty parity). The contract is: rely on the
   existing null-promotion; assert the CONCATENATED chunked output equals the
   oracle, not that each chunk is string-typed. Task 3b verifies the empty chunk
   is `null` (promotable), not `double`; it only adds normalization if that
   assumption is wrong.
4. **namespace-required** raises identically on both routes (no divergence); the
   plan-gate confirms the chunked route does not swallow or relocate that raise.
5. **Format-error passthrough.** Unparseable values are passed through unchanged
   PER VALUE (no cross-row quarantine), so this is chunk-safe; confirm the
   passthrough value is byte-identical to the oracle (the original source string).
6. **FK-key exclusion, both orientations** (code_set precedent).
7. **`when:` reject** required.
8. **Registry load-bearing.** The source-dtype collector calls
   `build_work_list(plan, registry)`; add a `bucket_perturb` + provider-backed
   `faker` sibling test that reddens under `registry=None` (the cross-slice
   registry lesson, so this ledger never repeats the equivalent mis-adjudication).

## Tasks

- [ ] **Task 1: Config-shape conditional admission.** `bucket_perturb` into
  `CHUNK_CONDITIONAL_STRATEGIES`; `bucket_perturb_conditional_failures`
  (date_format present) wired into `_conditional_admission_failures`. Free
  functions, mutation-graded.
- [ ] **Task 2: Runtime source-dtype gate.** Work-list `bucket_perturb` column
  collector + manual first-and-every-chunk schema gate + planner auto-route
  whole-source gate, mirroring the code_set trio. Coded per-chunk error.
- [ ] **Task 3: `when:` + FK-key gates.** `reject_bucket_perturb_when`
  (fail-closed) and `reject_bucket_perturb_fk_keys` (both orientations), wired
  beside the sibling gates.
- [ ] **Task 3b: Empty-chunk verification (not normalization).** Verify an empty
  `bucket_perturb` chunk yields Arrow `null` (promotable by `concat_masked_chunks`),
  not `double`. If `null`: no normalization; the contract is the existing
  null-promotion, matching the oracle. Add per-chunk string normalization ONLY if
  the empty chunk is `double` (it should not be); such normalization would have to
  live in the shared handler so full-frame and chunked agree on empty-table output.
- [ ] **Task 4: Byte-parity tests.** A `bucket_perturb` column (explicit
  date_format; week/month/quarter buckets; a source split across a chunk
  boundary; nulls and an unparseable value that passthrough; a real non-empty
  `mask_key`; a declared namespace) has a CONCATENATED chunked output
  byte-identical to the whole-column oracle, AND to the OOC route on the shared
  admitted shape. Include an empty + non-empty chunk concat case (asserting the
  concat equals the oracle, via the null-promotion, NOT that each chunk is
  string-typed) and a standalone all-null / empty-column case matching the
  oracle's `null` output.
- [ ] **Task 5: Admission-boundary tests.** Autodetect (no `date_format`),
  `date_format=""` (autodetect-equivalent: the handler resolves it via
  `cfg.get("date_format") or None`, so an empty string is NOT admission), an
  invalid-but-truthy `date_format` (proves both routes RAISE equivalently rather
  than one admitting on mere key presence), a non-string source (manual per-chunk
  AND auto-route), a `bucket_perturb` FK key edge in BOTH orientations (pinning
  the actual code/ordering: child-side is caught by `gate_fk_child_edges` first,
  parent-side by `reject_bucket_perturb_fk_keys`), `bucket_perturb` + `when:`, and
  a namespace-less config each take the documented reject / full-frame /
  fail-closed path with the chunked route's own codes.
- [ ] **Task 6: Registry load-bearing test.** A `bucket_perturb` + faker-sibling
  plan exercised through the source-dtype collector reddens under `registry=None`
  (cross-slice lesson).
- [ ] **Task 7: Lint + mutation + sentry.** ruff + mypy on the diff; mutation-
  grade the admission predicate + the schema gate + the reject gates to 0
  unresolved-logic survivors (pin the coded fields: code, path, offending column
  name, `"?"` placeholder; prose adjudicated per the ledger policy). Write
  `docs/quality/mutation-ledgers/execution_chunked_bucket_perturb.md`. Module-size
  sentry green (new `_chunked_bucket_perturb.py` module, the `_chunked_text_mask.py`
  / `_chunked_code_set.py` precedent).

## Non-goals (explicitly deferred)

- `bucket_perturb` autodetect (whole-column format detection).
- `bucket_perturb` FK self-masking (a follow-up; it is namespace-requiring and
  value-keyed like `date_shift`, so a later slice could self-mask it, but that
  makes conditional admission more invasive and is out of scope here).
- Admitting non-string (date32/timestamp) sources -- a possible later widening
  once proven chunk-stable; string-only here.
- Any other unported strategy; auto-route / planner threshold changes.

## Carry-forward (recorded, not fixed here)

Codex plan-gate observation: `date_shift` is in flat `CHUNK_SAFE_STRATEGIES` yet
a `date_shift` column carrying a `when:` predicate is exposed to the same
whole-column-reduction hazard this slice rejects for `bucket_perturb` (a
`when:` expression like `a > a.mean()` selects different rows per chunk). The
chunked route's flat `CHUNK_SAFE_STRATEGIES` do not appear to gate `when:`
uniformly. Record a follow-up audit of `when:` handling across ALL flat
`CHUNK_SAFE_STRATEGIES`; the hazard is not bucket-specific. Not addressed in
this slice.

## Acceptance

- Byte-identical to the pandas oracle on every admitted `bucket_perturb`
  explicit-date_format shape, including a chunk-boundary split, all three
  buckets, nulls, and an unparseable passthrough, and to the OOC route on the
  shared shape (Task 4).
- Every non-admitted shape (autodetect, non-string, FK key both orientations,
  `when:`, namespace-less) takes the documented reject / full-frame path with
  OOC-independent codes (Task 5).
- Registry proven load-bearing (Task 6).
- 0 unresolved-logic mutation survivors; coded fields pinned; ruff + mypy clean;
  sentry green; ledger written (Task 7).
- Held on `feat/native-phase3`; no merge.
