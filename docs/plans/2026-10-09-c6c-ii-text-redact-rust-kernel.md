# C6c-ii: a Rust span-detection kernel for text_redact

Status: plan (rev 2, folds Codex plan-gate round 1). Phase C, Rust-engine program.
Branch `feat/c6c-ii-text-redact-rust` off engine main `90e36c38`.
Supersedes the C6c-ii sketch in `docs/plans/2026-10-06-c6c-i-text-redact-arrow.md`.

Rules consulted: 00-universal, risk-and-exceptions, development-loop, autonomous-operation,
feature-dev, testing, performance, architecture, api-and-compatibility, documentation.

Rev 2 changes (Codex round 1, REVISE, architecture affirmed): the lookaround/backtracking
detectors stay in Python (H2); Rust takes only detectors proven byte-identical including
Unicode (H3); the merge candidate order and stable-sort precedence are pinned to the oracle
(H1); validator vectors, the PyO3/surrogate input contract, a versioned detector catalog, the
named merge helper + NER admission statement, raw-candidate/poisoned-integration/fallback-mode
acceptance, and a numeric perf target with a committed fixture are all specified (M4-M9).

## 1. Why

C6c-i admitted `text_redact` to both native routes as an `ARROW_PYTHON` operator, so a
text_redact column no longer forces its whole table onto pandas. But the column's own
detection still runs the Python per-cell span logic. Measured on the dev box (2026-10-09,
`scripts/bench_text_redact.py`, committed by this slice, see 7.7):

- **~32k rows/s single-thread, ~31us/cell**, stable from 100k to 1M rows.
- Extrapolated **10M ~= 5.2 min; 100M ~= 52 min**, far past the 600s/100M mask-only contract.
- cProfile of `TextRedactHandler.run` over 200k cells: **`iter_spans` Python body 66.8%**,
  validators ~9%, the regex engine (`finditer`/`sub`) only ~7-10%, pandas + splice ~17%.

The cost is the per-cell loop over ~11 detectors (for each: a `finditer` pass, a `Span`
object per match, a validator dispatch, an append), then a sort + overlap sweep. The scan is
cheap; the Python orchestration is the bottleneck. So the win needs the per-cell detector
loop in Rust, not just the scan (Cam 2026-10-09). A single fused Python alternation regex is
not a shortcut: each detector independently contributes candidates the final merge resolves,
so fusing changes which spans are found.

## 2. Goal and non-goals

**Goal.** Move the built-in-detector loop to a Rust kernel for the detectors that can be
**proven byte-identical** to the Python oracle (including Unicode and boundary behavior). The
kernel runs those detectors + their ported validators per cell and returns raw validated
candidate spans (code-point offsets, per-detector, in `finditer` order). Python keeps NER,
custom detectors, the lookaround/backtracking detectors (below), the single combined
leftmost-then-longest merge, and the splice. Output is byte-identical to
`storm.detectors.iter_spans` + `_text_redact._splice` in every case. Keep the `regex` crate.

**Correctness-first scoping rule (resolves H2/H3).** A detector moves to Rust only if a
differential proof (7.2/7.4) shows its Rust implementation is span-for-span identical to the
Python detector across the ASCII, Unicode, and boundary corpus. Any detector that cannot be
proven identical stays in Python, injected into the same merge. Concretely:

- **Stay in Python (verbatim), this slice:** `ssn`, `us_zip`, `street_address`. Their patterns
  use lookaround and Python backtracking that drive match advancement (H2); reproducing that
  in a lookaround-free engine is not reliably equivalent, so they are not ported. They run in
  Python exactly as today and inject as candidates (4.3), like custom detectors.
- **Candidates to move to Rust (each gated by its own 7.4 proof):** `email`, `us_phone`,
  `pan`, `iban`, `ipv4`, `icd10`, `npi`, `url`. A candidate that fails its proof (e.g. a
  Unicode class/boundary/case divergence that cannot be matched exactly) stays in Python and
  is recorded as such; the slice still ships with the provable subset.

The win is "as many detectors as are provably identical." The eight candidates are the
lookaround-free majority of the per-cell loop, so even the conservative subset removes most of
the orchestration cost; the exact subset and its measured speedup are reported at BUILD (7.7).

**Non-goals.** NER (ML) stays on its existing oracle route; C6c-i already rejects NER configs
from native admission and this slice does not change that (M7). Custom detectors stay Python.
The merge and splice stay Python (a later slice may move the splice). No new public config.
The structured/profiler detector path is out of scope.

## 3. Established methodology (cite in the kernel docstring)

- Multi-pattern scanning: the Rust `regex` crate per-pattern `find_iter` over the proven
  subset (linear-time, no catastrophic backtracking), which is why we keep the crate rather
  than `fancy-regex`.
- Validators port the executable behavior of `storm/_validators.py` (the reference), not an
  idealized standard: Luhn (ISO/IEC 7812-1) with the module's normalization, NPI (CMS Luhn
  over the 80840 prefix), ICD-10 chapter-range table, IBAN (ISO 13616 mod-97, incremental
  remainder), IPv4 octet range. Locked to shared vectors (7.3).

## 4. Architecture

### 4.1 The Rust kernel boundary and input contract (M5)

New kernel in `decoy-engine-native`, exported through the companion's existing
`decoy_engine_native._kernel` surface (the package root does not auto-re-export; the Python
loader imports from `_kernel`, M6). Python view:

    text_redact_candidates(
        values: Sequence[object],         # one column chunk, arbitrary Python objects
        detector_ids: Sequence[str],      # the resolved, ordered Rust-eligible subset (never None here)
        catalog_version: int,             # the caller's detector-catalog contract id (4.6/6)
    ) -> list[list[tuple[int, int, int]]] # per cell: (detector_index, start, end), code-point offsets

Input handling (pin exactly, the direct-call contract the oracle implies):
- The binding accepts arbitrary objects, not `list[str | None]`. A `None` or non-`str` element
  yields `[]` for that cell (the kernel scans strings only). The **native wrapper**
  (`native_text_redact`) keeps C6c-i's contract: it stringifies non-null non-string cells
  before calling the kernel, so a real non-string value is detected on its `str()` form exactly
  as the oracle does. The kernel's "non-string -> []" rule is only for a direct/raw call and
  must not be reached through the wrapper for non-null non-string cells.
- Python `str` can hold lone surrogates that are not valid UTF-8. The binding must not panic:
  a cell whose text is not representable as Rust `str` is handed back to the Python detector
  path for that cell (recorded), never silently dropped. Tests include a lone-surrogate cell.
- `detector_ids` is the already-normalized Rust-eligible subset in the oracle's requested
  order; the kernel never re-applies the empty-means-all rule (the resolver owns it, C6c-i 3b).

### 4.2 Where it plugs in

C6c-i's `native_text_redact` (`native/_kernels_scalar.py`) currently calls Python `iter_spans`
+ `_splice` per cell. C6c-ii swaps the built-in-detector half for a call to
`text_redact_candidates` over the Rust-eligible subset, then runs the Python detectors (the
three lookaround ones + any non-proven ones) and the merge/splice. The `OperatorSpec`,
resolver, step branch, route contracts, chunked string pin, admission, and evidence from C6c-i
are unchanged (no registry/dispatch change).

### 4.3 The merge seam, with exact oracle precedence (H1, M7)

A named helper `merge_text_redact_spans(...)` (new, unit-tested directly) assembles the final
span list per cell in the EXACT order `iter_spans` builds it, so Python's stable sort resolves
ties identically:

    candidates = []
    candidates += extra_spans (NER), in supplied order          # when present
    for det_id in detector_ids (the oracle's requested order):  # built-ins, requested order
        if det_id is Rust-eligible: candidates += kernel spans for det_id, in finditer order
        else:                       candidates += Python finditer spans for det_id, in order
    candidates += custom-detector matches, in supplied spec order then finditer order
    candidates.sort(key=lambda s: (s.start, -(s.end - s.start)))   # stable; preserves the above
    # leftmost-then-longest sweep, including zero-length custom spans, then _splice

This reproduces the oracle exactly: `iter_spans` seeds with `extra_spans`, then appends
built-ins in requested-detector order (each in `finditer` order), then custom; the stable sort
keeps that precedence for equal `(start, length)`. The Rust-eligible and Python-only built-ins
interleave by the SAME requested-detector order, so moving some to Rust cannot change
precedence. Counterexamples from the gate are pinned as tests (7.2): `"12345"` with `us_zip`
vs an injected `location` span at `(0,5)`; `"2234567891"` matching both `npi` and `us_phone`
under `["npi","us_phone"]` vs the reverse. Labeled output (`label_token=True`) is asserted for
each tie.

NER note (M7): this slice does not route NER through the native operator. C6c-i rejects NER
configurations from native admission, so the combined-merge path with `extra_spans` is reached
only on the oracle route today; `merge_text_redact_spans` is written and unit-tested to handle
`extra_spans` + custom so it is correct wherever reused, but the slice makes no admission
change and claims no native NER.

### 4.4 Code-point offset contract (M5 refinement)

Spans are code-point (Unicode scalar value) indices into the Python `str`, matching
`re.Match.start()/end()` and `_splice`'s `str` slicing. The kernel converts the `regex` crate's
UTF-8 byte offsets to scalar-value counts (not grapheme clusters, no normalization): an astral
char counts once, a combining mark counts separately, and the terminal boundary is the count at
the match end byte. The conversion is the first thing the differential corpus stresses (7.2)
with accented Latin, CJK, astral emoji, and combining marks inside and immediately adjacent to
matches.

### 4.5 Unicode matching-semantics parity (H3) and the per-detector proof

Rust's default Unicode `\w`, `\s`, word boundaries, and simple case folding are not identical
to Python `re`'s, and Python's Unicode DB varies across supported versions. Therefore each
Rust-eligible detector's pattern is written to match the Python detector's semantics on the
proof corpus, and **the proof is a build gate per detector** (7.4): the Rust detector must be
span-for-span identical to the Python pattern across ASCII, the Unicode corpus (fullwidth and
Arabic-Indic digits, accented/CJK text, astral, combining marks, the separator and case
examples the gate named), and boundary-adjacency cases. A detector whose Rust form cannot be
made identical (e.g. an irreducible `\b`/case-fold divergence) stays in Python and is listed in
the build record as deferred. The slice ships the proven subset; it never ships a detector that
diverges on any corpus case.

### 4.6 Catalog contract + companion-absent fallback (M6, M8)

- **Versioned catalog.** One authoritative ordered detector catalog (id -> index, id -> label)
  lives in Python and is mirrored in Rust, tagged with a `catalog_version` integer. The Python
  loader passes `catalog_version` on every call; the kernel rejects a mismatch, and the loader
  then falls back to the full Python path. An agreement test asserts the Python and Rust
  catalogs match on ids, labels, and order. This separates stable detector identity from the
  per-call requested execution order.
- **Three fallback modes, each oracle-compared (M8):** (a) companion package absent; (b)
  package present but the `_kernel` symbol/`text_redact_candidates` absent (older companion);
  (c) `catalog_version` mismatch. Each falls back to the C6c-i Python `iter_spans` path and is
  tested to produce byte-identical output to the oracle on the 7.2 corpus. The fallback is the
  literal C6c-i path (affirmed sound by the gate).

## 5. Build order (tests-first per [[plan-defines-acceptance-tests]])

1. **Acceptance tests first.** Land the committed benchmark fixture (7.7), the per-detector raw
   candidate differential harness (7.2), the validator KATs (7.3), the per-detector Unicode
   proof corpus (7.4), the merge-precedence tie tests (4.3), the NER/custom merge-helper unit
   tests (7.5), the three fallback-mode tests (4.6), and the mutation targets (7.6). They pass
   on the Python path and pin behavior; the ones that require the Rust binding are marked
   `xfail`/skip until step 2/3 so "green-before oracle fixtures" are distinct from "must fail
   until Rust exists."
2. **Rust kernel.** `decoy-engine-native/src/text_redact.rs` (keep `lib.rs` under budget): the
   Rust-eligible detectors, the 5 ported validators, byte->scalar offset conversion, the
   versioned catalog, surrogate-safe input handling, pyo3 binding via `_kernel`.
3. **Python swap.** `native_text_redact` calls `text_redact_candidates` for the eligible subset,
   runs the Python-only detectors, calls `merge_text_redact_spans`, splices; the three fallback
   modes wired.
4. **Gates.** Full existing text_redact suite green; differential + proof + fallback + mutation
   green; re-measure throughput (7.7).
5. **Docs.** CHANGELOG (internal: faster text_redact, output unchanged, list the ported subset
   and any deferred detector), kernel docstring with methodology cites, build record (with the
   measured speedup and the eligible/deferred detector lists), roadmap row.

## 6. Failure modes and fail-closed behavior

- Companion absent / old symbol / catalog mismatch -> Python fallback, byte-identical (4.6).
- Unknown detector id -> skipped, exactly as `iter_spans`.
- Empty detector list after normalization -> resolver maps to all built-ins (C6c-i owns it; the
  kernel does not re-apply, so a resolver mutant is observable).
- Validator rejects a match -> span dropped with ordinary `finditer` advancement retained
  (distinct from a lookaround assertion failure, H2).
- Non-string / null / empty cell -> `[]` from the kernel; the wrapper stringifies non-null
  non-string cells before the call (4.1).
- Lone-surrogate cell -> that cell handled on the Python path, never a panic (4.1).
- A Rust panic surfaces as a Python exception (pyo3), never a silent wrong result.

## 7. Acceptance tests (byte-identical is the bar)

**7.1 Golden snapshots.** `tests/snapshots/golden/mask_text_redact/` stays byte-identical; no
golden is re-recorded (no intended output change).

**7.2 Differential harness, RAW candidates then merged (M8, H1).** For a generated corpus
(clinical-note style; every built-in type; multiple PII per cell; overlapping types incl. the
gate's SSN-inside-NER-name and ZIP-adjacent-phone; no-PII prose; empty; null; non-string;
leading/trailing/repeated PII; the Unicode subset of 4.4): assert the **per-detector raw
ordered candidate list** (Rust vs Python `finditer`+validator) is identical BEFORE merging,
then assert the merged spans and the final spliced string (labeled and unlabeled) equal the
unchanged oracle. Failing generated cases are retained as fixtures. Detector-permutation and
overlap permutations are enumerated.

**7.3 Validator KATs (M4).** Shared Python<->Rust vectors capturing the executable behavior:
Luhn (thirteen zeroes pass; no upper-length limit; fullwidth digits pass; whitespace/hyphens
stripped); NPI (fullwidth `"１２３４５６７８９３"` passes; exact doubling parity; stripping);
IPv4 (`"001.002.003.004"` and Arabic-Indic `"١٢٧.٠.٠.١"` pass); IBAN (exact country set, global
15-34 length, whitespace removal, incremental mod-97, no overflow); ICD-10 (`F00` false, `F01`
true, `D89` true, `D90` false; `"A00!!!!"` passes — no alphanumeric-suffix enforcement).
Validator-unit inputs are distinguished from regex-reachable inputs.

**7.4 Per-detector Unicode/boundary proof (H3).** For each Rust-eligible candidate, a corpus of
accept/reject strings with Unicode inside and immediately adjacent to matches, boundary chars,
case variants, and the gate's named examples; the Rust detector must be span-for-span identical
to the Python pattern. A detector that fails stays in Python (2) and the test records it.

**7.5 NER + custom interplay (M7).** Unit-test `merge_text_redact_spans` directly with injected
`extra_spans` and custom specs across the tie/overlap cases; assert combined resolution + splice
equals the Python path.

**7.6 Integration + fallback + mutation (M8).** Companion-enabled integration on BOTH native
routes with the Python detection path poisoned/instrumented, proving the production wrapper
actually called Rust. The three fallback modes (4.6) each compared directly to the oracle.
Mutation targets: candidate advancement, tie order, offset conversion, label mapping,
empty-selection semantics, catalog-version check.

**7.7 Perf (performance.md: committed fixture, baseline, target, warmup, variance, budget).**
Commit `scripts/bench_text_redact.py` with a fixed corpus and command. Baseline is section 1
(~32k rows/s, ~31us/cell). Before BUILD acceptance, define numeric targets: a minimum
end-to-end speedup of **>=3x** on the eligible-subset workload at 1M rows (scanner-only and
end-to-end reported separately), and peak RSS no more than **1.2x** the Python path. Measure
Python and Rust on the same workload/host/release build, with a warmup pass and enough
repetitions to report min/median and spread (not mean-only). A detector subset that cannot meet
>=3x is reported honestly; the slice still ships if parity holds, with the achieved number
recorded. Fallback is detected by execution assertion (7.6), never by timing.

## 8. Risks

- **R-offset:** byte<->scalar conversion off-by-one. Mitigation: 7.2/7.4 Unicode corpus first.
- **R-unicode-parity:** a detector's Rust semantics diverge from Python. Mitigation: the per-
  detector proof gate (4.5/7.4) keeps it in Python rather than shipping a divergence.
- **R-merge-order:** tie precedence differs. Mitigation: 4.3 exact order + 7.2 tie tests.
- **R-validator:** a ported check-digit diverges. Mitigation: 7.3 KATs vs the Python reference.
- **R-catalog-skew:** companion/package revision mismatch. Mitigation: 4.6 versioned catalog +
  agreement test + fallback.
- **R-scope:** `lib.rs` size budget. Mitigation: new `text_redact.rs`.

## 9. Resolved / open for the Codex plan gate (round 2)

Resolved from round 1: H1 (4.3 exact precedence), H2 (lookaround detectors stay Python, 2),
H3 (per-detector proof gate, 4.5/7.4), M4 (7.3 vectors), M5 (4.1 input/surrogate contract +
4.4 offsets), M6 (4.6 versioned catalog + `_kernel` export), M7 (4.3 named helper + NER
admission statement), M8 (7.2/7.6 raw candidates, poisoned integration, 3 fallback modes,
mutation), M9 (7.7 committed fixture + numeric target + variance).

Open for round 2: confirm the eligible detector subset (2) is reasonable to decide at BUILD via
the 7.4 proof rather than fixed in the plan; confirm the `>=3x` / `1.2x RSS` bars; confirm the
surrogate-cell "hand back to Python" rule is the right fail-safe vs an explicit error.
