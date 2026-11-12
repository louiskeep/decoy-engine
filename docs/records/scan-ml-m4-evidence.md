Status: record

# SCAN-ML M4: model findings in the post-scan

Date: 2026-11-12. Branch: `feat/scanml-m4`, started from `feat/scanml-m5` head `91a52eaa` (M5, PR
#508, not merged). Main at the start was `7fc12e7f`, which M5 already contains (M2 is `c695eee4`),
so no merge of main was needed. Plan: SCAN-ML (`plan/scan-ml` at `e0ae4204`), section 4.5 with
4.3, 4.4, 4.8, 5, 6, 7 (AT-M4a to AT-M4g), 11 to 14; INIT-WIZARD 4.6 (`plan/init-wizard` at
`a2ea696`). Handoff: `handoffs/scan-ml-build`.

## Environment

- Python 3.11.18 (`.venv`, `uv sync --frozen --extra dev`), PyArrow 24.0.0, DuckDB 1.5.4,
  lightgbm 4.6.0 (base dependency since M2).
- spaCy 3.8.14 with `en_core_web_sm` 3.8.0 in a separate venv for the NER runs (below).

## Change

- `verification/postmask/_model_findings.py` (new): the classifier pass (`classify_frame` on the
  strictly loaded pack, `subsample=True`), the NER pass (`ner_rates`, the one NER scoring entry;
  only text-dtype columns are offered, the free-text gate is inside `ner_rates`), the section 4.5
  severity table (`_ner_severity`, `_classifier_severity`), each pass in its own `try` (one
  `error` row with source, model and exception class name; the regex rows stay), `review`
  (dismissals, then the `ner_findings: fail` promotion), `blocks` and `raise_if_blocked`,
  `post_scan_detectors` (the run-start load), the log lines, and `PostScanBlockedError`.
- `runner.run_post_mask(..., detectors=None)`: `None` keeps the regex-only path; with a set, the
  model passes run after the regex pass and outside its wrapper. The report gains `detectors`,
  `tables_checked` and `config_sha256`; findings gain `source`, `model`, `band`, `dismissed`,
  `promoted`; `match_rate` is `float | None` (`types.py`).
- `residual_pii.py`: the configured-column lookup moves into `configured_columns` (read by both
  passes) and `is_source_identity_finding` states the dismissal exclusion. Regex behavior unchanged.
- `run_pipeline` calls `post_scan_detectors(config)` before resolve, so a model that cannot run
  refuses before any row is masked; the set rides on `PipelineRunContext.post_scan_detectors` to
  `compute_post_scan_integrity(detectors=)`, which stamps the report and then raises
  `PostScanBlockedError` (empty chain, `raise_bare`) when blocked.
- `classify_frame(pack, frame, *, subsample=False)`: `True` classifies a seeded subsample of at
  most `_CLASSIFIER_ROWS = 60` non-null values per column (see Deviations 1).
- `detection/_model_status.log_detector_models` writes the `post_scan.models` line.
- Reason `post_scan.ner_blocked` (owner `PostScanBlockedError`, verify phase, verification outcome,
  retry `configuration_repair`), mapped in `reason_adapter._FIXED`. First built at a numbered
  minor with its own fixture; now declared pending (see "Merge with M5 and main").
- `PostScanBlockedError` exported from `decoy_engine`; `__init__.py` is 597 lines (one split
  `validation_result` import folded; no census entry). `docs/compatibility-contract.md` 3.1 and
  3.4, fragment `changelog.d/scan-ml-m4.md`.

Module sizes: `_model_findings.py` 331, `runner.py` 219, `residual_pii.py` 409,
`_pipeline_context.py` 596, `_pipeline_finalize.py` 512, `_pipeline.py` 371, `__init__.py` 597.
No census number moved.

## Deviations from the plan text

1. The 60-row classifier subsample. Plan 4.2 puts it inside `classify_frame`, plan 13 lists it
   under M3, and 4.5 has M4's classifier pass use it. M3 now builds after M4, and M1 did not add
   it, so M4 adds it in the one place 4.2 names, as a keyword whose default keeps
   `classify_fields` (the platform endpoint, 5.2 "unchanged") exactly as before. M3 should call
   `classify_frame(..., subsample=True)` rather than add a second subsample. Alternative: M4
   classifies whole outputs until M3 lands (rejected: 4.2 says never a full output).
2. `log_detector_models` is added to `detection/_model_status.py` (M1's module) so the
   `post_scan.models` line (4.8) uses the same logger and identifier check as
   `log_detector_run`. A table name that is not an identifier is logged by its position (`#3`).
3. `PostScanBlockedError` lives in `_model_findings.py` (the plan names no module; the
   `_route_guard.py` precedent keeps the error beside the code that raises it).
4. Dismissals apply whenever the config has them, also when `detectors` is `None`; the plan's
   regex dismissal example (`source: regex`) needs that. A platform config has none today.
5. The run-start set reaches `compute_post_scan_integrity` through a new
   `PipelineRunContext.post_scan_detectors` field, filled in `run_pipeline`.
6. AT-M4b "faker `name` gives `info`": a real `faker` person-name output is two tokens per cell,
   not free text, so it gives no NER row (Revision 6). The test covers both: a short faker
   names column gives no row, and a free-text column configured `faker` gives `info`.
7. `run_pipeline` refuses undeclared columns, so "unconfigured column" cases run through
   `run_post_mask`; the run-level block tests use `text_redact` without `ner`.
8. The error row of a raising pass names the table it was reading, `column` and `detector_id`
   are empty, `match_rate` is `0.0` for NER and `None` for the classifier.

## Red first

Commit `e6a19865` (tests only) on `91a52eaa`, every failure an `AssertionError`:

- dev venv: 29 failed, 108 passed, 13 skipped (the NER tests). Reasons: `run_post_mask takes no
  detectors` (21), `the model findings pass is missing` (2), `no detectors are passed in` (1),
  `residual-PII findings carry no source` (AT-M4e classifier, after a setup fix below), the
  registry fixture test (`post_scan.ner_blocked` not registered), `test_public_api`
  (`PostScanBlockedError` missing), the M1 tripwire `test_scan_ml_reasons` (now asserting the M4
  code is registered; it was 1.26.0 then, now the pending value), and the minor-fixture sentry
  (a fixture above the live minor).
- NER venv, `DECOY_REQUIRE_NER=1`: the same 29 plus the 13 NER tests, 42 failed, 108 passed,
  0 skipped (`run_post_mask takes no detectors`, `residual-PII findings carry no source`,
  `decoy_engine exports no PostScanBlockedError`).
- AT-M4a (two tests) passed on the base by design: it is a characterization test whose golden
  (`tests/fixtures/scanml_m4/regex_only_reports.json`) was written on the base. It is tied to
  M-M4a, red on the final head (below).

Corrections after the red commit, in setup only, each already red on the base for the reason above:
AT-M4e asserted that the full-frame run has no `auto_chunk` block (it has one with mode
`full_frame`; now asserted); the severity fixture's `memo` column holds whole emails so the regex
path really fails it; the canary log path is built without a dotted `decoy_engine` literal (the
string-target sentry).

## Mutations

Applied one at a time on the final source head (`25174016`, tests at `6e1a363b`), never
committed, reverted with `git checkout src`. Command: `DECOY_REQUIRE_NER=1 <venv-ner>/bin/python -m
pytest -q -p no:randomly -n 2` over the six M4 test files. Each went red:

- M-M4a (run the classifier when `detectors` is None): `test_platform_call_with_no_detectors_is_byte_equal_to_the_base`.
- M-M4b (ignore `ner_findings`): `test_fail_mode_promotes_only_standing_ner_warnings`,
  `test_an_ner_dismissal_for_an_old_model_version_stops_applying`,
  `test_a_blocked_run_leaves_no_canary_anywhere`, `test_an_ner_fail_finding_blocks_the_run`,
  `test_block_predicate_routing_with_a_fake_detector[fail_finding]`.
- M-M4b2 (NER through `_classify`): `test_warn_mode_severity_table`,
  `test_fail_mode_promotes_only_standing_ner_warnings`, AT-M4e NER, `test_an_ner_fail_finding_blocks_the_run`,
  `test_warn_mode_returns_with_the_finding_in_the_report`.
- M-M4b3 (score with `iter_ner_spans_batch` directly): `test_warn_mode_severity_table` (a
  short-value cell reached the pipeline spy), `test_fail_mode_promotes_only_standing_ner_warnings`,
  both raising-pass tests, `test_a_regex_fail_alone_never_blocks`.
- M-M4c (match without the strategy): `test_a_changed_field_makes_the_finding_report_again[strategy]`,
  `test_a_dismissal_matches_the_current_strategy_of_the_column`.
- M-M4d (a span in `message`): both canary tests.
- M-M4e (skip model passes on the chunked route): both AT-M4e tests.
- M-M4f (classifier through `_escalate`): `test_classifier_findings_never_exceed_warning[warn]`
  and `[fail]`, `test_classifier_pass_on_the_real_pack_gives_a_band_and_no_rate`, the dismissal tests.
- M-M4g (NER-pass error not blocking): `test_a_raising_ner_pass_blocks_and_keeps_the_regex_findings`,
  `test_block_predicate_routing_with_a_fake_detector[pass_error]`.
- M-M4g2 (model passes inside the existing wrapper): both raising-pass report tests,
  `test_a_raising_ner_pass_blocks_and_keeps_the_regex_findings`, `[pass_error]`.
- M-M4g3 (report in the exception message): `test_an_ner_fail_finding_blocks_the_run`,
  `test_a_raising_ner_pass_blocks_and_keeps_the_regex_findings`, both routing cases.
- M-M4sub (not in the plan; classify the whole output): `test_classifier_pass_reads_a_60_row_subsample_of_each_column`.

## Checks

```
uv sync --frozen --extra dev
GATES_WORKERS=2 scripts/dev/gates.sh --baseline --steps ruff,format,mypy,sentry,layers,licenses,tests <50 paths>
```

The 50 paths: every `tests/**/test_*.py` (perf excluded) that `git grep` finds for `run_post_mask`,
`compute_post_scan_integrity`, `classify_frame`, `classify_fields`, `check_residual_pii`,
`ResidualPIIFinding`, `PostMaskReport`, `PipelineRunContext`, `log_detector_run`,
`load_detectors`, `DetectorSet`, `PostScanBlockedError`, `ner_blocked`, `post_scan_integrity`,
`_model_status`, `_pipeline_full_frame` and `post-scan-integrity`, plus the six M4 files,
`tests/sentry`, `tests/unit/test_reason_registry.py`, `test_reason_release_pin.py`,
`tests/unit/progress/test_consumer_contract.py`, `test_public_api.py`,
`tests/unit/config/test_scan_ml_reasons.py`, `tests/unit/profiling/test_model_pack_cause.py`.

`gates.sh --baseline` compares with origin/main (`7fc12e7f`), head `6e1a363b`, clean tree:

| Step | Branch | origin/main |
|---|---|---|
| ruff, format, mypy | not installed in the `dev` sync (rc 127), run below | same |
| sentry | 5069 passed, 3 failed, 21 errors | 5035 passed, 3 failed, 21 errors |
| layers | 15 passed | not rerun (passed on branch) |
| licenses | 63 passed | not rerun |
| tests | 6002 passed, 2 failed, 21 errors, 73 skipped | 5899 passed, 2 failed, 21 errors, 60 skipped |

Failing ids identical (24 each, no branch-only id): `tests/sentry/parallel/test_serial_resources.py::test_the_required_serial_registry_is_marked`
and the `tests/sentry/test_outbound_clients.py` set (no `google` package).

The comparison base for this part is the M5 head. Same venv, same path list (the 44 that exist
there), `python -m pytest -p no:cacheprovider -p no:randomly -n 2 -q -rfE <paths>`, with
`PYTHONPATH` on a detached worktree of `91a52eaa`: M5 head 5975 passed, 12 failed, 21 errors, 59
skipped; branch 6007 passed, 12 failed, 21 errors, 72 skipped. Failing ids identical (the 24 above
less the serial-registry one, plus 10 `tests/sentry/layout/test_import_layers.py` ids that need
import-linter, which only the `layers` step venv has). No branch-only id, no base failure now
passing. Collected 6067 on the M5 head, 6112 on the branch (+45, the new tests; the extra skips
are the 13 NER tests).

Lint, separately:

```
uv sync --frozen --extra dev --extra lint
GATES_WORKERS=2 scripts/dev/gates.sh --baseline --steps ruff,format,mypy tests/unit/execution/test_scanml_m4_run.py
```

ruff PASS, format PASS, mypy the same 4 errors on branch and main (google stubs, `tomli`), no
branch-only error.

At this first build `scripts/dev/registry_renumber.py --check` reported `several-minors` (two
numbered minors stacked on main). The unreleased conversion below removed that; `--check` now exits 0.

## NER tests

The `ner-gate` steps in a separate venv (`UV_PROJECT_ENVIRONMENT=<venv-ner> uv sync --frozen
--extra dev --extra ner`), the URL and SHA-256 from `NER_MODEL_WHEELS[("en_core_web_sm", "3.8.0")]`,
`curl --fail` download, `sha256sum --check` OK, `uv pip install --no-deps <wheel>`. Then
`DECOY_REQUIRE_NER=1 pytest -m ner -n 2` on `6e1a363b`: 52 passed, 10 skipped (M5 had 39 passed).
Every skip is a module-level import skip for another optional dependency (google, paramiko, hvac,
mimesis, the wheel builder, mutmut); no NER test skipped. The 13 M4 NER tests run on the real
model and the real classifier pack.

## Existing tests changed

- `tests/unit/config/test_scan_ml_reasons.py`: the M1 tripwire asserted `post_scan.ner_blocked`
  was absent; it now asserts it is registered with `since` equal to the pending value `"unreleased"`.
- `tests/unit/test_reason_registry.py`, `tests/unit/test_public_api.py`: one test and one name added.
- `tests/unit/execution/test_scanml_m5_route_guard.py::test_run_pipeline_auto_chunk_lane_runs_model_post_scan_configs_as_on_main`:
  `run_pipeline` now loads its post-scan models first, which needs spaCy for `[regex, ner]`. The
  test (a routing test whose columns are not free text) now replaces `load_detectors` in
  `_model_findings` with a set naming the model; every assertion is unchanged.
- `tests/unit/detection_verification/test_model_status.py`: its autouse fixture also clears the
  model caches after each test. It left a fake `lgbm-v1` pack cached, which a later test in the
  same worker then classified with. Assertions unchanged.

## TEST SCOPE

```
Tiers:       T0, T1, T3 because engine exports, run behavior and the reason registry change
Named tests: tests/unit/detection_verification/test_scanml_m4_{regex_baseline,report_fields,
             severity,dismissals,canary}.py, tests/unit/execution/test_scanml_m4_run.py,
             tests/unit/test_reason_registry.py::test_scanml_m4_post_scan_block_is_registered_with_a_pinned_fixture
Selection:   scripts/dev/gates.sh --baseline
Plus:        git grep hits in tests/ for the changed signatures (50 paths, tests/perf excluded)
Sentries:    tests/sentry (module size, reason registry, minor fixtures, string targets, seam)
Triggers:    public engine export, run_pipeline raise, new pending reason code
Mutations:   M-M4a, M-M4b, M-M4b2, M-M4b3, M-M4c, M-M4d, M-M4e, M-M4f, M-M4g, M-M4g2, M-M4g3,
             M-M4sub, each red (see Mutations)
Full suite:  trigger engine exports plus reason registry, orchestrator runs it
```

## Not run

- The full suite (orchestrator) and the perf suite.
- ruff and mypy in the exact `--extra dev` sync (not installed there); run with `--extra lint`.
- Tests needing google, paramiko, hvac or mimesis.
- The platform hook and the CLI (P1, C2a): not this part.

## Merge with M5 and main (2026-11-12, second round)

`4cca4058` merges `origin/feat/scanml-m5` at `a0d4ad08`, which contains main `9e451dac` (#506
release-stamped registry, #514, #517, #524, #525, #526, #528, #534 and others). Conflicts, all in
registry carriers: `docs/reasons.md`, `reasons/_registry.py` and `consumer-contract.json` take the
M5 side, which equals main (1.24.0, no SCAN-ML fixture paragraph); `tests/unit/test_reason_registry.py`
takes the M5 side plus the M4 test rewritten by hand to the pending form
(`test_scanml_m4_post_scan_block_is_registered_as_pending`, no fixture). No conflict around
`compute_post_scan_integrity`: on main #517, #525 and #526 add library modules
(`postmask/_keys.py`, `_column_facts.py`, `_stream_input.py`) that nothing in the run calls yet, and
main's `_pipeline_context.py` change (a shorter module, 575 lines) merged beside the
`post_scan_detectors` field. Main's regex detector changes (`detection/detectors.py`,
`_scoring.py`) leave the AT-M4a golden byte-equal: AT-M4a passes on the merged head.

Unreleased conversion: `registry_renumber.py --to-unreleased --main origin/main` first refused one
line of this record naming the numbered fixture (rewritten by hand, `2bda97bd`), then converted
`post_scan.ner_blocked` to `since=UNRELEASED`, the tripwire, the contract doc and the fragment, and
deleted the fixture (`214928a8`). `--check --main origin/main`: "registry consistent", exit 0. The
fragment names `post_scan/ner_blocked` and states that the regex-only hook honours dismissals.

Owner conditions, checked in code:

- `PipelineRunContext.post_scan_detectors` is read in one place (`_pipeline_full_frame.py`, passed
  to `compute_post_scan_integrity`). Nothing dumps the context (`git grep` finds no `asdict`,
  `repr` or `vars` of it), and `ExecutionResult` does not carry it. The dataclass repr would have
  shown it, so `f7c1ccab` declares it `field(default=None, repr=False, compare=False)`.
  `DetectorSet` itself keeps `classifier_pack` out of repr; its repr shows sources and the NER
  `ModelRef` (ids and versions). The report's `detectors` key carries only `{source, model:
  {id, version}}`. Pinned by `tests/unit/execution/test_scanml_m4_run.py::test_the_run_context_never_shows_its_detector_set`.
- `classify_frame(..., subsample=False)` stays default off; only the M4 classifier pass passes
  `True`.
- The regex-only hook honouring dismissals is pinned by
  `tests/unit/detection_verification/test_scanml_m4_dismissals.py::test_the_regex_only_hook_honours_dismissals`.

Reruns on `f7c1ccab`:

- Mutations: M-M4a 2 red (the AT-M4a platform test and the new hook-dismissal test), M-M4b 5,
  M-M4b2 5, M-M4b3 5, M-M4c 2, M-M4d 2, M-M4e 2, M-M4f 8, M-M4g 2, M-M4g2 4, M-M4g3 4, M-M4sub 1;
  the same ids as under "Mutations" plus the new hook test.
- `GATES_WORKERS=2 scripts/dev/gates.sh --baseline --steps ruff,format,mypy,sentry,layers,licenses,tests <50 paths>`
  against main `9e451dac` (same recipe, plus `post_scan_detectors` and `configured_columns`):
  sentry 5192 passed, 3 failed, 21 errors (main 5157, 3, 21); layers 15; licenses 63; tests 6136
  passed, 2 failed, 21 errors, 73 skipped (main 6030, 2, 21, 60). Failing ids identical, 24 each
  (`test_serial_resources::test_the_required_serial_registry_is_marked` and the
  `test_outbound_clients` set), no branch-only id, no main failure passing. Collected 6232 against
  6113 (+119: the M4 and M5 tests in this selection). ruff, format, mypy rc 127 in the dev sync;
  with `--extra lint`: ruff PASS, format PASS, mypy the same 4 errors as main.
- NER gate: `DECOY_REQUIRE_NER=1 pytest -m ner -n 2`: 52 passed, 10 skipped (import skips for
  other extras only).
- `pytest tests/integration/compat_corpus`: 5 passed, 1 skipped (pre-GA real-artifact capture).
