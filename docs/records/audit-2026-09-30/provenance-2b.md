# Provenance backfill: stage 2b rows R025-R042

Status: record (input to `docs/plans/2026-09-30-rust-coverage-evidence-audit.md`)

Stage 2b's rows in `docs/records/audit-2026-09-30/runs.jsonl` (R025-R042) do not each
carry a `commits` field the way the stage 2d harness's records do. This note states
what environment they ran in, so a reader does not have to guess. It does not edit
those rows: per the task that produced it, the original records stay as written.

## Environment

All of R025-R042 ran in the single dedicated audit environment recorded in
`docs/records/audit-2026-09-30/environment.json`, the same venv stage 2a's R001-R024
used and stage 2c's R043-R068 continued in:

- Engine: this worktree (`docs/reality-2026-09-30`), editable install, commit
  `7cadcde08ea5876217de2490b06a7f9e1ced5a40` at the time environment.json was written.
- Platform: `/home/cam/vscode/decoy-platform/.claude/worktrees/audit-pinned`, a detached
  worktree at `origin/main` `0701a95422e405cff76eb38c12cfb4f372b151d4`.
- CLI: `/home/cam/vscode/decoy/.claude/worktrees/audit-pinned`, `origin/main`
  `b8274b0194748d5a60262b78b5969a8c85aeeac7`.
- Native companion: built from this worktree's pinned HEAD, `present=True, ok=True,
  abi decoy-native-abi-2, version 0.1.0`, module SHA-256
  `1a3b2d18e4e092691e05f7ab0c8e1725f6aeb94bc19e46f9adaced472510b061`.

The stage-2b-summary.md header states this explicitly (engine editable from the
worktree, platform installed from the detached worktree at `origin/main` `0701a954`,
CLI installed from a new detached worktree at `origin/main` `b8274b01`), and the git
commit log below shows the runs happened while the engine tree sat at exactly this
commit.

## Timing (from `git log`)

| Commit | Time (UTC) | What |
|---|---|---|
| `c882f32a` | 2026-09-30 09:38:25 | stage 2a probe harness + first batch committed |
| `320d6f1b` | 2026-09-30 09:39:48 | Codex independent audit recorded |
| `21298190` | 2026-09-30 10:04:47 | stage 2b committed (R025-R042) |
| `47e49e47` | 2026-09-30 10:26:34 | stage 2c committed (R043-R068) |

Stage 2b's runs happened between the 09:39 and 10:04 commits, on the same day, in the
same running venv as stage 2a and 2c -- there was no environment rebuild, engine
reinstall, or companion rebuild between stages. The engine worktree's `HEAD` did not
move during this window: the next commit after stage 2a's harness (`c882f32a`) that
touches this worktree at all is stage 2b's own commit (`21298190`), and that commit
adds only `docs/` and `scripts/audit/` files (see below), never `src/`.

## `7cadcde0` = `8dc559e5` plus docs/scripts only

Verified directly, not asserted from memory:

```
$ git diff --stat 8dc559e5 7cadcde0 -- src
(no output)
```

`git diff --stat 8dc559e5 7cadcde0` (no path filter) shows the full set of changes
between the plan's pinned baseline and the commit environment.json recorded for stage
2a/2b/2c: four files, all under `docs/` and `scripts/audit/`, 1,211 insertions, zero
deletions, and critically, zero files under `src/`:

```
 .../2026-09-30-rust-coverage-evidence-audit.md     | 109 ++
 .../audit-2026-09-30/branch-witness-ledger.md      | 494 ++
 docs/records/audit-2026-09-30/preflight.json       | 103 ++
 scripts/audit/preflight.py                         | 505 ++
 4 files changed, 1211 insertions(+)
```

So every run recorded against engine commit `7cadcde0` (R001 through R068, and by
extension R025-R042) executed the exact same `src/decoy_engine` and
`decoy-engine-native` code as the plan's pinned baseline `8dc559e5` -- the two commits
differ only in the audit's own plan, ledger, preflight record, and preflight script,
none of which the engine imports or executes at runtime. The same holds forward: `git
diff --stat 7cadcde0 HEAD -- src decoy-engine-native` is also empty, so every run in
this file through R083 (stage 2d) ran the same engine code as `8dc559e5`.
