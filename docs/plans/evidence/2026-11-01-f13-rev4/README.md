Status: evidence for plan revision 4 (`docs/plans/2026-10-31-f13-slices-3-4-consumers.md`). Sizing prototypes and probes only: nothing here is a build, nothing was pushed to the platform or CLI, and the prototypes are not the implementation (each slice is still written test-first by its worker).

# F13 slices 3-4, revision 4 evidence

## Environment (stated, not assumed)

- Platform: `louiskeep/decoy-platform` branch `chore/repin-engine-0.17.0` at `6e9967e6` (base of every 3a slice), local worktree. Environment built by the repo's own `scripts/dev/bootstrap.sh` from `requirements-dev.lock` (sha256 prefix `0b8f2b4f64cc5107`, reported "fresh"), Python 3.10.20, engine editable at `ENGINE_PIN_SHA` `1132d29e40a0c947174fff65185fb95168ce504f` (v0.17.0), native companion not installed (native_dispatcher tests deselected by the harness). Same venv for base and prototype runs; prototype runs used `PYTHONPATH=<worktree>` so `api` resolved to the worktree under test (checked by printing `api.__file__`).
- CLI: `louiskeep/decoy` `main` `a5d36b4`, read from a public clone (the CLI repo is not attached to this session). No CLI code was executed: running the clone's bootstrap was refused by the session's permission policy. CLI prototype files were only parsed with `ast.parse`.
- Engine: `louiskeep/decoy-engine` `main` `51548303`, tag `v0.17.0` = `1132d29e`, A-4 branch `claude/jolly-carson-3k5stf` head `16d72eca` (contains `d8f19393`).

## Measured sizes (production files only; from the prototype patches in this directory)

| Slice | Patch | Production files | Lines (+/-) |
|---|---|---|---|
| 3a-i | `proto-3a-i.patch` (6e9967e6 -> proto) | 6 | +204 / -83 (287) |
| 3a-ii | `proto-3a-ii.patch` (on 3a-i) | 10 | +41 / -3 (44) |
| 3a-iii | `proto-3a-iii.patch` (on 3a-ii) | 5 | +65 / -5 (70) |
| 4a | `proto-4a.patch` (a5d36b4 -> proto) | 8 | +168 / -109 (277) |
| 4b | `proto-4b.patch` (on 4a) | 5 | +21 / -14 (35) |

Counted with `git apply --numstat`. Test lines are not measured (tests are written red-first by the worker); the plan gives each slice a hard test-line cap so that measured production plus the cap stays within 800.

3a-ii also forces mechanical edits to existing tests that call the wrappers directly (the new keyword has no default): 64 call sites in 30 test files, listed in `platform-direct-wrapper-call-sites.txt` (grep at `6e9967e6`, excluding `def`, `monkeypatch.setattr` and `lambda` lines).

## Probe results (base `6e9967e6` vs prototype)

1. Keyed job and preview routes on the base. The 32 test modules matching `tests/test_(v2_|unmask|s6a|stream|sequential|out_of_core|ooc|task7|route|vault|job_run|orchestr|preview)*.py`: base 55 failed, 425 passed, 24 skipped (`platform-base-red-tests.txt`). On the 3a-ii prototype: 27 failed (`platform-proto-3a-ii-red-tests.txt`); every one of the 27 is either a direct wrapper call missing the new required keyword (`TypeError: ... missing 1 required keyword-only argument: 'key_provider'`) or `/unmask` (3a-iii). 40 base failures turn green with 3a-ii alone. Base raise sites, from the structured log `exc` chains:
   - admitting entries (full frame, preview child, vault writer): `execution/_pipeline_context.py:305` -> `secret_providers/_preflight.py:346` -> `:171` (the closed rebuild of the refusal raised at `:316`), 11 chains;
   - sequential: `api/jobs/v2_sequential.py:201` -> `execution/_sequential.py:332` -> `keyprovider.py:222`, 12 chains;
   - out-of-core: `execution/out_of_core/_runner.py:193` -> `out_of_core/_preflight.py:62` -> `keyprovider.py:222`, 3 chains;
   - every failed job logs `job_failure code=none exc_type=KeyedStrategyRequiresSecret` at `api/persistence/error_projection.py:153`.
2. Nested preview (`POST /api/nested/preview`, TestClient, `raise_server_exceptions=False`), base vs 3a-iii prototype:

   | child | base | prototype |
   |---|---|---|
   | hash | 500 (`StrategyError hash_requires_namespace`, engine `_hash.py:45`, re-raised at router `:100`) | 200, masked, differs across two requests |
   | fpe (`charset: digits`) | 500 (`fpe_requires_namespace`, `_fpe.py:116`) | 200, masked, differs across requests |
   | tokenize | 500 (`tokenize_requires_vault`, `_tokenize.py:75`) | 422 `{code: tokenize_requires_vault, message: constant}` |
   | text_mask | 500 (`AttributeError` on `ctx.mask_key`, `_text_mask.py:171`) | 200 |

   Revision 3 said hash, FPE and tokenize raise `AttributeError`; on v0.17.0 they fail earlier, on the namespace or vault check, because the route builds the seed with `namespace=None` (`api/nested_preview/router.py:87`). `text_mask` (no namespace check) is the one that reaches `ctx.mask_key`. Either way the route returns an unclassified 500. Uncoded engine `ValueError` (for example `faker` with no provider, `_faker.py:55`) stays a 500 on the prototype: finding F-6 in the plan.
3. Traceback locals, master key. Rendering every frame below the catching frame with `traceback.StackSummary.extract(walk_tb(exc.__traceback__.tb_next), capture_locals=True)` plus the `__cause__`/`__context__` chain. Base, via `derive_mask_job_seed_int()`: a 20-byte `DECOY_MASTER_KEY` leaks the env text in `load_master_key`/`_decode_env_key` locals and the message carries the length (`got 20`); a malformed value leaks it in `b64decode` frames and chains a `binascii.Error` context; a non-ASCII value chains a `UnicodeEncodeError`. Prototype (3a-i), via `instance_key_provider()`: all three are `MasterKeyError(master_key_invalid)` with no context and no env text in any frame; a forced `SecretKeyProvider.__post_init__` failure yields `MaskSecretConfigError(mask_key_derivation_failed)` with neither the marker master nor the derived secret in any frame. The test frame itself must be excluded (`tb_next`): a probe that renders its own frame finds its own needles.
4. Job-authored reference on the base. `run_v2_pipeline` with `global_settings.mask_secret_ref`: `env:DECOY_HOST_SECRET` is admitted; `file:/nonexistent/k` fails `missing_mask_secret`, so the server opened a host path the config author chose. Engine reason: with no provider, `admit_mask_key` preflights any configured ref, even for an unkeyed plan (`secret_providers/_preflight.py:314-328`, docstring `:340-342`). Finding F-3 in the plan.
5. Platform tests that change meaning (named pinned edits): `tests/test_master_key.py:41` (`match="32 bytes"`), `tests/test_master_key_kms.py:224-231` (free-text `KmsError` fake), `tests/test_v2_unmask.py:118-130` (`reversed_unverified` becomes `reversed` under a real secret).

## Not run

No mutation testing, no full platform suite (only the 32 modules above, plus `tests/test_master_key*.py`, `test_mg1_5_nested_preview.py`, `test_v2_validation.py`, `test_v2_config.py`, `test_engine_a8_guard.py`), no CLI execution of any kind, no engine tests. The repin worker's whole-suite numbers (249 failed, 311 errors) were not reproduced.
