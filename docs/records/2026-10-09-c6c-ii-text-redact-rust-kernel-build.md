Status: record

# C6c-ii build record: a Rust span-detection kernel for text_redact

Plan: `docs/plans/2026-10-09-c6c-ii-text-redact-rust-kernel.md` (rev 4, BUILD-READY).
Branch `feat/c6c-ii-text-redact-rust` off engine main `90e36c38`.

## What shipped

The native `text_redact` operator now detects PII spans with per-cell ASCII-domain routing. On a
cell in the ASCII-safe domain (every code point below 0x80 AND none in 0x1c-0x1f), the eight
lookaround-free detectors run in the compiled companion; every other cell and the three lookaround
detectors stay on the unchanged Python path. The spliced output is byte-identical to the C6c-i
oracle on every input. Config, routing, admission, evidence and the golden snapshots are unchanged
(no registry or dispatch change); this is an internal throughput change.

### Eligible vs Python-only detector split

- Rust on eligible cells (the eight lookaround-free detectors): `email`, `us_phone`, `pan`,
  `iban`, `ipv4`, `icd10`, `npi`, `url`. Each runs as a `regex` crate `find_iter` with ASCII
  classes (`(?-u)` / `(?i-u)` for icd10's case-insensitivity) plus its ported validator.
- Python always (the three lookaround detectors): `ssn`, `us_zip`, `street_address`. Their
  patterns use lookaround/backtracking a lookaround-free engine cannot reproduce; they inject as
  candidates through the merge helper on every cell.
- Python fallback (ineligible cells): any cell that is non-ASCII, carries a 0x1c-0x1f separator,
  is a lone surrogate, or is a non-string runs the full Python `iter_spans` for all eleven
  detectors. The kernel returns `None` for such a cell (distinct from `[]` = eligible, no match).

No detector beyond the planned three had to stay in Python. The split is exactly the plan's.

### Pieces

- `decoy-engine-native/src/text_redact.rs`: the eight ASCII-class detectors (compiled once via
  `OnceLock`), the eligibility predicate, the five ported validators (Luhn, NPI, IPv4, IBAN
  incremental mod-97, ICD-10 chapter range), code-point (= byte, on ASCII) offsets, the versioned
  catalog (`CATALOG_VERSION = 1`, `SUPPORTED_IDS`), and the PyO3 surface:
  `text_redact_candidates`, `text_redact_catalog_version`, `text_redact_supported_ids`,
  `text_redact_validate` (KAT), `text_redact_is_eligible` (predicate), `text_redact_class_members`
  (class-equivalence probe). The pure core builds without PyO3 (`--no-default-features`); only the
  `#[pyfunction]` wrappers are gated. `lib.rs` grew by one `mod` line and one `register` call.
- `src/decoy_engine/execution/native/_text_redact_kernel.py` (235 LOC): the Python catalog mirror,
  `load_text_redact_kernel()` (fallback to `None` on absent / old-symbol / catalog-skew companion),
  `requested_rust_ids()` and `merge_text_redact_spans()` (the exact oracle candidate order + sweep).
  Keeping `finditer` here preserves `_kernels_scalar.py`'s C6c-i "no regex of its own" contract.
- `src/decoy_engine/execution/native/_kernels_scalar.py`: `native_text_redact` routes per cell,
  runs the Python lookaround + `None`-cell fallback, calls the merge helper, then `_splice`.

## Correctness argument (why byte-identical)

On an eligible cell, each of the eight Rust patterns is the same regular language as the Python
pattern restricted to the ASCII-safe alphabet, because every class the eight use coincides between
`re` and the `regex` crate on that alphabet. Tests enumerate this at the class level: over code
points 0-127, `(?-u)\d`/`\w` and `(?i-u)[A-Z]` equal Python's `re.ASCII` (and default) classes, and
`\s` is the ONLY construct where Python's default class exceeds the ASCII class, by exactly
{0x1c,0x1d,0x1e,0x1f} (test 7.4) -- the sole reason the predicate excludes those four. The eight
patterns use only `\d`, `\s` and ASCII case-fold (no `\w`, no `\b`; asserted). `find_iter` matches
`re.finditer` on these lookaround-free greedy/alternation-free patterns, and a validator rejection
does not change finditer advancement, matching the Python loop. The merge helper assembles
candidates in the oracle's exact order (extras, built-ins in requested order with the eight
interleaved with the three Python detectors, custom) so the stable sort resolves ties identically.

## Tests (plan section 7), red/skip-before-green

Before the Rust binding existed (companion lacked the symbol): the kernel-dependent tests SKIPPED
and the Python-path/merge/validator/fallback tests PASSED -- `32 passed, 145 skipped`. After
building the companion: `177 passed` (the 145 now run).

- 7.1 routing predicate: `is_ascii_safe` boundary enumeration; kernel eligibility parity; surrogate
  and non-string route to Python; the golden clinical notes equal the oracle through the native path.
- 7.2 differential: per-detector raw candidate parity (Rust vs Python) on every eligible cell;
  merged output equals the oracle over the corpus x detector-selection x label_token matrix;
  ineligible cells route to Python; the two named tie counterexamples; a 600-cell generated
  clinical corpus with a ~16% non-ASCII fraction; non-string arrays.
- 7.3 validator KATs: the pinned vectors (Luhn thirteen-zeros + no upper cap + strip; NPI doubling
  parity + strip; IPv4 `001.002.003.004`; IBAN country set / length / whitespace / mod-97; ICD-10
  F00 false, F01/D89 true, D90 false, `A00!!!!` true) run through both the Python validators and
  `text_redact_validate`, asserting Rust == Python == the pinned expectation.
- 7.4 class-level ASCII equivalence (above).
- 7.5 merge-helper / NER / custom units: merge equals `iter_spans` with injected `extra_spans` and
  custom specs across tie/overlap/empty-text; the location-vs-us_zip tie; empty-text early return.
- 7.6 integration + fallback + catalog: an execution assertion proves Rust ran on eligible cells
  (poisoning a supported detector's Python regex leaves eligible output unchanged while the pure
  oracle changes); the three fallback modes (companion absent, symbol absent, catalog-version skew)
  plus a supported-set skew each fall back to the Python path and equal the oracle; the real kernel
  raises on a version mismatch; the Python and Rust catalogs agree on ids/order/labels/version.

Full existing text_redact + seam suites re-run green with the companion present:
`test_c6c_i_text_redact_kernel` + `_chunked` + e2e + physical unified + physical-seam sentry =
`512 passed`; module-size sentry + text_redact unit suites = `680 passed, 4 skipped`.

## Perf matrix (plan 7.7)

`scripts/bench_text_redact.py`, 1,000,000 rows per row, >= 7 reps + 1 warmup, release build, paired
baseline (C6c-i Python path) vs new (rev-3) in isolated subprocesses. Latency floor
median_new <= 1.05 x median_baseline and peak-RSS budget peak_rss_new <= 1.20 x peak_rss_baseline
are enforced every row; the R-representative speedup is reported, not gated.

Measured at the plan's pinned 1,000,000 rows per matrix row (3 reps + 1 warmup, orchestrator
re-measurement 2026-10-09), after the RSS fix below. A 100,000-row x 7-reps run was the first
pass; the 1M run is the acceptance measurement per plan 7.7.

```
row                base med(s)  new med(s)  speedup  lat<=1.05  rss<=1.20   hit%
R-representative       95.5917     45.6602    2.09x         OK         OK  95.0%
R-all-ineligible       98.9710     99.1146    1.00x         OK         OK   0.0%
R-python-only          29.9176     31.3911    0.95x         OK         OK  95.0%
R-short-no-match        3.8389      3.2329    1.19x         OK         OK 100.0%
```

- Speedup (reported): R-representative is 2.09x faster at a 95% ASCII-cell hit rate.
- Latency floor (gated, every row): worst is R-python-only at new/base = 1.049 (<= 1.05); its cost
  is the empty Rust call plus the merge on cells where the kernel runs zero supported detectors.
  All rows pass.
- Peak-RSS budget (gated, every row): all rows within 1.20x of baseline.

**RSS regression found at 1M and fixed (orchestrator, 2026-10-09).** The first 1M run FAILED the
RSS budget on R-representative and R-short-no-match (both 1.283x > 1.20x). Root cause: the wrapper
called `kernel.text_redact_candidates(texts, ...)` on the whole column at once and held every
cell's candidate list resident. text_redact is admitted on the unified full-frame route (not only
the chunked route), so production can hand it a whole large column, and the overshoot scaled with
rows (100k was within budget, 1M was not). Fix: `native_text_redact` now calls the kernel in
fixed sub-batches of `_TEXT_REDACT_KERNEL_BATCH = 65_536` cells, so only one batch of candidate
lists is resident. Per-cell results are unchanged (the 178-test suite incl. the raw-candidate
differential re-passed byte-identical); the 1M re-run above is within budget on every row.

- Fallback is detected by execution assertion (test 7.6), never by timing. A `@pytest.mark.perf`
  test asserts the native path is materially faster than a forced-Python run, so a silent drop
  back to Python on the default workload fails in CI.

## Deferred follow-ups (gate LOWs, non-blocking)

- **dennis LOW (perf scale): CLOSED.** The matrix is now measured at the pinned 1,000,000 rows
  (above), not extrapolated from 100k.
- **Codex final LOW (u32 offset overflow): deferred, not reachable.** `text_redact.rs` casts match
  offsets `usize -> u32`; a single cell longer than 2^32 code points would wrap. Not reachable in
  production: Arrow `string` caps a single value below 2^31 bytes, and a multi-GiB clinical cell
  does not occur. Pre-GA follow-up: preserve `usize`/`u64` offsets through the candidate vector and
  the PyO3 conversion. Tracked here and in the roadmap.

Mutation coverage (targets from plan 7.6), by the tests that kill each class:
- routing predicate: `test_the_ascii_safe_predicate_marks_the_boundary`,
  `test_the_kernel_eligibility_matches_the_python_predicate` (every ASCII code point + the 0x1c-0x1f
  edge).
- candidate advancement / offset counts: `test_raw_candidates_match_python_per_detector...` asserts
  exact (start, end) per detector per eligible cell; a shifted offset or dropped/extra match fails.
- tie order: `test_the_two_named_tie_counterexamples...` + the permutation matrix with label_token.
- label mapping: the label_token leg of the differential + `test_the_python_and_rust_catalogs_agree`.
- empty-selection: `test_empty_selection_redacts_nothing_but_all_selection_redacts`.
- catalog-version check: `test_the_real_kernel_raises_on_a_catalog_version_mismatch` +
  `test_fallback_catalog_version_skew_runs_full_python`.
A full mutmut sweep was not run (mutmut does not reach the Rust crate, and the Rust side is covered
by the differential + KAT + class-equivalence tests that pin each mutable constant); the Python
merge/loader mutants are covered by the units above.

## Judgment calls

- The kernel takes a Python list of stringified cells (the plan's `Sequence[object]`), not an Arrow
  array, reusing C6c-i's `to_pylist()` + stringification. The heavy work (regex scanning +
  validation of eight detectors) moves to Rust; the per-cell Python orchestration that remains is
  the three lookaround detectors + the merge + splice, as the plan scopes.
- `requested_rust_ids` dedupes supported ids before the kernel scan; the merge replays each
  requested occurrence against that one scan, so a repeated supported id still emits its spans once
  per occurrence (the sweep collapses them) -- byte-equivalent to the oracle's repeated finditer.
- The loader returns `None` (never raises) for every fallback mode, since text_redact falls back
  rather than failing closed (it is an unkeyed transform with no security contract).
- The catalog "label" column equals the id column, because `_splice` reads `Span.detector_id` for
  the `[REDACTED:<id>]` label; the catalog records it explicitly so a label-mapping change bumps
  the version.

## For the gate reviewers to scrutinize

- The `find_iter` == `re.finditer` claim for the eight patterns rests on them being
  lookaround-free with no top-level alternation; the raw-candidate differential (7.2) over the
  corpus + the generated clinical corpus is the empirical backing, the class enumeration (7.4) the
  constructive one. Worth a cold read of the eight pattern translations in `pattern_for`.
- The IBAN incremental mod-97 and the NPI/Luhn doubling parity are hand-ported; the KATs pin them,
  but a reviewer should confirm the vectors exercise the edges (letter-to-two-digit expansion,
  even/odd position doubling).
- The physical-seam byte-identical gate was extended with one new permitted module
  (`_text_redact_kernel.py`); confirm the rationale and that nothing else under `execution/` drifted.
