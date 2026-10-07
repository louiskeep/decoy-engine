Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, debugging, testing, observability-and-resilience, code-review.

# Isolated-run OOM classification: name every memory failure correctly

Branch `fix/oom-classification` off engine main `9bbde63c`. It is built AFTER C8-ii merges, because both touch `_unified_slice_admission.py`. Risk R1 after rev 2: no execution behavior changes; only classification, the stored error text and a test message change.

## 1. Problem

The isolated-run guarantee is: "a running job that exhausts its memory cap is named `oom_killed`, never an opaque `crashed`". `tests/unit/execution/test_isolated_run.py::TestMemCapOom::test_low_mem_cap_classifies_oom_cleanly_not_an_opaque_crash` failed in main CI on 2026-10-05 (run 37337950399) and 2026-10-07 (run 37548252974) with `'crashed' == 'oom_killed'`. It passes on most runs.

**Investigation (2026-10-07; evidence in the session scratchpad `oomflake/`).**
- About 70 local runs on the CI-mirror venv (py3.10, pyarrow 25.0.1, no companion), at caps from 1536 to 3200 MiB, never reproduced `crashed`. The CI logs cannot show the cause, because the test asserts `outcome` before anything prints `result.error`.
- The test's OOM always lands in `Table.to_pandas` through `to_pandas_fk_safe`. It surfaced in three shapes, depending on timing:
  - `ArrowMemoryError`, in most runs;
  - `ArrowException: Unknown error: Wrapping <value> failed`, once;
  - an uncaught C++ `std::bad_alloc` leading to SIGABRT, in 1 of 10 runs at 1536 MiB and 3 of 10 at 2280 MiB.

**Root causes (each verified by reading the code):**
1. **Memory failures are swallowed as "decline / fall back".**
   - `_unified_slice_admission.py:350-352`: `except Exception` around `to_pandas_fk_safe`.
   - `_unified_slice_admission.py:384-386`: `except Exception` around `Table.from_pandas`.
   - `_strategies/_redact.py:36-42` and `_adapter.py:~310-312`: `except pa.ArrowException`, of which `ArrowMemoryError` is a subclass.

   The job then repeats the same large allocation on a heavier path (the legacy adapter's conversion, `_pandas_adapter.py:212`), and the final failure happens at a second site in an unpredictable shape. This is the source of the nondeterminism.
2. **Post-run steps run outside the classifier.** `_isolated_worker.py:215-216` calls `_finalize_outputs` and `_stage_row_errors` outside `_run`'s `try`. A MemoryError there reaches `main()`, which hard-codes `"crashed"` (`:240-249`).
3. **The arrow-to-pandas OOM message is matched too narrowly, and it leaks data.**
   - `_isolated_common.py:114-116` matches `Wrapping \S+ failed`, so a cell value containing whitespace ("Wrapping John Smith failed") is missed.
   - The same message is copied into `result.error` (`_isolated_worker.py:212`), putting a raw cell value into the run result.
4. **Native deaths by a signal other than KILL or ABRT** (for example SIGSEGV or SIGBUS on a native OOM path) are `crashed` unless stderr carries a memory marker (`_isolated_common.classify_abnormal_exit`). Not fixed here; see section 6.

## 2. Scope

**In:**
- Fixes 2 and 3.
- A diagnostic assertion message on the flaking test.
- Classifier parity tests.

**Kept as-is (rev 2):** root cause 1's fallbacks. Codex round 1 showed a capped job that fails Arrow conversion but succeeds through the existing kernel-input and redact fallbacks. The admission round trip is also a speculative allocation the legacy route need not make. Failing fast would turn jobs that succeed today into failures. Every shape the second site produced in the investigation (`ArrowMemoryError`, the Wrapping message, SIGABRT) is classified `oom_killed` once fix 3 lands, so the nondeterminism stays harmless to classification. The reroute catch in `_unified_slice.py:362-364` (Codex round 1) is kept for the same reason.

**Out:**
- Root cause 4 (section 6).
- A general scrub of `result.error` text. The stored error is `f"{type(exc).__name__}: {exc}"[:500]`, and other exception messages can also carry data values; that site goes on the roadmap Observability list rather than widening this fix.

## 3. Decisions

**3a. (Removed in rev 2.)** No catch site changes; see section 2.

**3b. Post-run steps inside the classified region.**
- `_finalize_outputs` and `_stage_row_errors` move inside `_run`'s `try`, so a memory failure there self-reports `oom_killed`.
- `main()`'s outer handler classifies with `is_memory_failure` instead of hard-coding `crashed`. A malformed payload still gives `crashed`.

**3c. The Wrapping pattern and the stored error.**
- Recognition: the pattern becomes `Unknown error: Wrapping .+ failed` with DOTALL, applied to the FULL message, so a value with spaces or newlines is matched.
- Scrub: one function, `scrub_error_text(message)`. In a recognized Wrapping message, the span from just after the first `Wrapping ` to just before the LAST ` failed` is replaced by `<value>`. A value containing `failed`, or repeated Wrapping fragments, are therefore removed whole. Any other message is returned unchanged.
- Order: both worker handlers (`_run`'s and `main()`'s) build `f"{type(exc).__name__}: {scrub_error_text(str(exc))}"` and only THEN truncate to 500. Truncating first can cut off the terminal `failed`, so the scrub would not match (Codex round 1 MEDIUM).
- Classification is done on the exception object, before any scrub or truncation, so it is unaffected.
- This one scrub is in scope because the slice touches exactly this message. Other messages go to the Observability program (section 2).

**3d. Diagnostic on the flaking test.** Its assertion gains the message `f"{result.error!r} rc={result.returncode} sig={result.signal_number}"`, or the fields the result actually carries. The next CI failure then shows its shape. The assertion itself is unchanged.

## 4. Acceptance tests (written first; no later contributor weakens them)

1. **Fallbacks preserved:** the redact and kernel-input fallbacks still succeed after an `ArrowMemoryError` from the direct conversion (Codex's counterexample shape, by fault injection). Existing decline and fallback tests stay green unmodified.
2. A MemoryError injected into `_finalize_outputs`, and one injected into `_stage_row_errors`, give `oom_killed`.
3. A MemoryError raised in `main()` outside `_run` gives `oom_killed`. A malformed payload gives `crashed`.
4. Pattern: `Wrapping John Smith failed`, a value with a newline, a value containing `failed`, a 600-character value, and the original single-token form are all memory failures. An unrelated `ArrowException` is not.
5. Scrub, through both worker handlers: no part of the value survives for long, multiline, embedded-`failed` and repeated-fragment values. A long value is scrubbed before truncation. A non-Wrapping error text is unchanged.
6. Classifier parity: `MemoryError`, `ArrowMemoryError`, `duckdb.OutOfMemoryException`, `OSError(ENOMEM)`, both OpenSSL markers, the Wrapping message, and abnormal exits by SIGKILL and SIGABRT with and without stderr markers are all `oom_killed`. A non-memory exception and a SIGSEGV without a marker are `crashed`; the SIGSEGV case is pinned as current, deferred behavior (section 6).
7. A local soak, not a CI test: the flaking test 20 times at 1536 MiB and 20 times at 2280 MiB on the CI-mirror venv. Zero `crashed`. The counts and each run's (scrubbed) error shape go in the build record.
8. Sentries: log interpolation, module size, the public import boundary.
9. Mutation on the moved `try`, `main()`'s classification, the pattern and the scrub. Equivalents are argued in the record.

## 5. Failure modes

| Risk | Closed by |
|---|---|
| A successful fallback becomes a failure | Rev 2 changes no catch site; test 1 |
| Scrub misses a value, or truncation defeats it | Scrub before truncate; test 5 |
| Scrub removes useful diagnostics | Only the value is replaced; class and message shape stay |
| A memory shape still classified `crashed` | Test 6 parity table; the 3d diagnostic names any new shape in CI |

Rollback: revert the merge commit.

## 6. Deferred: classify native deaths by cause (root cause 4)

The driver could sample the child's VmData (the quantity RLIMIT_DATA bounds) from `/proc/<pid>/status` while it waits. Any non-completed death whose peak came within a margin of the cap would be named `oom_killed`, with "VmData peak X of cap Y" recorded. That covers native deaths whose shape nobody listed, but it is new machinery. It is held for a separate decision, made only if the diagnostic in 3d shows a CI failure of that shape after this slice merges.

## 7. Review log

- **Codex plan gate, round 1: REVISE** (2 HIGH, 2 MEDIUM). Rev 2:
  - **HIGH (fallbacks that succeed):** fail-fast removed (3a). The slice keeps every fallback and fixes classification only. Test 1 pins that the fallbacks still succeed.
  - **HIGH (reroute catch in `_unified_slice.py:362-364`):** moot once nothing propagates; it stays a fallback like the others.
  - **MEDIUM (scrub ordering):** scrub before truncation in both handlers, with a defined replacement span; test 5 widened.
  - **MEDIUM (traceback evidence does not exist):** the one-site test is dropped (no fail-fast to prove); a classifier parity table is added instead (test 6).
  - Codex confirmed deferring the other native signals: the SIGABRT shape seen in the investigation is already classified `oom_killed`. The raw error assignment is at `_isolated_worker.py:212`.
