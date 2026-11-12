Status: record

# SCAN-ML M2: classifier stack in the base install

Date: 2026-11-12. Branch: `feat/scan-ml-m2`, engine PR #492.

## What M2 changed

- `scikit-learn==1.7.2` and `lightgbm==4.6.0` moved from the `ml` extra into the base
  dependencies. The pins are unchanged, so the frozen lgbm-v1 golden retrain still holds. The `ml`
  extra is gone.
- `_CERTIFIED_STACKS` gained two rows: the engine dev profile and the CLI runtime closure
  (`1db3e3da...`). Older rows stay.
- NOTICE was regenerated. CI gained a `base-install` job that syncs with no extras and runs
  `scripts/dev/base_install_smoke.py`.
- Review round 1: the trainer now turns an `OSError` from `import lightgbm` into an `ImportError`
  that names libomp and libgomp1. ML tests gate through one helper, `tests/_ml.py`, so a host
  without the native library skips instead of aborting collection.

## Verified

- Linux, no extras: a fresh `uv sync --frozen --python 3.10.20` with no extras installs lightgbm
  and scikit-learn. `scripts/dev/base_install_smoke.py` prints
  `classifier ok: lgbm-v1 0.1.0 lightgbm 4.6.0` and exits 0. Run on a Linux test VM on 2026-11-12.
- macOS without libomp (Mac mini): `import decoy_engine` works and `classifier_status()` reports
  `available=False, cause='import'` without raising. The review-round-1 test run on the Mac
  (exception hierarchy, classifier status, classify fields, model status, locators, lightgbm
  golden) had no collection error and the lightgbm tests skipped. The 29 errors in
  `tests/fileaccess/test_x2_locators.py` on the Mac are the Linux-only `openat2` requirement
  (`FileAccessUnavailable`) and are unrelated to M2; the same file passes 29/29 on Linux.

## Not verified

- No Mac run with libomp installed.
- No Windows run.
- The real DP fit on the new CLI closure row. The `1db3e3da...` fingerprint is computed from the
  pins plus the four classifier packages, not from an installed venv. It moves to CLI slice C1,
  where `requirements-certified.txt` gains joblib 1.5.3 and threadpoolctl 3.6.0 and a real install
  and fit happen.

## Follow-ups at merge

Both done on this branch after PR #491 merged first:

- `base-install` is in the `ci.yml` merge list in `ci/required-jobs.toml`.
- The `ml` reason text there now says the tests skip when the ML stack cannot load, and that only
  `ml-gate` sets `DECOY_REQUIRE_ML=1`.

## For C1 and the platform image

The Linux lightgbm wheel needs the system `libgomp.so.1` and does not bundle it. A slim base image
must install `libgomp1`, or `import lightgbm` fails with `OSError`. On macOS the equivalent is
`brew install libomp`.
