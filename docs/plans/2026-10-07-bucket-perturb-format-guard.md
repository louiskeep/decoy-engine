Status: plan

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
- A string is acceptable only if it contains at least one strftime directive (`%` followed by a directive character). The two names `ISO8601` and `mixed` are rejected, whatever their case.
- The message names the field and asks for a concrete pattern such as `%Y-%m-%d`. It never echoes data.

**2b. Compile-time check.**
- A new `plan/_checks_bucket_perturb.py` holds `check_bucket_perturb_config(config)`, wired next to `check_truncate_config` in `plan/_compile.py:~252`.
- It raises `PlanCompileError(code="bucket_perturb_date_format_unsupported")` for any bucket_perturb column whose `provider_config.date_format` fails 2a.
- So a job fails before it reads any data, on every authoring path (CLI, YAML, platform).

**2c. Handler backstop.**
- `validate_bucket_perturb_config` also applies 2a. The existing callers then raise `StrategyError(code="bucket_perturb_invalid_config")` at run time:
  - the oracle handler (`_strategies/_bucket_perturb.py:62`);
  - out-of-core (`out_of_core/_mask_group_c.py:425, 462`).
- This covers raw-dict callers that skip compile.

**2d. Native gate.** `bucket_perturb_config_rejection` (`native/_operator_config_rejections.py:63-`) reuses 2a, so it never admits a column that compile rejects. On main it already requires a non-empty string and rejects `%z`/`%Z`. C8-iii-a's special-format decline becomes redundant but harmless.

**2e. Out of scope.** These are recorded for Cam and not changed here:
- **Undetectable format.** When `date_format` is unset and `_detect_format` finds nothing, the handler passes the column through UNMASKED with only a warning (`:151-155`). That is the same silent-passthrough class the truncate check closed in Sprint 13, and it needs its own decision.
- **Directive-bearing formats whose output form differs from the input.** For example, `%Y` written back is year-only. That is intended behavior.

## 3. Acceptance tests (written first)

1. **Compile.**
   - `ISO8601`, `iso8601`, `mixed`, `YYYY-MM-DD` and `foo` each raise `bucket_perturb_date_format_unsupported`, naming the column and not echoing data.
   - `%Y-%m-%d`, `%d/%m/%Y`, `%Y` and an unset format all compile.
   - A non-bucket_perturb column with a `date_format` key is untouched.
2. **Handler backstop.** A raw-dict run that bypasses compile, with `date_format: mixed`, raises `StrategyError(bucket_perturb_invalid_config)` on the oracle and on out-of-core, BEFORE writing any output.
3. **Native gate.** A rejected format is never admitted natively. The verdict agrees with the compile rule for every case in test 1.
4. **No change for valid configs.** Existing bucket_perturb tests stay green unmodified, apart from ones that pinned the destructive literal output (each listed in the record with the reason). Testflight fingerprints are unchanged; STOP if one moves.
5. **Sentries and mutation** on 2a.

## 4. Failure modes

| Risk | Closed by |
|---|---|
| A valid user format rejected | Rejection requires the absence of any `%` directive; test 1's accept cases |
| Raw-dict path bypasses compile | 2c backstop; test 2 |
| Native admits what compile rejects | 2d shared rule; test 3 |

Rollback: revert the merge commit.
