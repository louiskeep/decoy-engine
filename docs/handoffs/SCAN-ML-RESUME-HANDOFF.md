Status: plan

# SCAN-ML resume: finish M5 (#508), then M4 (#512); M3 stays blocked (roadmap row 32a)

Written 2026-11-12 20:00 UTC for a fresh remote session that takes over the SCAN-ML stack. It replaces every earlier SCAN-ML message (message 1 to 5). Repo `louiskeep/decoy-engine`.

Read first, in full:
1. `docs/handoffs/SCAN-ML-BUILD-HANDOFF.md` on branch `handoffs/scan-ml-build`. Its "Rules", "Per part: done when", "Report per part", TEST SCOPE block and "Parallel work" sections stay in force. Its "Already on main", "Order" and stop point 1 are out of date; this file gives the current facts.
2. The plan: `docs/plans/2026-11-07-scan-ml.md` on `plan/scan-ml` at `e0ae4204` (Codex GO; owner decisions Q1 to Q7 frozen; `NER_MIN_RATE = 0.211`). Sections 4.3 to 4.5, 4.8, 5, 6, 7, 11, 12, 13 and 14.
3. The two PR threads, #508 and #512, including the fix list comment on #508 of 18:20 UTC.

You build only. You do not review your own work and you do not merge. The orchestrator reviews, runs the full suites and merges.

## Where things stand

On engine main (`75b81efe` at writing time):
- M0 (#396), M1 (#405) and M2 (#492, the classifier stack in the base install) are merged. M2 was the wait for M4; it is over.
- The post-scan blocking verdict merged as #539. Main now owns `PostScanBlockedError` in `verification/postmask/_blocked.py` (class at line 261). Constructor `(report, verdict=None)`. Fields `report`, `reason`, `message`, `verdict`, `code`. It has one `_CODED` row in `reason_adapter.py` (line 95, namespace `post_scan`). `code` is the first blocking code when it is registered, else `None`. A report blocked only by the model check gets the ref `post_scan.ner_blocked`, which the adapter reports as `decoy.unregistered_reason` until M4 registers it.
- REG-RELEASE E2 is merged: new reason codes are declared `since=UNRELEASED`, each with one `changelog.d/` fragment in slash form.

Open:
- #508 `feat/scanml-m5` (M5, base `main`), head `d862aab9`. Review round 1: GO-WITH-FIXES (0 blocker, 0 high, 3 medium, 4 low). The fix round is NOT pushed yet. The PR shows a conflict with main.
- #512 `feat/scanml-m4` (M4, base `feat/scanml-m5`), head `fde9f201`. Not reviewed yet. It still carries its own copy of `PostScanBlockedError` in `verification/postmask/_model_findings.py:67`, a `_FIXED` row for it in `reason_adapter.py:119` and `NER_BLOCKED` bound to that copy in `reasons/post_scan/_part1.py:21`.

Still blocked: M3. It needs SCAN-MULTI S3-engine.2 (`profiling/starter_config.py` is not on main) and INIT-WIZARD E1 (`starter_review.py` and the full `ColumnSuggestion` are not on main). Do not start M3 and do not build a stand-in. Check with `git cat-file -e origin/main:src/decoy_engine/profiling/starter_config.py`.

## Step 1. Fix round 1 on #508 (on `d862aab9`, before any main merge)

Red test first for each item, committed before the fix.

1. MEDIUM-1 [DESIGN], one copy of the pre-read sequence. `execution/native/chunked/_core.py:36-45` and `execution/_chunked_oracle.py:165-189` copy the public entries' pre-read steps by hand (subset refusal, thread check, declaration gate), and those steps also stay in `native/_chunked_entry.py:657-665` and `_chunked.py` `run_mask_pipeline_chunked`. Fix: each public entry becomes `refuse_unchecked_route(config, ...)` followed by `return <core>(...)`, so the sequence lives only in the core. Give the cores typed parameters instead of `**kwargs`. Red test: a probe pre-read step added to the core runs through the public entry (or an equivalent that fails while the copy exists).
2. MEDIUM-2 [DESIGN], the declaration-gate sentry walks the right calls. `tests/sentry/source_config/test_declaration_gate.py:51` checks the gate precedes `_oracle_preflight` in `run_mask_pipeline_chunked`, which no longer calls it, so the check passes vacuously, and the two lane cores are not in `ROUTES`. Fix: the `run_mask_pipeline_chunked` entry (or, after item 1, the core) checks `{"_mask_declared"}`; add `("execution/_chunked_oracle.py", "_mask_pipeline_chunked"): {"_mask_declared"}` and `("execution/native/chunked/_core.py", "mask_chunked"): {"_run_chunked"}` (names as they end up after item 1). Seeded cases: the read moved above the gate goes red; the gate dropped from a core goes red. This is a classification change to a sentry's route table, not a baseline growth.
3. MEDIUM-3 [DESIGN], the guard sentry resolves aliases. `tests/sentry/test_post_scan_route_guard.py:146-153` (`_call_name`) and the caller and resident-lane checks match bare names, and the tree already aliases a watched name (`physical/drivers/_chunked.py:33`, `run_mask_chunked as run_chunked_masking`). Fix: reuse the alias and re-binding resolution the declaration-gate sentry already has. Do not write a second resolver. Seeded cases, one per spelling: `import ... as`, attribute call, `from x import y`.

Logged for V2, do nothing: LOW-1 `config={}` at the shadow coordinator passes the caller sentry; LOW-2 a non-Mapping config raises `AttributeError`, not a typed refusal; LOW-3 private-module reach into `_route_guard` and `secret_providers._errors.raise_bare`; LOW-4 untyped `**kwargs` on the cores (closed by item 1).

Push #508 (plain push). Merge `feat/scanml-m5` forward into `feat/scanml-m4`.

## Step 2. Main merge

1. Merge `origin/main` into `feat/scanml-m5` (#508). Never rebase. In ledger-style files (`docs/reasons.md`, sentry baseline rows, `changelog.d/`, `__init__` export lines, `CODEMAP.md`, `reason_adapter.py` rows) keep both sides. Never drop a registry row on a conflict.
2. Merge `feat/scanml-m5` forward into `feat/scanml-m4` (#512).

## Step 3. M4 onto main's `PostScanBlockedError` (on #512)

1. Delete M4's own `PostScanBlockedError` in `_model_findings.py` and import main's from `_blocked.py`. No second class with that name, no copy, no alias module. The engine `__init__.py` export line points at `_blocked.py`.
2. Drop M4's `_FIXED` row for `PostScanBlockedError` in `reason_adapter.py`, and the one for `PostScanRouteError` if main already has a row for it. Main's rows win.
3. The M4 raise site keeps `raise_bare(PostScanBlockedError(report))` with main's class. Main's verdict already returns `post_scan.ner_blocked` for a report blocked only by the model check; do not compute a second verdict on M4.
4. Register `ner_blocked` in the `post_scan` namespace as pending (`since=UNRELEASED`), bound to main's class, in your one `changelog.d/` fragment. After this, `code` is `ner_blocked` for such a report, not `None`. Add one test that pins that.

## Step 4. Checks and report

On the FINAL head of each PR (a run on an earlier head does not count):
- ruff check, ruff format, mypy on the touched files.
- `python scripts/dev/registry_renumber.py --check --main origin/main`, `scripts/dev/ratchet_check.py --base origin/main`, `scripts/dev/check_fragment.py --base origin/main`: all exit 0.
- The part's T1 selection (handoff "done when" item 7) and the NER tests for real (`pytest -m ner` with `DECOY_REQUIRE_NER=1`, the model wheel from `NER_MODEL_WHEELS` installed as the `ner-gate` job does).
- The compat corpus (`pytest tests/integration/compat_corpus`). A ticked compatibility pre-flight item must point at a real run on that head; untick it otherwise.
- The PR's named mutations on the final head, each shown red, then reverted by editing the line back. Never `git checkout -- <file>`, never `git stash`.

Your environment has 24 failing test ids that main also fails there. "Same as main" means your failing set equals main's in the same environment; say so in the PR body. Put each head and its totals in the PR body, saying which head each total belongs to.

After step 3, do not merge main again unless a PR shows a conflict or a review asks.

Report once, at the end: the new #508 and #512 heads, what each fix item changed in one or two lines, the totals per head, the mutation results, the main-merge commits and what conflicted, anything you could not run, V2-LOG items, and the M3 dependency check result.

## What comes next (do not start it)

- The #508 delta review starts from its pushed head. #512 gets its two reviews (lead reviewer, then the outside reviewer) after #508 merges. Fix lists are posted on the PRs. A fix list on #508 is done before anything new on #512, then merged forward.
- M3 starts only after SCAN-MULTI S3-engine.2 and IW E1 are on main. You will be told.

## Rules in short (the build handoff has the full list)

- V1 bar only: fix what breaks function, loses data, or shows real values or secrets to a non-admin. Everything else goes to V2-LOG with file and line.
- One central mechanism per concern: `ner_rates` is the only NER scoring entry, `_route_guard.py` the only post-scan route guard, `classify_frame` the only classifier inference, main's `PostScanBlockedError` the only blocked-run error.
- Value-free output: codes, counts and buckets only, never a cell value, span or entity text.
- Never raise a cap, ceiling, pin, ratchet or baseline. No skipped test, no weaker assertion. Stop and report instead.
- Commits: Conventional Commits subject, at most 72 characters, no body unless it explains why, last line exactly `cam`, no other trailers. No em-dashes anywhere.
- Plain push only. Never force-push, never rebase a pushed branch, never edit `main` or a branch you did not create. Never merge a PR.
- Do not collide with F25 (`execution/out_of_core/_runner.py`) or F22 and GEN-REL (the reason registry): merge main and keep both sides.

cam
