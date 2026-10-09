# C6c-ii: a Rust span-detection kernel for text_redact

Status: plan (rev 1, awaiting Codex plan gate). Phase C, Rust-engine program.
Branch `feat/c6c-ii-text-redact-rust` off engine main `90e36c38`.
Supersedes the C6c-ii sketch in `docs/plans/2026-10-06-c6c-i-text-redact-arrow.md`.

Rules consulted: 00-universal, risk-and-exceptions, development-loop, autonomous-operation,
feature-dev, testing, performance, architecture, api-and-compatibility, documentation.

## 1. Why

C6c-i admitted `text_redact` to both native routes as an `ARROW_PYTHON` operator, so
a text_redact column no longer forces its whole table onto pandas. But the column's
own detection still runs the Python per-cell span logic, unchanged. Measured on the
dev box (2026-10-09, `scratchpad/text_redact_timing.py` + `text_redact_profile.py`):

- **~32k rows/s single-thread, ~31us/cell**, stable from 100k to 1M rows.
- Extrapolated **10M ~= 5.2 min; 100M ~= 52 min**, far past the 600s/100M mask-only
  contract. Any table with a text_redact column at scale is bottlenecked here.
- cProfile of `TextRedactHandler.run` over 200k cells: **`iter_spans` Python body
  66.8%**, validators (Luhn/NPI/ICD-10) ~9%, the regex engine itself (`finditer`/`sub`)
  only ~7-10%, pandas + splice ~17%.

The cost is the **per-cell loop over ~11 detectors** (for each detector: a `finditer`
pass, a `Span` object per match, a validator dispatch, a list append), then a sort and
an overlap merge. The regex *scanning* is cheap; the Python *orchestration* around it
is the bottleneck. So accelerating only the scan (the original "regex-scan only" idea)
would remove ~10%, not the ~8x gap to the native kernels (~267k rows/s). The win needs
the whole per-cell detection loop in Rust. (Cam 2026-10-09, after the measurement:
build the full detection loop in Rust, port the validators too.)

A single fused Python alternation regex is **not** a shortcut: each detector
independently contributes candidate spans that the final leftmost-then-longest merge
resolves, so fusing changes which spans are found and breaks byte-identity. Rust is the
right tool because the 11-pass-per-cell structure is intrinsic.

## 2. Goal and non-goals

**Goal.** Replace the Python per-cell built-in-detector loop inside the `native_text_redact`
operator with a Rust kernel that runs the 11 built-in detectors + their validators and
returns the raw validated regex spans per cell. Output stays **byte-identical** to the
pandas oracle (`storm.detectors.iter_spans` + `_text_redact._splice`), including when
NER and custom detectors are present. Keep the `regex` crate (Cam). Target: text_redact
detection throughput from ~32k rows/s toward the native-kernel band; the acceptance bar
is a measured multiple on the same fixture (section 7), not a guess.

**Non-goals.**
- NER (ML) span production stays in Python (`storm.ner.iter_ner_spans`); it is not a
  regex kernel.
- Custom one-shot detectors (`custom` kwarg, arbitrary `re.Pattern`) stay in Python.
- The final overlap merge and the splice stay in Python (cheap; and the merge is where
  NER/custom spans inject, see 4.3). A later slice may move the splice to Rust.
- No new public config. `detectors`, `token`, `label_token` are unchanged.
- The structured/profiler detector path (`_evaluate`, column-level) is out of scope;
  this slice touches only span-level text_redact.

## 3. Established methodology (cite in the kernel docstring)

- Multi-pattern scanning: the Rust `regex` crate `RegexSet` + per-pattern `find_iter`
  (the crate's documented multi-pattern approach; linear-time, no catastrophic
  backtracking). This is why we keep the `regex` crate rather than `fancy-regex`.
- Check-digit validators port the standard algorithms: Luhn (ISO/IEC 7812-1), NPI
  (CMS check-digit, Luhn over the 80840 prefix), ICD-10-CM structural rule, IBAN
  (ISO 13616 mod-97), IPv4 octet range. The Python functions in `storm/_validators.py`
  are the reference; Rust ports are locked to shared test vectors (7.3).

## 4. Architecture (Cam-approved 2026-10-09)

### 4.1 The Rust kernel boundary

New kernel in the companion crate `decoy-engine-native` (pyo3), exposed as
`decoy_engine_native.text_redact_spans`. Signature (Python view):

    text_redact_spans(
        values: list[str | None],         # one chunk of the column, code-point strings
        detector_ids: list[str] | None,   # None = all built-ins, already normalized by the resolver
    ) -> list[list[tuple[int, int, int]]] # per cell: (detector_index, start, end), code-point offsets

- Returns **raw, validated** regex spans only: each built-in detector is run, its
  validator (if any) applied, and surviving `(detector_index, start, end)` emitted. No
  sort, no overlap merge, no splice, no token. `detector_index` maps to a fixed ordered
  detector-id table shared with Python (so labels and merge ordering match exactly).
- Offsets are **code-point** indices into the Python `str`, not UTF-8 byte offsets, so
  Python can splice with ordinary `str` slicing identically to the oracle (4.4).
- `None`/non-string cells return `[]` (oracle: `iter_spans` returns `[]` for non-str or
  empty; the kernel mirrors it so a direct call is correct).

### 4.2 Where it plugs in

C6c-i's `native_text_redact` kernel (`native/_kernels_scalar.py`) currently calls the
Python `iter_spans` + `_splice` per cell. C6c-ii swaps the **built-in-detector** half
for `text_redact_spans`, leaving the `OperatorSpec`, resolver, step branch, route
contracts, and chunked string pin from C6c-i untouched. No registry/dispatch change.
Both native routes already reach this kernel via `run_kernel_step`.

### 4.3 The Python merge seam (semantics-preserving)

Per cell, Python assembles the final span list exactly as `iter_spans` does today, over
the combined raw set:

    raw = [Span from each (det_idx, s, e) returned by the kernel]   # built-ins, validated
        + extra_spans (NER, if any)                                 # unchanged Python/ML
        + [Span from each custom-detector match]                    # unchanged Python
    raw.sort(key=lambda s: (s.start, -(s.end - s.start)))           # one combined sort
    # leftmost-then-longest sweep -> non-overlapping spans -> _splice

This is the key correctness point: the overlap resolution runs **once over all span
sources together**, so a long NER person-name span can still suppress a built-in span
and vice versa, exactly as today. The kernel never pre-merges, so moving built-in
detection to Rust cannot change the resolution outcome. The per-cell Python work drops
from O(11 detectors) to O(raw matches + NER + custom), which the profile shows is small.

When `detectors` excludes a built-in, the resolver passes the reduced id list; the
kernel runs only those. When `custom`/`extra_spans` are absent (the common path), the
Python side is just "build spans from the kernel output, sort, sweep, splice".

### 4.4 Code-point offset contract

The oracle uses `re.Match.start()/end()` (code-point indices) and splices with Python
`str` slicing. The Rust `regex` crate returns UTF-8 **byte** offsets. The kernel converts
byte offsets to code-point offsets before returning (a single pass per cell building a
byte->char index map, or matching over a char-aware view). Acceptance tests cover
multi-byte UTF-8 (accented Latin, CJK), astral-plane emoji, and combining marks so an
off-by-one in the conversion is caught (7.2).

### 4.5 Lookaround reformulation (the three detectors the `regex` crate cannot express)

The `regex` crate has no lookaround. Three built-in patterns use it and must be
reformulated lookaround-free, each with a differential test proving byte-identical spans
vs the Python pattern over a targeted corpus (7.4):

- **ssn** `(?!000|666|9\d{2})\d{3}-?(?!00)\d{2}-?(?!0000)\d{4}`: match the shape
  `\d{3}-?\d{2}-?\d{4}`, then reject in Rust the excluded area (000/666/900-999), group
  (00) and serial (0000) values. The rejection is a post-match check, not regex.
- **us_zip** `(?<!\w)\d{5}(?:-\d{4})?(?!\w)(?!\.\d)`: the `\w` boundaries become explicit
  boundary handling (`regex` `\b` is not identical to `(?<!\w)`/`(?!\w)` at string edges,
  so the port checks the preceding/following char in Rust); the `(?!\.\d)` becomes a
  post-match check that the next two chars are not `.` + digit.
- **street_address** trailing `(?=[\s,.;:!?)\"']|$)`: match without the lookahead, then
  in Rust require the following char to be one of the boundary set or end-of-string,
  trimming the span end accordingly so the emitted `end` matches the oracle exactly.

Each reformulation is validated two ways: (a) the differential corpus test (7.4), and
(b) the existing golden snapshots (7.1), which already exercise these detectors.

### 4.6 Companion-absent fallback (fail-safe, byte-identical)

The companion is optional (the shared `.venv` lacks it; CI installs it). When
`decoy_engine_native` or `text_redact_spans` is unavailable, `native_text_redact` falls
back to the existing Python `iter_spans` path (C6c-i behavior), byte-identical. A sentry
test runs the fallback with the companion import forced off and asserts identical output
to the companion path on the golden corpus. This matches the existing
companion-independent pattern (e.g. `native_date_shift`).

## 5. Build order (tests-first per [[plan-defines-acceptance-tests]])

1. **Acceptance tests first (red).** Land section 7's differential harness, validator
   KATs, lookaround-reformulation corpus, unicode-offset corpus, and the
   companion-absent sentry against the current Python path (they pass on Python, then
   guard the Rust swap).
2. **Rust kernel.** `text_redact_spans` in a new `decoy-engine-native/src/text_redact.rs`
   module (keep `lib.rs` under its size budget): the 11 detectors as a `RegexSet` + per-
   pattern finders, the 5 validators ported, byte->code-point offset conversion, the
   three reformulated patterns. pyo3 binding + the fixed detector-index table shared with
   Python.
3. **Python swap.** `native_text_redact` calls `text_redact_spans` for built-ins, builds
   the combined span list (4.3), keeps sort + sweep + splice. Companion-absent fallback.
4. **Parity + perf gates.** Full existing text_redact suite green; differential harness
   green; re-measure throughput on the section-1 fixture.
5. **Docs.** CHANGELOG (internal: faster text_redact, output unchanged), kernel docstring
   with the methodology cites, build record, roadmap row.

## 6. Failure modes and fail-closed behavior

- Companion absent/old -> Python fallback, byte-identical (4.6).
- Unknown detector id -> skipped, exactly as `iter_spans` (`_SPAN_DETECTORS.get` is None).
- Empty detector list after normalization -> the resolver already maps `[]`/`None` to
  "all built-ins" (C6c-i owns this; the kernel does not re-apply it, so a resolver mutant
  is observable, per C6c-i 3b).
- A validator rejecting a match -> span dropped, identical to Python.
- Non-string / null / empty cell -> `[]`.
- A Rust panic must surface as a Python exception (pyo3), never a silent wrong result;
  the kernel has no `unwrap` on cell data.

## 7. Acceptance tests (byte-identical is the bar)

**7.1 Golden snapshots.** `tests/snapshots/golden/mask_text_redact/` stays byte-identical;
no golden is re-recorded (there is no intended output change).

**7.2 Differential harness (Rust vs Python oracle).** For a corpus of free-text cells -
clinical-note style, every built-in PII type, multiple PII per cell, overlapping types
(SSN inside a longer NER name span; ZIP adjacent to a phone), no-PII prose, empty, null,
non-string, leading/trailing PII, repeated PII - assert `text_redact_spans` + the Python
merge == `iter_spans` span-for-span (detector id, start, end), then assert the final
spliced string is identical. Unicode subset: accented Latin, CJK, astral emoji,
combining marks, to pin the offset conversion (4.4).

**7.3 Validator KATs.** Shared Python<->Rust test vectors for Luhn, NPI, ICD-10, IBAN,
IPv4: known-valid and known-invalid values, including the boundary cases the Python
validators special-case. A wrong port is caught here before the differential harness.

**7.4 Lookaround-reformulation equivalence.** For ssn, us_zip, street_address: a targeted
corpus of accept/reject strings (excluded SSN areas/groups/serials; ZIP at string edges,
ZIP followed by `.5`, ZIP inside a word; street addresses with each trailing boundary
char and at end-of-string) asserting the reformulated Rust pattern yields the identical
span set to the Python lookaround pattern.

**7.5 NER + custom interplay.** With injected `extra_spans` (NER) and a `custom` detector,
assert the combined resolution + splice is identical to today's Python path. Proves the
seam (4.3) preserves the single combined merge.

**7.6 Companion-absent sentry.** Force the companion off; assert byte-identical output to
the companion path on the 7.2 corpus.

**7.7 Perf (per performance.md: baseline, warmup, variance, correctness guard).** The
baseline is section 1 (~32k rows/s, ~31us/cell). Re-measure the Rust path on the same
fixture with a warmup pass and enough runs to report min/median and run-to-run spread
(not mean-only), at 100k/1M, with peak RSS alongside throughput. Record the numbers in
the build record; the correctness guard is 7.1-7.6 (byte-identical), so a throughput win
is only counted with parity intact. Add a loose `@pytest.mark.perf` floor for the Rust
path (calibrated ~2x headroom like the existing budgets) so a silent drop back to the
Python path is caught as a regression.

## 8. Risks

- **R-offset:** byte<->code-point conversion off-by-one on multibyte text. Mitigation:
  7.2 unicode corpus; the conversion is the first thing the differential harness stresses.
- **R-lookaround:** a reformulation is subtly non-equivalent at a string edge. Mitigation:
  7.4 edge corpus + 7.1 goldens.
- **R-validator:** a ported check-digit diverges on a boundary value. Mitigation: 7.3 KATs
  with the Python function as reference.
- **R-scope:** `lib.rs` size budget. Mitigation: new `text_redact.rs` module.
- **R-NER-seam:** the combined merge must stay a single pass. Mitigation: the kernel never
  merges; 7.5 proves it.

## 9. Open points for the Codex plan gate

- Confirm the raw-spans-to-Python boundary (4.1/4.3) is the right cut vs doing the merge
  in Rust and passing NER spans down. (This plan keeps the merge in Python so NER/custom
  injection is unchanged and the merge stays a single combined pass.)
- Confirm the code-point offset contract (4.4) against how `_splice` indexes today.
- Confirm the fixed detector-index table is the right shared contract for labels
  (`label_token`) and merge ordering, vs returning detector-id strings from Rust.
- Whether the splice should also move to Rust in this slice or stay a follow-up
  (profile: splice ~5%).
