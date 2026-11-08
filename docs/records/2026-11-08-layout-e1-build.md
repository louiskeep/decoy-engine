# LAYOUT E1 build record

Date: 2026-11-08. Branch `feat/layout-e1`, base engine `origin/main` a5eb66e2.
Plan: `docs/plans/2026-11-06-layout-map-and-guards.md`, part E1 (engine map).

## Built

- `CODEMAP.md`: new "Where Things Go" section (section 4 engine rows). Paths re-checked
  against the base. Differences from the plan draft: `execution/native/protocol/`,
  `plan/validate/` and `execution/physical/drivers/` already exist, so they are listed as
  concrete paths, not `(target)`; the Dev tooling row lists the 12 existing `scripts/`
  topic folders; the Reason codes row drops `<namespace>/` because the catalog packages
  are already described by `reasons/`.
- `tests/sentry/layout/__init__.py` and `_layout.py`: `map_paths` and `section` only.
- `tests/sentry/test_package_map_paths.py`: the `_MAP.md` test and a new CODEMAP test both
  call `map_paths` (bases `src/decoy_engine` and the repo root), plus AT-1 seeded-string
  meta-tests (concrete, placeholder, glob, symbol, target, wrong column, missing path,
  base resolution).
- No `src/` file changed. No pin raised.

## Red mutations (AT-1), run on a VM, each reverted after

| # | Mutation | Result |
|---|---|---|
| M1 | CODEMAP row names `src/decoy_engine/nope/` | codemap test red |
| M2 | `_MAP.md` row names `nope/` | `_MAP.md` test red |
| M3 | `_MAP.md` case given the repo root as base | `_MAP.md` test red |
| M4 | placeholder/glob truncation removed | codemap test and 2 meta-tests red |
| M5 | `(target)` treated as concrete | codemap test and target meta-test red |
| M6 | symbols checked as paths | codemap test and symbol meta-test red |
| M7 | walk returns `[]` (vacuous) | 8 meta-tests red; real-map tests stay green, which is why the meta-tests exist |

Green on the final head: `tests/sentry/test_package_map_paths.py` 14 passed.

## Gate fixes

- Every given column is checked: a meta-test names a missing path in the second of two columns (Dennis X4 mutant, `columns[:1]`, now red).
- The CODEMAP row floor is 22, the section's row count.
- The Unit tests row is concrete, not `(target)`: `tests/unit/config/` and `tests/unit/generation/` exist.
- Under the section 4 grammar the `_MAP.md` check skips a bare span with no `/` and no `.py` (for example `README.md`); no current entry is such a span.
