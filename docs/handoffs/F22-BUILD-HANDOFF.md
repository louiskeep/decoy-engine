# F22 publication build: slices a1a to c2 (roadmap row 59)

Repo `louiskeep/decoy-engine`. Plan: `docs/plans/2026-10-30-f22-publication-replan.md` on `main` (revision 11, V1 scope, merged in engine #485; start from `main` at `9878d17e` or later). Status: handoff. Plan gate: Codex GO on revision 11 with 0 V1 findings (2026-11-11). You build only. You do not review your own work and you do not merge.

## What you are building

F22 makes output publication durable and safe under crash, retry and two concurrent writers: a durable directory publish with a lock, a strict descriptor, `publish_unit` with judge-and-bind, vault-before-output, sink restart recovery, and the isolated route on the same bind. The plan is the spec. Read sections 0, 2, 3, 5, 7, 9 and 10 in full before the first edit. Where this handoff and the plan disagree, the plan wins; report the disagreement.

## Slice chain (plan section 9; build in this order)

| # | Slice | Depends on | Branch |
|---|---|---|---|
| 1 | a1a: `_make_dir_core`, `make_parent_dir_at` at the real name, both POSIX policies | main | `feat/f22-a1a` |
| 2 | a1b: durable dir publish with the chain to the anchor, `publication` label, `OutputDir.lock`, `read_member`, `sync_chain` | a1a | `feat/f22-a1b` |
| 3 | a2: descriptor codec, `result_id`, request and receipt types, strict stored types | a1b | `feat/f22-a2` |
| 4 | a3: `publish_unit` judge and bind under the lock, D2 chain, `publish_tables`, registry codes | a2 | `feat/f22-a3` |
| 5 | b1: `OutputDir.adopt_file`, `VaultCommit`, `VaultWriter.publish` with the vault chain | a3 | `feat/f22-b1` |
| 6 | b2: rule V inside `publish_unit`, republish presence, digest and writer-row check | b1 | `feat/f22-b2` |
| 7 | c1a: sink state table, `StagedParquetWriter` split, characterization | a3 | `feat/f22-c1a` |
| 8 | c1b: `reconcile` from UNCERTAIN, restart through `commit()`, route wiring | c1a | `feat/f22-c1b` |
| 9 | c2: isolated parent and fallback on `publish_unit`; delete `atomic_commit` | c1b | `feat/f22-c2` |

Not in this handoff: w1 (Windows arm, built later in the QA window), a3r and the crash-safe parent rename (cut to V2), platform P1/P2 (another repo).

Stacking: each slice branches from the previous slice's branch head (a1a from `main`). One PR per slice, base = the previous slice's branch (a1a's base is `main`). The orchestrator retargets and merges in order. If main moves under you, merge `origin/main` into the slice (never rebase a pushed branch, never force-push). b1/b2 and c1a can run in either order after a3; keep the order above unless blocked.

You may take the whole chain. Stop and report at any slice where: the plan is wrong or silent on something that changes a guarantee; a defect of a class the plan already closes appears again; a census number would have to rise; or a test needs weakening to pass.

## Per slice: done when

1. First commit = the slice's red tests from plan 7.2 (rows tagged with that slice), recorded red on the slice start commit with the reason (absent API, wrong outcome). Every generated test carries its non-vacuity assertion (plan 7.1).
2. Then the production change, exactly the files plan 9 lists for the slice. New POSIX-only calls go in the POSIX arm of an explicit `sys.platform == "win32"` branch (plan 5.8, H2).
3. Each mutation named for the slice in plan sections 3 and 7.2 applied once, its test shown red, then reverted.
4. `docs/publication.md` updated for what the slice adds (create it in a1a). A `changelog.d/<slug>.md` fragment in the format of `changelog.d/README.md` for any `src/` change.
5. Checks green on the exact head:
   ```
   uv sync --frozen --extra dev
   uv run pytest tests/unit/publication tests/fileaccess tests/sentry -q -p no:cacheprovider
   uv run pytest tests/unit/execution tests/integration tests/parity tests/security -q -p no:cacheprovider
   uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src/decoy_engine
   ```
   plus `bash scripts/dev/gates.sh --keep-going --baseline <slice test paths>`. Record `python -V`, PyArrow and DuckDB versions and both failure sets (branch and main) in `docs/records/f22-<slice>-evidence.md`. Say what you did not run. No full suite per slice; the orchestrator runs it before merge.
6. Push the branch, open the PR (title = the slice's commit subject; body: what it does, the red-first and mutation evidence, the section 9 compatibility checklist copied from a recent engine PR such as #480, and the final line `cam`). Report the PR number and head, then start the next slice.

## Rules

- V1 bar: only a function break, data loss or a real value/secret reaching a non-admin blocks V1. Anything else you notice goes in the evidence file as V2-LOG; do not build it.
- Fix the class, not only the instance. One central mechanism per concern; never a second copy of a choke point (for example a second path validator or a second lock).
- Never widen a threshold, raise a census number or pin, skip a test, or weaken an assertion to make a change pass.
- No new top-level package; place files per plan 6.1 Placement. Files around 800 lines at most.
- No values or expression text in logs or error messages (codes and counts only).
- Commits: Conventional Commits subject, at most 72 characters, imperative, no body unless it explains why, final line `cam`. No file lists, test counts or tool trailers.
- No force-push, no rebase of pushed branches, no edits to `main` or to any branch you did not create.
- Do not review your own work. Codex reviews every slice PR on the orchestrator side; fix what it returns on the same branch.
- Worker standards and defect catalog: `docs/handoffs/WORKER-STANDARDS.md` and `docs/handoffs/DEFECT-CATALOG.md` on decoy-platform branch `handoffs/queue` if you can read it; the rules above are the binding minimum.

## Report per slice

PR number, head SHA, red-first summary (tests and why red), mutations run, check results, anything not run, V2-LOG items, and any stop condition hit.

cam
