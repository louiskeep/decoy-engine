Status: plan (rev 3, BUILD-READY: Codex plan gate GO in round 3)

Rules consulted: 00-universal, development-loop, risk-and-exceptions, debugging, testing, code-review.

# bucket_perturb: reject a `date_format` that cannot write a date back

Branch `fix/bucket-perturb-format-guard` off engine main `7ee20b55`. Cam decided this on 2026-10-07 ("reject with a clear error"). Risk R1: a config that today silently destroys data now fails up front.

## 1. Problem

`apply_bucket_perturb` (`transforms/bucket_perturb.py:123-172`) uses ONE `date_format` for two jobs:
- it parses each value with `pd.to_datetime(series, format=fmt)`;
- it writes each perturbed date back with `perturbed.strftime(fmt)` (`:170`).

When `fmt` contains no strftime directive, `strftime` returns the literal text. pandas accepts the special names `ISO8601` and `mixed` for PARSING, so with either of them every date that parses is replaced by the word `ISO8601` or `mixed`. dennis reproduced this on main on both routes: `['mixed','mixed',None,'mixed']`. Any directive-free string that pandas manages to parse would do the same.

Today `validate_bucket_perturb_config` (`:175-192`) checks only `bucket`, and no compile-time check covers `date_format`. C8-iii-a (in flight) moves `ISO8601`/`mixed` off the native route, but the oracle has the same defect, so it does not fix this.

## 2. Decision (Sprint-13 truncate precedent: compile check plus handler backstop)

**2a. One rule, one owner.** A new function `bucket_perturb_date_format_problem(date_format) -> str | None` in `transforms/bucket_perturb.py` returns a reason, or `None` when the format is acceptable.
- `None` or empty is acceptable. It means autodetect, which is today's behavior and unchanged here.
- **Guarantee (rev 2, Codex round 1 HIGH):** this rejects formats that cannot write a DATE back. It does not reject every lossy format.
  - bucket_perturb perturbs the calendar date: the oracle calls `.date()` before `strftime`. So time, fractional-second and timezone directives always write midnight, zero or empty. That is an inherent, documented property of this date strategy, and this slice does not change it.
- **Rule:** scan the string left to right.
  - `%%` is consumed as a literal percent and counts for nothing.
  - A real directive is `%` followed by one character.
  - **Recognized directives (rev 3):**
    - DATE: `%Y %y %G %m %b %B %d %j %U %W %V %a %A %u %w %x %c`. `%C`, `%h`, `%e`, `%D` and `%F` fail pandas parsing, so they are not listed.
    - TIME and OTHER: `%H %I %M %S %p %f %z %Z %X` (`%X` added in Codex final round 1; it parses and writes on main).
    - Every other directive is unknown.
  - The format is acceptable only if the WHOLE string contains no unknown directive, no dangling `%`, AND at least one DATE directive. Locale `%x` and `%c` are accepted.
  - A dangling trailing `%` is rejected.
  - An unknown directive (for example `%Q`) is rejected with the same code. The record documents this as an intentional move of today's runtime `ValueError` to an up-front error.
  - A non-string value is rejected.
  - `ISO8601`, `mixed` and any other string with no date directive are rejected, whatever their case. Time-only formats such as `%H:%M:%S` are rejected too, because they would write midnight over every value.
- The message names the field and asks for a concrete pattern such as `%Y-%m-%d`. It never echoes data.

**2b. Compile-time check.**
- A new `plan/_checks_bucket_perturb.py` holds `check_bucket_perturb_config(config)`. It is registered in BOTH engine validation entrypoints and their reporting tuples: next to `check_truncate_config` in the compile path (`plan/_compile.py:~252`) and in `run_config_only_checks` (`:~555`), Codex round 1.
- It raises `PlanCompileError(code="bucket_perturb_date_format_unsupported")` for any bucket_perturb column whose `provider_config.date_format` fails 2a.
- **Timing (rev 2).** The job fails before any masking or output write. That is NOT before source reads, because `run_pipeline` profiles sources before compiling (`_pipeline.py:380-382`).
- **Routes.** The check is verified on the whole-frame, sequential, multi-table, generate-plus-mask, chunked and both out-of-core paths.
- **Platform save-time validation.** `api/pipelines/v2_validation.py` keeps its own validators and has none for bucket_perturb. Adding one is EXCLUDED from this engine slice, because platform CI has no GitHub billing and platform merges need the local check. It is noted on the roadmap as a follow-up. The engine check still rejects at run time for every platform job.

**2c-0. Handler preflight (rev 3, Codex round 2).** The `when` gate skips `handler.run` when nothing matches (`_when_gate.py`). `BucketPerturbStrategyHandler` therefore gains `preflight(plan, ctx)`, using the same shared validation and error mapping as `run`, as code_set and top_code already do. A zero-match gate or an empty frame then still rejects.

**2c-1. Nested configurations (rev 3).** Both compile entrypoints also check bucket_perturb child configs inside `strategy: nested` (`strategy_config`), and report the child config path. At run time, the child's validation runs before any nested early return (`_strategies/_nested.py`), so empty, all-null and zero-match leaves still reject.

**2c. Handler backstop.**
- `validate_bucket_perturb_config` also applies 2a. The existing callers then raise `StrategyError(code="bucket_perturb_invalid_config")` at run time:
  - the oracle handler (`_strategies/_bucket_perturb.py:62`);
  - out-of-core (`out_of_core/_mask_group_c.py:425, 462`).
- This covers raw-dict callers that skip compile.

**2d. Native gate.** `bucket_perturb_config_rejection` (`native/_operator_config_rejections.py:63-`) reuses 2a. Compile rejection IMPLIES native rejection, but the two verdicts are not identical: native keeps its own extra restrictions, such as no autodetect and no `%z`/`%Z` (Codex round 1). On main it already requires a non-empty string and rejects `%z`/`%Z`. C8-iii-a's special-format decline becomes redundant but harmless.

**2e. Out of scope.** These are recorded for Cam and not changed here:
- **Undetectable format.** When `date_format` is unset and `_detect_format` finds nothing, the handler passes the column through UNMASKED with only a warning (`:151-155`). That is the same silent-passthrough class the truncate check closed in Sprint 13, and it needs its own decision.
- **Directive-bearing formats whose output form differs from the input.** For example, `%Y` written back is year-only. That is intended behavior.

## 3. Acceptance tests (written first)

1. **Compile (both entrypoints).**
   - Rejected with `bucket_perturb_date_format_unsupported`, naming the column and not echoing data:
     - `ISO8601`, `iso8601`, `mixed`;
     - `YYYY-MM-DD`, `foo`;
     - `%%Y` (escaped), a trailing `%`;
     - `%H:%M:%S` (time-only), `%f`, `%z` and `%Z` alone, `%Q` (unknown);
     - `%%%%Y` (two escapes, no directive), `%Y %Q` (date plus unknown), `%Y%` (date plus a dangling `%`);
     - a non-string value, including a falsy non-string such as `0`.
   - Compile cleanly: `%Y-%m-%d`, `%d/%m/%Y`, `%Y`, `%x`, `%c`, `100%% %Y`, `%%%Y` (an escape, then `%Y`), `%Y-%m-%dT%H:%M:%S%z`, `%Y-%m-%d %f`, `%Y-%m-%d %Z`, `%%Q %Y` (a literal `%Q`), and an empty or unset format, each keeping today's rendering.
   - A non-bucket_perturb column with a `date_format` key is untouched.
2a. **Nested:** a nested bucket_perturb child with `date_format: mixed` is rejected at compile, naming the child path. At run time it is rejected for populated, empty, all-null and zero-match leaves.
2. **Handler backstop.** A raw-dict run that bypasses compile, with `date_format: mixed`, raises `StrategyError(bucket_perturb_invalid_config)` on the oracle and on both out-of-core paths, before any output write (an output-write spy proves it). The rejection still happens with empty input, with all-null input, and under a `when:` predicate that selects nothing, tested through the REAL `when` gate (the preflight path).
3. **Native gate.** Every format test 1 rejects is also rejected natively. Native's own extra declines (autodetect, `%z`) are kept.
4. **No change for valid configs.** BEFORE implementing, the builder lists every existing config and test affected. That includes tests that pinned the destructive literal output, the C8-iii-a goldens, and tests that expect `%Q` to raise at runtime, which now raise up front. Each change is recorded with its reason and a migration note to a concrete pattern. Every other test stays green unmodified. Testflight fingerprints are unchanged; STOP if one moves.
5. **Sentries and mutation** on 2a.

## 4. Failure modes

| Risk | Closed by |
|---|---|
| A valid user format rejected | The whole-string recognized-directive rule requires at least one DATE directive (2a); test 1's accept cases |
| Raw-dict path bypasses compile | 2c backstop; test 2 |
| Native admits what compile rejects | 2d shared rule; test 3 |

Rollback: revert the merge commit.

## 5. Review log

- **Codex plan gate, round 1: REVISE** (1 HIGH, 3 MEDIUM). Rev 2:
  - **HIGH:** the guarantee is "a date must be written back". The rule requires a real DATE directive, consumes `%%`, and rejects time-only, unknown and dangling formats. Time, fractional-second and timezone loss is documented as inherent to this date strategy.
  - **MEDIUM:** both engine entrypoints; precise timing; the route matrix; the platform validator explicitly excluded and tracked.
  - **MEDIUM:** compile rejection implies native rejection; native's extra restrictions are kept.
  - **MEDIUM:** an inventory of affected configs and tests before implementation, including the `%Q` timing move.

  Codex agreed that section 2e (undetectable-format passthrough) stays separate and tracked.
- **Codex plan gate, round 2: REVISE** (3 MEDIUM). Rev 3:
  - a handler `preflight`, so a zero-match `when` gate cannot bypass the check (2c-0);
  - nested child configs checked at compile and before nested early returns (2c-1, test 2a);
  - an explicit recognized-directive table and whole-string boundary tests (2a, test 1). `%C`, `%h`, `%e`, `%D` and `%F` were dropped because they fail pandas parsing.

  The section 2e follow-up (undetectable-format passthrough) is now on the platform roadmap.
- **Codex plan gate, round 3: GO.** Two LOWs folded: a corrected failure-mode row, and the accepted cases `%Y-%m-%d %f`, `%Y-%m-%d %Z` and `%%Q %Y`.
- **Codex final gate, round 1: NO-GO** (2 MEDIUM, 1 LOW). Fixed:
  - `%X` (locale time) was missing from TIME/OTHER, which rejected `%Y-%m-%d %X`, a format that works on main;
  - the out-of-core runner validated table by table, so a valid first table reached the sink before a later table's bad format was caught. It now preflights every bucket_perturb node before any table (`preflight_group_c`), with a later-table zero-write test;
  - the docs paragraph moved from date_shift to bucket_perturb.
- **Codex final gate, round 2: GO** (high confidence). All round-1 findings closed; a reconstructed 29-format corpus matches pre-guard main output; 24 later-table probes rejected before scratch or sink writes.
