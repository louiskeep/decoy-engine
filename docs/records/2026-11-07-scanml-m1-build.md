Status: record

# SCAN-ML M1: detector config, model status and strict loading

Date: 2026-11-07. Branch: `feat/scanml-m1`, on engine main `d46dbc85`. Plan: SCAN-ML
(`plan/scan-ml`, `docs/plans/2026-11-07-scan-ml.md`), sections 4.1, 4.2, 4.3 (NER_MIN_RATE), 4.8,
AT-M1a to AT-M1f, Revision 5 for the classifier placement, Revisions 6 and 7 for the NER
free-text scope and `NER_MIN_RATE`.

## Problem

The scan and verify phases are to run the lgbm classifier and spaCy NER next to the regex
detectors. Before any phase runs a model, the engine needs the config that asks for one, a status
that says whether each model is usable without raising, a strict loader that never hands back a
partial set, and reason identities for each refusal. Existing configs must keep their dump and
hashes.

## Change

- Config: `PreScanConfig` (`config/_pre_scan.py`) and `post_scan.detectors`, `ner_model`,
  `ner_findings` and `dismissed` (with `Dismissal`). A field equal to its default is left out of
  the dump, so every existing config fixture dumps unchanged. The plan hash leaves out `pre_scan`
  and keeps `post_scan.dismissed`, because under `ner_findings: fail` a dismissal decides what
  blocks. Rejected values are never echoed.
- `validate_ner_model_name` (`detection/types.py`, one `re.fullmatch`) is called by both config
  models and by `ensure_ner_available`.
- NER availability: a name counts as an installed model only when it is an installed distribution
  that ships `<model>/meta.json`, found through `importlib.metadata` without importing anything,
  so `os` or `json` is refused as `provider.ner_model_not_installed`. A model built for another
  spaCy minor is refused as `provider.ner_model_incompatible`. A spaCy that fails to import is a
  typed refusal. `_PIPELINES` is keyed by (model, installed version).
- `detection/_model_status.py`: `ModelStatus`, `ner_status` (cached per (model, version), never
  raises), `ModelUnavailableError` raised outside the `except` block so `__cause__` and
  `__context__` are None, and `log_detector_run` (one INFO line per phase on
  `decoy_engine.detectors`, model ids and versions checked against a safe shape). The detection
  package exports the status helpers.
- `profiling/model_pack/_detectors.py`: `classifier_status`, `load_detectors` and `DetectorSet`,
  exported from `profiling.model_pack`. `load_detectors` returns a set only when every requested
  model is usable. `default_pack_loader(file_access=None)` is the one loader `classify_fields` and
  the status share. `ModelPackLoadError.cause` is `disabled`, `import` or `pack`, set at each raise
  site. `classify_frame` takes the pack it is given; `classify_fields` is its lenient wrapper.
- `detection/_ner_rate.py`: `NER_SCAN_CELLS = 2_000`, the shared cell sample, the free-text
  predicate `is_free_text` (`FREE_TEXT_MIN_TOKENS = 3`), the per-detector rate over
  `iter_ner_spans_batch` (counts only, spans dropped; `ner_rates` is the only scoring entry),
  `NerRate.hit`, `NER_MIN_RATE = 0.211` (see below) and the rate bucket.
- Reasons: registry 1.8.0 adds `config.detectors_invalid`, `config.post_scan_inactive`,
  `config.pre_scan_ner_inactive`, `config.ner_model_invalid`, `config.dismissal_invalid`,
  `provider.classifier_unavailable` and `provider.ner_model_incompatible`, pinned in
  `tests/fixtures/f29/reason-manifest-v1.8-scanml-m1.json` (the SUB-STOP pattern). The
  slice first claimed the previous minor; LIB L0.1 took that minor on main first, so the merge renumbered this
  slice to 1.8.0 by hand (the renumber tool refuses the one-line manifest list in
  `docs/reasons.md` as `mixed-line`). `registry_renumber.py --check` still flags
  `docs/reasons.md:21`; that line is L0.1's own entry, a known false positive.
- Reason adapter: when a family exception carries a registered `ReasonCode` that is not in the
  family's namespace, the record uses the code's own namespace. The family lookup runs first, so a
  code registered in both namespaces keeps the family record (a `TransformError` with
  `plan.SORT_UNSUPPORTED_TYPE` stays `runtime.sort_unsupported_type`). This also fixes a path that
  predates M1: `ner_model_not_installed` re-wrapped by a plan check used to reach consumers as
  `decoy.unregistered_reason`.
- `decoy_engine` exports `ModelRef`, `ModelStatus`, `classifier_status`, `ner_status`,
  `load_detectors`, `ModelUnavailableError`, `PreScanConfig` and `Dismissal`.

## AT-M1 evidence

Tests: `tests/unit/config/` (hash stability, hostile names, reasons),
`tests/unit/detection_verification/` (status, strict availability, run log, NER rate, base layer,
real-model status) and `tests/unit/profiling/` (pack cause, classifier status).

Red against stubs on `decoy-test-6`: 78 failed and 40 passed in the plain venv, every failure an
assertion or "DID NOT RAISE"; 7 failed and 2 passed for AT-M1d plus calibration in the ner and ml
venv. Green on the build head before the NER round: sentry 4207 passed / 2 skipped; the M1 and
wide set 1230 passed / 33 skipped; ner and ml venv `-m ner` 28 passed / 2 skipped / 1 xfailed,
`-m ml` 82 passed / 2 skipped. After the NER round (`d4b2e70f`): sentry 4210 passed / 2 skipped;
detection_verification, config, profiling and the public API 603 passed / 29 skipped; the
calibration test passes in the ner and ml venv and reproduces 0.211.

Mutations, each reverted:

| Mutation | Red |
| --- | --- |
| M-M1a: `pre_scan` dumped by default | 3 (F06 baseline, fixtures dump, defaults dropped) |
| M-M1a2: availability row in the plan hash | 1 (F06 baseline) |
| M-M1a3: `dismissed` dropped from the plan hash | 1 (dismissal hash separation) |
| M-M1b: classifier load made lenient | 2 |
| M-M1b2: refusal raised `from` an exception | 5 |
| M-M1b3: `classify_frame` reloads the default pack | 1 (given pack only) |
| M-M1c: `fullmatch` replaced by `match` | 4 (trailing newline cases) |
| M-M1d: version dropped from the pipeline, status and classifier cache keys | 1 each |
| M-M1e: version dropped from the run log line | 2 |
| `model_installed` back to `find_spec` | `os`, `json`, `this`, `packaging` cases |
| spaCy import error allowed to escape | 1 |
| upward import from detection (lazy and top level) | 1 each |
| adapter tries the code's own namespace first | 2 (sort and dedupe keep the runtime record) |
| adapter fallback removed | 2 (the re-wrapped NER refusals) |
| M-M1f: `is_free_text` gate dropped in `ner_rates` | 3 (short-value columns scored) |
| M-M1f2: `>` instead of `>=` in `is_free_text` | 6 (median-3 boundary, corpus shape, calibration) |
| `NER_MIN_RATE` 0.212 | 3 (calibration, accepted value, boundary 211/1000) |
| `NER_MIN_RATE` 0.210 | 3 (calibration, accepted value, boundary 210/1000) |
| M-M1hit: `hit` uses `>` | 1 (boundary 211/1000; no measured rate is exactly 0.211) |

M-M1b3 has no run path to mutate in M1; the faithful variant above stands in until M3 puts
`classify_frame` on the run path, where it is re-run.

## NER_MIN_RATE = 0.211

The Q7 rule (the largest of 2 x the highest false-hit rate, the highest one-sided 95%
Clopper-Pearson upper bound, and 0.01, on the share of cells) first measured 0.557 over every
column shape, above the 0.5 stop line. Short name-like values drove it: bare product names hit
`person_name` in 28% of cells. The owner then scoped NER to free-text columns (plan Revision 6): a
column is scored only when the median whitespace-token count of its sample is at least 3
(`is_free_text`), and any other column has no NER hit. The calibration corpus was rebuilt from
free-text negatives only, adding prose that names companies, products, cities and streets. That
corpus and a separate held-out corpus were committed with digests (`8f704450`) before the first
measurement.

Measured once on the frozen corpus with `en_core_web_sm` 3.8.0, 2,000 cells per column,
Clopper-Pearson from `scipy.stats.beta.ppf(0.95, k + 1, n - k)`:

| Column | Detector | Hits | Rate | Upper 95% |
| --- | --- | --- | --- | --- |
| redacted_notes | location | 4 | 0.0020 | 0.0046 |
| redacted_notes | person_name | 0 | 0.0000 | 0.0015 |
| faker_sentences | location | 6 | 0.0030 | 0.0059 |
| faker_sentences | person_name | 22 | 0.0110 | 0.0157 |
| faker_catch_phrases | location | 0 | 0.0000 | 0.0015 |
| faker_catch_phrases | person_name | 20 | 0.0100 | 0.0145 |
| faker_bs | location | 7 | 0.0035 | 0.0066 |
| faker_bs | person_name | 44 | 0.0220 | 0.0282 |
| prose_company_product | location | 14 | 0.0070 | 0.0109 |
| prose_company_product | person_name | 169 | 0.0845 | 0.0954 |
| prose_cities | person_name | 112 | 0.0560 | 0.0652 |
| prose_streets | person_name | 211 | 0.1055 | 0.1175 |

The city and street prose columns are negatives for `person_name` only, because the model maps
GPE, LOC and FAC to `location`. Rule output: 2 x 0.1055 = 0.211; highest bound 0.1175; floor 0.01.

The estimate before the measurement was about 0.044, taken from the Faker columns alone. The
prose columns moved it: `en_core_web_sm` tags street names and product names inside sentences as
PERSON. The value fell outside the agreed 0.02 to 0.10 band, so the build stopped there and the
owner chose. The owner accepted 0.211 for V1 on 2026-11-07 (plan Revision 7). Options declined:
0.118 (no 2x factor), a larger model before M1, and a narrower hit definition. Measuring
`en_core_web_md` on the same frozen corpus is separate work, and a model swap would be its own
slice.

This is an owner spec change, not a weakened test. The rule, the corpus and the digests did not
move after the measurement. AT-M1f now tests what 0.211 means, with the real model on 400-cell
notes columns built from the held-out company and product templates, with a seeded Faker name
appended to a fixed share of cells:

| Case | person_name rate | Hit |
| --- | --- | --- |
| 1 cell in 4 names a person | 125/400 = 0.3125 | yes |
| 1 cell in 10 names a person | 76/400 = 0.19 | no (rate stays visible) |
| held_out_faker_sentences | 6/1000 = 0.006 | no |
| held_out_company_product | 92/1000 = 0.092 | no |
| held_out_places | 134/1000 = 0.134 | no |

Two margins are thin. The 1-in-10 column sits 0.021 under the threshold, because the company and
product template sentences already give about 0.09 false hits on their own. The held-out place
prose (0.134) is above the calibration corpus's worst column (0.1055), so 2x was the factor that
kept it under. `NerRate.hit` (`scanned_cells > 0 and rate >= NER_MIN_RATE`) is the one place a
later slice decides a hit; a synthetic boundary test pins 211/1000 as a hit and 210/1000 as not.

## Plan deviations

- The classifier half of the status and loading code lives in `profiling/model_pack/_detectors.py`,
  not `detection/`, because detection is the base layer and must not import profiling. The plan
  carries this as Revision 5.
- `pre_scan.ner_model` without `ner` in `pre_scan.detectors` is refused with its own code,
  `config.pre_scan_ner_inactive`, in the same 1.8.0 claim.
- `ModelStatus` and `ModelUnavailableError` carry a `cause` the plan does not list, so a caller
  can tell a disabled, unimportable or bad pack apart.
- The logger is built with `getChild` in `src`, so the string-target sentry does not read its name
  as an import path; the test builds the name the same way and asserts it is the
  `decoy_engine.detectors` logger and its parent.
- The `pip install '.[ml]'` hint in the classifier refusal is left to M2, which moves the ML
  packages into the base install.
- AT-M1d checks both models on the `ner-gate` leg, but that job installs `dev ner` without
  lightgbm, so the classifier half skips there until M2. The `ml-gate` leg runs it now.

## Gates

- Dennis: GO-WITH-FIXES on `7b0cd642` (0/4/3/4: NER availability imported arbitrary modules, a raw
  `ModuleNotFoundError`, the `_compile.py` size pin, detection importing profiling), all fixed;
  GO-WITH-FIXES on `b73f4963` (0/1/1/2: the first adapter fix re-keyed codes registered in two
  namespaces; a private cross-package import), fixed; GO on `38515699`.
- Codex: GO-WITH-FIXES in round 1 on `d8f832e0` (DESIGN: the root package imported the detector
  API from a private module), fixed by exporting it from `profiling.model_pack`. Later rounds are
  in the task result.
- T1 (`scripts/dev/gates.sh --keep-going --baseline` on a GCP VM): the final head's run is in the
  task result.

## Carried forward (V2-LOG)

- The adapter fallback trusts any registered `ReasonCode` from any engine family. No current
  producer changes identity, but an explicit list of permitted re-wrap identities would be
  tighter.
- `log_detector_run` accepts an open `counts` mapping of safe-shaped keys and values; fixed typed
  fields would keep a future caller from logging a data value.
- `decoy_engine/__init__.py` still imports several names from private modules in other packages;
  a ratchet against new cross-package private imports would hold the line.
- `test_fixed_width_writer.py::test_round_trip_anything_accepted_reads_back_unchanged` failed
  once in a full `tests/unit` run and passed alone; it looks like a hypothesis flake in code this
  slice does not touch.
