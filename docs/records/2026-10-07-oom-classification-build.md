Status: record

# Isolated-run OOM classification: build record

Contract: `docs/plans/2026-10-07-oom-classification.md` rev 3. Branch `fix/oom-classification` off engine main `9bbde63c`. No catch site changed and every fallback is kept.

## What changed

- `_isolated_worker._run`: `_finalize_outputs` and `_stage_row_errors` now run inside the classified `try`.
- `_isolated_worker.main`: the outer handler classifies with `is_memory_failure` instead of hard-coding `crashed`. A malformed payload is still `crashed`.
- `_isolated_common.scrub_error_text`: replaces the span between the first `Wrapping ` and the last ` failed` with `<value>`. Both handlers build the stored text through `_error_text`, which scrubs first and truncates to 500 after.
- Recognition (`_MEMORY_ERROR_PATTERNS`) is untouched.
- `TestMemCapOom` keeps its assertion; the message is `error=... returncode=... signal_number=...`, the fields `IsolatedRunResult` carries.
- Tests: `tests/unit/execution/test_isolated_oom_classification.py` (57 tests, plan tests 1 to 6).

## Judgment calls

- Tests were written first. Against a stub `scrub_error_text` that returned its input, 19 of 54 failed for the planned reasons: the post-run and `main()` shapes raised or hard-coded `crashed`, and the scrub tests saw the raw value. The parity, recognition and fallback tests passed from the start, as expected (they pin current behavior).
- Test 1 injects the `ArrowMemoryError` at `redact_array` (redact fallback) and at `pa.array` (kernel-input fallback) rather than driving a full pipeline.
- Test 4's malformed-UTF-8 case builds a real Parquet string (`b"John Smith\xff"`) and has a patched `run_pipeline` read it with `to_pandas`. pyarrow 25.0.1 gives `Unknown error: Wrapping John Smith? failed`, which classifies `crashed` and is scrubbed.
- `tests/sentry/test_physical_seam_disconnection.py` lists every file under `execution/` that may differ from the merge-base. `_isolated_common.py` was missing, so it was added with a comment. This is not the log-interpolation allowlist, which did not grow.
- Hand mutation found two survivors. M5e (`rfind` lower bound) is equivalent: a ` failed` starting at the trailing space of `Wrapping ` is rejected by the guard anyway, so the bound was removed. M5f (guard `< 0`) was a real gap, closed with `test_wrapping_without_a_later_failed_is_unchanged`.

## Counts

| Suite | Python | Result |
|---|---|---|
| New file | 3.11 | 57 passed |
| `ruff check src tests`, `ruff format --check src tests`, `mypy src` | 3.10 | clean (487 source files) |
| `tests/sentry` (final tree) | 3.10 | 2420 passed, 1 skipped |
| `tests/sentry` (final tree) | 3.11 | 2420 passed, 1 skipped |
| `tests/unit/execution`, `tests/physical/test_unified_slice_isolated_worker.py`, `tests/perf/test_governor_reroute_completion.py`, `tests/sentry` | 3.11 | 8866 passed, 5 skipped, 1 failed (the seam allowlist above, fixed and re-run in the sentry rows) |

## Mutation (plan test 9), run against the new file on 3.11

| Mutant | Result |
|---|---|
| M1 `_finalize_outputs` moved out of the `try` | killed |
| M2 `_stage_row_errors` moved out of the `try` | killed |
| M3 `main()` hard-codes `crashed` | killed |
| M3b `_run` always `crashed` | killed |
| M3c `_run` always `oom_killed` | killed |
| M4 pattern widened to `.+` | killed |
| M4b pattern removed | killed |
| M5 truncate before scrub | killed |
| M5b scrub removed from `_error_text` | killed |
| M5c first ` failed` instead of last | killed |
| M5d value start off by one | killed |
| M5e `rfind` lower bound removed | survived; equivalent, bound deleted from the code |
| M5f guard `value_end < 0` | survived; test added, then killed |
| M5g non-Wrapping text replaced | killed |
| M5h placeholder dropped | killed |
| M5i `main()` handler unscrubbed | killed |

## Soak (plan test 7)

Flaking test body, `auto_chunk=False`, 4,000,000 rows, 8 columns, CI-mirror venv (py3.10, pyarrow 25.0.1, no companion), 20 runs at each cap. Zero `crashed`. Memory stayed above the 2.5 GB floor, so no reduction.

| Cap | Runs | `oom_killed` | Error shape (value scrubbed, sizes elided) |
|---|---|---|---|
| 1536 MiB | 20 | 20 | 20x `ArrowMemoryError: malloc of size N failed` |
| 2280 MiB | 20 | 20 | 12x `ArrowMemoryError: malloc of size N failed`; 7x driver-classified SIGABRT (`returncode=-6`, stderr tail `what():  std::bad_alloc`); 1x `ArrowException: Unknown error: Wrapping <value> failed` |

The SIGABRT shape is the one the plan saw in the investigation and was already `oom_killed`. The single Wrapping shape is single-token, so recognition caught it, and the stored text shows `<value>`.

In the first soak pass 3 of 40 pytest items ended without a result line (2 failed, 1 setup error); the output was clipped and the cause was not captured. Those three cases and their neighbours re-ran clean (4 of 4 `oom_killed`). The 40 results above are one per run. The cause of the 3 is not established.

## Not verified

- The original CI failure (`crashed`, run 37337950399 and 37548252974) is not reproduced or explained; the diagnostic message will show its shape on the next occurrence.
- Native deaths by other signals (SIGSEGV or SIGBUS without a marker) stay `crashed`, pinned by test 6 (plan section 6).
- The driver-side stderr tail copied into the result is not scrubbed.
