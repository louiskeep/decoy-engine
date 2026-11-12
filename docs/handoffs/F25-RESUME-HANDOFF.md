Status: plan

# F25 resume: land the #500 fix round, merge up the stack once, then report (roadmap row 58)

Written 2026-11-12 21:15 UTC for a fresh remote session that takes over the F25 stack. It replaces every earlier F25 message (message 1 to 4). Repo `louiskeep/decoy-engine`.

Read first, in full:
1. `docs/handoffs/F25-BUILD-HANDOFF.md` on branch `handoffs/f25-build`. Its rules, test scope and report sections stay in force. Where it disagrees with this file on state or order, this file wins.
2. The plan: `docs/plans/` on `plan/f25-v1-finish` at `7cc4a088` (revision 8.6). Sections 5 and 10.2 are the #500 fix list.
3. The #500 thread, in particular the comment of 13:22 UTC ("Plan revision 8.6 is pushed ...", the fix list) and the message 4 comment of 17:46 UTC.

You build only. You do not review your own work and you do not merge. The orchestrator reviews, runs the full suites and merges.

## Where things stand

Engine main is `051b84cb` at writing time. It has the multi-parent crash fix (#537), the subset stage inside the run (#531, #547), F22's one directory creator `_make_dir_core` (#488) and the vault key floor (#511).

The stack, bottom first. Nothing in it is merged.

| PR | branch | base | state |
|---|---|---|---|
| #500 | `feat/f25-s1b` (S1b-i) | main | head `aea91854`; fix round built but not on this branch |
| #502 | `feat/f25-s1b-ii` | #500 | waits |
| #505 | `feat/f25-s2a-1` (S2a-1-i) | #502 | shows a conflict |
| #507 | `feat/f25-s2a-1-ii` | #505 | waits |
| #509 | `feat/f25-s2a-1a` | #507 | waits |
| #513 | `feat/f25-s2a-1b` (S2a-1b-i) | #509 | waits |
| #516 | `feat/f25-s2a-1b-ii` | #513 | waits |
| #518 | `feat/f25-s2a-1b-fix` | #516 | waits |
| #522 | `feat/f25-s2a-1c` (S2a-1c-i) | #518 | waits |

The #500 fix round is on `wip/f25-s1b-fix-round` at `fb5cfd76`: the red cells (`3913aef5`) and the fix (`fb5cfd76`) on top of a clean merge of main `353d98bf` (`cea28646`). It fast-forwards onto `feat/f25-s1b` (checked). Main is 13 commits ahead of it.

## Step 1. Land the #500 fix round

1. Fast-forward `feat/f25-s1b` to `fb5cfd76` and push (plain push).
2. Ruling, the `scan.py` module-size entry: drop it. `test_module_size.py` measures only `src/` and `scripts/`, so the entry gates nothing, and the baseline is shrink-only. `tests/sentry/fa/scan.py` stays unmeasured; keep it under 700 lines. Name the dropped entry in the #500 body as a deviation from plan 10.2.
3. Accepted deviation, name it in the #500 body: the plan's "remove it from the result names" mutation was replaced by treating `to_arrow_reader` as plain data.
4. Merge current `origin/main` into #500 only if #500 then shows a conflict. Otherwise leave it.
5. Put the head and the totals in the #500 body, saying which head each total belongs to.

## Step 2. Merge up the stack once (#502 to #522), never rebase

1. Merge #500 into #502, then each PR into the next, up to #522.
2. On #502, in the follow-up fix commit after the merge commit: `_make_dir_core` joins `OUTPUT_RAW` (it holds the only raw `os.mkdir` in `fileaccess/_output.py`), and `make_stage_dir_at` leaves `OUTPUT_RAW` in the same commit. The list stays at five names. `make_parent_dir_at` does not join. Leave `RootedOutputDir._open_parent` and `ensure_dir` as they are unless the sentry shows either no longer holds a raw creation; then drop it in the same commit and say so. Name it in the #502 body. A new raw `os.mkdir` anywhere else in `_output.py` is a stop.
3. On #505, after the merge commit, one fix commit: the `symlink_refused` cell of `test_caller_temp_dir` becomes `symlink_followed`. The run succeeds, the arena child is made inside the link's target, the link is still a link afterwards, and the target holds no arena child after the run. Keep `strict_refused`. No refusal at the runner. Name it in the #505 body. This also clears #505's conflict.
4. On #522, in the same pass, the section 10.2 changes: drop the `scratch_native` key edit, keep the `scratch_path_release` entry, `_duckdb.py` leaves the pending set (15 names to 14).
5. `execution/out_of_core/_multi_parent.py` (#537): where it conflicts, keep both sides. The fix's rule stays: a relation file is removed only when no remaining edge of the run still reads it.
6. Ledger-style files (`CHANGELOG` fragments, sentry baseline rows, `CODEMAP.md`, `__init__` export lines): keep both sides. Never drop a registry row.

## Step 3. Checks and report

On the FINAL head of each PR you pushed:
- ruff check, ruff format, mypy on the touched files.
- `python scripts/dev/registry_renumber.py --check --main origin/main`, `scripts/dev/ratchet_check.py --base origin/main`, `scripts/dev/check_fragment.py --base origin/main`: all exit 0.
- The PR's T1 selection from the build handoff, and its named mutations, each shown red and then reverted by editing the line back.

Your environment fails some `test_outbound_clients` ids that main also fails there (23 to 24 ids). On the orchestrator's nodes main has 0 failing tests. "Same as main" means your failing set equals main's in the same environment; say so in each PR body.

Report once, at the end: every new head, the totals per head, the mutation results, the merge commits and what conflicted, anything you could not run, and V2-LOG items.

## What comes next (do not start it)

- The delta review of #500 starts from its pushed head. Fix lists then come on each PR, bottom first. A fix list on a lower PR comes before new slices on top, then merge it forward.
- S2a-1c-ii is split (accepted): part a is the relations and the merged copy on the adopted `temp_dir/duckdb`; part b is `read_footer`, the `_route_policy.py` swap and the footer reads, stacked on part a. Start part a only after step 2 and the #500 delta review. A module leaves `SCRATCH_NATIVE_PENDING` in the part that makes it clean, in the same commit; the set only shrinks. A one-line `rules.py` name removal does not count against the 10-file bound; say so in the PR body.
- `execution/pipeline/_subset_stage.py` is a second caller of `declared_casting.stage_typed_sources`. It moves with T18, not earlier.

## Rules in short (the build handoff has the full list)

- Still in force: the relation layout for S2a-1c-ii (adopt `temp_dir/duckdb`, remove then create fresh, fail-closed remove); per-call `scratch_native` as revision 8.6 says (the guarded connection is not adopted); unsigned commits are fine.
- V1 bar only: fix what breaks function, loses data, or shows real values or secrets to a non-admin. Everything else is V2-LOG with file and line.
- Never raise a cap, ceiling, pin, ratchet or baseline. No skipped test, no weaker assertion. Stop and report instead.
- Commits: Conventional Commits subject, at most 72 characters, no body unless it explains why, last line exactly `cam`, no other trailers. No em-dashes.
- Plain push only. Never force-push, never rebase a pushed branch, never merge a PR. Full suites and macOS runs are the orchestrator's.
- Do not collide with F22 (`fileaccess/_output.py` beyond ruling 2), SCAN-ML or GEN-REL (the reason registry): merge main and keep both sides.

cam
