# P4 slice 4: `code_set` (mask mode) on the chunked route

Status: plan

> Part 2 Phase 4, chunked-route strategy port, slice 4. Follows slices 1-3
> (windowed_date, group_key, text_mask), all double-gated and held on
> `feat/native-phase3`. Design doc: `docs/plans/2026-08-31-part2-phase4-plan.md`.
> Ledger: auto-memory `decoy-engine-efficiency-plan.md`. Phase 4 merges once at
> the end after all testing; this slice is built and held, not merged.
> Revised after Codex plan-gate round 1 (NO-GO); the corrections are folded in
> and called out where they overturned a draft assumption.

## Goal

Admit `code_set` in mask mode to the memory-bounded chunked route
(`run_mask_pipeline_chunked`), so a job masking a code column with `code_set`
no longer forces the whole table resident on the full-frame route. Output must
be byte-identical to the pandas oracle for the admitted config shape, or the
column is not admitted and falls through to full-frame. The out-of-core route
already admits `code_set` for a related shape
(`out_of_core/_compat.py::_group_c_conditional_rejection`); this slice is a
**stricter subset** of that admission (see "Relation to the OOC route").

## Why mask mode is chunk-safe, and the one thing that is not

`apply_code_set(value, config, mode="mask", ...)` in mask mode is
`_pick_mask(value, record, mask_key, namespace)`
(`transforms/code_set.py:546-569`): it selects a corpus code by
`HMAC(derive(mask_key, namespace or "code_set", salt), value) % candidate_count`,
excluding the input's own corpus position so output != input. Its inputs are
the value, the corpus record, the column's `mask_key`, and the column's
namespace (defaulting to `"code_set"`). Round-1 correction: mask mode keys on
`mask_key` and `namespace`, NOT `job_seed` alone, and namespace IS used. All of
these except the value are per-column constants that do not vary across chunks,
`row_index` is ignored in mask mode, and there is no whole-column reduction. So
per-chunk masking reproduces whole-column masking value-for-value, which is the
chunk invariant. The output is always a string corpus code.

The one structure that is NOT automatically chunk-invariant is the **corpus
record**. Round-1 correction: the chunked route does NOT share one context
across chunks. Each chunk calls `adapter.run(...)` (`_chunked.py:479`) and every
call builds a fresh `StrategyContext` (`_pandas_adapter.py:232`), so
`ctx.code_set_records` is reset per chunk. The whole-column handler resolves the
corpus once and threads the same record to every value
(`_strategies/_code_set.py:180-202`) precisely so a mid-run file swap cannot
split output across values. The chunked route must reproduce that guarantee
across chunks by an explicit shared-record injection (below); without it, two
chunks could resolve to different corpus versions.

## Corpus pinning across chunks (the build's central task)

Resolve one corpus record per `code_set` column ONCE, after plan compilation and
before any output is yielded, and inject that shared record into every per-chunk
`StrategyContext` so each chunk masks against the identical record. Requirements
the plan-gate round 1 made explicit:

- **Injection seam.** A fresh `StrategyContext` per chunk means "thread it into
  each chunk" needs a real channel: an adapter API / state parameter that seeds
  each chunk's context `code_set_records` with the pre-resolved mapping. The
  build defines that channel (extend the adapter's run/context construction to
  accept a caller-supplied `code_set_records` mapping), covering the pandas
  adapter and any other accepted explicit adapter.
- **Eager resolution before the empty-input early return.** Resolve (and thus
  version-check) the corpus before any chunk-loop early return. Otherwise a job
  with zero input rows would "succeed" against an invalid or version-mismatched
  corpus while the oracle fails closed. Resolution failure must surface before
  streaming begins.
- **`corpus_source_version` pin preserved.** The version pin is checked at
  resolution and re-checked against the pinned record in every `apply_code_set`
  call (`transforms/code_set.py:380-397`). With one shared record, every chunk
  re-checks the same version.

Round-1 correction to the swap semantics: after successful pinning, replacing
the corpus file mid-run must NOT fail. The run continues consistently on the
pinned record. A version mismatch is a fail-closed error at initial resolution,
before streaming. The test matrix (Task 5) encodes both.

## Scope: mask mode, no chapter_preserve, string source, non-FK columns

Admit `code_set` only for the shape whose chunked output provably equals the
whole-column output:

- **mask mode only.** Gen mode selects by `row_index` (a whole-run offset the
  per-chunk kernel does not carry) and is namespace-bound. Reject gen mode.
- **no chapter_preserve.** chapter_preserve restricts candidates by a `chapter`
  column and raises per-value fail-closed errors (`transforms/code_set.py:421-447`),
  a cross-value/quarantine shape the chunked route does not model. Reject it.
- **chunk-stable string source.** Round-1 correction: the pandas chunked route
  converts each chunk Arrow->pandas and the handler calls `str(value)`
  (`_strategies/_code_set.py:196`), so it IS exposed to the int-plus-null ->
  float widening that made `str(1)` become `"1.0"` in one chunk and `"1"` in
  another (the text_mask slice-3 hole). Require a chunk-stable string /
  large_string source. This is sufficient though not strictly necessary
  (null-free integer sources are also stable); a string-only first slice is the
  defensible narrow cut. The OUTPUT dtype never drifts (always a corpus string;
  nulls stay null, handled by the existing all-null schema normalization).
- **no FK-key participation, either orientation.** `code_set` is admitted as a
  CONDITIONAL strategy, NOT added to the flat `CHUNK_SAFE_STRATEGIES`. Round-2
  correction: `gate_fk_child_edges` only inspects edges where the chunked table
  is the CHILD (`_chunked_fk.py:292` skips parent-only edges), so a `code_set`
  PARENT-key column would slip through conditional admission. That leaves the
  parent chunked (code_set-masked key) while a `code_set`-child cannot self-mask
  (rejected) and falls to full-frame, a mixed-route RI coordination this slice
  does not take on. So a new gate `reject_code_set_fk_keys` rejects the chunked
  table whenever it participates in any FK edge with a `code_set` key column, as
  PARENT or CHILD, forcing the whole FK job onto a coherent route. FK
  self-masking for `code_set` (both orientations) is a deliberate follow-up.

## Admission mechanism (two layers, mirroring slice 3)

Config-shape checks and runtime-schema checks live in different layers, exactly
as text_mask (slice 3) split them:

- **Config-shape (conditional predicate).** Add `code_set` to
  `CHUNK_CONDITIONAL_STRATEGIES` (`_chunked.py:154`) and a free function
  `code_set_conditional_failures(col_entry)` returning the mode / chapter_preserve
  failure reasons, wired into `_conditional_admission_failures`
  (`_chunked.py:265-269`). Round-1 correction: the source-dtype check does NOT
  belong here (a config-only predicate cannot see the runtime Arrow type).
- **Runtime source-dtype gate (schema-level, the text_mask pattern).** Add a
  work-list-derived `code_set` column collector, a first-and-every-chunk schema
  gate for the manual `run_mask_pipeline_chunked` entry
  (`reject_unsafe_code_set_source_dtype`, raising per chunk with a coded error),
  and a whole-source schema rejection in `_planner._runtime_source_rejections`
  for the auto-route. This mirrors slice 3's
  `text_mask_source_columns` / `reject_unsafe_text_mask_chunk_schema` /
  `unsafe_text_mask_source_columns` trio.
- **`when:` gate.** Add `reject_code_set_when` (fail-closed), wired beside the
  sibling gates (`_chunked.py:251-256`). Confirmed required at round 1: a `when:`
  predicate can carry a pandas-eval reduction (e.g. `a > a.mean()`) whose
  per-chunk mean selects different rows than the whole-frame mean, a genuine
  chunk hazard independent of output dtype; rejecting it also avoids a per-chunk
  `preflight()` re-resolving the corpus.

## Relation to the OOC route (stricter subset, not identical)

Round-1 correction: do NOT claim identical shapes or reason codes. The OOC route
gates only mode / chapter_preserve, ADMITS non-string Arrow sources (it reads via
`to_pylist`, not pandas str-coercion), and its rejection codes differ. This
chunked slice is a STRICTER subset: it additionally requires a string source and
rejects `when:`. Parity is asserted only on the shape BOTH routes admit (mask
mode, no chapter_preserve, string source, no `when:`); the reason codes are
route-specific and are not promised equal.

## Memory qualification

Pinning one corpus record holds a single record reference; it does not scale with
table rows. The corpus itself is an auxiliary, potentially large resident input,
the same as on the oracle and OOC routes. The slice bounds the TABLE residency,
not the corpus; the docstring/plan states that qualification rather than claiming
a strict total-memory bound.

## Hazards to clear at the plan-gate (each prior slice hid one)

1. **Corpus consistency across chunks** (central). Explicit shared-record
   injection; eager resolution before the empty-input return; identical version
   re-check on every chunk; swap-after-pin continues on the pinned record.
2. **Source-dtype chunk-drift.** String-source gate at the schema layer (manual
   per-chunk + auto-route), not the config predicate.
3. **FK self-mask exclusion** confirmed clean (round 1, Q3).
4. **Gen-mode / chapter_preserve rejection** fail-closed.
5. **`when:` reject** required (round 1, Q4).
6. **Evidence / provenance.** Each chunk produces `code_set_corpora` evidence,
   but `run_mask_chunked` aggregates only timings / conversion / warnings
   (`_pipeline_route_exec.py:545`). Add quality-metric aggregation with
   once-per-column, `masked_any` semantics, so the execution-evidence contract is
   not broken. `outputs` bytes are unaffected; this is a metrics-contract fix.
7. **Empty-chunk output-schema drift** (round-2 finding). `CodeSetHandler`
   assigns `out=[]` for an empty chunk, which pandas infers as `float64` and
   Arrow emits as `double`, while a non-empty chunk emits `string`; the chunk
   concatenator normalizes only `null`-typed chunks, not `double`, so an iterable
   mixing an empty and a non-empty string chunk raises `chunked_schema_mismatch`.
   Normalize `code_set`'s empty-chunk output to the string output type (an empty
   string-typed array) before concatenation, so an empty chunk carries the same
   type as a non-empty one. This is the `code_set` analogue of the all-null
   normalization the concatenator already does.

## Tasks

- [ ] **Task 1: Config-shape conditional admission.** `code_set` into
  `CHUNK_CONDITIONAL_STRATEGIES`; `code_set_conditional_failures` (mask mode, no
  chapter_preserve) wired into `_conditional_admission_failures`. Free functions,
  mutation-graded.
- [ ] **Task 2: Runtime source-dtype gate.** Work-list `code_set` column
  collector + manual first-and-every-chunk schema gate + planner auto-route whole-
  source gate, mirroring slice 3's text_mask trio. Coded per-chunk error.
- [ ] **Task 3: `when:` + FK-key gates.** `reject_code_set_when` (fail-closed),
  and `reject_code_set_fk_keys` rejecting the chunked table when it participates
  in any FK edge with a `code_set` key column as parent OR child, both wired
  beside the sibling gates in the admission entry.
- [ ] **Task 3b: Empty-chunk output normalization.** Normalize `code_set`'s
  empty-chunk output to the string output type before concatenation so an empty
  and a non-empty chunk carry the same Arrow type (no `double`/`string`
  mismatch), the analogue of the existing all-null normalization.
- [ ] **Task 4: Corpus pinning seam.** Resolve one corpus record per `code_set`
  column before any output (before the empty-input return), inject a shared
  `code_set_records` mapping into every per-chunk `StrategyContext` via a new
  adapter channel; preserve the `corpus_source_version` pin. Cover the pandas
  adapter (and any other accepted adapter).
- [ ] **Task 5: Byte-parity + pinning tests.** A `code_set` mask-mode column
  (source split across a chunk boundary, nulls, repeated codes, a real non-empty
  `mask_key`, and at least two distinct namespaces) is byte-identical to the
  whole-column oracle, AND to the OOC route on the shared admitted shape. Pinning:
  one resolution across all chunks (spy on `resolve_corpus_record`); a mid-stream
  file swap CONTINUES on the pinned record (no failure); an INITIAL version
  mismatch fails closed before streaming; a zero-row job with an invalid corpus
  fails closed (eager resolution).
- [ ] **Task 6: Admission-boundary tests.** gen mode, chapter_preserve, a
  non-string source (manual per-chunk AND auto-route), a `code_set` FK key edge
  in BOTH orientations (chunked table as parent, and as child), and `code_set` +
  `when:` each take the documented rejection / full-frame path with the chunked
  route's own codes (not promised equal to OOC). Plus an empty-chunk parity case:
  an iterable mixing an empty and a non-empty string chunk concatenates to the
  string type without `chunked_schema_mismatch`.
- [ ] **Task 7: Evidence aggregation.** Aggregate `code_set_corpora` once per
  column (`masked_any` semantics) in the chunked/auto-chunk metrics, matching the
  whole-column evidence contract; a test pins it.
- [ ] **Task 8: Lint + mutation + sentry.** ruff + mypy on the diff; mutation-
  grade the admission predicate, the schema gate, and the pinning logic to 0
  unresolved-logic survivors (prose adjudicated per the ledger policy); module-
  size sentry green (new module if `_chunked.py` would exceed its ceiling,
  following the `_chunked_text_mask.py` precedent).

## Non-goals (explicitly deferred)

- `code_set` gen mode and chapter_preserve.
- `code_set` FK self-masking.
- Admitting non-string sources (an OOC-parity widening; string-only here).
- `bucket_perturb` (a separate Group-C slice).
- Auto-route / planner threshold changes.

## Acceptance

- Byte-identical to the pandas oracle on every admitted `code_set` mask-mode
  shape, including a chunk-boundary split and multiple namespaces, and to the OOC
  route on the shared admitted shape (Task 5).
- Every non-admitted shape (gen, chapter_preserve, non-string, FK key, `when:`)
  takes the documented full-frame/reject path (Task 6).
- All chunks share one pinned corpus record; swap-after-pin continues, initial
  mismatch and zero-row-invalid-corpus fail closed (Task 5).
- Evidence surfaced once per column (Task 7).
- 0 unresolved-logic mutation survivors; ruff + mypy clean; sentry green (Task 8).
- Held on `feat/native-phase3`; no merge.
