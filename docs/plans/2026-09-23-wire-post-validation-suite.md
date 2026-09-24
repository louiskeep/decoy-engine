# A1: Wire the post-validation suite into the run path

Status: plan, BUILD-READY (Codex GO-WITH-CHANGES 2026-09-23; changes incorporated below).
Risk: R2. Author: Opus. Locked decision: post-validation is warn-only by default with a
`post_validation_enforce` opt-in that promotes hard-fails to a job failure (Cam may override).
Roadmap: `decoy-platform/docs/ROADMAP.md` "Ready to build" and "NEXT SET OF WORK" item 4.

## FRAME

### Problem

The engine ships a complete, unit-and-privacy-tested post-execution scan suite at
`src/decoy_engine/validation/post/`, but nothing invokes it on a real run.
`PostValidationRunner.run()` (`validation/post/_runner.py:52`) is constructed only in
tests, and no module under `src/decoy_engine/execution/` imports `validation.post`. The
platform already expects the output: `api/jobs/v2_node_runs.py:88` reads
`result.quality_metrics["failed_checks"]` and its own docstring says the wiring that
populates that key is deferred, so on a live run the list is absent. The result: eight
real correctness guarantees (uniqueness, regulated-ID format conformance, composite
coherence, null-position audit, cleartext-leak detection, FK validity, determinism, plus
the `sampled_values` evidence step, registered in `validation/post/_checks/__init__.py:31`)
exist in the tree but never run against customer data.

This is the gap that made explainer 2/3 honest: today the live post-mask guarantees come
only from the STORM post-mask check and the Quality report, not from this suite.

### Definition of done

`run_pipeline` invokes the suite when, and only when, a default-OFF opt-in is set; a real
leak or uniqueness violation in an opted-in job surfaces in `failed_checks` and the
platform node-run consumer acts on it; with the flag off the hot path and byte output are
unchanged. The config contract, routing interaction, and the fail-vs-warn job-outcome
semantics are decided and tested.

### Boundaries

- Change wiring, config plumbing, and routing only. Do NOT change any scan's logic; the
  eight scans and the merge site are already built and reviewed.
- In-memory / full-frame jobs only. The suite materializes whole columns (`to_pylist`),
  so out-of-core (100M-class) jobs are explicitly out of scope for A1; see the routing
  interaction below. A1 does not deliver per-row post-validation at 100M.
- No platform UI work (surfacing `quality_summary` is a separate, mostly-existing path).

### Applicable rulebooks

`feature-dev`, `architecture` (a new phase in the pipeline), `api-and-compatibility` (new
`PipelineConfig` fields), `testing`, `verification`, `security` (privacy of the emitted
manifest). Engine `CLAUDE.md`: established-methodology citation already present in
`_runner.py` (SDV `evaluate.run_diagnostic` + NIST SP 800-188).

### Risk

R2: adds a pipeline phase, changes a config contract, and (when enabled) changes job
outcome. No author self-certification; written plan + independent plan review before
implementation; adversarial review + exact-artifact gate before merge.

## Approach

Mirror the existing `fidelity_report` opt-in, which is the proven template for a
default-OFF, report-shaped attachment to `ExecutionResult.quality_metrics`
(`execution/_pipeline.py:152,188-198`).

1. Opt-in surface (locked: runtime argument, not a config field). Add
   `post_validation: bool = False`, `post_validation_skip: list[str] = []`, and
   `post_validation_sample_size: int = 100` as RUNTIME ARGUMENTS to `run_pipeline`,
   mirroring `fidelity_report` exactly, and pass them into the `config` dict the runner
   reads (`_runner.py:71,75,84`). This deliberately avoids adding `PipelineConfig` fields,
   which would feed `pipeline_config_hash` (`plan/_compile.py:664`) and move golden
   fixtures. Do not depend on the nested `global_settings.post_validation`
   (`config/_global_settings.py:105`); the platform sets the runtime argument. If a future
   need forces a config field instead, the byte-identical test must additionally assert
   `pipeline_config_hash` against a pre-change fixture.
2. Wire point. In the finalize stage that already computes fidelity reports
   (`_pipeline_finalize.compute_fidelity_reports()`), add a sibling
   `compute_post_validation()` that constructs the `ScanContext` inputs the runner needs
   (`plan`, `execution_result`, `sources`, `profile`, `registry`, `relationship_graph`,
   `namespace_registry`, `config`) and calls `PostValidationRunner().run(...)`. Confirm
   during DEVELOP that all seven inputs are in scope at that seam; if `profile` /
   `registry` / the relationship + namespace registries are not threaded there yet,
   thread them (they are constructed earlier in the pipeline for masking).
3. Routing interaction. The suite requires the full frame in memory, exactly like
   `fidelity_report`, which already declines out-of-core eligibility
   (`_pipeline_routing.py` returns `"fidelity_report_requested"`; shadow generation raises
   `GENERATION_SHAPE_UNSUPPORTED`). Add `post_validation` to that same decline path so an
   opted-in job is never silently sent out-of-core (where the checks cannot run) and is
   instead run full-frame or fail-closed-rejected if too big for the box.
4. Job-outcome semantics (locked: warn-only default + `post_validation_enforce` opt-in).
   The runner sets `failed_checks`, and the platform fails a node run on a non-empty
   `failed_checks` (`api/jobs/v2_node_runs.py:88`). To avoid switching the suite on for
   observation and surprising a customer job into failing, the default is warn-only: a
   hard-fail scan records a finding and surfaces it but does not fail the job. An explicit
   `post_validation_enforce` runtime flag promotes hard-fails to a job failure. The
   platform consumer must therefore branch on enforce, not on `failed_checks` alone; wire
   the enforce flag through to that check. Cam may flip the default.

## Observable behavior

1. Flag off (default): outputs byte-identical to today; no `quality_summary` key; no
   measurable overhead. Golden and compat-corpus fixtures do not move.
2. Flag on, clean job: `quality_metrics["quality_summary"]` present, `failed_checks`
   empty, job succeeds.
3. Flag on, injected leak: a substitution-strategy column (for example `hash`) in which a
   source value survives into the output causes the `leakage` scan to hard-fail;
   `failed_checks` contains `"leakage"`; the platform marks the node run failed (subject
   to the fail-vs-warn decision).
4. Flag on, out-of-core-sized FK job: routing declines the out-of-core route (mirrors
   `fidelity_report`); the job runs full-frame or is fail-closed rejected. The checks are
   never silently skipped while reporting success.
5. Flag on, a scan raises: that scan's outcome is recorded failed with a `scan_crashed`
   warning and the manifest is still produced (already implemented at `_runner.py:94-110`).

## Known failure modes

- Memory blow-up when a large frame is validated in memory. Mitigated by the full-frame /
  out-of-core-decline gate and by documenting the in-memory ceiling. Honest boundary: A1
  gives post-validation for in-memory-eligible jobs, not for the 100M out-of-core path.
- Config key mismatch (runner reads a key the validated config never surfaces). Covered by
  a round-trip acceptance test.
- Interaction when `fidelity_report` and `post_validation` are both on (both force
  full-frame). Covered by a combined-flags test.
- Privacy: the manifest's `sampled_values` must carry only synthetic, non-passthrough
  masked values, never source PII. The suite is designed this way; A1 must not regress it.

## Acceptance tests

Fail-before proof (the meaningful one): a job whose `hash` column leaks a source value.
Before wiring, `run_pipeline(...).quality_metrics` has no `failed_checks`; the leak is not
caught. After wiring with the flag on, `failed_checks == ("leakage",)`. This test fails on
`main` today and passes after A1.

- `test_post_validation_off_is_byte_identical` (golden corpus unchanged; hot path
  untouched).
- `test_post_validation_on_clean_job_succeeds_with_summary`.
- `test_post_validation_on_injected_leak_populates_failed_checks` (the fail-before proof:
  valid config + the real leakage scan; leak uncaught on `main`, in `failed_checks` after).
- `test_injected_leak_warn_only_does_not_fail_job` (default: finding recorded, job succeeds).
- `test_injected_leak_enforce_fails_job` (with `post_validation_enforce`, the platform
  consumer marks the node run failed).
- `test_post_validation_on_duplicate_pk_populates_failed_checks` (a second hard-fail scan).
- `test_post_validation_declines_out_of_core` (routing signal returned; job not sent OOC).
- `test_config_round_trips_post_validation_flags` (contract reaches the runner).
- `test_summary_contains_no_source_pii` (privacy invariant).
- `test_fidelity_and_post_validation_both_on` (combined full-frame path).
- Test strength (per `testing.md`): line + branch coverage on the wiring, config plumbing,
  and routing gate; mutation score on the wiring and the routing-decline predicate, since a
  silent skip there is the costly defect. Bars set from a measured baseline.

## Out of scope

Scan logic changes; 100M / out-of-core per-row validation; the platform UI for
`quality_summary`; tuning `post_validation_sample_size` beyond its default of 100.

## Open questions (for the plan-gate and Cam)

1. Fail-vs-warn (product call, Cam). Should a hard-fail scan fail the job (platform marks
   the node run failed) or only warn in the first slice? Recommendation: land warn-only by
   default with an explicit `post_validation_enforce` opt-in that promotes hard-fails to a
   job failure, so enabling the suite for observation cannot surprise a customer job into
   failing. Turning enforcement on is then a deliberate, documented choice.
2. Config key reconciliation. Recommendation: make the top-level `PipelineConfig`
   `post_validation` authoritative (what the runner reads) and deprecate the nested
   `global_settings.post_validation`, to remove the two-key trap the review flagged.
3. Where the `ScanContext` inputs are threaded. Confirm the finalize seam has `profile` /
   `registry` / relationship + namespace registries; if not, thread them (small).

## Plan-gate review + revisions (Codex, 2026-09-23)

Verdict: GO-WITH-CHANGES. Resolutions below are folded into the plan; implementation must
follow them.

1. Seam correction (runtime arg vs config field). The existing `fidelity_report` is a
   runtime argument to `run_pipeline`, not a `PipelineConfig` field, so `post_validation`
   cannot share its gate by being a config field alone. Decision: add `post_validation`
   (and `post_validation_skip`, `post_validation_sample_size`) as runtime arguments to
   `run_pipeline` mirroring `fidelity_report` exactly, and have the finalize seam pass them
   into the `config` dict the runner reads. Reconcile to one source rather than leaving the
   nested `global_settings.post_validation` as a second key.
2. Config-hash / byte-identical proof. If any `PipelineConfig` field is added it feeds
   `pipeline_config_hash` (`plan/_compile.py:664`) and can move golden fixtures. Preferring
   the runtime-arg route (finding 1) avoids the hash impact. If a config field is used, the
   "off is byte-identical" test must assert the full default-off result including
   `pipeline_config_hash` against a pre-change fixture.
3. Fail-before proof correction. A newly added top-level config field would fail schema
   validation on `main`, so a leak test would fail for the wrong reason, and mocking the
   leakage scan proves nothing. The real proof: a valid config plus the REAL leakage scan
   through `run_pipeline`, asserting the leak is uncaught before wiring and caught after.
   Add: final-output assertion, the platform `failed_checks` consumer contract,
   privacy-on-leak (no leaked value in the report), unified-route coverage, and the
   hash-compat assertion.
4. Byte-stable report. The summary carries wall-clock `post_validation_phase_ms`
   (`_runner.py:112`), so any byte-stability assertion must exclude or pin the timing field.
5. R2 confirmed appropriate. Fail-vs-warn (open question 1) is the one genuine Cam product
   decision; config reconciliation and input threading are engineering calls settled above.
