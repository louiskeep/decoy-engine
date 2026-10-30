# F13 HIGH-1: keyed-test migration audit (fallback removal)

Baseline `fdd08a89`; branch `feat/f13-secret-provider`. Removing the keyed `job_seed` fallback was
measured, not estimated: the first full-suite run after the change (admission wired, no migration)
produced **3,636 failures + 10 collection errors across 220 test files** (24,267 passed). Nearly all
(~2,900 of the error lines) were `KeyedStrategyRequiresSecret` from tests that run keyed strategies
with no secret.

## Mechanism (one shared throwaway secret)

- `tests/_keyed_secret.py`: `THROWAWAY_SECRET` / `THROWAWAY_KEY_PROVIDER`, the only throwaway key.
- `tests/conftest.py::_default_throwaway_secret` (autouse) supplies it at the public admission
  seams (`admit_mask_key`, `require_mask_key`) **only when the work is keyed and no key was given**,
  so unkeyed behavior is untouched and every keyed assertion still runs against a real admitted
  secret. Opt-out: `@pytest.mark.no_default_secret` (registered), used by tests asserting the
  absence of a secret.
- Tests that cross a process boundary (isolated workers, subprocess probes) cannot see the
  fixture; they pass an explicit `mask_secret_ref` (file/env) instead.

## Residual after the fixture: 405 failures / 8 errors in 82 files, all migrated

| Class | Technique | Examples |
|---|---|---|
| Literals/snapshots pinned under the job_seed key | Re-captured from the SAME producing (oracle) route under the throwaway key; every other route (native/OOC/sequential/sink) independently reproduces the pin. Each capture method was first run on the baseline worktree and reproduced the OLD literals exactly. | `_d1b1_bucket_expected.py` (14 lists), `_d1a_hash_data.py` (126 lines), `_d1b2_categorical_data.py` (7 lists), `_d2a_seq_data` tokens/digests, `r3_within_route_baseline.json` (1,268 string leaves, structure asserted unchanged), `c5c_a_unchanged_jobs.json` rows, `docs/proof-manifest.json` |
| Route-vs-route comparisons where one side silently keyed off job_seed | Explicit identical key on both sides | `test_out_of_core_relation`, `fk_parity` `_run_oracle/_run_ooc`, stream-driver harness, dgrn/bucket/group_key oracles |
| Tests pinning the removed fallback itself | Inverted to assert fail-closed in BOTH release phases (`no_default_secret`) | `test_pre_ga_keyed_no_secret_falls_back_to_seed` -> `..._fails_closed_in_both_phases`, tokenize, chunked empty-input gate |
| "Output changes under a secret vs the no-secret baseline" path-completeness proof | Re-expressed as "output changes between two distinct real secrets" for every original keyed column (same 8-column parametrization + handler-level proofs) so a site left on `job_seed` is still detected | `tests/unit/test_de02_keyprovider.py` |
| Removed resolver API | Re-pointed at `key_provider_from_ref` / `admit_mask_key`; leak properties kept and tightened | `tests/property/test_keyprovider_invariants.py` |
| Scripts/tools run by tests or the gates | Explicit throwaway key | `scripts/fk_memory_probe.py`, `scripts/fk-scale/bench_worker_fk.py`, `scripts/gen_proof_manifest.py`, `testflight/_runner.py` |

## Coverage accounting (no net loss)

- Selected tests, excluding the new `tests/secret_providers` suite: baseline `fdd08a89` **33,330**
  (33,378 collected) -> branch `969ff7e4` **33,409** (33,457 collected), **+79**; none removed, and the
  48 deselected (the default `-m` filter) are the same on both sides. The earlier "+78 (33,408)" was a
  miscount and is corrected here. Measured on CPython 3.10.20, Linux x86_64, venv from `uv sync
  --frozen --extra dev --extra hashicorp-vault --extra xdist --extra lint`, no `decoy-engine-native`
  companion, one worktree per SHA:
  `PYTHONPATH=<worktree>/src:<worktree> python -m pytest --collect-only -q -p no:cacheprovider --ignore=tests/secret_providers`.
  The reviewer's run (Python 3.10, `pytest-one`) measured 33,361 / 33,409 -> 33,440 / 33,488. That is
  the same +79 delta, with 31 more tests on both sides. The cause of the constant offset is not
  identified; it is environment-dependent collection (likely an optional module present there and
  absent here). The two runs agree on the coverage delta.
- Skip/xfail markers added to migrated tests: **0**. New skip markers exist only on the new
  Vault-live / Semgrep tests (environment-gated, reported as NOT RUN when skipped).
- Assertions deliberately dropped: **1** - `mask_batch` called with no `mask_key` equals the
  job_seed-keyed output (it pinned the removed fallback; replaced by rekey + determinism checks).
- Assertions changed from "equals the job_seed-keyed literal" to "equals the throwaway-keyed
  literal": re-pins listed above. Assertion kind (route == pinned baseline) is unchanged.

## Golden / test-flight

`testflight/_runner.py` passes a fixed throwaway `key_provider`. The pytest gate
(`pytest testflight -m testflight`) is at its baseline failure set after ONE deliberate recapture:
`d_longitudinal_visits` (keyed output now derives from the throwaway secret) was updated to the value the
gate reported, `fa244ee9... -> f4f1bc8c...`. All 53 invariant checks still pass. Not done: the CLI
`scripts/test_flight.py` cross-process fingerprint check already drifts on the baseline in this
Python 3.12 sandbox for all five jobs (goldens were captured on 3.10), so `--update-fingerprints`
was NOT run here; the canonical-environment recapture of a/b/c/e is a required pre-merge step.
The `tokenize` strategy-coverage guard failure is also pre-existing on the baseline.

## Unrun / environment-limited

- Failures that are identical on the baseline worktree in this sandbox (DuckDB 2.0.0
  `ResourceInUseException` on orphan-FAIL paths, root-user permission tests, missing `google`
  module, a reserved-names assertion, F29 error-class drift): not F13, left untouched.
- `decoy-engine-native` companion absent: native-kernel tests skip; two native sensitive-token
  re-pins were derived from the Python hash and are UNCONFIRMED against the real kernel.
