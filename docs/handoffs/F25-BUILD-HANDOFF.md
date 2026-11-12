# F25 build: protected scratch on A-4 (roadmap row 58)

Repo `louiskeep/decoy-engine`. One plan, on a plan branch (read it with `git show origin/plan/f25-v1-finish:<path>`):

- F25 V1 finish: `docs/plans/2026-10-30-f25-4c-consumer-capabilities.md` on `plan/f25-v1-finish` at `bfba949b`, revision 8.3. Plan gate: Codex GO on 8.3 (0 V1 findings, 15 V2-LOG items). Dennis rounds are used up. The plan's status line says no build is authorized; this handoff is that authorization.
- Background only, same branch: `docs/plans/2026-10-29-f25-protected-scratch-quotas-orphan.md` and `docs/f25-slice0/`. Revision 8.3 replaces their design where they differ. Read them only where 8.3 cites them.

Start from `main` at `c96e2855` or later. Status: handoff. You build only. You do not review your own work and you do not merge.

## Already on main (do not rebuild)

- S1a-0 merged as #416 (`809c2218`). `fileaccess/_output.py` has `make_stage_dir_at`, `create_private_at` and `create_private_child_at`. `fileaccess/_protected_dir.py` has `_allocate`, `_parent_base`, `_register`, `_unregister`, `_owned_private` and the win32 `_win_create_file`. `tests/fileaccess/scratch/` exists with `test_private_creators.py`, and CODEMAP already lists `fileaccess/scratch/`, so S1a does not add that row.
- LIB L0.2 merged as #411 (`942dae2d`). `_ENGINE_ROOTS` is gone. `engine_scratch_root` is still at `execution/out_of_core/_scratch.py:39`; S2a-1 deletes it. The plan's "after L0.2 merges" condition is met. Re-read #411 before S2a-1, S3a and S3b, as plan section 13 asks.
- The plan's anchors are on `a5eb66e2`. Main has moved since. Re-check each anchor you touch on the slice start commit. Report any that moved in a way that changes the design.

## The old branch is dead

`origin/feat/f25-protected-scratch` (`4c901d38`) is 140 commits behind main and carries the revision-1 design. Do not build on it, rebase it, merge it or cherry-pick from it. Build fresh from main. Plan section 10.1 names the only two things you may take from it, with `git show` into your branch: `scratch/_reports.py` (trimmed, as `fileaccess/_scratch_reports.py`, S1a) and the faulting-file `ENOSPC`/`EDQUOT` injection from `tests/scratch/test_quota_enforcement_fs.py` and `tests/scratch/_fakes.py` (rewritten for T-A02b, S4a). Leave that branch untouched.

## What you are building

Engine scratch gets one capability, `ScratchArena`, an A-4 `StageDir`. Its root and missing parent levels come from L0.1's allocator; its child directories and private files come from the named creators in `_output.py`. The arena itself creates nothing. Scratch is 0700 (directories) and 0600 (files) under any umask, 0777 included. Each producer in plan section 3 (T01-T08, T11-T13, T17, T18) moves onto the arena behind four transition arms, which S2b-3 and S4b end. `scratch_native` maps DuckDB and pyarrow failures to value-free errors. A job out of scratch space stops with a frozen `QuotaReport` and commits nothing. `sweep_scratch()` (S5) removes only marker-proven, unlocked arenas under the one temp root.

The plan is the spec. Read sections 1 (guarantee and named limits), 2 (invariants), 4 (design), 4.9 (Placement), 5 (acceptance tests), 8 (frozen contracts) and 10 (slices) in full before the first edit. Where this handoff and the plan disagree, the plan wins; report the disagreement.

## Parallel work you must not collide with

F22 is being built by another remote worker. Its open PR #488 (`feat/f22-a1a`, head `126fa464`) rewrites `fileaccess/_output.py` (a new `_make_dir_core` and `make_parent_dir_at`; `make_stage_dir_at` now calls the core). It also edits `fileaccess/_local.py` and `tests/sentry/file_access_registry.toml`.

- Keep out of `fileaccess/_output.py`. No remaining F25 slice lists it. If a slice turns out to need it, stop and report.
- If any F22 slice lands on main first, merge `origin/main` into your current slice branch before you go on. Record the merge commit in the report.
- S1b's `[create_choke_point]` sentry names the only `_output.py` functions allowed to create (plan section 5, the `test_arena_prefix_used_only_by_open` row). Write that list from `_output.py` as it is on main at S1b start. If F22's creators are on main and the plan's list does not fit them, stop and report. Do not widen the rule yourself.
- `tests/sentry/file_access_registry.toml`, `file_access_modules.toml` and their baselines are a one-writer lane. On a merge conflict there, keep both sides' rows, drop none, and say so in the report.

SCAN-ML M5 (also remote) makes `config` a required keyword on `run_fk_out_of_core` in `execution/out_of_core/_runner.py`, which S2a-1, S2a-1a, S2b-1 and S4a also edit. Same rule: if it lands first, merge `origin/main` and keep its signature.

## Slice chain (build in this order)

Plan section 10 order. F25's own test-migration slices M1, M2 and M3 are written F25-M1, F25-M2 and F25-M3 here so they are not confused with SCAN-ML parts.

| # | Slice | Content | Depends on | Branch |
|---|---|---|---|---|
| 1 | S1a | Arena, marker, release rule, `_adopt`, `_scratch_reports.py`; registry rows for the arena's root open, `rename`, `unlink`, `rmdir` | main | `feat/f25-s1a` |
| 2 | S1b | `scratch_native` (memory and else arms), prefix and choke-point sentries, evasions; CODEMAP `sentry/scratch/` | S1a | `feat/f25-s1b` |
| 3 | S2a-1 | T01 root, T18 hand-over, `engine_scratch_root` deleted; CODEMAP `unit/execution/scratch/` | S1b | `feat/f25-s2a-1` |
| 4 | S2a-1a | T01 subtrees, arm (a) | S2a-1 | `feat/f25-s2a-1a` |
| 5 | S2a-1b | Stager, emit (arm (d)); `ScratchAccess` moved into fa `_scratch.py`; `scratch_access_for` deleted | S2a-1a | `feat/f25-s2a-1b` |
| 6 | S2a-1c | Relations, connections; arms (b), (c) | S2a-1b | `feat/f25-s2a-1c` |
| 7 | S2a-2 | Joiners on the arena (arm (b)) | S2a-1c | `feat/f25-s2a-2` |
| 8 | S2b-0 | Q5 benchmark: sorter-merge and payload-store medians | S2a-2 | `feat/f25-s2b-0` |
| 9 | S2b-1 | Spools, `ipc_iter_stream`, `stream_table` on the arena | S2b-0 | `feat/f25-s2b-1` |
| 10 | S2b-2 | Sorter, reorder joiner; last F06 helpers deleted | S2b-1 | `feat/f25-s2b-2` |
| 11 | F25-M1 | Migrate up to 10 test files (stager, emit, relation, joiner, stream-driver callers) | S2b-2 | `feat/f25-m1` |
| 12 | F25-M2 | Migrate the next up to 10 test files | F25-M1 | `feat/f25-m2` |
| 13 | S2b-3 | Arms (a), (b), (d) ended | F25-M2 | `feat/f25-s2b-3` |
| 14 | S3a | T11, T12; payload writer to `_isolated_commit.py` | S2b-3 (plan: S1b and L0.2) | `feat/f25-s3a` |
| 15 | S3b | Probe first; T13, T17 | S3a (plan: S1b and L0.2) | `feat/f25-s3b` |
| 16 | S4a | Report arms, fixed budget text; T-A02a/b, T-A08, T-A10 | S3b (plan: S2b-3 and S3a) | `feat/f25-s4a` |
| 17 | F25-M3 | Migrate `connect_duckdb` and pool-quality test callers | S4a | `feat/f25-m3` |
| 18 | S4b | Arm (c) and `_adopt` ended; T08; `test_ooc_internals_take_only_an_arena` green | F25-M3 | `feat/f25-s4b` |
| 19 | S5 | `_scratch_sweep.py`, sweep with real subprocesses; `docs/security/file-access-policy.md` | S4b (plan: S3a) | `feat/f25-s5` |

Not yours: S1w (the win32 arm). It needs WIN-RUNNER and the L0.1w Windows host, which you do not have. Do not build it.

Use the exact source and test files the plan's section 10 table lists for each slice, the acceptance tests section 5 tags with that slice, and the transition arms as section 10 describes them.

Stacking: the plan lands every slice on one integration branch, `feat/f25-v1-scratch`, merged to main once after S4b (owner answer Q6). You do not create or push that branch; the orchestrator owns it. You stack instead: each slice branches from the previous slice's branch head (S1a from `main`). One PR per slice, base = the previous slice's branch (S1a's base is `main`). The orchestrator retargets and merges in order. If main moves under you, merge `origin/main` into the slice (never rebase a pushed branch, never force-push).

Full suite. These slices hit a trigger, so the orchestrator runs the full suite on their merge head (you do not):
- S1a and S1b: additive `fileaccess` exports (`ScratchArena`, `QuotaReport`, `CleanupReport`, `scratch_native`) and the error-class change (a non-resource scratch failure now raises `FileAccessReadError("engine.scratch")` under ambient, plan section 8).
- S2a-1: `run_fk_out_of_core(temp_dir=...)` now makes a private child in a caller directory (frozen entry-point meaning).
- S3a: the same change for `run_pipeline_isolated(staging_dir=...)`.
- S4b: the plan's single merge point to main.
- S5: `sweep_scratch` and `SweepReport` exports.
- F25-M1, F25-M2, F25-M3 if you change a shared fixture or `conftest.py`.
- Any slice where you cannot tell which tests your change reaches. Say so.

You may take the whole chain. Stop and report at any slice where: the plan is wrong or silent on something that changes a guarantee (G1-G7); a defect of a class the plan already closes (section 6) appears again; a slice would go over 10 files or 800 lines (the plan says split; the orchestrator picks the split); a file would pass its size limit; a test needs weakening to pass; or a step needs `_output.py`.

## Per slice: done when

1. First commit = the slice's red tests (the section 5 rows tagged for that slice), recorded red on the slice start commit with the reason (wrong mode, absent behavior). An `ImportError` is not a red (plan section 5). A row marked "green by design" or green on F06 is shown red against its named control or mutation instead. A test that passes unexpectedly is tied to a named mutation and shown red on it, or the slice stops.
2. Then the production change, exactly the files the plan lists for the slice. At most 10 files and 800 lines.
3. Each mutation the plan names for the slice applied once on the final source head, its test shown red, then reverted. A later source edit invalidates that evidence; rerun it.
4. A `changelog.d/<slug>.md` fragment in the format of `changelog.d/README.md` for any `src/` change. Main uses fragments now; the fragment fills the plan's "CHANGELOG" slot and counts as that file. Docs and CODEMAP rows the plan names for the slice updated.
5. Size limits on `c96e2855`: `_relation.py` and `_batch_join.py` are 599 lines; `_stream_join.py` is 645 and `_isolated_run.py` is 659, and both may only shrink. If a slice would push `_relation.py` or `_batch_join.py` past 600, stop and report. Do not add a `test_module_size.py` census entry yourself, even though the plan allows one; the orchestrator decides.
6. Run the tests as a non-root user. The mode tests assert `geteuid() != 0` first. If you can only run as root, stop and report.
7. Checks green on the exact head:
   ```
   uv sync --frozen --extra dev
   scripts/dev/gates.sh --baseline --steps ruff,format,mypy,sentry,layers,licenses,tests <selected-test-paths>
   ```
   where the selected paths are the slice's section 5 test files, `tests/sentry`, `tests/fileaccess` (plan "Loop per slice"), plus every file `git grep` finds in `tests/` for each changed signature's name (gates.sh also adds every test file that imports a changed module). Record `python -V`, PyArrow and DuckDB versions, the exact commands, and the failure sets on the branch and on main in the PR body under "Evidence" (no separate record file: the plan's 10-file bound counts every file). Fill in the TEST SCOPE block from the rules below. Say what you did not run.
8. S2b-0 only: five runs each of the sorter-merge and payload-store benchmarks, before and after, on one machine. Report the medians and the machine. If the regression is over 5%, follow plan Q5 (T06/T07 keep path writers, L1 widens) and say so.
9. Push the branch, open the PR (title = the slice's commit subject; body: what it does, the red-first and mutation evidence, the Evidence section, the `docs/compatibility-contract.md` section 9 checklist copied from a recent engine PR such as #480, and the final line `cam`). Report the PR number and head, then start the next slice.

## Rules

- V1 bar: fix only what (1) breaks product function, (2) loses or corrupts data, or (3) puts real values or secrets where a non-admin can see them (API responses, UI, `Job.log`, notifications, output files, world-readable files; `str(exc)` counts). Values, paths or class names only in admin logs, tracebacks or `__cause__` chains, hostile in-process code, and "every entry point, provably" completeness are V2. Do not fix them. List each under "V2-LOG" in the report with file and line, one line each.
- One central mechanism per concern. Directories come only from L0.1's allocator; descriptor-relative children and private files only from `_output.py`'s named creators. `fileaccess/_scratch.py` and `_scratch_sweep.py` hold no `mkdir`, `mkdtemp`, `chmod` or `O_CREAT`. Never add a second allocator, creator, issued-root set or error class.
- Reports and errors are value-free: no path, table, column or cell value in `str()` or `repr()`. Codes and counts only.
- Never raise a cap, ceiling, pin or ratchet: no higher module-size number, no higher boundary-registry count (M-RATCHET counts only drop), no wider threshold, no skipped test, no weaker assertion. If a change needs one, stop and report.
- Red-first acceptance tests, and each named mutation run on the final head, for every new mechanism. Never commit with a mutation applied.
- No new top-level package. Place files per plan section 4.9. Files around 800 lines at most.
- Tests: one test process at a time, `-n 2` at most, no perf suite (S2b-0's named benchmarks are the one exception). Never claim "suite green". Report failing ids against main's failing ids, any new id, any main failure that now passes, and any change in the collected count. Never edit a known red test to make it pass.
- Commits: Conventional Commits subject `type(scope): summary`, at most 72 characters, imperative, lowercase after the colon, no period. Scope may carry the slice id, for example `feat(f25 S1a): add the scratch arena`. No body unless it explains why, in one to three short sentences. Last line exactly `cam`, nothing after it, no other trailers. Never in a body: test counts, file lists, gate or review history, templates, headings, bold.
- No em-dashes anywhere: code, comments, docs, commits, PR text.
- Never force-push. Never rebase a pushed branch. No edits to `main` or to any branch you did not create.
- Do not review your own work. Codex (and Dennis on S1a, S3a and S5, per the plan's Gate column) reviews each slice PR on the orchestrator side; fix what it returns on the same branch.
- Stop and report on any plan gap, or when a test, threshold or rule would have to be weakened.

TEST SCOPE block (fill in for every slice report):
```
Tiers:       T0, T1 [, T3 because <trigger>]
Named tests: <files and ids>
Selection:   scripts/dev/gates.sh --baseline
Plus:        <git grep hits for changed signatures>
Sentries:    <paths>
Triggers:    none | <trigger>
Mutations:   <mechanism -> test that went red>
Full suite:  not at slice | trigger <name>, orchestrator runs it
```

## Report per slice

PR number, head SHA, red-first summary (tests and why red), mutations run, check results with exact commands and failing ids against main, the TEST SCOPE block, merges of `origin/main` (commit and why), anchors that moved, anything not run, V2-LOG items, and any stop condition hit.

cam
