Status: record (completion slice done; awaiting the orchestrator's code-level Dennis -> Codex gate on
the pushed branch — no cross-model CODE review run here)

# C6c-ii follow-up build record: porting the three lookaround detectors into the Rust kernel

Plan: `docs/plans/2026-10-09-c6c-ii-text-redact-rust-kernel.md` (the lookaround port is the "possible
later slice" named in plan section 2). Builds on the C6c-ii record
`docs/records/2026-10-09-c6c-ii-text-redact-rust-kernel-build.md`.
Branch `claude/text-redact-detection-speedup` off engine main `b63be00`.

**Completion (A-7 remote slice, 2026-10-28, plan
`docs/plans/2026-10-28-a7-text-redact-speedup-completion-remote.md`).** Rebased onto current engine
`main` (the physical-seam sentry's allowlist for the changed native modules preserved; the sentry
RAN and PASSED). Rebuilt the companion and re-ran the byte-parity gate with an explicit
companion-present preflight (0 skips). Added the vocabulary + pattern-agreement test (plan 7) and
fixed the perf harness to measure the SHIPPED 8-detector kernel vs the 11-detector port with
run-to-run variance + tail (plan 4.1). Detection is unchanged (byte-identical, perf-only); the
Byte-parity evidence and Perf matrix sections below are updated accordingly.

## Goal

`text_redact` masking ran ~8x slower than the other native kernels because its Rust span kernel
handled only the eight lookaround-free detectors on ASCII-safe cells and fell back to the Python
oracle for the three lookaround detectors (`ssn`, `us_zip`, `street_address`). On an eligible cell
those three still ran a Python `finditer` per detector during the merge, which dominated the
per-cell cost. This slice moves all three into the Rust kernel so an eligible cell does no Python
per-detector scanning at all — closing the tractable half of the gap. Byte-parity with the Python
oracle is preserved exactly (the hard gate).

## What shipped

The Rust catalog grew from eight to eleven supported detectors; `CATALOG_VERSION` bumped `1 -> 2`
(the supported set changed, per plan 4.6). On an ASCII-safe cell all eleven built-in detectors now
run in the companion; non-ASCII / 0x1c-0x1f-separator / lone-surrogate / non-string cells still take
the full Python path, byte-identical. No config, routing, admission, evidence or golden-snapshot
change; this is an internal throughput change on both the `text_redact` and the keyed `text_mask`
native paths (they share the kernel and catalog).

### The three ported detectors are hand-written ASCII byte scanners, not a regex engine

`fancy-regex` was implemented first (it supports the lookahead/lookbehind these patterns use) and
then **rejected on measured performance**: its backtracking VM is 1.6–2.6x *slower* than Python's C
`re` on these patterns (micro-bench over 20k clinical cells: street_address 0.573s vs Python 0.217s;
ssn 0.230s vs 0.092s; us_zip 0.142s vs 0.090s). That erased the native win (the full 11-detector
kernel came out ≈ break-even with pure Python) and **failed the plan's latency floor** on the
lookaround-only workload: `R-python-only` regressed to 0.45x (new 14.2s vs base 6.4s, 2.23x > the
1.05x floor). The fancy-regex approach is recorded here only as the rejected alternative.

The shipped implementation is pure-Rust byte scanners in `decoy-engine-native/src/text_redact.rs`
(`scan_ssn` / `scan_us_zip` / `scan_street` + helpers), each reproducing its Python pattern's exact
`re.finditer` semantics — leftmost, non-overlapping, greedy with the same ordered-alternation
precedence — directly over ASCII bytes:

- **ssn** `(?!000|666|9\d{2})\d{3}-?(?!00)\d{2}-?(?!0000)\d{4}`: fixed-shape; the two optional
  hyphens are greedy but non-backtracking (a present hyphen must be consumed because the next field
  is digits), so each start position is a direct test.
- **us_zip** `(?<!\w)\d{5}(?:-\d{4})?(?!\w)(?!\.\d)`: the non-word lookbehind/lookahead and the
  decimal-digit guard; the greedy `-\d{4}` extension is tried first with the five-digit form as the
  backtrack fallback (the `12345-67890` case exercises that fallback).
- **street_address** `\b\d{1,6}\s+(?:[A-Za-z0-9][A-Za-z0-9'.\-]*\s+){1,4}(?:SUFFIX)\.?`
  `(?:\s+(?:UNIT)\.?\s*[\w\-]+)?(?=[\s,.;:!?)"']|$)` (IGNORECASE): the only genuinely backtracking
  one. The greedy `{1,4}` street-name repetition backtracks over the iteration count (most first),
  then the 47-entry USPS suffix alternation (listed order), then dot present-before-absent, then
  unit present-before-absent, exactly matching Python's backtrack order so the span end lands
  identically.

### Why the ASCII predicates equal Python's classes

The scanners run ONLY on an eligible cell (every code point < 0x80 and none in 0x1c-0x1f), where
byte offsets equal code-point offsets and the byte predicates coincide with Python's `re` classes:
`\d` is `[0-9]`, `\w` is `[0-9A-Za-z_]`, `\b` is the transition between them, ASCII case-fold is
`A-Z`<->`a-z`, and `\s` is `[ \t\n\x0b\x0c\r]` — Python `\s` additionally matches 0x1c-0x1f, the
exact code points the eligibility predicate excludes (the existing class-enumeration test 7.4 pins
that this is the only `\s` divergence). The `$` in street_address's trailing lookahead only fires at
true end-of-string (a trailing `\n` is caught by the `\s` alternative first), so the Rust vs Python
end-of-haystack `$` difference is unobservable here.

### Shared-kernel blast radius (text_mask)

`text_mask.rs`'s `detect_splice_cell` / `text_mask_detect_splice` index a fixed-size per-detector
`verbatim` array by catalog index; it was `[bool; 8]` and is now `[bool; CATALOG_LEN]`
(`= SUPPORTED_IDS.len() = 11`), so the keyed path does not panic on the new indices and benefits
from the port too. Because every built-in span detector is now Rust-supported, `plan_native_splice`
resolves every built-in redact/passthrough config to the one-call `text_mask_detect_splice` path;
the merged-span path (`text_mask_splice_spans`) is retained for a hypothetical future Python-only
detector but is no longer triggered by any built-in. `NATIVE_FPE_DETECTORS` and the FPE hybrid path
are unaffected (they key off `_FPE_CONFIG` and span detector_ids, not the detection source), so FPE
output is unchanged.

### Pieces

- `decoy-engine-native/src/text_redact.rs`: `SUPPORTED_IDS` (8 -> 11, the three appended so the
  existing indices are stable), `CATALOG_VERSION` (1 -> 2), `CATALOG_LEN`, `LOOKAROUND_FREE_COUNT`,
  the three scanners + helpers (`is_ascii_word`, `is_ascii_space`, `ascii_ci_eq`, `STREET_SUFFIXES`,
  `STREET_UNITS`), and `candidates_for_cell` dispatching regex-crate (idx < 8) vs scanner (idx >= 8).
- `decoy-engine-native/src/text_mask.rs`: the `[bool; 8] -> [bool; CATALOG_LEN]` change.
- `decoy-engine-native/Cargo.toml`: no new dependency (fancy-regex removed after rejection).
- `src/decoy_engine/execution/native/_text_redact_kernel.py`: `SUPPORTED_IDS` (11), `CATALOG_VERSION`
  (2). The merge helper and loader are unchanged in logic — they already handled any supported id
  generically.
- `src/decoy_engine/execution/native/_text_mask_native_splice.py`: docstring only (the routing logic
  already keyed off `_SUPPORTED_SET`).

## Byte-parity evidence (the hard gate)

- **Rust unit tests** (`cargo test --no-default-features`): 109 lib tests pass, including new
  per-detector scanner tests for ssn (SSA reserved blocks, hyphenated + bare), us_zip (embedded in
  word/number tokens rejected, zip+4), and street_address (house number + USPS suffix + trailing
  punctuation, abbreviated case-insensitive forms, the `\.?` dot consumption).
- **Python differential vs the oracle** (`tests/native/test_c6c_ii_text_redact_rust.py`, companion
  present): 187 pass, 0 skipped (the completion slice added the preflight + vocabulary/pattern-
  agreement tests below). New tests:
  - `test_lookaround_raw_candidates_match_python_on_an_adversarial_corpus`: 4,000 generated
    ASCII cells stressing each ported detector's boundary semantics (reserved SSN blocks, optional
    hyphens, zip word/decimal guards, house-number/suffix/unit/punctuation shapes, plus raw random
    ASCII runs). Asserts the kernel's per-detector raw candidate list equals Python `finditer`
    span-for-span on every cell — before any merge.
  - `test_lookaround_merged_output_matches_oracle_on_the_adversarial_corpus`: 3,000 cells, the
    lookaround-only and all-detector selections, labeled and unlabeled, end-to-end vs the oracle.
  - `test_preflight_companion_present_kernel_loads_with_expected_catalog` (completion-plan 9): a
    companion-present preflight that shares the exact `load_text_redact_kernel()` condition every
    `@NEEDS_TR_KERNEL` test guards on, so a loaded kernel here proves the byte-parity gate RAN and
    did not silently skip. With `DECOY_REQUIRE_TR_KERNEL=1` an absent/old/catalog-skewed companion
    is a FAILURE, not a skip; it pins the expected `CATALOG_VERSION` + ordered `SUPPORTED_IDS` on
    both the Python and the compiled side.
  - `test_the_oracle_patterns_and_flags_match_the_pinned_scanner_contract` +
    `test_the_rust_street_vocabularies_reconstruct_the_oracle_pattern` + per-alternative
    differentials (completion-plan 7): a frozen snapshot of every detector's oracle regex body +
    flags (a one-sided boundary/quantifier/lookahead/flag edit fails CI and forces a coordinated
    `CATALOG_VERSION` bump), the Rust `STREET_SUFFIXES`/`STREET_UNITS` vocabularies exposed from the
    companion and asserted to reconstruct the oracle's suffix/unit alternations, and targeted
    differentials for every suffix + unit alternative (plain and optional-dot) and street-name
    repetition backtracking (the random corpus draws mostly from fixed examples).
  - The existing per-detector raw-candidate differential (`RUST_DETECTORS` now = all eleven) and the
    full corpus × selection × label matrix re-run byte-identical.
- **Shared-kernel regression (text_mask / FPE / parity / physical / e2e), companion present**: the
  text_mask splice + detection + FPE suites, the out-of-core text_redact native routing/real
  suites, the physical shadow FPE suite, the operator-table snapshot, and the text_redact e2e all
  pass. Three tests that asserted the *old* "ssn stays Python" routing were updated to the new
  contract (ssn/us_zip/street_address now reach the Rust candidate call / detect-splice path); their
  byte-parity assertions are unchanged and still green.

## Perf matrix (plan 7.7 / completion-plan 4.1), `scripts/bench_text_redact.py`

**Baseline correction (completion-plan 4.1).** The earlier harness forced
`load_text_redact_kernel = lambda: None`, so its "baseline" was FULL PYTHON, not the shipped kernel;
the numbers it printed (R-representative 3.69x) were a full-Python comparison mislabelled as a
shipped-kernel one. A regression against what actually ships could have hidden under a green run. The
harness now measures three paths on an identical fixture, host and release build, each in its own
subprocess for isolated peak RSS:

- **baseline8** — the SHIPPED path: the eight lookaround-free detectors in the compiled kernel, with
  `ssn`/`us_zip`/`street_address` on the per-cell Python scan. Reproduced from the same build by
  narrowing the Rust-supported set to the eight (so the three lookaround ids route to
  `merge_text_redact_spans`'s Python `finditer`), exactly the pre-port routing. This is the baseline
  the port must not regress against.
- **new11** — the lookaround-port path: all eleven detectors in the compiled kernel.
- **python** — full Python. REPORTED ONLY, never gated.

The acceptance guard (latency floor `median_new11 <= 1.05 * median_baseline8`, RSS budget
`peak_rss_new11 <= 1.20 * peak_rss_baseline8`) is applied to **new11 vs baseline8** every row. Before
timing, the driver asserts baseline8, new11 and full Python redact BYTE-IDENTICALLY on a 3,000-cell
representative + 3,000-cell lookaround-only sample, so the three paths are measured doing the same
work.

200,000 rows, 10 reps + 1 warmup (the completion plan sanctions 200k with >= 5 reps over 1M with 3,
because a credible run-to-run stdev/tail needs reps, and per-cell throughput extrapolates linearly to
1M). Variance is RUN-TO-RUN (stdev over reps / median), NOT per-cell; tail is the observed max/median
(a handful of reps cannot support a reliable p95/p99, so an observed tail is reported instead):

```
row                base8 med(s)  new11 med(s)  x vs base8  x vs py  lat<=1.05  rss<=1.20  new11 stdev  new11 tail  hit%
R-representative        12.3042        7.1499      1.72x     3.94x        OK         OK         8.3%       1.23x   95.0%
R-all-ineligible        28.7167       27.9805      1.03x     1.00x        OK         OK         6.3%       1.14x    0.0%
R-python-only            8.9844        3.1344      2.87x     2.83x        OK         OK         7.6%       1.14x   95.0%
R-short-no-match         0.8898        0.6934      1.28x     1.95x        OK         OK        12.8%       1.22x  100.0%
```

Headline, honestly scoped: against the SHIPPED 8-detector kernel the representative workload is
**1.72x** and the lookaround-only selection (the direct measure of the port — those three detectors
previously ran in Python even on the native path) is **2.87x**. The full-Python speedup, reported for
context only, is 3.94x representative. `R-all-ineligible` (every cell non-ASCII, 0% hit) is 1.03x:
the port adds no measurable overhead on the worst-case routing path, well inside the latency floor.
RSS stays within budget every row (representative: new11 460 MB vs baseline8 442 MB = 1.04x). The
rejected fancy-regex variant, for contrast, measured R-representative 1.08x and R-python-only 0.45x
(FAIL) against full Python.

Variance/tail: run-to-run stdev is 6.3–12.8% and the observed tail (max/median) is 1.14–1.23x on this
shared cloud container; the fixed-overhead rows (short-no-match) show the widest relative spread, as
expected for sub-second medians. The gate margins (1.72x / 2.87x headroom over a 1.05x floor) are far
larger than this dispersion, so the PASS verdicts are not noise-sensitive. At this rate new11 runs the
representative workload at ~28.0k rows/s single-thread (200,000 / 7.15 s); linear extrapolation puts
1M at ~35.7 s (the earlier 1M figure of 25.97 s was measured on a less-contended host and against the
mislabelled full-Python baseline, so it is not directly comparable).

Reproduce with `python scripts/bench_text_redact.py --rows 200000 --reps 10` (or `--rows 1000000
--reps 10` where time allows); `--equivalence-check` runs just the byte-parity pre-check.

### ASCII-hit-rate assumptions

The value depends on the ASCII-cell hit rate of real free text. The representative fixture
(clinical-notes-style, ~5% non-ASCII fraction) measures a 95.0% hit rate; at that rate 95% of cells
take the all-Rust path and 5% fall back to Python. The latency floor guarantees no regression even
at a 0% hit rate (R-all-ineligible: 1.03x vs the shipped kernel). The reported speedup scales with
the hit rate.

## Companion build in this environment

The compiled companion builds and loads here. Toolchain: `python3.12` venv with `pip install -e
.[dev]`; Rust pinned `1.98.0` (per `rust-toolchain.toml`) installed via rustup; `maturin` 1.15.0;
built with `maturin develop --release` into the venv. `cargo test --no-default-features` (109 lib +
integration tests), `cargo fmt --check`, and `cargo clippy --all-targets -- -D warnings` are green.
The full byte-parity acceptance (`DECOY_REQUIRE_TR_KERNEL=1 pytest tests/native/
test_c6c_ii_text_redact_rust.py -m "perf or not perf"`) ran 187 passed / 0 skipped, and the
physical-seam sentry `test_production_execution_modules_are_byte_identical_to_origin_main` RAN and
PASSED (not skipped — `origin/main` was fetched first). `ruff check`/`ruff format --check` are green;
`mypy src/decoy_engine testflight` is green (597 files). A freshly-resolved newer numpy/`deprecated`
ships PEP 695 `type` stubs that the pinned 3.10 mypy target rejects before reaching project code, so
the local run used `--python-version 3.12` to get past third-party stubs; CI pins compatible versions
and runs at 3.10.

## Judgment calls / for the reviewer to scrutinise

- **Hand-written street_address scanner is the highest-risk piece.** It is the only ported detector
  with genuine backtracking (the `{1,4}` street-name repetition interacts with the suffix match).
  The implementation tries iteration counts most-first, then the suffix alternation in listed order,
  then dot/unit present-before-absent, to mirror Python's backtrack order and land the same span
  end. The 4,000-cell raw-candidate differential + the 3,000-cell merged differential + the golden
  clinical notes are the empirical gate; a cold read of `scan_street`/`street_rest`/`street_unit`
  against `_STREET_ADDRESS_RE` is still worth it. If a divergence is ever found, the fail-closed
  move is to REVERT the port (or force the full-Python path for all three ported detectors), NOT a
  surgical single-detector drop (completion-plan 6): `scan_lookaround` dispatches FIXED indices
  (8 -> ssn, 9 -> us_zip, 10 -> street), so dropping `ssn` or `us_zip` from `SUPPORTED_IDS` shifts
  the later indices without changing that dispatch and a remaining detector then invokes the wrong
  scanner while the catalog-agreement test still passes. A kept selective fallback must, in one
  change, update the index dispatch, update BOTH `SUPPORTED_IDS` arrays (Rust + Python), bump
  `CATALOG_VERSION`, and re-verify the remaining detectors AND `text_mask` against their oracles.
- `SUPPORTED_IDS` appends the three (indices 8/9/10) rather than reordering, so the existing eight
  keep their indices; only the catalog version and the supported set changed.
- The u32-offset deferral from the original C6c-ii record still applies (a single > 2^32-code-point
  cell would wrap; not reachable under Arrow's string cap). The scanners use `usize` internally and
  cast to `u32` at the push, same as the regex-crate path.

## Not done (out of scope / deferred)

- The STRETCH (widening the Rust path beyond ASCII-safe cells toward full Unicode) is NOT attempted:
  it needs per-Python-version Unicode-table care and is only provable byte-identical with
  significant extra work; the plan says defer if not provably byte-identical.
- No cross-model review was run (per the head-start instructions).
- The cross-repo roadmap row (`decoy-platform/docs/ROADMAP.md`) is in a different repo and not
  updated here.

## Coordination note

A separate branch is renaming `src/decoy_engine/storm/` -> `detection/`/`profiling/`/`verification/`
(the STORM split). This work was done against the `storm/` paths as they exist on `origin/main`
today (`_patterns.py`, `detectors.py`, `_validators.py`); the reviewer will need to reconcile the
rename.
