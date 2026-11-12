# SCAN-ML build: M5, then M4, then M3 (roadmap row 32a)

Repo `louiskeep/decoy-engine`. One plan, on a plan branch (read it with `git show origin/plan/scan-ml:<path>`):

- SCAN-ML: `docs/plans/2026-11-07-scan-ml.md` on `plan/scan-ml` at `e0ae4204`. Plan gate: Codex GO. The file's last revision note is Revision 7 (owner decision: `NER_MIN_RATE = 0.211`, already on main at `detection/_ner_rate.py:57`). Owner decisions Q1 to Q7 are frozen in it.
- Shared shapes for M3 and M4 live in the INIT-WIZARD plan, section 4.6, in the CLI repo `louiskeep/decoy`: `docs/plans/2026-11-07-init-wizard.md` on `plan/init-wizard` at `a2ea696`. Its revision 4 already carries the six SCAN-ML alignment edits the plan asks for.

Start from `main` at `c96e2855` or later. Status: handoff. You build only. You do not review your own work and you do not merge.

## Already on main (do not rebuild)

- M0 merged as #396 (`ab7a36fc`): `click` in the `ner` extra, the `ner` marker, `NER_MODEL_WHEELS` in `detection/ner.py`, the `ner-gate` CI job.
- M1 merged as #405 (`38951412`): config fields, `ModelRef` (`detection/types.py:42`), `detection/_ner_rate.py`, `load_detectors`, `DetectorSet`, `classify_frame` and `classifier_status` in `profiling/model_pack/_detectors.py`, `log_detector_run`.
- SCAN-MULTI S1a merged as #414 (`profiling/scan_set.py`).

Not on main:
- M2 (base dependencies: scikit-learn and lightgbm leave the `ml` extra). It is being built locally and is not pushed. Do not build it, and do not guess at its contents.
- SCAN-MULTI S3-engine.2 (`profiling/starter_config.py` does not exist on main).
- INIT-WIZARD E1 (`starter_review.py` and the full `ColumnSuggestion` do not exist on main).

## Order: corrected from M5, M3, M4 to M5, M4, M3

Plan section 13 sets the dependencies:

- M5 needs M1 only. M1 is merged, so M5 builds from main now.
- M4 needs M1, M2 and the IW alignment edits. The edits are done (IW revision 4). So M4 waits only on M2.
- M3 needs M1, M2, SCAN-MULTI S1a, SCAN-MULTI S3-engine.2 and IW E1. S3-engine.2 and IW E1 are not on main, so M3 waits on three merges, not one.

Nothing in M4 uses M3's code (M4's classifier pass uses M1's `classify_frame`). So M4 goes before M3.

## What you are building

- M5: one postmask route guard. `verification/postmask/_route_guard.py` defines `PostScanRouteError` (`route.post_scan_models_unsupported`). The four non-resident entries (`run_sequential`, `run_fk_out_of_core`, `run_mask_pipeline_chunked`, native `run_mask_chunked`) refuse a config with model post-scan detectors or `ner_findings: fail` before any source is read. The two plan-level entries gain a required keyword-only `config`, and every engine and test caller passes it. `run_out_of_core_route` forwards its config. An entry sentry and a caller sentry hold the rule. Platform threading is P1, not yours.
- M4: model findings in the post-scan. `verification/postmask/_model_findings.py`, a `detectors` keyword on `run_post_mask`, new finding and report fields (`tables_checked` included), dismissals, the section 4.5 severity table, `PostScanBlockedError` (`post_scan.ner_blocked`) raised in `compute_post_scan_integrity`, and each model pass in its own `try`.
- M3: model hits in the pre-scan. `profiling/_model_pass.py`, a `pre_scan` keyword on `scan_sources`, `ModelHit`, `DetectorRun`, `starter_config` model suggestions (always `live=False`), `CLASSIFIER_LABELS`, the 60-row classifier subsample.

The plan is the spec. Read sections 4.3 to 4.5, 4.8, 5, 6, 7, 11 (Placement), 12 (frozen contracts), 13 and 14 in full before the first edit. For M3 and M4 also read IW section 4.6. Where this handoff and a plan disagree, the plan wins; report the disagreement.

## Slice chain (build in this order)

| # | Part | Content | Depends on | Branch |
|---|---|---|---|---|
| 1 | M5 | Route guard and `PostScanRouteError`; required `config` on the two plan-level entries and all callers; guard calls in the four non-resident entries; entry and caller sentries; AT-M5a | main (M1 merged) | `feat/scanml-m5` |
| 2 | M4 | `_model_findings.py`, `run_post_mask(detectors=)`, finding and report fields, dismissals, severity table, `PostScanBlockedError`, model passes in their own `try`; AT-M4a to AT-M4g | M5 branch, plus M2 on main | `feat/scanml-m4` |
| 3 | M3 | `_model_pass.py`, `scan_sources(pre_scan=)`, `ModelHit`, `DetectorRun`, `starter_config` model suggestions, `CLASSIFIER_LABELS`, 60-row subsample; AT-M3a to AT-M3h | M4 branch, plus M2, SCAN-MULTI S3-engine.2 and IW E1 on main | `feat/scanml-m3` |

Not yours: M2, P1 (platform), C1, C2a, C2b (CLI).

Stacking: each part branches from the previous part's branch head (M5 from `main`). If the previous part has merged by then, branch from `main` instead. One PR per part, base = the previous part's branch (or `main`). The orchestrator retargets and merges in order. When a dependency lands on main, merge `origin/main` into your branch (never rebase a pushed branch, never force-push).

### Stop-and-report points

1. After M5's PR is open, check whether M2 is on main:
   ```
   git fetch origin
   git show origin/main:pyproject.toml | grep -n '^ml = \['
   git log origin/main --oneline --grep='classifier stack into the base install'
   ```
   M2 is on main only when the first command prints nothing and the second prints a commit. If not, stop and report "M5 done, waiting on M2". Do not start M4.
2. Before M4, confirm you can read IW section 4.6 in `louiskeep/decoy`. If you cannot, stop and report; the orchestrator will copy it to you.
3. After M4's PR is open, check for M2, SCAN-MULTI S3-engine.2 and IW E1 on main: `git cat-file -e origin/main:src/decoy_engine/profiling/starter_config.py` and the full `ColumnSuggestion` and `starter_review.py` that IW E1 adds. If any is missing, stop and report which. Do not start M3, and do not build a stand-in for a missing part.

Full suite. Plan section 13 names the triggers. All three parts hit one, so the orchestrator runs the full suite on each merge head (you do not):
- M5: public execution signatures change (`config` becomes required on two entries), plus a new reason code.
- M4 and M3: public engine exports, config schema and the reason registry change.

You may take the whole chain. Stop and report at any part where: the plan is wrong or silent on something that changes a guarantee; a defect of a class the plan already closes (section 6) appears again; a census number (module size, registry count) would have to rise; a test needs weakening to pass; or a dependency above is not on main.

## Per part: done when

1. First commit = the part's red tests (the section 7 rows tagged for that part, and the generated corpora section 14 names), recorded red on the part's start commit as assertion failures against stubs, with the reason. A test that passes unexpectedly is tied to a named mutation and shown red on it, or the part stops.
2. Then the production change, exactly the files plan sections 11 and 13 list for the part.
3. Each mutation section 7 names for the part applied once on the final source head, its test shown red, then reverted. A later source edit invalidates that evidence; rerun it.
4. A `changelog.d/<slug>.md` fragment in the format of `changelog.d/README.md` for any `src/` change. `docs/compatibility-contract.md` updated for each new export (plan section 12).
5. New reason codes (`route.post_scan_models_unsupported` in M5, `post_scan.ner_blocked` in M4, any other the plan assigns) registered by the registry rules on main at the part's start. If REG-RELEASE E2 has landed (an `UNRELEASED` marker in `src/decoy_engine/reasons/`), declare them `since=UNRELEASED`.
6. NER tests run for real, not skipped. Install the `ner` extra and the model wheel pinned in `NER_MODEL_WHEELS` the way the `ner-gate` job in `.github/workflows/ci.yml` does (download, check the SHA-256, install with `--no-deps`), and run `pytest -m ner` with `DECOY_REQUIRE_NER=1`.
7. Checks green on the exact head:
   ```
   uv sync --frozen --extra dev
   scripts/dev/gates.sh --baseline --steps ruff,format,mypy,sentry,layers,licenses,tests <selected-test-paths>
   ```
   where the selected paths are the part's acceptance test files, `tests/sentry`, the reason-registry and module-size tests, plus every file `git grep` finds in `tests/` for each changed signature's name (gates.sh also adds every test file that imports a changed module). For M5 that grep covers every caller of `run_sequential` and `run_fk_out_of_core`. Record `python -V`, PyArrow, DuckDB and spaCy versions and the failure sets on the branch and on main in `docs/records/scan-ml-<part>-evidence.md`. Fill in the TEST SCOPE block from the rules below. Say what you did not run.
8. Push the branch, open the PR (title = the part's commit subject; body: what it does, the red-first and mutation evidence, the `docs/compatibility-contract.md` section 9 checklist copied from a recent engine PR such as #480, and the final line `cam`). Report the PR number and head, then go to the next stop point.

## Parallel work you must not collide with

F25 (another remote worker) edits `execution/out_of_core/_runner.py`, which holds `run_fk_out_of_core`. GEN-REL (another remote worker) touches the reason registry. If either lands on main first, merge `origin/main` into your branch, keep both sides' changes, and record the merge commit. Never drop a registry row on a conflict.

## Rules

- V1 bar: fix only what (1) breaks product function, (2) loses or corrupts data, or (3) puts real values or secrets where a non-admin can see them (API responses, UI, `Job.log`, notifications, output files, world-readable files; `str(exc)` counts). Values, paths or class names only in admin logs, tracebacks or `__cause__` chains, hostile in-process code, and "every entry point, provably" completeness are V2. Do not fix them. List each under "V2-LOG" in the report with file and line, one line each.
- One central mechanism per concern. `ner_rates` in `detection/_ner_rate.py` is the only NER scoring entry, with the free-text gate inside it; never call `iter_ner_spans_batch` directly. `_route_guard.py` is the only post-scan route guard. `classify_frame` on a strictly loaded pack is the only classifier inference. Never add a second table, resolver or guard.
- Value-free output: logs, reports, `ScanSet`, `StarterConfig` and errors carry codes, counts and buckets only. No cell value, span or entity text. `str(error)` and `message` equal the registry summary, and `__cause__` and `__context__` are `None` where the plan says so.
- Never raise a cap, ceiling, pin or ratchet. The constants `NER_MIN_RATE`, `NER_SCAN_CELLS`, `FREE_TEXT_MIN_TOKENS` and `_CLASSIFIER_ROWS` change only with an owner note. No higher module-size number, no wider threshold, no skipped test, no weaker assertion. If a change needs one, stop and report.
- Red-first acceptance tests, and each named mutation run on the final head, for every new mechanism. Never commit with a mutation applied.
- If a new defect of a class already in the catalog appears, stop and report it. Do not patch the same class a third time.
- No new top-level package. Place files per plan section 11. Files around 800 lines at most.
- Tests: one test process at a time, `-n 2` at most, no perf suite. Never claim "suite green". Report failing ids against main's failing ids, any new id, any main failure that now passes, and any change in the collected count. Never edit a known red test to make it pass.
- Commits: Conventional Commits subject `type(scope): summary`, at most 72 characters, imperative, lowercase after the colon, no period. Scope may carry the part, for example `feat(scan-ml M5): refuse model post-scan on streaming routes`. No body unless it explains why, in one to three short sentences. Last line exactly `cam`, nothing after it, no other trailers. Never in a body: test counts, file lists, gate or review history, templates, headings, bold.
- No em-dashes anywhere: code, comments, docs, commits, PR text.
- Never force-push. Never rebase a pushed branch. No edits to `main` or to any branch you did not create.
- Do not review your own work. Codex reviews every part PR on the orchestrator side; fix what it returns on the same branch.
- Stop and report on any plan gap, or when a test, threshold or rule would have to be weakened.

TEST SCOPE block (fill in for every part report):
```
Tiers:       T0, T1 [, T3 because <trigger>]
Named tests: <files and ids>
Selection:   scripts/dev/gates.sh --baseline
Plus:        <git grep hits for changed signatures>
Sentries:    <paths>
Triggers:    <trigger>
Mutations:   <mechanism -> test that went red>
Full suite:  trigger <name>, orchestrator runs it
```

## Report per part

PR number, head SHA, red-first summary (tests and why red), mutations run, check results with exact commands and failing ids against main, the TEST SCOPE block, NER tests run (count, not skipped), merges of `origin/main` (commit and why), anything not run, V2-LOG items, and any stop condition hit (with which dependency is missing).

cam
