Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, debugging, testing, observability-and-resilience, code-review.

# Isolated-run OOM classification: one failure site, classified by cause

Branch `fix/oom-classification` off engine main `9bbde63c`. It is built AFTER C8-ii merges, because both touch `_unified_slice_admission.py`. Risk R2: it changes how memory failures propagate on the default path.

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
   - The same message is copied into `result.error` (`_isolated_worker.py:211`), putting a raw cell value into the run result.
4. **Native deaths by a signal other than KILL or ABRT** (for example SIGSEGV or SIGBUS on a native OOM path) are `crashed` unless stderr carries a memory marker (`_isolated_common.classify_abnormal_exit`). Not fixed here; see section 6.

## 2. Scope

**In:**
- Fixes 1-3.
- A diagnostic assertion message on the flaking test.
- Unit tests per path.

**Out:**
- Root cause 4 (section 6).
- A general scrub of `result.error` text. The stored error is `f"{type(exc).__name__}: {exc}"[:500]`, and other exception messages can also carry data values; that site goes on the roadmap Observability list rather than widening this fix.

## 3. Decisions

**3a. A memory failure is never a reason to decline or fall back.**
- One helper, `reraise_if_memory_failure(exc)`, re-raises when the exception is a memory failure under the SAME predicate the classifier uses (`is_memory_failure`, or a dependency-free core of it if importing `_isolated_common` from these modules would pull duckdb or create a cycle; the builder chooses and records why).
- Each of the four catch sites calls it first, then keeps its existing decline or fallback for every other exception.
- Effect: the job fails at the FIRST allocation that exhausts the cap, as a memory error. In-process (non-isolated) runs behave the same way: a MemoryError at unified admission now propagates instead of retrying the identical conversion on the legacy path, which needs at least as much memory.

**3b. Post-run steps inside the classified region.**
- `_finalize_outputs` and `_stage_row_errors` move inside `_run`'s `try`, so a memory failure there self-reports `oom_killed`.
- `main()`'s outer handler classifies with `is_memory_failure` instead of hard-coding `crashed`. A malformed payload still gives `crashed`.

**3c. The Wrapping pattern and the stored error.**
- The pattern becomes `Unknown error: Wrapping .* failed` with DOTALL, so any value is matched.
- Before an error string is stored in the envelope, a Wrapping message has its value replaced by `<value>`. The class name and the rest of the message are kept.
- This one scrub is in scope because this slice's own fix makes the message more common. Other messages stay as they are (section 2).

**3d. Diagnostic on the flaking test.** Its assertion gains the message `f"{result.error!r} rc={result.returncode} sig={result.signal_number}"`, or the fields the result actually carries. The next CI failure then shows its shape. The assertion itself is unchanged.

## 4. Acceptance tests (written first; no later contributor weakens them)

1. For each of the four catch sites: a `MemoryError` (and an `ArrowMemoryError`) raised at the site propagates instead of declining or falling back. A non-memory exception still declines or falls back exactly as before. Existing decline tests stay green unmodified.
2. A MemoryError injected into `_finalize_outputs`, and one injected into `_stage_row_errors`, give `oom_killed`.
3. A MemoryError raised in `main()` outside `_run` gives `oom_killed`. A malformed payload gives `crashed`.
4. Pattern: `Wrapping John Smith failed`, a value with a newline, and the original single-token form are all memory failures. An unrelated `ArrowException` is not.
5. Scrub: an envelope built from a Wrapping error contains no part of the value.
6. A one-site check: with a cap that exhausts during unified admission, the traceback recorded in the envelope shows the failure at the admission conversion. It never reaches `_pandas_adapter.py`.
7. A local soak, not a CI test: the flaking test 20 times at 1536 MiB and 20 times at 2280 MiB on the CI-mirror venv, plus the same on the companion venv. Zero `crashed` and every run `oom_killed`. The counts go in the build record.
8. Sentries: log interpolation, module size, the public import boundary.
9. Mutation on the helper, the four call sites, the moved `try` and the pattern. Equivalents are argued in the record.

## 5. Failure modes

| Risk | Closed by |
|---|---|
| A memory failure still declines somewhere unlisted | Test 6 (one site); a grep audit listed in the record of every `except Exception` / `except pa.ArrowException` on the conversion path |
| Re-raise changes non-memory behavior | Test 1's non-memory half; existing decline tests unmodified |
| The helper pulls duckdb into hot modules | 3a lets the builder use a dependency-free core; import-boundary sentry |
| Scrub removes useful diagnostics | Only the value is replaced; class and message shape stay |

Rollback: revert the merge commit.

## 6. Deferred: classify native deaths by cause (root cause 4)

The driver could sample the child's VmData (the quantity RLIMIT_DATA bounds) from `/proc/<pid>/status` while it waits. Any non-completed death whose peak came within a margin of the cap would be named `oom_killed`, with "VmData peak X of cap Y" recorded. That covers native deaths whose shape nobody listed, but it is new machinery. It is held for a separate decision, made only if the diagnostic in 3d shows a CI failure of that shape after this slice merges.
