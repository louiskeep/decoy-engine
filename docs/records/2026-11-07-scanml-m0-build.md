Status: record

# SCAN-ML M0: declare click for spaCy and gate the ner extra in CI

Date: 2026-11-07. Branch: `feat/scanml-m0`, on engine main `b4c6915d`. Plan: SCAN-ML
(`plan/scan-ml`, `docs/plans/2026-11-07-scan-ml.md`), section 4.9 and AT-M0.

## Problem

spaCy imports Click, but Typer no longer depends on Click and vendors its own copy, so a fresh
`decoy-engine[ner]` install failed at `import spacy` (`ModuleNotFoundError: click`). No CI job
installed the `ner` extra, so nothing caught it. Click 8.4.2 was already in `uv.lock`, but only
through `mutmut` in the `mutation` extra.

## Change

- `ner` extra: `["spacy>=3.7,<4", "click>=8.1,<9"]`. `uv.lock` changes in metadata only (the
  extra's dependency list and `requires-dist`); no package version moved. `NOTICE` lists click
  because the license sentry now sees it in a runtime extra.
- `detection/ner.py`: `NER_MODEL_WHEELS`, keyed by `(model, version)`, maps
  `("en_core_web_sm", "3.8.0")` to the wheel URL on the spaCy models GitHub release, its SHA-256
  (`1932429d...0fb85`) and the spaCy range from the wheel's `meta.json` (`>=3.8.0,<3.9.0`).
  `DEFAULT_NER_MODEL_VERSION` names the default entry. The user-facing
  `NER_MODEL_NOT_INSTALLED` message, the module docstring, `docs/guides/free-text-ner.md`,
  `docs/strategies.md` and `examples/ner_redaction_demo.py` point at the pinned, hash-checked wheel
  instead of the unpinned `python -m spacy download`.
- CI job `ner-gate`: `lock-env` with extras `dev ner`, download the default wheel, check its
  SHA-256, install that exact file with `uv pip install --no-deps`, then `pytest -m ner` with
  `DECOY_REQUIRE_NER=1`. No CI lock-scanner allowlist entry.
- Tests: `ner` marker; one shared gate `tests/_ner.py` `needs_ner` (marks `ner`, skips without
  spaCy or the model, fails instead when `DECOY_REQUIRE_NER == "1"`), used by the real-model tests
  in `test_ner_spans.py`, `test_text_mask_ner.py`, `test_text_redact.py`, the two out-of-core NER
  parity tests and the new `test_ner_install.py`; `test_ner_model_pins.py` checks the pin shape,
  the URL derived from the key, and that the locked spaCy is inside the pinned range.

## AT-M0 evidence

Reproduced on a Linux GCP VM in a throwaway venv built the way `lock-env` builds it
(`uv sync --frozen --extra dev --extra ner`, CPython 3.10.20), with the install step copied from
`ci.yml`.

| Run | Tree | Result |
| --- | --- | --- |
| RED | `click` removed from the `ner` extra, `uv.lock` from `cf68a280` | wheel SHA-256 OK; `import click` raises `ModuleNotFoundError`; `pytest -m ner`: 22 failed, 5 passed (traceback `spacy/cli/_util.py:18: from click import NoSuchOption`) |
| GREEN | branch head | wheel SHA-256 OK; click 8.4.2; `pytest -m ner`: 27 passed |

The 5 that pass in the red run never import spaCy (availability cells). `ner-gate` has not yet run
on a GitHub runner: it is `workflow_dispatch` only, like `ml-gate`.

## Gates

- Dennis: GO-WITH-FIXES on `6698cfb1` (0/0/3/6), fixes applied; GO-WITH-FIXES on `52938604`
  (0/0/2/4), fixes applied; GO on `b1bbb62b` (0/0/0/3).
- Codex: GO on `52938604` (two V2-LOG notes); GO-WITH-FIXES on `b1bbb62b` (one V1: remaining
  `spacy download` guidance; one DESIGN: placement of `tests/_ner.py`), both fixed (`8d2754dc`
  here, plan `1dcfa189`).
- T1 (`scripts/dev/gates.sh --keep-going --baseline` on a GCP VM, selection plus all sentries): on
  `b1bbb62b`, sentry 4186 passed / 2 skipped, selected tests 969 passed / 31 skipped, 0 branch-only
  failures. The final head's run is in the task result.

## Carried forward (V2-LOG)

- The pins test checks the hand-written `spacy_range` against the locked spaCy, not against the
  wheel's own `meta.json`; C1 reads the checked wheel's `meta.json` and asserts equality.
- The CI lock scanner accepts any `.whl` path without requiring a hash check before install; add
  seeded cases (download, check, install is clean; download, install is a finding).
- `test_ner_model_pins.py` parses `uv.lock` inline; `scripts/dev/licenses.py` `locked_versions`
  already exists.
- `docs/guides/free-text-ner.md` names `en_core_web_sm-3.8.0` by hand; C1's `models install`
  replaces that text.
- `tests/parity/test_out_of_core_group_c_parity.py` is 822 lines (V2 cleanup band).
