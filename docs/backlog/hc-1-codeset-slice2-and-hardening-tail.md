# HC-1 code_set: slice-2 scope + hardening tail

Status: backlog. Written at HC-1 slice-1 merge (2026-07-17). Slice 1 (provenance
+ scale infrastructure, no external data) is merged; this tracks the remaining
work the multi-round adversarial gate surfaced but deliberately did not fold
into slice 1.

## How slice 1 was gated

Build -> Dennis (adversarial, Opus) -> Codex GPT-5.6-sol (cross-model), rounds
1 through 7. Correctness / fail-closed / evidence-integrity findings were fixed
at source in-round regardless of round number; performance and evidence-
completeness edges on exotic routes were documented and deferred here. Round 7
was the final gate round. Its two P2 correctness findings (cross-invocation
record divergence under `orphan_policy=REMAP`; shipped-corpus `is_seed` /
`corpus_version` fail-closed strictness) were fixed in-round. Its one P1 is the
lead item below.

## 1. Per-value selection is O(corpus) — the slice-2 scale item (Codex R7 P1)

`transforms/code_set.py::_pick_from_candidates` / `_apply_chapter_preserve`
rebuild the candidate list (and, under `chapter_preserve`, the chapter bucket)
by scanning the whole corpus **per masked value**. Masking is therefore
O(input_rows x corpus_rows) with a large per-row allocation.

Why it is not a slice-1 defect:

- Slice 1 ships only **seed** corpora (tens of rows: icd10 65, hcpcs 32, ndc 38,
  mcc 62). At that size the scan is negligible and masking is correct.
- The full public corpora (ICD-10-CM ~70k rows) that make the scan bite are
  **slice-2 data** (the real-data ETL), which has not landed. There is no ~70k
  corpus in the repo today for this to be slow against.
- Correctness is unaffected either way — this is purely throughput.

Slice-2 fix (do this WITH the real-data ETL, not before):

- Precompute in `_CorpusRecord` at load time: a code-sorted index / position
  map, and per-chapter buckets (a `chapter -> list[code]` dict), alongside the
  existing `chapter_index`.
- Select from those precomputed structures without materializing a fresh
  candidate list per row.
- **Determinism is load-bearing and at risk here.** HMAC-keyed selection depends
  on the exact candidate ordering (ascending-by-code, CS.1-CS.9 contract). Any
  precompute MUST preserve byte-identical selection: land the determinism/parity
  suites green first, and treat this as its own reviewed change (it is not a
  drive-by — it deserves its own Dennis + Codex pass), because a reordering bug
  here silently changes every masked output.

## 2. HC-1 fast-follow: true all-null OOC code_set evidence parity

Tracked separately (auto-memory / task #10). The out-of-core route's evidence
surfacing for a genuinely all-null code_set column: confirm parity with the
pandas/sequential routes (a column that never dispatches a value must not stamp
its corpus as "used"), and add the cross-route pinning assertion. The
cross-invocation pin regression (`tests/unit/execution/test_code_set_job_pinning.py`)
landed with slice 1; the OOC all-null parity assertion is the remaining piece.

## Slice-2 real-data ETL (the other half of HC-1)

Open question still owned by Cam: sourcing the full public-domain corpora
(CMS/CDC/FDA) requires network access to those sources, which is unverified in
this environment, plus a release-dated + user-update path (HC-1 spec). This is
the substantive remainder of HC-1 and is not a "tail" item — it is the second
half of the feature. Provenance (`source_version`, `effective_date`, `is_seed`,
metadata format version) is already fully wired and fail-closed for shipped
corpora, so slice 2 is data + ETL, not new evidence plumbing.
