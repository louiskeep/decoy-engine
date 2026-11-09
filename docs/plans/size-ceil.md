# SIZE-CEIL: module-size pins become ceilings with headroom

Status: plan

Rules consulted: 00-universal, development-loop, architecture ("Size"), testing,
catalogs/recurring-defects (item 11, owner 2026-11-10)

Base: engine main `84f4290d`. One slice, engine only. Test-only change; no `src/` file moves.

**Guarantee.** A dense module's census entry and the three exact `_pipeline.py` pins become ceilings
with 20 lines of initial headroom (effective stale slack is bounded at 40), never above `MAX`. `GOAL`, `MAX` and legacy entries do not move.
**Non-goals:** splitting any module; changing the fan-out or other ratchets; the CLI sentry.
**Design notes:** none, no new structure.

## Why

`tests/sentry/test_module_size.py` records every module over `GOAL` (600) at its exact line count.
Any edit to a dense module then fails the sentry until the census is bumped, so unrelated small
changes keep tripping it. There have been 142 growth bumps in the file's history: median 7 lines,
p75 19 lines. Three bookkeeping tests also pin `execution/_pipeline.py` at exactly 351 lines. The
owner rule says guards pin ceilings with headroom. `GOAL` (600), `MAX` (700, this repo's cap) and the
ratchet counts never rise.

## Behavior

- New constant `MARGIN = 20` and helper `ceiling_for(loc) = min(MAX, loc + MARGIN)` in
  `test_module_size.py`.
- **Dense entry** (recorded value at most `MAX`). The recorded value is a ceiling. It passes when
  `loc <= ceiling` and `ceiling - loc <= 2 * MARGIN`.
  - Growth past the ceiling fails. The fix (item 11) raises the ceiling by at most the lines the
    change adds, never above `MAX`, and lists the moved pin in the report. Past `MAX`, decompose.
  - A shrink of more than `2 * MARGIN` below the ceiling fails with "ratchet down to
    `ceiling_for(loc)`". This bounds the silent regrow hole the exact match closed.
- **Legacy entry** (recorded value above `MAX`). No change: it stays an exact match and may only
  shrink. Only pins below the cap become ceilings.
- **Unrecorded file.** No change: over `GOAL` needs an entry, and over `MAX` fails.
- **Conversion.** The build sets each dense entry to `ceiling_for(current loc)`, regenerated on the
  slice's build base and never copied from this plan. Legacy values do not move. The `module-size`
  ratchet baseline tracks keys only, so it is unchanged.
- **Bookkeeping pins.** `_loc(EXEC / "_pipeline.py") == 351` in `test_b6a_bookkeeping.py`,
  `test_b6b_bookkeeping.py` and `test_auto_chunk_bookkeeping.py` becomes
  `<= size_sentry.ceiling_for(351)`.
- **Rise rule.** A ceiling may rise only by the PR's own LOC growth (`new_ceiling - old_ceiling <= max(0, new_loc - old_loc)`); `ceiling_for` is for conversion or a required shrink. The sentry reads one head, so this is review-enforced and stated in the policy docstring.
- **Docs.** The module docstring, the `CLAUDE.md` size bullet and the policy line in
  `docs/decisions/module-size-census-history.md` say "ceiling with up to 20 lines of headroom"
  instead of "exact LOC".

## Margin rule

`MARGIN = 20`, so a ceiling is `min(MAX, loc + 20)`.

- 20 covers three quarters of past census bumps (p75 = 19), the small threading and comment edits
  the rule targets.
- A feature-sized growth (p90 = 31, the largest 321) still crosses the ceiling and shows as a
  re-pin in the diff.
- The `min(MAX, ...)` keeps a ceiling from ever passing the cap.
- The stale bound is `2 * MARGIN`. Without it, a file that shrinks by one line after a fresh pin
  would fail at once (`ceiling - loc` would be 21).

## Pre-mortem (failure modes)

| Risk | Guard |
|---|---|
| A dense file is recorded above `MAX` (690 pinned as 710) and then grows past the cap | An entry above `MAX` is legacy and must match exactly, so 690 against 710 fails (AT-3) |
| A stale ceiling lets a shrunk file regrow silently | Slack over `2 * MARGIN` fails (AT-2) |
| The cap or the goal rises in the same change | `MAX <= 700` and `GOAL <= 600` are asserted (AT-6) |
| The conversion edits a legacy value or a file under `src/` | AT-5 checks the conversion. The diff touches `tests/`, `CLAUDE.md` and `docs/` only (reviewer check) |
| A pin is moved by more than the change adds | Recurring-defects item 11: the report lists each moved pin |

## Acceptance tests

Each test is red on the wrong build: the current exact-match build, or the mutant named for it.

| # | Test | Red on |
|---|---|---|
| AT-1 | `_size_verdict` branch table: `(660, 670)` passes; `(671, 670)` fails; `(700, 700)` passes; `(701, 700)` fails | current build (`(660, 670)` fails today) |
| AT-2 | `(620, 661)` fails with "ratchet down"; `(630, 670)` passes (slack exactly 40) | mutant without the slack check; mutant using `MARGIN` as the bound |
| AT-3 | `(690, 710)` fails; legacy `(809, 809)` passes; `(800, 809)` fails; `(815, 809)` fails | mutant that gives legacy entries headroom |
| AT-4 | `ceiling_for(600) == 620`, `ceiling_for(690) == 700`, `ceiling_for(700) == 700` | mutant without `min(MAX, ...)` |
| AT-5 | Every dense entry satisfies `ceiling == ceiling_for(loc)` at the slice head. Every legacy entry equals its `loc` | a conversion that skips an entry or touches a legacy value |
| AT-6 | `MAX <= 700`, `GOAL <= 600`, `MARGIN <= 20` | a raised cap or margin |
| AT-7 | The three bookkeeping tests pass at `_pipeline.py` = 351 and at 352 | current build (`== 351` fails at 352) |

AT-1 to AT-4 and AT-6 are cases in `test_size_verdict_branches` and new small tests in the same file.
AT-5 is a one-off check in the build report, not a standing test, because the census moves after this
slice. Mutations to run on the final head: drop the slack check, drop `min(MAX, ...)`, give legacy
entries headroom, change `<=` to `<` in the ceiling check. Each must turn a test red.

## Reuse inventory, enumerations, frozen contracts

- **Reuse:** `_loc` (the `wc -l` convention), `_size_verdict` and its branch table, and the
  `module-size` key ratchet, all unchanged in role.
- **Enumerations:** every exact line-count pin in the engine tests, found by grepping `tests/`
  for `_loc(` and line-count asserts:
  - `tests/sentry/test_module_size.py:69` `ALLOWLIST` (dense and legacy entries; caps at `:59-60`,
    verdict at `:150`, branch table at `:223`);
  - `tests/unit/execution/test_b6a_bookkeeping.py:29`, `test_b6b_bookkeeping.py:34` and
    `test_auto_chunk_bookkeeping.py:31`, each `_pipeline.py == 351`.
  Exit paths, routes, FK shapes, fields and output surfaces: none, test-only.
- **Frozen contracts:** none. Test-only change.

## Placement

| Change | File | Sits beside |
|---|---|---|
| `MARGIN`, `ceiling_for`, new verdict branches, docstring | `tests/sentry/test_module_size.py` | `GOAL`, `MAX`, `_size_verdict` |
| Dense census values re-pinned | `tests/sentry/test_module_size.py` `ALLOWLIST` | existing entries |
| `== 351` becomes `<= ceiling_for(351)` | `tests/unit/execution/test_b6a_bookkeeping.py`, `test_b6b_bookkeeping.py`, `test_auto_chunk_bookkeeping.py` | the existing `<= 600` asserts |
| Policy wording | `CLAUDE.md`, `docs/decisions/module-size-census-history.md` | the size bullet; the header line |

No new file. No `src/` change.

## Slices

| Slice | Repo | Branch | Size | Gate |
|---|---|---|---|---|
| E1 | engine | `feat/size-ceil-e1` | S (about 60 hand-written lines plus the census re-pin) | T1: `tests/sentry/` and the three bookkeeping files |

Out of scope: the CLI's own module-size sentry (layout plan, `C:tests/sentry/layout/`). It should
take the same rule in a CLI slice once that sentry exists.
