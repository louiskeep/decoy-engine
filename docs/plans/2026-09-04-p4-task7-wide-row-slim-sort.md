# P4-A Task 7 wide-row fix: slim the reorder sort (raw payload out-of-line)

Status: plan (GATE-APPROVED — Codex plan-gate 0 P0; design verified correct;
3 P1 + P2/P3 plan-precision findings incorporated, "suitable for GO once
incorporated"). Author: Opus, 2026-09-04.
Held target branch `feat/native-phase3`; merges with the Phase-4 bundle.
Supersedes the incomplete masked-width admission guard (commit 969ddbe4).

## 0. Why this exists

Codex-final round 3 found a real works-on-batch / fails-on-reorder P0: the
reorder route's `BoundedExternalSorter` rejects any single row wider than its
per-merge-head cap `run_bytes_cap // (2 * merge_fan_in)`
(`out_of_core_sort_row_too_wide`, `_external_sort.py`), while `_batch_join` has no
per-row width limit. A first fix (969ddbe4) gated route admission on the parent
MASKED key width; dennis proved it wrong — the sorter rejects on the RAW
joined-row width, which is dominated by the raw child columns
(`__decoy_fk_join_key`, `__decoy_src_{idx}`) that ride through at source width
REGARDLESS of masking strategy, plus wide orphans and dictionary-encoded keys.
Four distinct width vectors, one root cause: the sorter carries raw child data it
does not need.

Design consultation (Codex, 2026-09-04, best-practice survey — WiscSort
key-pointer sort, DuckDB separate string-heap sort, Postgres TOAST out-of-line
storage) recommended the structural fix below over admission-gating each vector.
Cam chose to build it (2026-09-04).

## 1. The insight

The sorter's ONLY job is to restore order by `__decoy_row_nr`, a small int. The
wide part of each sorted row is the RAW child data (`__decoy_fk_join_key`,
`__decoy_src_{idx}`), which is (a) not needed to sort, and (b) ALREADY persisted
out-of-line in the phase-1 `SpillChildKeys` spill, keyed by the same
`__decoy_row_nr`. The reorder route simply failed to exploit that: it fed the
full join row — raw child columns included — into the sorter.

Fix: sort a SLIM record (row_nr + a compact match flag + the masked PARENT
components), and re-fetch the raw child columns from `SpillChildKeys` in phase 3
by the existing row-number alignment. This removes the raw child data from the
sorter entirely, so wide raw keys (any masking strategy), wide orphans, and
dictionary-encoded raw keys can no longer overflow the sorter. The only residual
width is a genuinely-wide MASKED PARENT value (e.g. passthrough of a huge parent
key), handled by a small conservative gate on the actual slim payload.

## 2. Current shape (verified)

`StreamFkJoiner._iter_unordered_join_rows` builds the join output columns
(`_stream_join.py` select_list): `__decoy_row_nr`, `__decoy_fk_join_key` (raw
join-key encoding), `__decoy_src_{idx}` (raw child value at source type),
`__decoy_parent_match` (`p.<join_key>`, NULL iff orphan), `__decoy_parent_masked_{idx}`
(masked parent). `run_ordered_join` drains this into the sorter and sorts by
`__decoy_row_nr`. Phase 3 (`_stream_driver.py::rewritten`): per payload batch,
`cursor.take(m, row_nr_start)` pulls that batch's sorted join rows, `resolve_batch`
produces the FK output (`_batch_remap_values` + `_append_output_batch` read the
raw components `__decoy_src`/`__decoy_fk_join_key` + `__decoy_parent_match` +
masked parent from the join row), `_replace_fk_columns` overwrites.
`SpillChildKeys` already holds `(__decoy_row_nr, __decoy_fk_join_key,
__decoy_src_{idx})` per row (the phase-1 stage), finalized before phase 2.

## 3. Design (Codex's slim-sort, Option D)

### 3.1 Slim sorter projection
`run_ordered_join`'s join output (what enters the sorter) becomes ONLY:
`__decoy_row_nr` (sort key) + `__decoy_parent_match` (a COMPACT fixed-width token,
§3.3) + `__decoy_parent_masked_{idx}` (masked parent components). DROP
`__decoy_fk_join_key` and `__decoy_src_{idx}` from the sorter output. Keep
`__decoy_fk_join_key` in the JOIN `ON` (it is the join predicate) but not in the
SELECT projection that reaches the sorter.

EXPLAIN parity (plan-gate P1-2): `explain_join()` and `_iter_unordered_join_rows()`
intentionally share ONE `_unordered_join_query()` — an exact-plan safety contract
(EXPLAIN must describe the query actually drained; a test enforces it). So the
SLIM query becomes that single shared query: BOTH `explain_join()` and the real
sorter drain use it. ONLY the legacy `iter_join_rows()` shim retains the full
projection (it is the deferred-removal DuckDB-ORDER-BY path, not on the sorter
path). Add an assertion (runtime + test) that the schema entering the sorter
contains NEITHER `__decoy_fk_join_key` NOR any `__decoy_src_*`.

### 3.2 Out-of-line raw fetch in phase 3
Phase 3 aligns two source-ordered slices per payload batch, both keyed by
`__decoy_row_nr`:
- the SLIM sorted slice from the sorter cursor (row_nr + match + masked parent);
- the raw child slice from the phase-1 `SpillChildKeys` spill (row_nr +
  fk_join_key + src_{idx}), re-read for phase 3.

BOUNDED residency (plan-gate P1-3): the raw child reads MUST be lockstep with the
phase-1 child/payload batch boundaries — NOT a `JoinRowCursor`-style retaining
cursor (which pins its current Arrow batch after consumption and would leave one
unbounded raw child batch resident per edge across phase 3, breaking the RSS
envelope). `SpillChildKeys` and the payload store are both written per source
batch in the SAME phase-1 pass, so their batch boundaries coincide by row_nr.
Phase 3 reads one raw child batch per payload batch, asserts row_nr AND batch-size
alignment (a lost/misaligned/short/long suffix fails closed, never silently
self-validates — the same guard class `JoinRowCursor` enforces), consumes it, and
RELEASES it immediately before the next. Readers are closed on normal completion,
early exit, AND exception (ExitStack-owned, like the sorter cursors). Residency is
therefore O(one raw child batch) at a time, matching `_batch_join`'s O(batch)
transit. If lockstep alignment is not achievable for a shape, that shape's raw
cursor heads must be explicitly budgeted and proven — but the phase-1 co-written
boundaries make lockstep the expected path.

`resolve_batch` is refactored to take BOTH slices: `_batch_remap_values` and
`_append_output_batch` read the raw components (`src_{idx}`, `fk_join_key`) from
the child slice and the match/masked-parent values from the slim slice. The
payload-store batch boundary (source-chunk granularity) is preserved EXACTLY, so
output row/chunk order is byte-identical to today and to `_batch_join`.

### 3.3 Compact match token
Replace the raw `p.<join_key>` match sentinel (currently a full-width raw value)
with a fixed-width nullable token — a nullable BOOLEAN (`TRUE` when matched, NULL
when orphan) via `CASE WHEN p.<join_key> IS NULL THEN NULL ELSE TRUE END`.
Preserves the exact `matched[row] is not None` semantics (`_append_output_batch`
distinguishes matched-null-masked from orphan by nullness of the match column,
NOT the masked value) while removing another potential multi-MB duplicate.

### 3.4 Residual admission gate (masked parent only)
After §3.1, the only remaining sorter-width risk is a wide MASKED PARENT value.
Replace `ParentKeyRelation.max_key_bytes` (currently the masked-key width, misused
by the sum-and-8x gate) with `max_sort_payload_row_bytes`: a CONSERVATIVE upper
bound on the **entire materialized slim sorter row's** `nbytes`, which is what the
sorter actually rejects on (`_external_sort.py` measures `_materialize(view).nbytes`
per row). That row is `__decoy_row_nr` (fixed 8B) + the nullable-boolean match
token (1B data + validity) + ALL masked-parent components — so the bound must sum,
per row: every masked component's max value width PLUS its Arrow structural
overhead (variable-width offsets, validity bitmaps) PLUS fixed-column widths PLUS
the row_nr/token overhead. An empty or all-null relation still contributes the
schema-derived fixed + offset/validity overhead (not 0). Composite components are
summed WITHIN an edge (they share one sorter row). Rules (fixing the current
`_key_width.py` gaps dennis/Codex flagged):
- unknown / extension / unsupported-dictionary representations → treat as
  UNBOUNDED and fall back; NEVER map to 0.
- resolve dictionary types to their value type before classifying.
- In `decide_route`: compare EACH incoming edge's `max_sort_payload_row_bytes`
  independently against `per_head_cap = run_bytes_cap // (2 * merge_fan_in)`
  (phase-2 sorts are sequential per edge — do NOT sum across edges). Fall back to
  `_batch_join` if any edge's bound ≥ per_head_cap. REMOVE `_JOINED_ROW_WIDTH_FACTOR`
  (the empirical 8x): the tracked quantity is now a real conservative bound on the
  materialized slim row, not an inferred proxy.
- Keep the existing high-fan-in phase-3 head guard SEPARATELY (a concurrent-reader
  concern, not a row-width one).
If the sorter nevertheless raises `out_of_core_sort_row_too_wide` after this, that
is an INTERNAL INVARIANT failure (a bug in the bound), NOT a case for runtime
fallback — phase 1 has already consumed the single source read, so re-running
`_batch_join` would violate the single-read permutation guarantee. The gate must
be correct up front.

## 4. Scope

IN: `_stream_join.py` (slim projection, compact match token, `resolve_batch` two-slice
refactor), `_stream_driver.py` (phase-3 dual-cursor alignment), `_stream_join_cursors.py`
(a child-key cursor with row-number alignment), `_relation.py` + `_key_width.py`
(rename/replace to the slim-payload bound; dict + unknown handling),
`_route_policy.py` (per-edge residual gate, remove factor + sum), doc corrections
(`_external_sort.py` docstring's contradictory "bounded regardless of row width"
claim; plan §6.1/§7 wording). Tests per §6.

OUT: a general oversized-row side-lane sorter (only if real workloads need to
reorder multi-MB MASKED outputs — deferred, §7); `_batch_join`; the sorter's
per-head cap (unchanged — it stays the reorder memory contract); single-read
invariant (preserved).

BUILDABILITY / module-size (plan-gate P2-4): `_stream_join.py` is at its 720-LOC
allowlist ceiling (719) and `_relation.py` at the 600 cap (599) — neither can
absorb net growth. Allocate the phase-3 raw-read/lockstep cursor + ownership
logic to `_stream_join_cursors.py` (cycle-safe: it already imports only Arrow /
BoundedExternalSorter, not `_stream_join`), the width bound to `_key_width.py`,
and keep the two capped modules NET-NEUTRAL (the slim projection removes columns,
which offsets the small additions; verify with the module-size sentry). Preserve
a legacy one-argument `resolve_batch()` path OR introduce a distinct
reorder-specific resolver, and update the harness, direct tests, and
`scripts/route_reorder_vs_batchjoin_ab.py` to the new resolve signature.

## 5. Memory guarantee (honest wording)

The reorder memory target stays the existing measured `1.35 × process ceiling`
envelope for the sorter path. Raw child values NO LONGER affect persisted sort-run
or merge-head size (they never enter the sorter). Raw values DO still transit
transient Arrow/DuckDB residency in phase 1 (staging), phase 2 (DuckDB reads the
full `SpillChildKeys` IPC schema to evaluate the join even though the SELECT is
slim — plan-gate P2-1), and phase 3 (the one-at-a-time raw child batch, §3.2) —
all O(batch), unavoidable, and matching `_batch_join`'s own O(batch) transit;
covered by end-to-end RSS tests, not the sorter envelope. Truly-wide MASKED
payloads fall back before the source is consumed. Do
NOT claim `max(1.35× ceiling, widest row)` — the honest whole-route form is the
ordinary envelope + the largest simultaneously-resident Arrow batch +
implementation copies of the widest materialized value.

## 6. Acceptance tests (Codex's matrix; pin before build)

- Matched 6 MiB raw string FK under `hash`: reorder SELECTED, output ==
  `_batch_join` exactly (the exact case 969ddbe4 failed to close).
- 6 MiB ORPHAN under PRESERVE and WARN: reorder selected, raw value retained,
  warning content+order identical to `_batch_join`.
- Same orphan under REMAP and FAIL (the real orphan policies are PRESERVE, REMAP,
  WARN, FAIL — there is no DROP; plan-gate P3-1), fail-closed per the §6.1
  exception carve-out where applicable.
- Compact match-token semantics (plan-gate P2-2): ONE case with BOTH a matched
  row whose masked parent value is NULL AND an orphan row — assert both routes
  agree (the token distinguishes them by nullness, not by the masked value).
- Dictionary-encoded raw string AND binary keys, matched and orphaned; plus an
  explicit assertion that a dict-encoded case actually SELECTS reorder (not a
  silent fallback).
- Composite key with one very wide raw component; and composite masked components
  whose SUMMED per-edge slim width exceeds the per-edge cap (falls back).
- Multiple + overlapping incoming edges; two edges each individually under the cap
  but collectively over (proves the gate is PER-EDGE, not summed — must still
  reorder); one over-cap edge among several (that table falls back).
- Genuinely-wide MASKED passthrough parent value: route FALLS BACK before reading
  the child (residual gate); a NARROW masked value still routes to reorder.
- Residual-bound boundary via the ACTUAL slim `pc.take(...).nbytes`: cap−1
  (reorder), cap (fall back), cap+1 (fall back); plus empty/all-null schemas
  (schema-derived overhead, not 0) and unknown/extension/unsupported-dict layouts
  (unbounded → fall back).
- Direct proof that the raw columns never enter the sorter: assert the sorter
  input schema contains neither `__decoy_fk_join_key` nor `__decoy_src_*`.
- Phase-3 child-spill alignment fails closed: missing middle rows, truncated
  child spill, extra rows, wrong `row_nr` each raise (never silently self-validate).
- Single-source-read proof: a source that would permute on a second read yields
  identical output (exactly one source read).
- Full existing route-seam parity suite (exception ordering + sink-uncommitted).
- Existing 1.35× single-edge sorter RSS proof + the multi-edge RSS proof (T12)
  stay green.
- NEW end-to-end RSS cases with one and several multi-MB raw values, including a
  max-fan-in (N = 2·merge_fan_in) overlapping-edge wide-raw case (peak RSS within
  the envelope — proving raw width no longer drives sorter residency and the
  phase-3 lockstep raw reads stay O(batch)).
- Re-run string-FK routing at 2M/6M/10M to confirm the string/UUID fast path is
  RETAINED (excluding all string keys would forfeit the 10-15%→4.5x win).

## 7. Risk + future

R2 — reorder-driver internals change (phase-2 projection + phase-3 resolution),
guarded by the byte-parity + RSS matrix above. The single-read and byte-parity
contracts are preserved by construction (raw data moves from the sort to the
already-existing out-of-line child spill, re-aligned by the same row_nr).
Held on `feat/native-phase3`; merges only with explicit Cam go. FUTURE (deferred,
not this slice): a general oversized-row side lane (normal rows use the sorter;
oversized masked rows become one-row IPC batches + fixed-size `(row_nr, batch_id)`
descriptors, merged at the end) — build ONLY if real workloads need to reorder
multi-MB MASKED outputs, which the residual gate currently falls back on.
