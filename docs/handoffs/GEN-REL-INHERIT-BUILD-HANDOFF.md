# GEN-REL then GEN-INHERIT build: relational generation (roadmap rows 24n and 24r)

Repo `louiskeep/decoy-engine`. Three plans, both on plan branches (read them with `git show origin/<branch>:<path>`):

- REG-RELEASE (slice 0 only): `docs/plans/2026-11-11-reg-release.md` on `plan/reg-release` at `c9d2eb72`. Plan gate: Codex GO on round 3 (2026-11-12), 0 V1 findings. Owner decision R1 (pending deprecations and summary fixes on released codes, stamped at release) is frozen in it. Build only its E2; E1 is already merged (#450).
- GEN-REL: `docs/plans/gen-rel.md` on `plan/gen-rel` at `26a83542`. Plan gate: Codex GO on round 3 (2026-11-11), 0 V1 findings. Owner decisions are frozen in it.
- GEN-INHERIT: `docs/plans/2026-11-10-gen-inherit.md` on `plan/gen-inherit` at `c31ca102`. Plan gate: Codex GO on the re-check (2026-11-11), 0 V1 findings.

Start from `main` at `0651d215` or later. Status: handoff. You build only. You do not review your own work and you do not merge.

## What you are building

GEN-REL adds cross-table generation: bounded reference repair (a child row count per parent within min and max), self-referencing tables, a date anchored to a parent row's date (`windowed_date` with `anchor_via`), and per-parent aggregates (`derived_aggregate` with `from_table` and `via`). It adds one central generate-input registry (`OptionSpec` descriptors and `iter_generate_input_refs`) that strict schema checks, ordering, reproducibility and numeric typing all read. GEN-INHERIT then adds `inherit` (copy a parent column through the child's reference) and the conditional categorical (`by:` beside `condition_on`), both on GEN-REL's registry, parent lookup and refusal codes.

The plans are the spec. For GEN-REL read Behavior, Frozen contracts, Placement, Slices and Acceptance tests in full before the first edit. Where this handoff and a plan disagree, the plan wins; report the disagreement.

## Slice chain (build in this order)

| # | Slice | Plan | Depends on | Branch |
|---|---|---|---|---|
| 0 | E2: release-time registry stamping, `since=UNRELEASED`, versioned release pin, `--to-unreleased` conversion (no per-PR registry bumps from here on) | REG-RELEASE | main | `feat/reg-release-e2` |
| 1 | E1: bounded reference repair, capacity layer, relationship parser and safe messages, warn/preserve/remap policies, fitted precedence. Declare every new reason code `since=UNRELEASED` | GEN-REL | REG-RELEASE E2 | `feat/gen-rel-e1` |
| 2 | E2: self-reference, cycle refusal, option matrix, pinned and seed cells | GEN-REL | E1 | `feat/gen-rel-e2` |
| 3 | E3a: parent-anchored `windowed_date`, typed parent lookup, five coded date failures, numeric typing on the central edges | GEN-REL | E2 | `feat/gen-rel-e3a` |
| 4 | E3b: per-parent `derived_aggregate`, every op, childless identity | GEN-REL | E3a | `feat/gen-rel-e3b` |
| 5 | E2: conditional categorical (`by:`) | GEN-INHERIT | E3b | `feat/gen-inherit-e2` |
| 6 | E1: `inherit` over `_parent_lookup` and the resolved `inherit.column` edge | GEN-INHERIT | GEN-INHERIT E2 | `feat/gen-inherit-e1` |

Use the exact source and test files the GEN-REL Slices table lists for each slice, and the red-first order in "Red-first execution per slice". GEN-INHERIT's own Slices section lists its files.

Stacking: the plan says each slice starts from main after the previous one merges. You cannot merge, so stack instead: each slice branches from the previous slice's branch head (slice 0 from `main`). One PR per slice, base = the previous slice's branch (slice 0's base is `main`). Slice 0 is a frozen-contract change: it needs a full suite on its merge head, which the orchestrator runs. The orchestrator retargets and merges in order. If main moves under you, merge `origin/main` into the slice (never rebase a pushed branch, never force-push).

You may take the whole chain. Stop and report at any slice where: the plan is wrong or silent on something that changes a guarantee; a defect of a class the plan already closes appears again; a census number (module count, file size, registry count) would have to rise beyond what the plan allows; or a test needs weakening to pass.

## Per slice: done when

1. First commit = the slice's red tests (the acceptance tests the plan tags for that slice, in the plan's red-first order), recorded red on the slice start commit with the reason (absent API, wrong outcome). A test that passes unexpectedly is tied to a named mutation and shown red on it, or the slice stops.
2. Then the production change, exactly the files the plan lists for the slice.
3. Each mutation the plan names for the slice applied once on the final source head, its test shown red, then reverted. A later source edit invalidates that evidence; rerun it.
4. A `changelog.d/<slug>.md` fragment in the format of `changelog.d/README.md` for any `src/` change. Docs the plan names for the slice updated.
5. E1 only: the slice report lists every re-pinned snapshot (owner decision 4, the one-time bounded-reference output change, accepted pre-GA, on both the v2 path and the V1 ColumnGenerator path).
6. Checks green on the exact head:
   ```
   uv sync --frozen --extra dev
   scripts/dev/gates.sh --steps ruff,format,mypy,sentry,layers,licenses,tests <selected-test-paths>
   ```
   where the selected paths are the plan's T1 set for the slice (changed tests, importer and symbol hits, the new sentries, `test_generate_option_reads.py`, import-layer, module-size, reason-registry, pinned, streamed and snapshot tests). Record `python -V`, PyArrow and DuckDB versions and the failure sets on the branch and on main in `docs/records/gen-rel-<slice>-evidence.md` (or `gen-inherit-<slice>-evidence.md`). Say what you did not run. No full suite per slice; the orchestrator runs it before merge.
7. Push the branch, open the PR (title = the slice's commit subject; body: what it does, the red-first and mutation evidence, the section 9 compatibility checklist copied from a recent engine PR such as #480, and the final line `cam`). Report the PR number and head, then start the next slice.

## Rules

- V1 bar: only a function break, data loss or a real value reaching a non-admin blocks V1. Anything else you notice goes in the evidence file as V2-LOG; do not build it.
- One central mechanism per concern. Generate inputs are resolved only by `iter_generate_input_refs`; the parent row is found only through `generation/relational/_parent_lookup.py`. Never add a second option-key table, sibling resolver or lookup.
- Reason codes are fixed strings registered in the reason registry. No values, row positions or submitted policy tokens in messages, warnings or logs (codes and counts only).
- Never widen a threshold, raise a census number or pin, skip a test, or weaken an assertion to make a change pass.
- No new top-level package; place files per the plan's Placement table. Files around 800 lines at most.
- Commits: Conventional Commits subject, at most 72 characters, imperative, no body unless it explains why, final line `cam`. No file lists, test counts or tool trailers.
- No force-push, no rebase of pushed branches, no edits to `main` or to any branch you did not create.
- Do not review your own work. Codex reviews every slice PR on the orchestrator side; fix what it returns on the same branch.

## Report per slice

PR number, head SHA, red-first summary (tests and why red), mutations run, check results, re-pinned snapshots (E1), anything not run, V2-LOG items, and any stop condition hit.

cam
