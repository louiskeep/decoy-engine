# C6c-ii: a Rust span-detection kernel for text_redact

Status: plan (rev 3, folds Codex plan-gate rounds 1-2). Phase C, Rust-engine program.
Branch `feat/c6c-ii-text-redact-rust` off engine main `90e36c38`.
Supersedes the C6c-ii sketch in `docs/plans/2026-10-06-c6c-i-text-redact-arrow.md`.

Rules consulted: 00-universal, risk-and-exceptions, development-loop, autonomous-operation,
feature-dev, testing, performance, architecture, api-and-compatibility, documentation.

Rev 3 (Cam 2026-10-09: "option A, Rust + Python fallback for harder values"). The strict
"prove each detector byte-identical across all Unicode" approach of rev 2 could only cover
`email`/`url` (2 of 11), because the other detectors depend on Python's `\d`/`\s`/case-fold
semantics, which differ from Rust's and shift across Python versions. Rev 3 replaces that with
**per-cell ASCII-domain routing**: the Rust kernel runs the eight lookaround-free detectors on
cells that lie in a defined ASCII-safe domain where Python and Rust provably agree, and every
other cell falls back to the unchanged Python path. Output is byte-identical always; the
correctness argument is constructive (a precise per-cell predicate), not corpus-based, which
resolves the round-2 HIGH. Rounds 1-2 findings and their resolutions are in section 9.

## 1. Why

C6c-i admitted `text_redact` to both native routes as an `ARROW_PYTHON` operator, so a
text_redact column no longer forces its whole table onto pandas, but the column's own detection
still runs the Python per-cell span logic. Measured on the dev box (2026-10-09,
`scripts/bench_text_redact.py`, committed by this slice, 7.7):

- **~32k rows/s single-thread, ~31us/cell**, stable 100k-1M rows.
- Extrapolated **10M ~= 5.2 min; 100M ~= 52 min**, past the 600s/100M mask-only contract.
- cProfile over 200k cells: **`iter_spans` Python body 66.8%**, validators ~9%, the regex
  engine itself ~7-10%, pandas + splice ~17%.

The cost is the per-cell loop over ~11 detectors (each: a `finditer` pass, a `Span` per match,
a validator dispatch, an append), then a sort + overlap sweep. The scan is cheap; the Python
orchestration is the bottleneck. This is scale-hardening for large free-text columns (clinical
notes); structured PII masking does not use text_redact and is already fast. The value depends
on the ASCII-cell hit rate of real free-text, which 7.7 measures.

## 2. Architecture: per-cell ASCII-domain routing (Cam option A)

Three detector groups, by how they run:

- **Rust on eligible cells (the eight lookaround-free detectors):** `email`, `us_phone`, `pan`,
  `iban`, `ipv4`, `icd10`, `npi`, `url`. The Rust kernel runs these, with ASCII-restricted
  character classes and the five ported validators, ONLY on cells in the ASCII-safe domain
  (2.1). On an eligible cell the Rust spans are byte-identical to Python's by construction (2.2).
- **Python always (the three lookaround/backtracking detectors):** `ssn`, `us_zip`,
  `street_address`. Their patterns use lookaround and Python backtracking that drive match
  advancement (round-1 H2), which a lookaround-free engine cannot reliably reproduce. They run
  in Python on every cell, exactly as today, and inject as candidates (4.3). (Moving them to
  Rust via an explicit advancement search is a possible later slice, out of scope.)
- **Python fallback (ineligible cells):** any cell NOT in the ASCII-safe domain is handled
  entirely by the existing Python `iter_spans` path for all eleven detectors. Byte-identical to
  today by definition (same code).

So per cell: Python always runs the 3 lookaround detectors (+ custom + NER); on an eligible
cell the other 8 come from Rust; on an ineligible cell the other 8 come from Python. All
candidates feed one combined merge (4.3). The speedup is the 8-of-11 detectors moved off the
Python loop on eligible cells; the residual Python cost is the 3 lookaround detectors + the
merge + splice. 7.7 reports the measured end-to-end result honestly.

### 2.1 The ASCII-safe domain (the routing predicate)

A cell is **eligible** iff every code point is ASCII (`< 0x80`) AND none is in `0x1c-0x1f`
(the C0 information separators FS/GS/RS/US). Rationale, per Unicode construct the eight
patterns use:

- `\d`: Python (UNICODE) matches any `Nd` code point; on an all-ASCII cell the only `Nd` present
  are `[0-9]`, which Rust's ASCII `\d` / `[0-9]` matches identically. (Excluding non-ASCII
  removes fullwidth/Arabic/astral digits, where the engines and Python versions diverge.)
- `\s`: Python matches `[ \t\n\x0b\x0c\r]` plus `0x1c-0x1f` plus Unicode whitespace; Rust ASCII
  `\s` is `[ \t\n\x0b\x0c\r]`. They agree on an all-ASCII cell ONLY if `0x1c-0x1f` are excluded,
  hence the extra exclusion. (Round-2 proved `212\x1c555\x1c1234` diverges.)
- `\w`, `\b`, ASCII case-fold (`icd10` is case-insensitive): identical on all-ASCII cells; the
  divergences (combining marks in `\b`, Turkish `ı` case-fold) are all non-ASCII.

So on an eligible cell the eight Rust patterns, written with explicit ASCII classes
(`[0-9]`, explicit whitespace, `(?i)` restricted to ASCII via `(?i-u)` / explicit alternation),
match Python span-for-span. The predicate is a single O(len) scan (Rust `is_ascii()` on the
bytes plus a C0-separator check; or `str.isascii()` plus a separator check on the Python side).
The plan pins the predicate in one owner (2.3).

### 2.2 Why eligible-cell output is byte-identical (constructive, not corpus)

On an eligible cell, each of the eight Rust patterns is defined to be the same regular language
as the Python pattern restricted to the ASCII-safe alphabet, because every class the pattern
uses (`\d`, `\s`, `\w`, `\b`, ASCII case-fold) coincides between the engines on that alphabet
(2.1). None of the eight uses lookaround or relies on backtracking beyond greedy/alternation
that the `regex` crate reproduces (they have no `(?=)`/`(?<=)`; the lookaround ones are the
three Python-only detectors). Offsets are code-point counts (4.4). Therefore the Rust candidate
list for an eligible cell equals Python's `finditer`+validator list span-for-span. This is
checked exhaustively at the class level (7.4) and differentially (7.2), but it does not depend
on a corpus being "representative": the guarantee is the alphabet restriction plus the
routing predicate.

### 2.3 Kernel boundary, input contract, and the fallback owner (round-2 #2, #5)

New kernel in `decoy-engine-native`, exported via the companion's `decoy_engine_native._kernel`
surface (the package root does not auto-re-export). Python view:

    text_redact_candidates(
        values: Sequence[object],     # one column chunk, arbitrary Python objects
        detector_ids: Sequence[str],  # the eight lookaround-free ids, in the oracle's requested order
        catalog_version: int,         # detector-catalog contract id (4.6)
    ) -> list[list[tuple[int, int, int]] | None]   # per cell: candidates, OR None = "not eligible, Python must handle"

- The **native wrapper** (`native_text_redact`) owns routing. It keeps C6c-i's contract:
  non-null non-string cells are stringified first. For each cell the kernel returns either the
  eight-detector candidate list (eligible) or `None` (ineligible: non-ASCII, a C0 separator, a
  lone surrogate, or any non-string the kernel sees). `None` is an explicit per-cell fallback
  marker distinct from `[]` (= eligible, no match). The wrapper then runs the full Python
  `iter_spans` for the `None` cells and the three lookaround detectors (+ custom/NER) for the
  rest, and merges (4.3).
- Lone surrogates: the kernel cannot build a Rust `str` for them, so it returns `None` and the
  cell goes to Python. The Arrow-encoding boundary is unchanged and out of scope: a value that
  cannot be encoded to an Arrow string fails at the sink exactly as today, not here (round-2 #2).
- The kernel never re-applies the empty-means-all rule (the resolver owns it, C6c-i 3b).

## 3. Established methodology (cite in the kernel docstring)

- Multi-pattern scanning: the Rust `regex` crate per-pattern `find_iter` with Unicode disabled
  on the relevant classes (`(?-u)` / explicit ASCII classes), valid because the kernel only
  runs on the ASCII-safe domain (2.1). Linear-time; no `fancy-regex`.
- Validators port the executable behavior of `storm/_validators.py` (the reference), not an
  idealized standard: Luhn (ISO/IEC 7812-1) with the module's normalization, NPI (CMS Luhn over
  the 80840 prefix), ICD-10 chapter-range table, IBAN (ISO 13616 mod-97 incremental), IPv4
  octet range. On an eligible cell the matched text is ASCII, so the ASCII-input behavior of
  each validator is what must be ported (7.3).

## 4. Seam details

### 4.1 Where it plugs in

C6c-i's `native_text_redact` (`native/_kernels_scalar.py`) currently calls Python `iter_spans`
+ `_splice` per cell. C6c-ii inserts the routing + Rust call for the eight detectors on eligible
cells, keeping the Python path for ineligible cells and the three lookaround detectors. The
`OperatorSpec`, resolver, step branch, route contracts, chunked string pin, admission, and
evidence from C6c-i are unchanged (no registry/dispatch change).

### 4.2 (reserved)

### 4.3 The merge seam, exact oracle precedence (round-1 H1 — RESOLVED, confirmed by the gate's 576-case probe)

A named helper `merge_text_redact_spans(...)` (new, unit-tested) assembles the per-cell final
list in the EXACT order `iter_spans` builds it, so Python's stable sort resolves ties
identically; the helper returns immediately for empty text, before touching extras/custom
(round-2 #5):

    if not text: return []                                       # oracle early return
    candidates  = list(extra_spans)                              # NER, supplied order
    for det_id in detector_ids (oracle requested order):         # built-ins, requested order
        candidates += (Rust spans for det_id if eligible-cell and det_id in the 8
                       else Python finditer+validator spans for det_id), in finditer order
    candidates += custom-detector matches, supplied spec then finditer order
    candidates.sort(key=lambda s: (s.start, -(s.end - s.start))) # stable
    # leftmost-then-longest sweep (start >= last_end), incl zero-length custom spans, then _splice

On an eligible cell the 8 Rust detectors and the 3 Python lookaround detectors interleave by the
SAME requested-detector order, so moving 8 to Rust cannot change precedence. On an ineligible
cell the whole thing is the Python `iter_spans` path. The gate's two counterexamples (`"12345"`
us_zip vs injected `location` at `(0,5)`; `"2234567891"` npi/us_phone under both permutations)
are pinned as tie tests with `label_token=True` (7.2). NER is not routed through the native
operator (C6c-i rejects NER configs from native admission); the helper handles `extra_spans`
so it is correct wherever reused, but this slice makes no admission change (round-2 #7).

### 4.4 Code-point offset contract

Spans are code-point (Unicode scalar value) indices into the Python `str`, matching
`re.Match.start()/end()` and `_splice` slicing. On an eligible (all-ASCII) cell, byte offsets
equal code-point offsets, so the conversion is trivial and exact there; the kernel still returns
scalar-value counts so a direct call on any valid `str` is correct. No normalization, no
grapheme clustering. Ineligible cells never reach the Rust offset path.

### 4.6 Versioned detector catalog + fallback modes (round-2 #3, #6)

- One authoritative ordered catalog (id -> index, id -> label, id -> supported-in-Rust flag)
  lives in Python and is mirrored in Rust under a `catalog_version` integer that bumps when any
  detector's id, order, label, pattern, validator, OR supported flag changes (not just
  ids/labels). The loader passes `catalog_version`; the kernel rejects a mismatch and the loader
  falls back to full Python. An agreement test asserts Python and Rust catalogs match on ids,
  labels, order, and the supported set.
- A requested detector that the Rust catalog marks unsupported (e.g. a future detector not yet
  ported) runs in Python, never a silent unknown-id no-op (round-2 #3).
- Three fallback modes, each oracle-compared on the 7.2 corpus: (a) companion package absent;
  (b) `_kernel`/`text_redact_candidates` symbol absent (older companion); (c) `catalog_version`
  mismatch. Each uses the literal C6c-i Python path.

## 5. Build order (tests-first per [[plan-defines-acceptance-tests]])

1. **Acceptance tests first.** Commit the benchmark fixture (7.7); the routing-predicate
   boundary tests (7.1); the class-level ASCII-equivalence tests (7.4); the raw-candidate
   differential harness over eligible AND ineligible cells (7.2); validator KATs (7.3); merge
   tie + empty-text tests (4.3); fallback-mode + catalog-skew tests (4.6); mutation targets
   (7.6). They pass on the Python path and pin behavior; the ones needing the Rust binding are
   marked skip/xfail until step 2/3 (distinct from green-before oracle fixtures).
2. **Rust kernel.** `decoy-engine-native/src/text_redact.rs` (keep `lib.rs` under budget): the
   eight ASCII-class detectors, the eligibility predicate, five ported validators, scalar offset
   counts, the versioned catalog, surrogate/non-ASCII -> `None`, pyo3 binding via `_kernel`.
3. **Python swap.** `native_text_redact` routes per cell, runs the Python lookaround detectors +
   `None`-cell fallback, calls `merge_text_redact_spans`, splices.
4. **Gates.** Full existing text_redact suite green; differential + boundary + fallback +
   mutation green; re-measure throughput + ASCII hit rate (7.7).
5. **Docs.** CHANGELOG (internal: faster text_redact on ASCII-dominant free text, output
   unchanged, Python fallback for non-ASCII/separator/surrogate cells), kernel docstring with
   methodology cites, build record (measured speedup + hit rate), roadmap row.

## 6. Failure modes

- Ineligible cell (non-ASCII / C0 separator / lone surrogate / non-string) -> kernel returns
  `None` -> Python `iter_spans` for that cell, byte-identical.
- Companion absent / old symbol / catalog mismatch -> full Python fallback (4.6).
- Unknown detector id -> skipped as `iter_spans` does; an unsupported-but-known id -> Python.
- Validator rejects a match -> span dropped with ordinary `finditer` advancement retained.
- A Rust panic surfaces as a Python exception (pyo3), never a silent wrong result.

## 7. Acceptance tests (byte-identical is the bar)

**7.1 Routing-predicate boundary + golden.** Enumerate the predicate edge: an all-ASCII cell is
eligible; a cell with one non-ASCII char, or one `0x1c-0x1f`, or a lone surrogate is ineligible
and produces the oracle output via fallback. `tests/snapshots/golden/mask_text_redact/` stays
byte-identical; no golden re-recorded.

**7.2 Differential harness, raw candidates then merged, eligible AND ineligible cells.** For a
generated corpus (clinical-note style; every built-in type; multiple/overlapping PII incl. the
gate's SSN-in-NER-name and ZIP-adjacent-phone; no-PII prose; empty; null; non-string; leading/
trailing/repeated PII; and a Unicode/separator/surrogate subset that must route to Python):
assert the per-detector raw ordered candidate list (Rust vs Python) is identical on eligible
cells BEFORE merge, then assert merged spans + spliced string (labeled and unlabeled) equal the
oracle on every cell. Retain failing generated cases as fixtures. Enumerate detector
permutations and the two named tie counterexamples.

**7.3 Validator KATs.** Shared Python<->Rust vectors capturing executable behavior on ASCII
input: Luhn (thirteen zeroes pass; no upper-length cap; whitespace/hyphens stripped); NPI
(exact doubling parity; stripping); IPv4 (`"001.002.003.004"` passes); IBAN (exact country set,
15-34 length, whitespace removal, incremental mod-97); ICD-10 (`F00` false, `F01` true, `D89`
true, `D90` false; `"A00!!!!"` passes). (Non-ASCII validator inputs never reach Rust by routing,
but the KATs still pin the ASCII behavior the port must match.)

**7.4 Class-level ASCII equivalence.** For each Unicode construct the eight patterns use
(`\d`, `\s`, `\w`, `\b`, ASCII case-fold), a test enumerating every ASCII code point asserts the
Rust class and the Python class agree on the ASCII-safe domain and that `0x1c-0x1f` are the only
ASCII code points excluded. This is the constructive backing for 2.2.

**7.5 NER + custom interplay.** Unit-test `merge_text_redact_spans` with injected `extra_spans`
and custom specs across tie/overlap/empty-text cases; assert combined resolution + splice equals
the Python path.

**7.6 Integration + fallback + mutation.** Companion-enabled integration on BOTH native routes
with the Python detection path poisoned/instrumented, proving the wrapper actually called Rust
on eligible cells. The three fallback modes (4.6) + a catalog-skew case each compared to the
oracle. Mutation targets: routing predicate, candidate advancement, tie order, offset counts,
label mapping, empty-selection, catalog-version check.

**7.7 Perf (performance.md; committed fixture, representative + default workload, enforceable
floor).** Commit `scripts/bench_text_redact.py` with (a) a representative clinical-notes corpus
(ASCII-dominant, a realistic small fraction non-ASCII) and (b) an all-detector default config,
plus a reported ASCII-cell hit rate. Baseline is section 1. Report min/median and spread (not
mean-only) after a warmup, on the same host/release build, with peak RSS. **Enforceable floor:
no end-to-end regression vs the C6c-i Python path on ANY workload, including an all-ineligible
corpus (where routing overhead must be negligible).** Target (aspirational, reported honestly):
a material end-to-end speedup on the representative corpus (the 8/11 detectors moved on eligible
cells); scanner-only and end-to-end reported separately. Fallback is detected by execution
assertion (7.6), never timing.

## 8. Risks

- **R-predicate:** the eligibility predicate admits a cell where an engine class still diverges.
  Mitigation: 7.4 enumerates every ASCII code point per class; the predicate is derived from it.
- **R-merge-order / R-validator:** as rev 2; mitigated by 4.3 + 7.2 and 7.3.
- **R-catalog-skew:** 4.6 versioned catalog covering semantics + supported set, agreement test.
- **R-value:** the ASCII hit rate on real free text is lower than expected, shrinking the win.
  Mitigation: 7.7 measures it on a representative corpus and reports honestly; the floor
  guarantees no regression even at a 0% hit rate.
- **R-scope:** `lib.rs` budget -> new `text_redact.rs`.

## 9. Round 1-2 findings and resolutions

- R1 H1 merge precedence -> 4.3 (gate-confirmed, 576 cases). R1 H2 lookaround -> the three stay
  Python (2). R1 H3 / R2 #1 Unicode equivalence -> ASCII-domain routing makes it constructive
  (2.1/2.2/7.4), not corpus-dependent; the eligible detector set is fixed (8), not build-decided.
- R2 #2 surrogate/fallback owner -> explicit `None` per-cell marker + wrapper ownership (2.3);
  Arrow-encoding boundary explicitly out of scope. R2 #3/#6 catalog -> version covers semantics +
  supported set; unsupported-but-known id runs in Python (4.6). R2 #4 perf -> enforceable
  no-regression floor + representative/default workload + hit rate (7.7). R2 #5 empty-text early
  return -> 4.3.

Open for round 3: confirm the ASCII-safe predicate (2.1) and the `0x1c-0x1f` exclusion are the
complete set of ASCII divergences for these eight patterns; confirm the enforceable floor (7.7)
is the right perf bar given the value depends on a measured hit rate.
