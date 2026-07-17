# Chunked audit-evidence row indices are chunk-local and under-report

Status: open, low priority. Discovered 2026-07-17 during the HC-3(b) `top_code`
Codex cross-model gate.

## Symptom

A strategy that emits a per-row `QualityWarning` audit sidecar (e.g. `top_code`'s
`top_code_generalized`, geo_generalize's `geo_generalize_cascade`) records row
indices **local to the handler invocation** (the current frame). On the chunked
route (`run_mask_pipeline_chunked`) each chunk is a separate invocation, so:

- the indices are chunk-local (`row_1` means row 1 *of this chunk*, not the
  global row), and
- the per-`(code, provider, column)` warning dedup in `_chunked.py` collapses
  the per-chunk warnings into one, dropping the others' rows and under-reporting
  the counts.

Example (`top_code`, tails at global rows 1 and 3, chunk size 2): full-frame
reports both rows and `over_count=2`; chunked reports a single merged warning
with one `row_1` and `over_count=1`.

## Scope / severity

- **The masked OUTPUT is correct and unaffected** — every tail cell is
  generalized identically to the full-frame run (pinned by `top_code`'s
  `TestChunkSafety`). Only the audit *evidence* sidecar is wrong on chunked runs.
- **Shared**, not top_code-specific: any warning-emitting chunk-safe strategy has
  it. It is a property of how `_chunked.py` merges per-chunk `QualityWarning`s.

## Fix direction (when picked up)

Make the chunked route either (a) offset per-chunk warning row indices by the
chunk's global start row and merge (union) the per-row maps + sum the counts
instead of dedup-collapsing, or (b) have handlers emit counts + a bounded sample
rather than a full per-row map, and have the merge sum the counts. Do it once in
the chunked warning-merge path so every warning-emitting strategy benefits.
